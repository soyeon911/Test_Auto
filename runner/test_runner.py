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
        self.base_url = server_cfg.get("base_url", "http://127.0.0.1:8080")

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
            "--import-mode=importlib",
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

    def collect_nodeids(self, test_dirs: list[str] | None = None) -> list[str]:
        """
        pytest --collect-only 로 전체 테스트 ID를 실행 순서대로 수집한다.
        재기동 후 이어달리기에 사용.
        """
        if test_dirs is None:
            test_dirs = self._default_test_dirs

        cmd = [
            sys.executable, "-m", "pytest",
            "--collect-only", "-q", "--no-header",
            "--import-mode=importlib",
            *[d for d in test_dirs if Path(d).exists()],
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            print("[Runner] collect_nodeids timed out")
            return []

        nodeids = []
        for line in result.stdout.splitlines():
            line = line.strip()
            # nodeid 라인은 반드시 "::" 포함, 요약 라인("N tests collected")은 제외
            if "::" in line and not line.startswith("<"):
                nodeids.append(line)

        print(f"[Runner] Collected {len(nodeids)} test ID(s) total.")
        return nodeids

    def run_nodeids(self, nodeids: list[str]) -> dict[str, Any]:
        """
        지정된 nodeid 목록만 실행한다.
        서버 재기동 후 미완료 테스트를 이어서 실행할 때 사용.
        결과는 별도 pytest_report_resume.json 에 저장.

        Windows 커맨드라인 길이 제한(~32767자) 우회를 위해
        nodeid 목록을 임시 argfile 에 기록하고 pytest @argfile 방식으로 전달한다.
        """
        import os
        import tempfile

        if not nodeids:
            print("[Runner] 이어달릴 테스트 없음 — 건너뜀.")
            return {"passed": 0, "failed": 0, "error": 0, "total": 0,
                    "failed_tests": [], "collection_errors": []}

        self.allure_results.mkdir(parents=True, exist_ok=True)
        self.html_report.parent.mkdir(parents=True, exist_ok=True)

        json_report = self.html_report.parent / "pytest_report_resume.json"

        # ── nodeid 목록을 임시 파일에 기록 (Windows 커맨드라인 길이 제한 우회) ──
        argfile_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8"
            ) as f:
                for nid in nodeids:
                    f.write(nid + "\n")
                argfile_path = f.name

            cmd = [
                sys.executable, "-m", "pytest",
                "--import-mode=importlib",
                f"@{argfile_path}",          # ← argfile로 nodeid 전달
                f"--base-url={self.base_url}",
                f"--alluredir={self.allure_results}",
                f"--html={self.html_report}",
                "--self-contained-html",
                "--json-report",
                f"--json-report-file={json_report}",
                "--tb=short",
                "-q",
            ]

            print(f"[Runner] Resume: {len(nodeids)}개 테스트 이어서 실행 중...")
            print(f"[Runner] argfile: {argfile_path}")

            if json_report.exists():
                json_report.unlink()

            start = datetime.now()
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
            except subprocess.TimeoutExpired:
                return {"error": "pytest timed out", "passed": 0, "failed": 0, "total": 0}

        finally:
            # 임시 파일 정리
            if argfile_path and os.path.exists(argfile_path):
                try:
                    os.unlink(argfile_path)
                except Exception:
                    pass

        elapsed = (datetime.now() - start).total_seconds()

        summary = self._parse_json_report(json_report)
        summary["duration_seconds"] = round(elapsed, 1)
        summary["stdout"] = result.stdout[-3000:]
        summary["return_code"] = result.returncode
        summary["pytest_json_path"] = str(json_report)
        summary["html_report_path"] = str(self.html_report)

        print(f"[Runner] Resume 완료 ({elapsed:.1f}s) — "
              f"passed={summary['passed']} failed={summary['failed']} total={summary['total']}"
              f"  (pytest rc={result.returncode})")

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
