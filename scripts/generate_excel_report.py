"""
generate_excel_report.py — CI용 Excel 리포트 생성 스크립트

Usage:
    python scripts/generate_excel_report.py \
        --report-dir reports \
        --base-url   http://192.168.150.158:8080 \
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
import sys
from pathlib import Path

# ── 프로젝트 루트를 sys.path 에 추가 ──────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from reports.excel_reporter import ExcelReportBuilder


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

    xlsx_path   = report_dir / "test_report.xlsx"
    json_report = _find_json_report(report_dir)
    summary     = _load_runner_summary(report_dir)
    endpoints   = _parse_endpoints(args.swagger, config)
    allure_dir  = report_dir / "allure-results"

    print(f"[Excel] passed={summary['passed']}  failed={summary['failed']}  total={summary['total']}")
    print(f"[Excel] json_report={json_report}")
    print(f"[Excel] endpoints={len(endpoints)}")
    print(f"[Excel] output={xlsx_path}")

    try:
        out = ExcelReportBuilder(xlsx_path).build(
            runner_summary=summary,
            pytest_json_path=json_report,
            source_file=args.swagger,
            base_url=args.base_url,
            endpoints=endpoints,
            allure_results_dir=allure_dir if allure_dir.exists() else None,
        )
        print(f"[Excel] 완료: {out}")
        return 0
    except Exception as e:
        print(f"[Excel] 생성 실패: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
