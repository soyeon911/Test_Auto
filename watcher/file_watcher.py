"""
File Watcher — Week 1 / Week 4

Monitors directories for new or modified Swagger/OpenAPI files.
When a matching file appears, the pipeline runs automatically
(parse → generate TC → run tests → report → email).

Uses `watchdog` for cross-platform file-system events.
A debounce timer prevents double-triggering on rapid saves.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler, PatternMatchingEventHandler
from watchdog.observers import Observer


class _DebounceHandler(PatternMatchingEventHandler):
    """Debounce repeated events and forward unique file paths to the callback."""

    def __init__(self, patterns: list[str], callback: Callable[[str], None], debounce_secs: float):
        super().__init__(patterns=patterns, ignore_directories=True, case_sensitive=False)
        self._callback = callback
        self._debounce = debounce_secs
        self._pending: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def on_created(self, event: FileSystemEvent) -> None:
        self._schedule(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._schedule(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._schedule(event.dest_path)

    def _schedule(self, path: str) -> None:
        with self._lock:
            if path in self._pending:
                self._pending[path].cancel()
            timer = threading.Timer(self._debounce, self._fire, args=(path,))
            self._pending[path] = timer
            timer.start()

    def _fire(self, path: str) -> None:
        with self._lock:
            self._pending.pop(path, None)
        print(f"[Watcher] Detected: {path}")
        self._callback(path)


class SwaggerFileWatcher:
    """
    Start/stop a background watcher thread.

    Usage:
        watcher = SwaggerFileWatcher(config, on_file_detected=pipeline.run)
        watcher.start()
        ...
        watcher.stop()
    """

    def __init__(self, config: dict, on_file_detected: Callable[[str], None]):
        watcher_cfg = config.get("watcher", {})
        self._watch_dirs: list[str] = watcher_cfg.get("watch_dirs", ["./input"])
        patterns: list[str] = watcher_cfg.get("patterns", ["*.yaml", "*.yml", "*.json"])
        debounce: float = float(watcher_cfg.get("debounce_seconds", 2))

        self._handler = _DebounceHandler(
            patterns=patterns,
            callback=on_file_detected,
            debounce_secs=debounce,
        )
        self._observer = Observer()

    def start(self) -> None:
        for d in self._watch_dirs:
            p = Path(d)
            p.mkdir(parents=True, exist_ok=True)
            self._observer.schedule(self._handler, str(p), recursive=False)
            print(f"[Watcher] Watching: {p.resolve()}")

        self._observer.start()
        print("[Watcher] Started. Drop a Swagger/OpenAPI file into a watched directory to trigger the pipeline.")

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()
        print("[Watcher] Stopped.")

    def run_forever(self) -> None:
        """Block the current thread until KeyboardInterrupt."""
        self.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()
