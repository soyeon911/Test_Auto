"""
Crash Probe 결과 JSON → CSV 변환 스크립트
GitHub Actions crash-probe.yml 에서 호출됨.

Usage:
    python scripts/gen_probe_csv.py
"""

import csv
import json
import os
from pathlib import Path

PROBE_DIR = Path(os.environ.get("PROBE_REPORT_DIR", "reports/crash_probe"))
REPORT_FILE = PROBE_DIR / "report.json"
SUMMARY_OUT = PROBE_DIR / "final_report.csv"

FIELDS = [
    "test_id",
    "nodeid",
    "outcome",
    "classification",
    "duration_s",
    "failure_reason",
]


def classify(t: dict) -> str:
    call = t.get("call", {}) or {}
    longrepr = str(call.get("longrepr", ""))
    if "CRASH_DETECTED" in longrepr:
        return "CRASH_DETECTED"
    if "VALIDATION_GAP" in longrepr:
        return "VALIDATION_GAP"
    if t["outcome"] == "passed":
        return "GRACEFUL_REJECTION"
    if t["outcome"] == "skipped":
        return "SKIPPED"
    return "OTHER_FAILURE"


def main() -> None:
    if not REPORT_FILE.exists():
        print(f"[WARN] {REPORT_FILE} 없음 — pytest-json-report 결과 파일 없음")
        return

    try:
        data = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[WARN] JSON 파싱 실패: {e}")
        return

    rows = []
    for t in data.get("tests", []):
        call = t.get("call", {}) or {}
        longrepr = str(call.get("longrepr", ""))
        rows.append(
            {
                "test_id": t["nodeid"].split("::")[-1],
                "nodeid": t["nodeid"],
                "outcome": t["outcome"],
                "classification": classify(t),
                "duration_s": round(call.get("duration", 0), 3),
                "failure_reason": longrepr[:300].replace("\n", " "),
            }
        )

    if not rows:
        print("[WARN] 프로브 결과 없음")
        return

    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    with SUMMARY_OUT.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    crashes = sum(1 for r in rows if r["classification"] == "CRASH_DETECTED")
    gaps = sum(1 for r in rows if r["classification"] == "VALIDATION_GAP")
    graceful = sum(1 for r in rows if r["classification"] == "GRACEFUL_REJECTION")
    sep = "=" * 60
    print(f"\n{sep}")
    print("  Crash Probe 결과")
    print(sep)
    print(f"  전체 프로브     : {len(rows)}")
    print(f"  CRASH_DETECTED  : {crashes}  <- 서버가 죽은 입력값")
    print(f"  VALIDATION_GAP  : {gaps}  <- 수락하면 안 되는데 success=True")
    print(f"  GRACEFUL_REJECT : {graceful}  <- 올바른 에러 반환 (정상)")
    print(sep)
    print(f"  리포트 -> {SUMMARY_OUT}")


if __name__ == "__main__":
    main()
