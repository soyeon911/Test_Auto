"""
Test Runner — Week 3

Runs pytest programmatically against tests/generated/ and tests/manual/,
collects pass/fail results, and optionally generates an Allure report.
"""

from __future__ import annotations

import json
import subprocess
import sys
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any



class TestRunner:
    print("[DEBUG] runner/test_runner.py imported")
    def __init__(self, config: dict):
        self.config = config
        runner_cfg = config.get("runner", {})
        self.allure_results = Path(runner_cfg.get("allure_results_dir", "./reports/allure-results"))
        self.allure_report = Path(runner_cfg.get("allure_report_dir", "./reports/allure-report"))
        self.html_report = Path(runner_cfg.get("html_report_path", "./reports/summary.html"))
        self.timeout = int(runner_cfg.get("timeout_seconds", 6000))
        self._default_test_dirs: list[str] = runner_cfg.get(
            "test_dirs",
            ["./tests/generated/rule", "./tests/generated/ai", "./tests/manual"],
        )

        server_cfg = config.get("server", {})
        self.base_url = server_cfg.get("base_url", "http://192.168.150.162:8080")

    # ─── public API ──────────────────────────────────────────────────────────

    def run(self, test_dirs: list[str] | None = None) -> dict[str, Any]:
        """Run pytest and return a summary dict."""
        print("[DEBUG] TestRunner.run entered")
        if test_dirs is None:
            test_dirs = self._default_test_dirs

        # Ensure output dirs exist
        self.allure_results.mkdir(parents=True, exist_ok=True)
        self.html_report.parent.mkdir(parents=True, exist_ok=True)


        json_report = self.html_report.parent / "pytest_report.json"

        cmd = [
            sys.executable, "-m", "pytest",
            *[d for d in test_dirs if Path(d).exists()],
            f"--base-url={self.base_url}",
            f"--alluredir={self.allure_results}",
            f"--html={self.html_report}",
            "--self-contained-html",
            f"--json-report",
            f"--json-report-file={json_report}",
            "--tb=short",
            "-q",
        ]

        print(f"[Runner] Running: {' '.join(cmd)}")
        start = datetime.now()

        # Remove stale report before running so _parse_json_report always reads
        # a fresh file written by THIS run (not a leftover from a previous run).
        if json_report.exists():
            json_report.unlink()

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            return {"error": "pytest timed out", "passed": 0, "failed": 0, "total": 0}

        elapsed = (datetime.now() - start).total_seconds()

        summary = self._parse_json_report(json_report)
        summary["duration_seconds"] = round(elapsed, 1)
        summary["stdout"] = result.stdout[-3000:]   # trim for email
        summary["return_code"] = result.returncode
        summary["pytest_json_path"] = str(json_report)
        summary["html_report_path"] = str(self.html_report)

        print(f"[Runner] Finished in {elapsed:.1f}s — "
              f"passed={summary['passed']} failed={summary['failed']} total={summary['total']}"
              f"  (pytest rc={result.returncode})")

        # Surface any pytest output when something looks wrong
        if summary.get("parse_error"):
            print(f"[Runner] Report parse error: {summary['parse_error']}")
        if result.returncode not in (0, 1) or summary["total"] == 0:
            if result.stdout.strip():
                print(f"[Runner] pytest stdout:\n{result.stdout[-2000:]}")
            if result.stderr.strip():
                print(f"[Runner] pytest stderr:\n{result.stderr[-2000:]}")

        self._generate_allure_report()
        return summary

    # ─── internal ─────────────────────────────────────────────────────────────

    def _parse_json_report(self, report_path: Path) -> dict[str, Any]:
        if not report_path.exists():
            return {"passed": 0, "failed": 0, "error": 0, "total": 0,
                    "failed_tests": [], "collection_errors": [],
                    "parse_error": "report file not written by pytest"}
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
            summary = data.get("summary", {})

            failed_tests = [
                {"nodeid": t["nodeid"], "longrepr": t.get("longrepr", "")}
                for t in data.get("tests", [])
                if t.get("outcome") == "failed"
            ]

            # Collect any collection-phase errors (fixture not found, import errors, etc.)
            # These appear in 'collectors' as outcome=error but are NOT counted in summary.total
            collection_errors = [
                {
                    "nodeid": c["nodeid"],
                    "longrepr": c.get("longrepr", ""),
                }
                for c in data.get("collectors", [])
                if c.get("outcome") == "error"
            ]
            if collection_errors:
                print(f"[Runner] WARNING: {len(collection_errors)} collection error(s) detected:")
                for ce in collection_errors:
                    print(f"  - {ce['nodeid']}: {str(ce['longrepr'])[:200]}")

            return {
                "passed":            summary.get("passed", 0),
                "failed":            summary.get("failed", 0),
                "error":             summary.get("error", 0),
                "total":             summary.get("total", 0),
                "failed_tests":      failed_tests,
                "collection_errors": collection_errors,
            }
        except Exception as e:
            return {"passed": 0, "failed": 0, "error": 0, "total": 0,
                    "failed_tests": [], "collection_errors": [], "parse_error": str(e)}

    def _generate_allure_report(self) -> None:
        """Generate Allure HTML report if allure CLI is available."""
        try:
            subprocess.run(
                ["allure", "generate", str(self.allure_results),
                 "--clean", "-o", str(self.allure_report)],
                check=True, capture_output=True, timeout=60,
                
            )
            print(f"[Runner] Allure report → {self.allure_report}")
        except (subprocess.CalledProcessError, FileNotFoundError):
            print("[Runner] Allure CLI not found — skipping Allure report generation.")
