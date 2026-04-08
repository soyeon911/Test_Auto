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


# ─── config loader ────────────────────────────────────────────────────────────

def load_config(path: str = "config/config.yaml") -> dict:
    p = Path(path)
    if not p.exists():
        print(f"[Main] Config not found at {p}, using defaults.")
        return {}
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ─── smart source detector ────────────────────────────────────────────────────

def detect_source_and_parse(source: str, config: dict) -> list[dict]:
    """
    Automatically detect whether `source` is:
      - an OpenAPI/Swagger file (YAML/JSON)
      - a Python module/file
      - a URL to a running server's spec

    Returns a list of endpoint descriptors.
    """
    from parsers import OpenAPIParser, PythonFunctionParser

    if source.startswith(("http://", "https://")):
        print(f"[Main] Fetching OpenAPI spec from URL: {source}")
        return OpenAPIParser(source).load().parse()

    p = Path(source)
    if p.suffix in {".yaml", ".yml", ".json"}:
        print(f"[Main] Parsing OpenAPI file: {source}")
        return OpenAPIParser(source).load().parse()

    if p.suffix == ".py" or not p.suffix:
        print(f"[Main] Parsing Python module: {source}")
        return PythonFunctionParser(source).load().parse()

    print(f"[Main] Unknown source format: {source}")
    return []


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
