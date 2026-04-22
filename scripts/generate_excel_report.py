"""
generate_excel_report.py — CI용 Excel 리포트 생성 스크립트

Usage:
    python scripts/generate_excel_report.py \
        --report-dir reports \
        --base-url   http://127.0.0.1:8080 \
        --swagger    input/QFEapi.json \
        --config     config/config.yaml

동작:
  1. <report-dir>/report.json 또는 pytest_report.json 을 읽어 pass/fail 집계
  2. swagger 파일을 파싱해 API 목록 시트에 채움
  3. <report-dir>/test_report.xlsx 를 출력
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


# ── 프로젝트 루트를 sys.path 에 추가 ─────────────────────────────────────────
# 1) 스크립트 파일 기준 (scripts/ 의 부모 디렉터리)
_SCRIPT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 2) 현재 작업 디렉터리 기준 (CI 에서는 체크아웃 루트)
_CWD_ROOT = os.getcwd()

for _root in (_SCRIPT_ROOT, _CWD_ROOT):
    if _root not in sys.path:
        sys.path.insert(0, _root)

# 진단 출력 (CI 로그에서 경로 확인용)
print(f"[Excel] PROJECT_ROOT candidates: {_SCRIPT_ROOT!r}, {_CWD_ROOT!r}", flush=True)
print(f"[Excel] sys.path[0:3] = {sys.path[:3]}", flush=True)

try:
    from reports.excel_reporter import ExcelReportBuilder
except ImportError as _e:
    print(f"[Excel] ImportError: {_e}", flush=True)
    print(f"[Excel] reports/ 디렉터리 내용: {list(Path(_CWD_ROOT, 'reports').iterdir()) if Path(_CWD_ROOT, 'reports').exists() else '없음'}", flush=True)
    raise

try:
    from reports.excel_reporter2 import ExcelReportBuilder2
except ImportError as _e:
    print(f"[Excel] excel_reporter2 ImportError: {_e} — 기존 리포트만 생성합니다.", flush=True)
    ExcelReportBuilder2 = None  # type: ignore[assignment, misc]


def _load_runner_summary(report_dir: Path) -> dict:
    """report.json / pytest_report.json 에서 pass/fail 집계를 읽는다."""
    for name in ("report.json", "pytest_report.json"):
        p = report_dir / name
        if not p.exists():
            continue
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            s = raw.get("summary", {})
            return {
                "passed":           s.get("passed", 0),
                "failed":           s.get("failed", 0),
                "error":            s.get("error", 0),
                "total":            s.get("total", 0),
                "duration_seconds": raw.get("duration", ""),
                "return_code":      "",
            }
        except Exception as e:
            print(f"[Excel] {name} 파싱 실패: {e}")
    return {"passed": 0, "failed": 0, "error": 0, "total": 0}


def _find_json_report(report_dir: Path) -> Path | None:
    for name in ("report.json", "pytest_report.json"):
        p = report_dir / name
        if p.exists():
            return p
    return None


def _parse_endpoints(swagger: str, config: dict) -> list[dict]:
    """Swagger 파일을 파싱해 endpoint 목록을 반환한다."""
    if not swagger:
        return []
    p = Path(swagger)
    if not p.exists():
        print(f"[Excel] Swagger 파일 없음: {swagger}")
        return []
    try:
        from main import detect_source_and_parse
        return detect_source_and_parse(swagger, config)
    except Exception as e:
        print(f"[Excel] 엔드포인트 파싱 실패: {e}")
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Excel 리포트 생성기")
    parser.add_argument("--report-dir", default="reports",
                        help="pytest 리포트가 있는 디렉터리 (기본: reports)")
    parser.add_argument("--base-url",   default="",
                        help="테스트 대상 서버 URL")
    parser.add_argument("--swagger",    default="",
                        help="Swagger/OpenAPI 파일 경로 (API List 시트용)")
    parser.add_argument("--config",     default="config/config.yaml",
                        help="config.yaml 경로")
    parser.add_argument("--probe-report-dir", default="",
                        help="Crash Probe 리포트 디렉터리 (reports/crash_probe)")
    args = parser.parse_args()

    # config 로드
    config: dict = {}
    try:
        import yaml
        with open(args.config, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[Excel] config 로드 실패 ({args.config}): {e}")

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    xlsx_path    = report_dir / "test_report.xlsx"
    xlsx_path2   = report_dir / "test_report2.xlsx"
    json_report  = _find_json_report(report_dir)
    summary      = _load_runner_summary(report_dir)
    endpoints    = _parse_endpoints(args.swagger, config)
    allure_dir   = report_dir / "allure-results"

    swagger_source = os.getenv("SWAGGER_SOURCE", "").strip()
    display_source = swagger_source or args.swagger

    # Crash Probe 리포트 경로 결정 (--probe-report-dir 우선, 없으면 환경변수, 없으면 기본값)
    probe_report_dir = (
        args.probe_report_dir
        or os.getenv("PROBE_REPORT_DIR", "")
        or "reports/crash_probe"
    )
    probe_json = Path(probe_report_dir) / "report.json"
    crash_probe_json_path: Path | None = probe_json if probe_json.exists() else None

    print(f"[Excel] passed={summary['passed']}  failed={summary['failed']}  total={summary['total']}")
    print(f"[Excel] json_report={json_report}")
    print(f"[Excel] endpoints={len(endpoints)}")
    print(f"[Excel] probe_json={probe_json}  (found={crash_probe_json_path is not None})")
    print(f"[Excel] output={xlsx_path}")
    print(f"[Excel] output2={xlsx_path2}")

    rc = 0

    # ── 리포트 1: test_report.xlsx (excel_reporter.py) ────────────────────────
    try:
        out = ExcelReportBuilder(xlsx_path).build(
            runner_summary=summary,
            pytest_json_path=json_report,
            source_file=display_source,
            base_url=args.base_url,
            endpoints=endpoints,
            allure_results_dir=allure_dir if allure_dir.exists() else None,
            crash_probe_json_path=crash_probe_json_path,
        )
        print(f"[Excel] 리포트1 완료: {out}")
    except Exception as e:
        print(f"[Excel] 리포트1 생성 실패: {e}")
        rc = 1

    # ── 리포트 2: test_report2.xlsx (excel_reporter2.py) ─────────────────────
    if ExcelReportBuilder2 is not None:
        try:
            out2 = ExcelReportBuilder2(xlsx_path2).build(
                runner_summary=summary,
                pytest_json_path=json_report,
                source_file=display_source,
                base_url=args.base_url,
                endpoints=endpoints,
                allure_results_dir=allure_dir if allure_dir.exists() else None,
                crash_probe_json_path=crash_probe_json_path,
            )
            print(f"[Excel] 리포트2 완료: {out2}")
        except Exception as e:
            print(f"[Excel] 리포트2 생성 실패: {e}")
            rc = 1
    else:
        print("[Excel] excel_reporter2 없음 — 리포트2 건너뜀")

    return rc


if __name__ == "__main__":
    sys.exit(main())
