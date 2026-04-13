"""
main.py — AutoTC Pipeline Entry Point

Modes:
  watch   (default) — background watcher; triggers pipeline when a file is dropped
  run     <file>    — one-shot: parse given file, generate TCs, run tests, email
  parse   <file>    — parse only, print endpoint summary
  generate <file>   — parse + generate TCs, no test run
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
    from parsers import OpenAPIParser, PythonFunctionParser

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
        endpoints = OpenAPIParser(source).load().parse()
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


# ─── full pipeline ────────────────────────────────────────────────────────────

def run_pipeline(source: str, config: dict) -> None:
    """Parse → Generate TC → Run Tests → Email Report."""
    from agents import TCGeneratorAgent
    from runner import TestRunner
    from notifier import EmailSender

    print(f"\n{'='*60}")
    print(f"[Pipeline] Source: {source}")
    print(f"{'='*60}")

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

    # 4. Email
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
