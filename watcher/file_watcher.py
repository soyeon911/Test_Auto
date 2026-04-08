"""
File Watcher — queue-based, WSL-safe

[TODO-5] 핵심 변경
  - 이벤트 → Queue 에만 쌓음 (콜백 직접 호출 X)
  - Worker 스레드 1개가 Queue 에서 꺼내 순차 실행
    → 중복 실행 / 동시 실행 / append 충돌 방지
  - 동일 경로 짧은 시간 반복 이벤트는 debounce + coalesce
    (같은 파일이 큐에 이미 있으면 덮어쓰기)

[WSL] inotify 는 /mnt/c/... 경로에서 이벤트를 못 받는 경우가 많음.
  → WSL 환경 자동 감지 후 PollingObserver 로 폴백.
  → polling_interval 은 config.yaml watcher.polling_interval_secs 로 조정 가능.
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, PatternMatchingEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver


# ─── WSL detection ────────────────────────────────────────────────────────────

def _is_wsl() -> bool:
    """Return True when running inside Windows Subsystem for Linux."""
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except (FileNotFoundError, PermissionError):
        return False


def _make_observer(polling_interval: float = 1.0) -> Observer | PollingObserver:
    if _is_wsl():
        print(f"[Watcher] WSL detected → using PollingObserver (interval={polling_interval}s)")
        return PollingObserver(timeout=polling_interval)
    return Observer()


# ─── event handler (queue only, no direct callback) ──────────────────────────

class _QueueingHandler(PatternMatchingEventHandler):
    """
    Receives watchdog events, debounces per path, and puts unique paths
    into a shared queue.  The worker thread does the actual processing.
    """

    def __init__(
        self,
        patterns:      list[str],
        event_queue:   "queue.Queue[str]",
        debounce_secs: float,
    ):
        super().__init__(patterns=patterns, ignore_directories=True, case_sensitive=False)
        self._queue        = event_queue
        self._debounce     = debounce_secs
        self._pending:     dict[str, threading.Timer] = {}
        self._lock         = threading.Lock()

    # watchdog callbacks
    def on_created(self, event: FileSystemEvent) -> None:
        self._schedule(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._schedule(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._schedule(event.dest_path)

    def _schedule(self, path: str) -> None:
        with self._lock:
            if path in self._pending:
                self._pending[path].cancel()          # reset timer
            timer = threading.Timer(self._debounce, self._enqueue, args=(path,))
            self._pending[path] = timer
            timer.start()

    def _enqueue(self, path: str) -> None:
        with self._lock:
            self._pending.pop(path, None)

        # Coalesce: if the same path is already in the queue, skip
        # (queue.Queue has no O(1) membership check → use a shadow set)
        # This is done at the worker side via the shadow set.
        print(f"[Watcher] Queued: {path}")
        self._queue.put(path)


# ─── worker thread ────────────────────────────────────────────────────────────

class _PipelineWorker(threading.Thread):
    """
    Single worker that pulls paths from the queue and runs the pipeline
    sequentially.  A shadow set prevents the same path from being processed
    twice if it was queued multiple times before the first run finished.
    """

    def __init__(
        self,
        event_queue: "queue.Queue[str]",
        callback:    Callable[[str], None],
    ):
        super().__init__(daemon=True, name="tc-pipeline-worker")
        self._queue    = event_queue
        self._callback = callback
        self._stop_evt = threading.Event()
        # Paths currently in the queue (for coalescing)
        self._queued_paths: set[str] = set()
        self._lock = threading.Lock()

    def enqueue(self, path: str) -> None:
        """Thread-safe enqueue with coalescing."""
        with self._lock:
            if path in self._queued_paths:
                return                       # already waiting, skip
            self._queued_paths.add(path)
        self._queue.put(path)

    def run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                path = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            with self._lock:
                self._queued_paths.discard(path)

            try:
                self._callback(path)
            except Exception as e:
                print(f"[Worker] Pipeline error for {path}: {e}")
            finally:
                self._queue.task_done()

    def stop(self) -> None:
        self._stop_evt.set()


# ─── public watcher ───────────────────────────────────────────────────────────

class SwaggerFileWatcher:
    """
    Drop-in replacement for the old SwaggerFileWatcher.

    Usage:
        watcher = SwaggerFileWatcher(config, on_file_detected=pipeline.run)
        watcher.start()   # non-blocking
        ...
        watcher.stop()

    Or blocking:
        watcher.run_forever()
    """

    def __init__(self, config: dict, on_file_detected: Callable[[str], None]):
        watcher_cfg = config.get("watcher", {})
        self._watch_dirs: list[str] = watcher_cfg.get("watch_dirs", ["./input"])
        patterns:       list[str]  = watcher_cfg.get("patterns", ["*.yaml", "*.yml", "*.json"])
        debounce:       float      = float(watcher_cfg.get("debounce_seconds", 2))
        poll_interval:  float      = float(watcher_cfg.get("polling_interval_secs", 1.5))

        # Shared queue between handler and worker
        self._event_queue: queue.Queue[str] = queue.Queue()

        # Worker (single pipeline thread)
        self._worker = _PipelineWorker(self._event_queue, on_file_detected)

        # Handler wires events → queue (via worker.enqueue for coalescing)
        self._handler = _QueueingHandler(
            patterns=patterns,
            event_queue=self._event_queue,   # raw queue for now; worker coalesces
            debounce_secs=debounce,
        )
        # Override handler's enqueue to go through worker's coalescing enqueue
        self._handler._enqueue = self._worker.enqueue  # type: ignore[method-assign]

        # Observer (PollingObserver on WSL)
        self._observer = _make_observer(poll_interval)

    def start(self) -> None:
        for d in self._watch_dirs:
            p = Path(d)
            p.mkdir(parents=True, exist_ok=True)
            self._observer.schedule(self._handler, str(p), recursive=False)
            print(f"[Watcher] Watching: {p.resolve()}")

        self._worker.start()
        self._observer.start()
        print(
            "[Watcher] Started (queue-mode). "
            "Drop a Swagger/OpenAPI file into a watched directory to trigger the pipeline."
        )

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()
        self._worker.stop()
        self._worker.join(timeout=5)
        print("[Watcher] Stopped.")

    def run_forever(self) -> None:
        """Block the current thread until KeyboardInterrupt."""
        self.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()
