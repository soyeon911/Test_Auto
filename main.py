"""
main.py — AutoTC Pipeline Entry Point

Modes:
  watch   (default) — background watcher; triggers pipeline when a file is dropped
  run     <file>    — one-shot: parse given file, generate TCs, run tests, email
  parse   <file>    — parse only, print endpoint summary
  generate <file>   — parse + generate TCs, no test run

환경변수 (서버 관리):
  SERVER_LOG_FILE       서버 stderr 로그 파일 경로 (config.server.log_file 로도 지정 가능)
  SERVER_DIR            서버 실행 파일 디렉터리
  SERVER_EXE_NAME       서버 실행 파일 이름 (기본: qfe-server.exe)
  SERVER_LICENSE_KEY    라이선스 키 stdin 응답 (기본: 1)
  SERVER_MODE_CHOICE    처리 모드 stdin 응답 (기본: 1 = CPU)
  SERVER_INSTANCE_COUNT 인스턴스 수 stdin 응답 (기본: 1)
  SERVER_MODEL_PATH     모델 경로 stdin 응답
  SERVER_DB_PATH        DB 경로 stdin 응답
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ─── config loader ────────────────────────────────────────────────────────────

def load_config(config_path: str) -> dict:
    """Load config.yaml and return as dict."""
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ─── smart source detector ────────────────────────────────────────────────────

def detect_source_and_parse(source: str, config: dict) -> list[dict]:
    """
    Automatically detect whether `source` is an OpenAPI spec, a Python module,
    or a C/C++ header. Injects 'target_type' into every endpoint descriptor.

    target_type is resolved in priority order:
      1. config.target.type  (if not 'auto')
      2. file extension / URL heuristic
    """
    from parsers import APIParser, PythonFunctionParser

    # Resolve target type
    configured = config.get("target", {}).get("type", "auto")

    if configured != "auto":
        target_type = configured
    elif source.startswith(("http://", "https://")):
        target_type = "api"
    else:
        ext = Path(source).suffix.lower()
        if ext in {".yaml", ".yml", ".json"}:
            target_type = "api"
        elif ext == ".py":
            target_type = "python"
        elif ext in {".h", ".hpp"}:
            target_type = "lib"
        else:
            target_type = "api"   # best-effort fallback

    print(f"[Main] target_type={target_type}  source={source}")

    # Parse based on resolved type
    if target_type == "api":
        endpoints = APIParser(source).load().parse()
    elif target_type == "python":
        endpoints = PythonFunctionParser(source).load().parse()
    elif target_type == "lib":
        print("[Main] C/C++ library parser not yet implemented — skipping.")
        return []
    else:
        print(f"[Main] Unknown target_type '{target_type}' — skipping.")
        return []

    # Inject target_type so downstream generators can branch on it
    for ep in endpoints:
        ep.setdefault("target_type", target_type)

    return endpoints


# ─── server management helpers ────────────────────────────────────────────────

def _setup_server_log_env(config: dict) -> str:
    """
    SERVER_LOG_FILE 환경변수를 설정하고 로그 디렉터리를 생성한다.

    우선순위:
      1. 이미 환경변수로 설정된 값
      2. config.server.log_file 값
      3. 기본값: server_logs/server_stderr.log
    반환: 실제 사용되는 로그 파일 경로 (빈 문자열이면 설정 안 됨)
    """
    existing = os.environ.get("SERVER_LOG_FILE", "")
    if existing:
        return existing

    cfg_log = config.get("server", {}).get("log_file", "")
    log_path = cfg_log or "server_logs/server_stderr.log"

    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    os.environ["SERVER_LOG_FILE"] = log_path
    print(f"[Pipeline] SERVER_LOG_FILE → {log_path}")
    return log_path


def _check_server_after_run(base_url: str, config: dict) -> bool:
    """
    테스트 완료 후 서버 상태를 확인한다.
    서버가 다운됐으면 자동 재기동을 시도한다.

    반환: 최종적으로 서버가 살아있으면 True
    """
    try:
        from tests.helpers.server_manager import is_alive, restart_server
    except ImportError:
        return True  # 헬퍼 없으면 판단 불가 → 무시

    if is_alive(base_url, timeout=3):
        return True

    print("\n[Pipeline] ⚠ 테스트 완료 후 서버 다운 감지 → 자동 재기동 시도...")
    ok = restart_server(base_url)
    if ok:
        print("[Pipeline] ✓ 서버 재기동 성공 — 다음 파이프라인 실행 준비 완료")
    else:
        print("[Pipeline] ✗ 서버 재기동 실패 — 수동 확인 필요")
        _print_log_tail(config)
    return ok


def _print_log_tail(config: dict, n: int = 40) -> None:
    """SERVER_LOG_FILE 마지막 N줄을 출력한다 (디버깅용)."""
    log_path = os.environ.get("SERVER_LOG_FILE", "")
    if not log_path:
        log_path = config.get("server", {}).get("log_file", "")
    if not log_path:
        return
    p = Path(log_path)
    if not p.exists():
        print(f"[Pipeline] 서버 로그 파일 없음: {log_path}")
        return
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(lines[-n:])
        print(f"[Pipeline] 서버 로그 마지막 {n}줄:\n{tail}")
    except Exception as exc:
        print(f"[Pipeline] 서버 로그 읽기 실패: {exc}")


# ─── full pipeline ────────────────────────────────────────────────────────────

def run_pipeline(source: str, config: dict) -> None:
    """Parse → Generate TC → Run Tests → Email Report."""
    from agents import TCGeneratorAgent
    from runner import TestRunner
    from notifier import EmailSender

    print(f"\n{'='*60}")
    print(f"[Pipeline] Source: {source}")
    print(f"{'='*60}")

    # ── 서버 로그 환경변수 설정 (crash 감지 + log tail 수집용) ──────────────
    _setup_server_log_env(config)

    # 1. Parse
    endpoints = detect_source_and_parse(source, config)
    if not endpoints:
        print("[Pipeline] No endpoints found. Aborting.")
        return
    print(f"[Pipeline] Found {len(endpoints)} endpoint(s).")

    # 2. Generate TCs
    agent = TCGeneratorAgent(config)
    written = agent.generate_for_endpoints(endpoints)
    print(f"[Pipeline] Generated {len(written)} TC file(s).")

    # 3. Run tests
    runner = TestRunner(config)
    summary = runner.run()

    # 4. 테스트 완료 후 서버 상태 점검 + 자동 재기동
    base_url = config.get("server", {}).get("base_url", "")
    if base_url:
        server_ok = _check_server_after_run(base_url, config)
        summary["server_alive_after_run"] = server_ok
    else:
        print("[Pipeline] server.base_url 미설정 — 서버 상태 점검 건너뜀")

    # 5. Email
    sender = EmailSender(config)
    html_report = config.get("runner", {}).get("html_report_path", "./reports/summary.html")
    sender.send_report(summary, source_label=source, html_report_path=html_report)

    print(f"\n[Pipeline] Done — passed={summary['passed']} failed={summary['failed']}")


# ─── watcher mode ─────────────────────────────────────────────────────────────

def run_watch_mode(config: dict) -> None:
    from watcher import SwaggerFileWatcher

    def on_file(path: str) -> None:
        run_pipeline(path, config)

    watcher = SwaggerFileWatcher(config, on_file_detected=on_file)
    watcher.run_forever()


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AutoTC — Automated Test Case Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("mode", nargs="?", default="watch",
                        choices=["watch", "run", "parse", "generate"],
                        help="Operation mode (default: watch)")
    parser.add_argument("source", nargs="?", default=None,
                        help="Swagger file / Python module / URL (required for run|parse|generate)")
    parser.add_argument("--config", default="config/config.yaml",
                        help="Path to config.yaml")

    args = parser.parse_args()
    config = load_config(args.config)

    if args.mode == "watch":
        print("[Main] Starting in watch mode. Press Ctrl+C to stop.")
        run_watch_mode(config)

    elif args.mode == "run":
        if not args.source:
            parser.error("'run' mode requires a source argument.")
        run_pipeline(args.source, config)

    elif args.mode == "parse":
        if not args.source:
            parser.error("'parse' mode requires a source argument.")
        import pprint
        endpoints = detect_source_and_parse(args.source, config)
        print(f"\nParsed {len(endpoints)} endpoint(s):\n")
        pprint.pprint(endpoints)

    elif args.mode == "generate":
        if not args.source:
            parser.error("'generate' mode requires a source argument.")
        from agents import TCGeneratorAgent
        endpoints = detect_source_and_parse(args.source, config)
        agent = TCGeneratorAgent(config)
        written = agent.generate_for_endpoints(endpoints)
        print(f"\nGenerated {len(written)} TC file(s):")
        for f in written:
            print(f"  {f}")


if __name__ == "__main__":
    main()
