"""
Excel Report Builder v2 — 4-sheet workbook (Korean)

  Sheet 1 · TC 명세서             — TC ID / 메서드 / 엔드포인트 / 기능설명 /
                                    테스트유형 / 테스트조건 / 기댓값(HTTP) /
                                    기댓값(응답) / 실행결과 / P/F / 비고
  Sheet 2 · 엔드포인트별 요약      — 엔드포인트 단위 PASS/FAIL 집계
  Sheet 3 · 종합 요약              — KPI 박스 + 유형별 분석 + 실행 환경
  Sheet 4 · 전체 결과 상세         — 모든 TC 전체 컬럼 상세 결과
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

# ─── colour palette ────────────────────────────────────────────────────────────
_BLUE_DARK   = "1F497D"
_BLUE_TITLE  = "2E75B6"
_BLUE_LIGHT  = "BDD7EE"
_GREEN_DARK  = "375623"
_GREEN_BG    = "EAF7EA"
_GREEN_FILL  = "C6EFCE"
_RED_BG      = "FFE5E5"
_RED_FILL    = "FFC7CE"
_RED_FONT    = "C00000"
_YELLOW_BG   = "FFEB9C"
_YELLOW_FILL = "FFEB9C"
_GRAY_HEADER = "D9D9D9"
_WHITE       = "FFFFFF"
_STRIPE      = "FAFAFA"

_METHOD_COLORS = {
    "GET":    "D9EAD3",
    "POST":   "FCE5CD",
    "PUT":    "FFF2CC",
    "DELETE": "F4CCCC",
    "PATCH":  "EAD1DC",
}

_TYPE_KO = {
    "positive":        "정상(Positive)",
    "wrong_type":      "타입오류(WrongType)",
    "boundary":        "경계값(Boundary)",
    "semantic_probe":  "시맨틱(SemanticProbe)",
    "missing_required":"필수누락(MissingRequired)",
}

_EP_DESC = {
    "/health":                "서버 상태 확인",
    "/api/v2/detect":         "얼굴 인식/검출",
    "/api/v2/compare":        "얼굴 비교",
    "/api/v2/identify":       "얼굴 식별",
    "/api/v2/enroll":         "얼굴 등록",
    "/api/v2/delete":         "얼굴 데이터 삭제",
    "/api/v2/list":           "등록 목록 조회",
    "/api/v2/get_config":     "설정값 조회",
    "/api/v2/save_config":    "설정값 저장",
    "/api/v2/reset_config":   "설정 초기화",
    "/api/v2/get_license":    "라이선스 조회",
    "/api/v2/get_version":    "버전 정보 조회",
}

_THIN = Side(style="thin", color="CCCCCC")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


class ExcelReportBuilder2:
    """excel_reporter.py 인터페이스 호환, 4-시트 한국어 워크북 생성."""

    def __init__(self, output_path: str | Path):
        self.output_path = Path(output_path)

    # ─── public entry point ───────────────────────────────────────────────────

    def build(
        self,
        runner_summary: dict[str, Any],
        pytest_json_path: str | Path | None = None,
        source_file: str = "",
        base_url: str = "",
        endpoints: list[dict] | None = None,
        allure_results_dir: str | Path | None = None,
    ) -> Path:
        tests = self._load_test_results(
            pytest_json_path=pytest_json_path,
            allure_results_dir=allure_results_dir,
        )

        wb = Workbook()

        ws1 = wb.active
        ws1.title = "TC 명세서"
        self._build_tc_spec(ws1, tests)

        ws2 = wb.create_sheet("엔드포인트별 요약")
        self._build_endpoint_summary(ws2, tests)

        ws3 = wb.create_sheet("종합 요약")
        self._build_overall_summary(ws3, runner_summary, tests, source_file, base_url)

        ws4 = wb.create_sheet("전체 결과 상세")
        self._build_detail_table(ws4, tests, base_url)

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(self.output_path)
        return self.output_path

    # ─── Sheet 1: TC 명세서 ────────────────────────────────────────────────────

    def _build_tc_spec(self, ws, tests: list[dict[str, Any]]) -> None:
        self._title_banner(ws, "A1:K1", "TC 명세서 (Test Case Specification)")
        ws.row_dimensions[1].height = 26

        headers = [
            "TC ID", "HTTP 메서드", "엔드포인트", "기능설명",
            "테스트유형", "테스트조건",
            "기댓값\n(HTTP 상태)", "기댓값\n(응답)",
            "실행결과", "P/F", "비고",
        ]
        self._header_row(ws, 2, headers)

        col_widths = [10, 12, 32, 18, 20, 42, 14, 28, 28, 8, 20]
        for i, w in enumerate(col_widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

        ca = Alignment(horizontal="center", vertical="top", wrap_text=True)
        la = Alignment(horizontal="left",   vertical="top", wrap_text=True)

        for idx, item in enumerate(tests, start=1):
            info   = self._parse_nodeid(item.get("nodeid", ""), {})
            method = (item.get("request_method") or info["method"]).upper()
            path   = item.get("request_path") or info["path"] or ""
            rtype  = item.get("rule_type") or info["rule_type"] or ""
            cond   = item.get("condition") or info["condition"] or ""
            outcome = str(item.get("outcome", "")).lower()
            pf     = "PASS" if outcome == "passed" else "FAIL"

            exp_http, exp_resp = self._split_expected(
                item.get("expected_status_display") or info.get("expected_status", "")
            )
            actual = self._build_actual_short(item)
            note   = self._build_note(item, outcome)

            row_vals = [
                f"TC-{idx:04d}",
                method,
                path,
                _EP_DESC.get(path, ""),
                _TYPE_KO.get(rtype, rtype),
                cond,
                exp_http,
                exp_resp,
                actual,
                pf,
                note,
            ]

            r = idx + 2
            for ci, val in enumerate(row_vals, start=1):
                c = ws.cell(row=r, column=ci, value=val)
                c.border = _BORDER
                c.alignment = ca if ci in (1, 2, 7, 10) else la

            # P/F 색상
            pf_cell = ws.cell(row=r, column=10)
            if pf == "PASS":
                pf_cell.font = Font(name="Arial", size=9, bold=True, color=_GREEN_DARK)
                pf_cell.fill = PatternFill("solid", start_color=_GREEN_FILL, end_color=_GREEN_FILL)
            else:
                pf_cell.font = Font(name="Arial", size=9, bold=True, color=_RED_FONT)
                pf_cell.fill = PatternFill("solid", start_color=_RED_FILL, end_color=_RED_FILL)

            # 행 stripe
            if pf == "FAIL":
                for ci in range(1, 10):
                    ws.cell(row=r, column=ci).fill = PatternFill("solid", start_color="FFF0ED", end_color="FFF0ED")
            elif idx % 2 == 0:
                for ci in range(1, 10):
                    ws.cell(row=r, column=ci).fill = PatternFill("solid", start_color=_STRIPE, end_color=_STRIPE)

            ws.row_dimensions[r].height = max(14, min(60, 14 + len(cond) // 5))

        ws.freeze_panes = "A3"
        ws.auto_filter.ref = f"A2:K{len(tests) + 2}"

    # ─── Sheet 2: 엔드포인트별 요약 ───────────────────────────────────────────

    def _build_endpoint_summary(self, ws, tests: list[dict[str, Any]]) -> None:
        self._title_banner(ws, "A1:H1", "엔드포인트별 테스트 요약")
        ws.row_dimensions[1].height = 26

        headers = [
            "HTTP 메서드", "엔드포인트", "기능설명",
            "전체 TC", "PASS", "FAIL", "통과율 (%)", "주요 이슈",
        ]
        self._header_row(ws, 2, headers)

        col_widths = [12, 32, 18, 10, 10, 10, 12, 50]
        for i, w in enumerate(col_widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

        # 엔드포인트 × 메서드 집계
        ep_map: dict[tuple[str, str], dict] = {}
        for item in tests:
            info   = self._parse_nodeid(item.get("nodeid", ""), {})
            method = (item.get("request_method") or info["method"]).upper() or "POST"
            path   = item.get("request_path") or info["path"] or "(unknown)"
            key    = (method, path)
            if key not in ep_map:
                ep_map[key] = {"total": 0, "pass": 0, "fail": 0, "fail_conditions": []}
            ep_map[key]["total"] += 1
            outcome = str(item.get("outcome", "")).lower()
            if outcome == "passed":
                ep_map[key]["pass"] += 1
            else:
                ep_map[key]["fail"] += 1
                cond = item.get("condition") or ""
                if cond and cond not in ep_map[key]["fail_conditions"]:
                    ep_map[key]["fail_conditions"].append(cond)

        ca = Alignment(horizontal="center", vertical="center")
        la = Alignment(horizontal="left",   vertical="center", wrap_text=True)

        for ri, ((method, path), stat) in enumerate(
            sorted(ep_map.items(), key=lambda x: x[0][1]), start=3
        ):
            total  = stat["total"]
            passed = stat["pass"]
            failed = stat["fail"]
            rate   = round(passed / total * 100, 1) if total else 0.0
            issues = "; ".join(stat["fail_conditions"][:3])
            if len(stat["fail_conditions"]) > 3:
                issues += f" 외 {len(stat['fail_conditions']) - 3}건"

            row_vals = [
                method, path, _EP_DESC.get(path, ""),
                total, passed, failed, f"{rate}%", issues or "없음",
            ]
            bg = _METHOD_COLORS.get(method, _WHITE)

            for ci, val in enumerate(row_vals, start=1):
                c = ws.cell(row=ri, column=ci, value=val)
                c.border = _BORDER
                c.alignment = ca if ci in (1, 4, 5, 6, 7) else la
                if ci <= 3:
                    c.fill = PatternFill("solid", start_color=bg, end_color=bg)

            # PASS/FAIL 셀 색
            ws.cell(row=ri, column=5).fill = PatternFill("solid", start_color=_GREEN_FILL, end_color=_GREEN_FILL)
            if failed > 0:
                ws.cell(row=ri, column=6).fill = PatternFill("solid", start_color=_RED_FILL, end_color=_RED_FILL)

            # 통과율 색상
            rate_cell = ws.cell(row=ri, column=7)
            if rate >= 90:
                rate_cell.fill = PatternFill("solid", start_color=_GREEN_FILL, end_color=_GREEN_FILL)
                rate_cell.font = Font(bold=True, color=_GREEN_DARK)
            elif rate >= 70:
                rate_cell.fill = PatternFill("solid", start_color=_YELLOW_FILL, end_color=_YELLOW_FILL)
                rate_cell.font = Font(bold=True)
            else:
                rate_cell.fill = PatternFill("solid", start_color=_RED_FILL, end_color=_RED_FILL)
                rate_cell.font = Font(bold=True, color=_RED_FONT)

            ws.row_dimensions[ri].height = 18

        ws.freeze_panes = "A3"

    # ─── Sheet 3: 종합 요약 ────────────────────────────────────────────────────

    def _build_overall_summary(
        self,
        ws,
        summary: dict[str, Any],
        tests: list[dict[str, Any]],
        source_file: str,
        base_url: str,
    ) -> None:
        self._title_banner(ws, "A1:F1", "자동화 테스트 최종 리포트 — 종합 요약")
        ws.row_dimensions[1].height = 28
        ws.column_dimensions["A"].width = 20
        ws.column_dimensions["B"].width = 18
        ws.column_dimensions["C"].width = 18
        ws.column_dimensions["D"].width = 18
        ws.column_dimensions["E"].width = 18
        ws.column_dimensions["F"].width = 28

        total  = len(tests)
        passed = sum(1 for t in tests if str(t.get("outcome","")).lower() == "passed")
        failed = total - passed
        rate   = round(passed / total * 100, 1) if total else 0.0

        # ── KPI 박스 ────────────────────────────────────────────────────────
        self._section_header(ws, "A3:F3", "📊 핵심 지표 (KPI)")

        kpi_labels = ["총 TC 수", "PASS", "FAIL", "통과율"]
        kpi_values = [str(total), str(passed), str(failed), f"{rate}%"]
        kpi_fgs    = [_BLUE_LIGHT, _GREEN_FILL, _RED_FILL,
                      _GREEN_FILL if rate >= 80 else _YELLOW_FILL if rate >= 60 else _RED_FILL]
        kpi_fonts  = [_BLUE_DARK,  _GREEN_DARK, _RED_FONT,
                      _GREEN_DARK if rate >= 80 else "7F6000" if rate >= 60 else _RED_FONT]

        for ci, (label, value, fg, fc) in enumerate(zip(kpi_labels, kpi_values, kpi_fgs, kpi_fonts), start=1):
            lc = ws.cell(row=4, column=ci, value=label)
            lc.font  = Font(bold=True, size=10, color=_WHITE)
            lc.fill  = PatternFill("solid", start_color=_BLUE_DARK, end_color=_BLUE_DARK)
            lc.alignment = Alignment(horizontal="center", vertical="center")
            lc.border = _BORDER
            ws.row_dimensions[4].height = 20

            vc = ws.cell(row=5, column=ci, value=value)
            vc.font  = Font(bold=True, size=16, color=fc)
            vc.fill  = PatternFill("solid", start_color=fg, end_color=fg)
            vc.alignment = Alignment(horizontal="center", vertical="center")
            vc.border = _BORDER
            ws.row_dimensions[5].height = 32

        # ── 테스트유형별 분석 ────────────────────────────────────────────────
        self._section_header(ws, "A7:F7", "🔍 테스트유형별 분석")

        type_headers = ["테스트유형", "전체", "PASS", "FAIL", "통과율", "판정"]
        for ci, h in enumerate(type_headers, start=1):
            c = ws.cell(row=8, column=ci, value=h)
            c.font  = Font(bold=True, color=_WHITE)
            c.fill  = PatternFill("solid", start_color=_BLUE_DARK, end_color=_BLUE_DARK)
            c.alignment = Alignment(horizontal="center", vertical="center")
            c.border = _BORDER
        ws.row_dimensions[8].height = 18

        type_counts: dict[str, dict] = {}
        for item in tests:
            info  = self._parse_nodeid(item.get("nodeid", ""), {})
            rtype = item.get("rule_type") or info["rule_type"] or "기타"
            if rtype not in type_counts:
                type_counts[rtype] = {"total": 0, "pass": 0, "fail": 0}
            type_counts[rtype]["total"] += 1
            if str(item.get("outcome","")).lower() == "passed":
                type_counts[rtype]["pass"] += 1
            else:
                type_counts[rtype]["fail"] += 1

        for ri, (rtype, stat) in enumerate(
            sorted(type_counts.items(), key=lambda x: -x[1]["total"]), start=9
        ):
            t = stat["total"]; p = stat["pass"]; f = stat["fail"]
            r2 = round(p / t * 100, 1) if t else 0.0
            verdict = "✅ 양호" if r2 >= 90 else "⚠️ 주의" if r2 >= 60 else "❌ 불량"

            row_vals = [_TYPE_KO.get(rtype, rtype), t, p, f, f"{r2}%", verdict]
            for ci, val in enumerate(row_vals, start=1):
                c = ws.cell(row=ri, column=ci, value=val)
                c.alignment = Alignment(horizontal="center", vertical="center")
                c.border = _BORDER
                if ci == 1:
                    c.alignment = Alignment(horizontal="left", vertical="center")
            if f > 0:
                ws.cell(row=ri, column=4).fill = PatternFill("solid", start_color=_RED_FILL, end_color=_RED_FILL)
            ws.cell(row=ri, column=3).fill = PatternFill("solid", start_color=_GREEN_FILL, end_color=_GREEN_FILL)
            ws.row_dimensions[ri].height = 17

        next_row = 9 + len(type_counts) + 1

        # ── 주요 실패 원인 ───────────────────────────────────────────────────
        self._section_header(ws, f"A{next_row}:F{next_row}", "⚠️ 주요 실패 원인 분석")
        fail_tests = [t for t in tests if str(t.get("outcome","")).lower() != "passed"]

        fail_reasons: dict[str, int] = {}
        for item in fail_tests:
            cond = item.get("condition") or ""
            if not cond:
                info = self._parse_nodeid(item.get("nodeid",""), {})
                cond = info.get("condition","")
            key = cond[:60] if cond else "(조건 정보 없음)"
            fail_reasons[key] = fail_reasons.get(key, 0) + 1

        top_reasons = sorted(fail_reasons.items(), key=lambda x: -x[1])[:10]
        for i, (reason, cnt) in enumerate(top_reasons):
            rr = next_row + 1 + i
            ws.cell(row=rr, column=1, value=f"{i+1}.").alignment = Alignment(horizontal="center")
            ws.cell(row=rr, column=1).border = _BORDER
            c = ws.cell(row=rr, column=2, value=reason)
            c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
            c.border = _BORDER
            ws.merge_cells(f"B{rr}:E{rr}")
            cnt_c = ws.cell(row=rr, column=6, value=f"{cnt}건")
            cnt_c.alignment = Alignment(horizontal="center")
            cnt_c.fill = PatternFill("solid", start_color=_RED_FILL, end_color=_RED_FILL)
            cnt_c.border = _BORDER
            ws.row_dimensions[rr].height = 16

        next_row = next_row + 1 + len(top_reasons) + 1

        # ── 실행 환경 ────────────────────────────────────────────────────────
        self._section_header(ws, f"A{next_row}:F{next_row}", "🖥️ 실행 환경 정보")
        env_rows = [
            ("생성일시",     datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ("소스 파일",    source_file),
            ("서버 URL",     base_url),
            ("총 실행 시간", f"{summary.get('duration_seconds', '')} 초"),
            ("테스트 프레임워크", "pytest + pytest-json-report"),
            ("리포트 작성",  "excel_reporter2.py (AutoTC)"),
        ]
        for i, (label, value) in enumerate(env_rows):
            rr = next_row + 1 + i
            lc = ws.cell(row=rr, column=1, value=label)
            lc.font = Font(bold=True)
            lc.fill = PatternFill("solid", start_color=_GRAY_HEADER, end_color=_GRAY_HEADER)
            lc.border = _BORDER
            lc.alignment = Alignment(horizontal="left", vertical="center")
            vc = ws.cell(row=rr, column=2, value=value)
            vc.border = _BORDER
            vc.alignment = Alignment(horizontal="left", vertical="center")
            ws.merge_cells(f"B{rr}:F{rr}")
            ws.row_dimensions[rr].height = 16

    # ─── Sheet 4: 전체 결과 상세 ──────────────────────────────────────────────

    def _build_detail_table(self, ws, tests: list[dict[str, Any]], base_url: str) -> None:
        self._title_banner(ws, "A1:L1", "자동화 테스트 최종 리포트 — 전체 결과 상세")
        ws.row_dimensions[1].height = 26

        headers = [
            "#", "TC ID", "HTTP 메서드", "엔드포인트",
            "테스트유형", "테스트조건",
            "기댓값(HTTP)", "기댓값(응답)",
            "실제 HTTP", "실제 응답",
            "P/F", "소요시간(s)",
        ]
        self._header_row(ws, 2, headers)

        col_widths = [5, 10, 12, 30, 20, 42, 14, 26, 14, 26, 8, 12]
        for i, w in enumerate(col_widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

        ca = Alignment(horizontal="center", vertical="top", wrap_text=True)
        la = Alignment(horizontal="left",   vertical="top", wrap_text=True)

        for idx, item in enumerate(tests, start=1):
            info    = self._parse_nodeid(item.get("nodeid", ""), {})
            method  = (item.get("request_method") or info["method"]).upper()
            path    = item.get("request_path") or info["path"] or ""
            rtype   = item.get("rule_type") or info["rule_type"] or ""
            cond    = item.get("condition") or info["condition"] or ""
            outcome = str(item.get("outcome", "")).lower()
            pf      = "PASS" if outcome == "passed" else "FAIL"
            dur     = round(float(item.get("duration", 0) or 0), 3)

            exp_http, exp_resp = self._split_expected(
                item.get("expected_status_display") or info.get("expected_status", "")
            )
            act_http  = str(item.get("actual_status") or "")
            act_resp  = self._build_actual_resp(item)

            row_vals = [
                idx,
                f"TC-{idx:04d}",
                method,
                path,
                _TYPE_KO.get(rtype, rtype),
                cond,
                exp_http,
                exp_resp,
                act_http,
                act_resp,
                pf,
                dur,
            ]

            r = idx + 2
            for ci, val in enumerate(row_vals, start=1):
                c = ws.cell(row=r, column=ci, value=val)
                c.border = _BORDER
                c.alignment = ca if ci in (1, 2, 3, 7, 9, 11, 12) else la

            # P/F 색상
            pf_c = ws.cell(row=r, column=11)
            if pf == "PASS":
                pf_c.font = Font(bold=True, color=_GREEN_DARK)
                pf_c.fill = PatternFill("solid", start_color=_GREEN_FILL, end_color=_GREEN_FILL)
            else:
                pf_c.font = Font(bold=True, color=_RED_FONT)
                pf_c.fill = PatternFill("solid", start_color=_RED_FILL, end_color=_RED_FILL)

            # 행 stripe
            if pf == "FAIL":
                for ci in [1,2,3,4,5,6,7,8,9,10,12]:
                    ws.cell(row=r, column=ci).fill = PatternFill("solid", start_color="FFF0ED", end_color="FFF0ED")
            elif idx % 2 == 0:
                for ci in [1,2,3,4,5,6,7,8,9,10,12]:
                    ws.cell(row=r, column=ci).fill = PatternFill("solid", start_color=_STRIPE, end_color=_STRIPE)

            ws.row_dimensions[r].height = max(14, min(56, 14 + len(cond) // 5))

        ws.freeze_panes = "A3"
        ws.auto_filter.ref = f"A2:L{len(tests) + 2}"

    # ─── test result loaders ─────────────────────────────────────────────────

    def _load_test_results(
        self,
        pytest_json_path: str | Path | None,
        allure_results_dir: str | Path | None,
    ) -> list[dict[str, Any]]:
        tests: list[dict[str, Any]] = []
        if pytest_json_path:
            p = Path(pytest_json_path)
            if p.exists() and p.is_file():
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(raw, dict) and "tests" in raw:
                        tests = self._normalize_pytest_json(raw)
                except Exception:
                    pass

        if not tests and allure_results_dir:
            d = Path(allure_results_dir)
            if d.exists() and d.is_dir():
                tests = self._normalize_allure(d)

        diag_map = self._load_diag_jsonl(pytest_json_path)
        if diag_map:
            for t in tests:
                diag = diag_map.get(t.get("nodeid", ""))
                if diag:
                    self._apply_diag(t, diag)

        return tests

    def _normalize_pytest_json(self, raw: dict) -> list[dict[str, Any]]:
        out = []
        for t in raw.get("tests", []):
            call = t.get("call") or {}
            meta = self._extract_tc_meta(t)
            out.append({
                "nodeid":   t.get("nodeid", ""),
                "outcome":  t.get("outcome", "unknown"),
                "duration": call.get("duration", t.get("duration", 0)),
                "longrepr": str(call.get("longrepr") or t.get("longrepr") or ""),
                "rule_type":       meta.get("rule_type", ""),
                "target_param":    meta.get("target_param", ""),
                "condition":       meta.get("condition", ""),
                "request_method":  meta.get("request_method", ""),
                "request_path":    meta.get("request_path", ""),
                "request_query":   meta.get("request_query", {}),
                "request_headers": meta.get("request_headers", {}),
                "request_body":    meta.get("request_body"),
                "expected_status": meta.get("expected_status", []),
                "expected_status_display": self._coerce_expected(meta),
                "actual_status":   meta.get("actual_status", ""),
                "response_text":   meta.get("response_text", ""),
                "exception_type":  meta.get("exception_type", ""),
                "exception_message": meta.get("exception_message", ""),
                "server_crashed":  meta.get("server_crashed", False),
                "axis": "", "reason_code": "", "error_detail": "",
                "response_success": None, "response_error_code": None, "response_msg": None,
            })
        return out

    def _normalize_allure(self, d: Path) -> list[dict[str, Any]]:
        out = []
        for p in sorted(d.glob("*-result.json")):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            start = raw.get("start"); stop = raw.get("stop")
            dur = round((stop - start) / 1000.0, 3) if isinstance(start, (int, float)) and isinstance(stop, (int, float)) and stop >= start else 0.0
            details = raw.get("statusDetails") or {}
            out.append({
                "nodeid":  raw.get("fullName") or raw.get("name") or p.stem,
                "outcome": str(raw.get("status", "unknown")).lower(),
                "duration": dur,
                "longrepr": str(details.get("message") or details.get("trace") or ""),
                **{k: "" for k in ["rule_type","target_param","condition","request_method","request_path","actual_status","response_text","exception_type","exception_message","error_detail","axis","reason_code"]},
                "request_query": {}, "request_headers": {}, "request_body": None,
                "expected_status": [], "expected_status_display": "",
                "server_crashed": False,
                "response_success": None, "response_error_code": None, "response_msg": None,
            })
        return out

    @staticmethod
    def _load_diag_jsonl(pytest_json_path: str | Path | None) -> dict[str, dict]:
        candidates = []
        if pytest_json_path:
            candidates.append(Path(pytest_json_path).parent / "test_diag.jsonl")
        candidates.append(Path("reports/test_diag.jsonl"))
        for p in candidates:
            if not p.exists():
                continue
            result: dict[str, dict] = {}
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    test_id = rec.get("test_id", "")
                    diag    = rec.get("diag")
                    if test_id and isinstance(diag, dict):
                        result[test_id] = diag
                return result
            except Exception:
                pass
        return {}

    @staticmethod
    def _apply_diag(item: dict[str, Any], diag: dict[str, Any]) -> None:
        item["axis"]            = diag.get("axis", "")
        item["reason_code"]     = diag.get("reason_code", "")
        item["target_param"]    = diag.get("target_field", item.get("target_param", ""))
        item["condition"]       = diag.get("test_condition", item.get("condition", ""))
        exp_http = diag.get("expected_http") or ""
        exp_app  = diag.get("expected_app")  or ""
        parts = [p for p in [exp_http, exp_app] if p]
        if parts:
            item["expected_status_display"] = " / ".join(parts)
        if diag.get("actual_status") is not None:
            item["actual_status"] = str(diag["actual_status"])
        for k in ("request_body", "request_query", "request_headers"):
            v = diag.get(k)
            if v is not None:
                item[k] = v
        item["response_text"]     = diag.get("response_snippet", item.get("response_text", ""))
        item["exception_type"]    = diag.get("exception_type", "")
        item["exception_message"] = diag.get("exception_message", "")
        item["server_crashed"]    = bool(diag.get("server_crash", False))
        item["error_detail"]      = diag.get("error_detail", "")
        item["response_success"]  = diag.get("response_success")
        item["response_error_code"] = diag.get("response_error_code")
        item["response_msg"]      = diag.get("response_msg")

    # ─── metadata / parse helpers ─────────────────────────────────────────────

    @staticmethod
    def _extract_tc_meta(test_obj: dict[str, Any]) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        md = test_obj.get("metadata") or {}
        if isinstance(md, dict):
            tc_meta = md.get("tc_meta")
            if isinstance(tc_meta, dict):
                meta.update(tc_meta)
        for up in test_obj.get("user_properties", []) or []:
            if isinstance(up, (list, tuple)) and len(up) == 2 and up[0] == "tc_meta":
                if isinstance(up[1], dict):
                    meta.update(up[1])
        return meta

    @staticmethod
    def _coerce_expected(meta: dict[str, Any]) -> str:
        if meta.get("expected_status_display"):
            return str(meta["expected_status_display"])
        if meta.get("expected_status"):
            return ", ".join(map(str, meta["expected_status"]))
        return ""

    def _parse_nodeid(self, nodeid: str, src_cache: dict) -> dict:
        result: dict[str, str] = {
            "method": "", "path": "", "rule_type": "",
            "condition": "", "expected_status": "",
        }
        if not nodeid:
            return result

        file_part, func_name = (nodeid.split("::", 1) if "::" in nodeid else ("", nodeid))

        if file_part:
            method, path = self._read_file_header(file_part, src_cache)
            result["method"] = method
            result["path"]   = path

        if not result["method"]:
            m = re.search(r"test_(get|post|put|delete|patch)__", func_name, re.IGNORECASE)
            if m:
                result["method"] = m.group(1).upper()

        fn = func_name.lower()

        if fn.endswith("_positive"):
            result.update(rule_type="positive", condition="정상 요청 (모든 필수 필드 유효값 포함)", expected_status="HTTP 200 / success=true")
        elif m2 := re.search(r"_missing_body_(.+)$", fn):
            result.update(rule_type="missing_required", condition=f"필수 바디 필드 누락: {m2.group(1)}", expected_status="HTTP 200 / success=false")
        elif fn.endswith("_missing_body"):
            result.update(rule_type="missing_required", condition="요청 바디 전체 누락", expected_status="HTTP 200 / success=false")
        elif m2 := re.search(r"_missing_(.+)$", fn):
            result.update(rule_type="missing_required", condition=f"필수 파라미터 누락: {m2.group(1)}", expected_status="HTTP 200 / success=false")
        elif m2 := re.search(r"_wrong_type_body_(.+)$", fn):
            result.update(rule_type="wrong_type", condition=f"바디 필드 타입 오류: {m2.group(1)}", expected_status="HTTP 200 / success=false")
        elif m2 := re.search(r"_wrong_type_(.+)$", fn):
            result.update(rule_type="wrong_type", condition=f"파라미터 타입 오류: {m2.group(1)}", expected_status="HTTP 200 / success=false")
        elif m2 := re.search(r"_boundary_body_(.+?)_(min|max|example|above_max|below_min)$", fn):
            result.update(rule_type="boundary", condition=f"경계값 탐색: {m2.group(1)} [{m2.group(2)}]", expected_status="HTTP 200 / 500 미만")
        elif m2 := re.search(r"_boundary_(.+?)_(min|max|example|above_max|below_min)$", fn):
            result.update(rule_type="boundary", condition=f"경계값 탐색: {m2.group(1)} [{m2.group(2)}]", expected_status="HTTP 200 / 500 미만")
        elif m2 := re.search(r"_semantic_(.+?)_(.+)$", fn):
            result.update(rule_type="semantic_probe", condition=f"시맨틱 탐색: {m2.group(1)} [{m2.group(2)}]", expected_status="HTTP 200 / 500 미만")
        elif m2 := re.search(r"_invalid_enum_body_(.+)$", fn):
            result.update(rule_type="wrong_type", condition=f"허용 범위 외 열거형: {m2.group(1)}", expected_status="HTTP 200 / success=false")

        return result

    def _read_file_header(self, file_path: str, cache: dict) -> tuple[str, str]:
        if not file_path:
            return "", ""
        if file_path not in cache:
            p = Path(file_path)
            cache[file_path] = p.read_text(encoding="utf-8") if p.exists() else ""
        src = cache[file_path]
        m = re.search(r"^#\s*(GET|POST|PUT|DELETE|PATCH)\s+(\S+)", src, re.MULTILINE | re.IGNORECASE)
        if m:
            return m.group(1).upper(), m.group(2).strip()
        return "", ""

    # ─── display helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _split_expected(display: str) -> tuple[str, str]:
        """'HTTP 200 / success=true, error_code=0' → ('HTTP 200', 'success=true, error_code=0')"""
        text = str(display or "")
        if "/" in text:
            parts = text.split("/", 1)
            return parts[0].strip(), parts[1].strip()
        if re.match(r"^\d{3}", text.strip()):
            return text.strip(), ""
        return "", text.strip()

    def _build_actual_short(self, item: dict[str, Any]) -> str:
        parts = []
        sts = item.get("actual_status")
        if sts:
            parts.append(str(sts))
        rs = item.get("response_success")
        if rs is not None:
            parts.append(f"success={'true' if rs else 'false'}")
        ec = item.get("response_error_code")
        if ec not in (None, ""):
            parts.append(f"error_code={ec}")
        msg = item.get("response_msg") or ""
        if msg:
            parts.append(str(msg)[:40])
        return " / ".join(parts)

    def _build_actual_resp(self, item: dict[str, Any]) -> str:
        parts = []
        rs = item.get("response_success")
        if rs is not None:
            parts.append(f"success={'true' if rs else 'false'}")
        ec = item.get("response_error_code")
        if ec not in (None, ""):
            parts.append(f"error_code={ec}")
        msg = item.get("response_msg") or ""
        if msg:
            parts.append(str(msg)[:60])
        return " / ".join(parts)

    @staticmethod
    def _build_note(item: dict[str, Any], outcome: str) -> str:
        if item.get("server_crashed"):
            return "⚠️ 서버 크래시 발생"
        if item.get("exception_type"):
            return f"예외: {item['exception_type']}"
        if outcome not in ("passed", ""):
            return str(item.get("error_detail") or "")[:50]
        return ""

    # ─── generic styling helpers ──────────────────────────────────────────────

    def _title_banner(self, ws, cell_range: str, text: str) -> None:
        ws.merge_cells(cell_range)
        c = ws[cell_range.split(":")[0]]
        c.value = text
        c.font  = Font(bold=True, size=13, color=_WHITE)
        c.fill  = PatternFill("solid", start_color=_BLUE_TITLE, end_color=_BLUE_TITLE)
        c.alignment = Alignment(horizontal="center", vertical="center")

    def _section_header(self, ws, cell_range: str, text: str) -> None:
        ws.merge_cells(cell_range)
        c = ws[cell_range.split(":")[0]]
        c.value = text
        c.font  = Font(bold=True, size=11, color=_BLUE_DARK)
        c.fill  = PatternFill("solid", start_color=_BLUE_LIGHT, end_color=_BLUE_LIGHT)
        c.alignment = Alignment(horizontal="left", vertical="center")
        c.border = _BORDER
        row = int(re.search(r"\d+", cell_range).group())
        ws.row_dimensions[row].height = 20

    def _header_row(self, ws, row: int, headers: list[str]) -> None:
        for col, h in enumerate(headers, start=1):
            c = ws.cell(row=row, column=col, value=h)
            c.font  = Font(bold=True, color=_WHITE, size=9)
            c.fill  = PatternFill("solid", start_color=_BLUE_DARK, end_color=_BLUE_DARK)
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            c.border = _BORDER
        ws.row_dimensions[row].height = 20
