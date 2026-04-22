"""
Excel Report Builder — 3-sheet workbook

  Sheet 1 · Summary    — test run key metrics
  Sheet 2 · API List   — endpoint catalog from the API spec
  Sheet 3 · TC Table   — per-test-case: condition / data / expected /
                          actual status / outcome / duration (실행 결과 중심)
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# ─── colour palette ───────────────────────────────────────────────────────────
_BLUE_DARK = "1F497D"
_BLUE_TITLE = "2E75B6"
_GREEN_BG = "EAF7EA"
_RED_BG = "FFE5E5"
_YELLOW_BG = "FFEB9C"

# Failure Cause 색상
_FC_COLORS: dict[str, str] = {
    "PASS":                              "EAF7EA",
    "서버 Crash (5xx)":                  "F4CCCC",
    "서버 미응답":                        "F4CCCC",
    "상태 미충족 (DB/fixture 없음)":      "FCE5CD",
    "엔드포인트 버그 (Validation 미수행)": "FFE5E5",
    "엔드포인트 버그 (도메인 검증 미수행)": "FFE5E5",
    "예상된 실패":                        "EAF7EA",
    "Probe Only":                        "FFF2CC",
    "TC 실패 (단언 오류)":               "FFF2CC",
    "알 수 없음":                         "EFEFEF",
}

def classify_failure_cause_from_item(item: dict[str, Any]) -> str:
    outcome = str(item.get("outcome", "")).lower()
    if outcome == "passed":
        return "PASS"

    if item.get("server_crashed"):
        return "서버 Crash (5xx)"

    exc_type = str(item.get("exception_type") or "")
    if "Connection" in exc_type or "Connect" in exc_type:
        return "서버 미응답"

    expected_result_type = str(item.get("expected_result_type") or "")
    axis = str(item.get("axis") or "")
    reason_code = str(item.get("reason_code") or "")
    response_success = item.get("response_success")
    response_error_code = item.get("response_error_code")

    # probe_only는 crash만 아니면 의미상 탐색 성공인데,
    # pytest outcome이 failed면 단언 또는 분류 문제로 본다.
    if expected_result_type == "probe_only":
        return "Probe Only"

    # expected_pass인데 상태/fixture가 없어 실패
    if expected_result_type == "expected_pass" and reason_code == "precondition_not_met":
        return "상태 미충족 (DB/fixture 없음)"

    # expected_fail인데 서버가 success=true를 반환
    if expected_result_type == "expected_fail" and response_success is True:
        if axis == "schema":
            return "엔드포인트 버그 (Validation 미수행)"
        return "엔드포인트 버그 (도메인 검증 미수행)"

    # expected_fail인데 실제로 fail 응답을 잘 돌려준 경우는 원래 PASS여야 하므로
    # 여기까지 왔다는 건 보통 assertion/분류 문제
    if expected_result_type == "expected_fail":
        return "TC 실패 (단언 오류)"

    return "알 수 없음"

# Axis 한글 표시
_AXIS_LABEL: dict[str, str] = {
    "schema":  "schema (구조 검증)",
    "domain":  "domain (값 의미 검증)",
    "state":   "state (상태 검증)",
    "runtime": "runtime (안정성 검증)",
}

_METHOD_COLORS = {
    "GET": "D9EAD3",
    "POST": "FCE5CD",
    "PUT": "FFF2CC",
    "DELETE": "F4CCCC",
    "PATCH": "EAD1DC",
}


class ExcelReportBuilder:
    def __init__(self, output_path: str | Path):
        self.output_path = Path(output_path)

    def build(
        self,
        runner_summary: dict[str, Any],
        pytest_json_path: str | Path | None = None,
        source_file: str = "",
        base_url: str = "",
        endpoints: list[dict] | None = None,
        allure_results_dir: str | Path | None = None,
    ) -> Path:
        normalized_tests = self._load_test_results(
            pytest_json_path=pytest_json_path,
            allure_results_dir=allure_results_dir,
        )

        wb = Workbook()

        ws1 = wb.active
        ws1.title = "Summary"
        self._build_summary(ws1, runner_summary, source_file, base_url)

        ws2 = wb.create_sheet("API List")
        self._build_api_list(ws2, endpoints or [])

        ws3 = wb.create_sheet("TC Table")
        self._build_tc_table(ws3, normalized_tests, base_url)

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(self.output_path)
        return self.output_path

    # ─── Sheet 1: Summary ─────────────────────────────────────────────────────

    def _build_summary(
        self,
        ws,
        summary: dict[str, Any],
        source_file: str,
        base_url: str,
    ) -> None:
        self._title_banner(ws, "A1:B1", "AutoTC — Test Execution Summary")
        ws.row_dimensions[1].height = 28
        ws.column_dimensions["A"].width = 22
        ws.column_dimensions["B"].width = 55

        rows: list[tuple[str, Any]] = [
            ("Generated At", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ("Source File", source_file),
            ("Base URL", base_url),
            ("", ""),
            ("Total Tests", summary.get("total", 0)),
            ("Passed", summary.get("passed", 0)),
            ("Failed", summary.get("failed", 0)),
            ("Error", summary.get("error", 0)),
            ("Duration (s)", summary.get("duration_seconds", "")),
            ("Return Code", summary.get("return_code", summary.get("returncode", ""))),
        ]

        for i, (label, value) in enumerate(rows, start=2):
            ws.cell(row=i, column=1, value=label).font = Font(bold=bool(label))
            cell = ws.cell(row=i, column=2, value=value)
            if label == "Passed" and isinstance(value, int) and value > 0:
                cell.fill = PatternFill(fill_type="solid", fgColor=_GREEN_BG)
                cell.font = Font(bold=True)
            elif label == "Failed" and isinstance(value, int) and value > 0:
                cell.fill = PatternFill(fill_type="solid", fgColor=_RED_BG)
                cell.font = Font(bold=True)

    # ─── Sheet 2: API List ────────────────────────────────────────────────────

    def _build_api_list(self, ws, endpoints: list[dict]) -> None:
        self._title_banner(ws, "A1:I1", "API Endpoint List")
        ws.row_dimensions[1].height = 24

        headers = [
            "#", "Method", "Path", "Operation ID",
            "Description",
            "Required Params",
            "Optional Params",
            "Request Body Fields (* = required)",
            "Success Code(s)",
        ]
        self._header_row(ws, 2, headers)

        col_widths = [5, 10, 35, 30, 45, 30, 28, 38, 15]
        for i, w in enumerate(col_widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

        for idx, ep in enumerate(endpoints, start=1):
            method = ep.get("method", "").upper()
            path = ep.get("path", "")
            op_id = ep.get("operation_id", "")
            desc = ep.get("description") or ep.get("summary") or ""

            params = ep.get("parameters", [])
            req_params = [
                f"{p['name']} ({(p.get('schema') or {}).get('type', '?')})"
                for p in params if p.get("required")
            ]
            opt_params = [
                f"{p['name']} ({(p.get('schema') or {}).get('type', '?')})"
                for p in params if not p.get("required")
            ]

            body_schema = self._get_request_body_schema(ep.get("request_body"))
            body_req_set = set(body_schema.get("required", []))
            body_fields = [
                f"{'*' if f in body_req_set else ' '} {f} ({(s or {}).get('type', '?')})"
                for f, s in (body_schema.get("properties") or {}).items()
            ]

            responses = ep.get("responses", {})
            success_codes = [str(k) for k in responses if str(k).startswith("2")]

            row_vals = [
                idx, method, path, op_id, desc,
                "\n".join(req_params) or "—",
                "\n".join(opt_params) or "—",
                "\n".join(body_fields) or "—",
                ", ".join(success_codes) or "200",
            ]

            r = idx + 2
            bg = _METHOD_COLORS.get(method, "FFFFFF")
            for col, val in enumerate(row_vals, start=1):
                cell = ws.cell(row=r, column=col, value=val)
                cell.alignment = Alignment(wrap_text=True, vertical="top")
                if col == 2:
                    cell.fill = PatternFill(fill_type="solid", fgColor=bg)
                    cell.font = Font(bold=True)

            line_count = max(len(req_params), len(opt_params), len(body_fields), 1)
            ws.row_dimensions[r].height = max(18, 15 * line_count)
            ws.freeze_panes = "A3"
    
    def _extract_expected_success(self, expected_display: str) -> str:
        text = str(expected_display).lower()

        if "success=true" in text:
            return "true"
        if "success=false" in text:
            return "false"

        return ""
    

    # ─── Sheet 3: TC Table ────────────────────────────────────────────────────
    
    def _build_tc_table(self, ws, tests: list[dict[str, Any]], base_url: str) -> None:
        self._title_banner(ws, "A1:Y1", "TC (Test Case) Table")
        ws.row_dimensions[1].height = 24

        headers = [
            "#",                          # 1
            "Test Environment",           # 2
            "Target Type",                # 3
            "HTTP Method / Function",     # 4
            "Path / Module",              # 5
            "Rule Type",                  # 6
            "Rule Subtype",               # 7
            "Endpoint Profile",           # 8
            "Expected Result Type",       # 9
            "Semantic Tag",               # 10
            "Policy",                     # 11
            "Axis",                       # 12
            "Reason Code",                # 13
            "Target Field",               # 14
            "Test Condition",             # 15
            "Request Query",              # 16
            "Request Headers",            # 17
            "Request Body / Arguments",   # 18
            "Expected",                   # 19
            "Actual",                     # 20
            "Response Msg",               # 21
            "Data Error Code",            # 22
            "Match Score",                # 23
            "Match Status",               # 24
            "Exception Type",             # 25
            "Exception Message",          # 26
            "Server Crash",               # 27
            "Server Log Tail",            # 28
            "Outcome",                    # 29
            "Failure Cause",              # 30
            "Duration (s)",               # 31
            "Error Detail",               # 32
            "Failure Detail (pytest)",    # 33
        ]
        self._header_row(ws, 2, headers)

        col_widths = [
            5, 26, 12, 22, 34,   # 1-5
            16, 18, 18, 18, 16, 12,  # 6-11
            22, 22, 18, 42,      # 12-15
            28, 28, 70,          # 16-18
            28, 28, 38,          # 19-21
            14, 14, 14, 18,      # 22-25
            18, 42, 12, 42,      # 26-29
            30, 12, 42, 55,      # 30-33
        ]
        for i, w in enumerate(col_widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

        src_cache: dict[str, str] = {}

        for idx, item in enumerate(tests, start=1):
            nodeid = item.get("nodeid", "")
            outcome = str(item.get("outcome", "unknown")).lower()
            duration = round(float(item.get("duration", 0) or 0), 3)
            longrepr = str(item.get("longrepr") or "")

            info = self._parse_nodeid(nodeid, src_cache)

            _raw_status = item.get("actual_status")
            if _raw_status is not None and _raw_status != "":
                actual_status = str(_raw_status)
            else:
                actual_status = self._extract_actual_status(longrepr)

            if not actual_status and outcome == "passed":
                actual_status = self._first_status_code(
                    item.get("expected_status_display") or info.get("expected_status", "")
                )

            target_type = item.get("target_type") or self._infer_target_type(item, info)
            method_or_function = item.get("request_method") or item.get("function") or info["method"]
            path_or_module = item.get("request_path") or info["path"] or item.get("request_url") or ""

            request_query = self._format_value(item.get("request_query", {}))
            request_headers = self._format_value(item.get("request_headers", {}))
            request_body_or_args = self._pick_request_payload(item)

            expected_display = item.get("expected_status_display") or self._build_expected_display(item, info)
            actual_display = self._build_actual_display(item, actual_status)

            rule_type = item.get("rule_type") or info["rule_type"]
            rule_subtype = item.get("rule_subtype", "")
            endpoint_profile = item.get("endpoint_profile", "")
            expected_result_type = item.get("expected_result_type", "")
            semantic_tag = item.get("semantic_tag", "")
            policy = item.get("policy", "")

            axis = item.get("axis", "")
            axis_display = _AXIS_LABEL.get(axis, axis)

            target_field = item.get("target_param", "")
            condition = item.get("condition") or info["condition"]

            response_msg = item.get("response_msg", "") or ""
            failure_cause = classify_failure_cause_from_item(item)

            row_vals = [
                idx,                                          # 1
                base_url,                                     # 2
                target_type,                                  # 3
                method_or_function,                           # 4
                path_or_module,                               # 5
                rule_type,                                    # 6
                rule_subtype,                                 # 7
                endpoint_profile,                             # 8
                expected_result_type,                         # 9
                semantic_tag,                                 # 10
                policy,                                       # 11
                axis_display,                                 # 12
                item.get("reason_code", ""),                  # 13
                target_field,                                 # 14
                condition,                                    # 15
                request_query,                                # 16
                request_headers,                              # 17
                request_body_or_args,                         # 18
                expected_display,                             # 19
                actual_display,                               # 20
                response_msg,                                 # 21
                item.get("response_data_error_code", ""),     # 22
                item.get("response_data_match_score", ""),    # 23
                item.get("response_data_status", ""),         # 24
                item.get("exception_type", ""),               # 25
                item.get("exception_message", ""),            # 26
                "Y" if item.get("server_crashed") else "",   # 27
                (item.get("server_log_tail") or "")[:2000],  # 28
                outcome.upper(),                              # 29
                failure_cause,                                # 30
                duration,                                     # 31
                item.get("error_detail", ""),                 # 32
                longrepr[:2000] if outcome in {"failed", "broken"} else "",  # 33
            ]

            r = idx + 2
            for col, val in enumerate(row_vals, start=1):
                cell = ws.cell(row=r, column=col, value=val)
                cell.alignment = Alignment(wrap_text=True, vertical="top")

            # 행 배경색: Failure Cause 기반으로 결정 (기존 response_success 기반보다 명확)
            bg = _FC_COLORS.get(failure_cause, _YELLOW_BG)

            for col in range(1, len(headers) + 1):
                ws.cell(row=r, column=col).fill = PatternFill(fill_type="solid", fgColor=bg)

            fc_cell = ws.cell(row=r, column=30)
            if failure_cause not in {"PASS", "알 수 없음"}:
                fc_cell.font = Font(bold=True)

            
            max_len = max(
                len(str(v)) if v else 0
                for v in [
                    request_query,
                    request_body_or_args,
                    response_msg,
                    item.get("exception_message", ""),
                    longrepr
                ]
            )
            # 길이에 따라 높이 증가
            if max_len > 300:
                height = 120
            elif max_len > 150:
                height = 80
            elif max_len > 80:
                height = 50
            else:
                height = 38
            ws.row_dimensions[r].height = height
            
            ws.freeze_panes = "A3"

    # ─── test result loaders ──────────────────────────────────────────────────

    def _load_test_results(
        self,
        pytest_json_path: str | Path | None,
        allure_results_dir: str | Path | None,
    ) -> list[dict[str, Any]]:
        # 1) pytest JSON report 정규화
        tests: list[dict[str, Any]] = []
        if pytest_json_path:
            p = Path(pytest_json_path)
            if p.exists() and p.is_file():
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    if self._is_pytest_json_report(raw):
                        tests = self._normalize_pytest_json_report(raw)
                except Exception:
                    pass

        if not tests and allure_results_dir:
            d = Path(allure_results_dir)
            if d.exists() and d.is_dir():
                tests = self._normalize_allure_results_dir(d)

        # 2) test_diag.jsonl merge (있으면 diag 필드로 덮어쓰기)
        diag_map = self._load_diag_jsonl(pytest_json_path)
        if diag_map:
            for t in tests:
                diag = diag_map.get(t.get("nodeid", ""))
                if diag:
                    self._apply_diag(t, diag)

        return tests

    @staticmethod
    def _load_diag_jsonl(pytest_json_path: str | Path | None) -> dict[str, dict]:
        """test_diag.jsonl 을 읽어 {nodeid: diag} 매핑을 반환한다."""
        candidates: list[Path] = []
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
        """diag 필드를 item 에 적용한다 (기존 값을 diag 우선으로 덮어씀)."""
        item["axis"]               = diag.get("axis", "")
        item["reason_code"]        = diag.get("reason_code", "")
        item["target_param"]       = diag.get("target_field", item.get("target_param", ""))
        item["condition"]          = diag.get("test_condition", item.get("condition", ""))

        # expected 표시
        exp_http = diag.get("expected_http") or ""
        exp_app  = diag.get("expected_app")  or ""
        parts = [p for p in [exp_http, exp_app] if p]
        if parts:
            item["expected_status_display"] = " / ".join(parts)

        # actual status
        if diag.get("actual_status") is not None:
            item["actual_status"] = str(diag["actual_status"])

        # ── request 데이터 (build_diag가 resp.request 에서 추출) ──
        req_body = diag.get("request_body")
        if req_body is not None:
            item["request_body"] = req_body
        req_query = diag.get("request_query")
        if req_query is not None:
            item["request_query"] = req_query
        req_headers = diag.get("request_headers")
        if req_headers is not None:
            item["request_headers"] = req_headers

        # response snippet
        item["response_text"]      = diag.get("response_snippet", item.get("response_text", ""))

        # exception
        item["exception_type"]     = diag.get("exception_type", "")
        item["exception_message"]  = diag.get("exception_message", "")

        # server crash
        item["server_crashed"]     = bool(diag.get("server_crash", False))

        # error detail (새 컬럼)
        item["error_detail"]       = diag.get("error_detail", "")

        # QFE response fields
        item["response_success"]   = diag.get("response_success")
        item["response_error_code"]= diag.get("response_error_code")
        item["response_msg"]       = diag.get("response_msg")
        item["response_data"] = diag.get("response_data")
        item["response_data_error_code"] = diag.get("response_data_error_code")
        item["response_data_match_score"] = diag.get("response_data_match_score")
        item["response_data_status"] = diag.get("response_data_status")

    @staticmethod
    def _is_pytest_json_report(raw):
        return isinstance(raw, dict) and "tests" in raw

    def _normalize_pytest_json_report(self, raw):
        out = []
        for t in raw.get("tests", []):
            call = t.get("call") or {}
            meta = self._extract_tc_meta(t)

            out.append({
                "nodeid": t.get("nodeid", ""),
                "outcome": t.get("outcome", "unknown"),
                "duration": call.get("duration", t.get("duration", 0)),
                "longrepr": str(
                    call.get("longrepr")
                    or t.get("longrepr")
                    or ""
                ),

                # tc_meta 기반 핵심 필드
                "target_type": self._detect_target_type_from_meta(meta),
                "rule_type": meta.get("rule_type", ""),
                "rule_subtype": meta.get("rule_subtype", ""),
                "endpoint_profile": meta.get("endpoint_profile", ""),
                "semantic_tag": meta.get("semantic_tag", ""),
                "policy": meta.get("policy", ""),
                "expected_result_type": meta.get("expected_result_type", ""),

                "target_param": meta.get("target_param", ""),
                "condition": meta.get("condition", ""),
                "request_method": meta.get("request_method", ""),
                "request_url": meta.get("request_url", ""),
                "request_path": meta.get("request_path", ""),
                "request_path_params": meta.get("request_path_params", {}),
                "request_query": meta.get("request_query", {}),
                "request_headers": meta.get("request_headers", {}),
                "request_body": meta.get("request_body"),
                "function": meta.get("function", ""),
                "arguments": meta.get("arguments"),
                "arguments_repr": meta.get("arguments_repr", ""),
                "expected_status": meta.get("expected_status", []),
                "expected_status_display": self._coerce_expected_display(meta),
                "actual_status": meta.get("actual_status", ""),
                "response_text": meta.get("response_text", ""),
                "actual_result_repr": meta.get("actual_result_repr", ""),
                "actual_outcome": meta.get("actual_outcome", ""),
                "exception_type": meta.get("exception_type", ""),
                "exception_message": meta.get("exception_message", ""),
                "server_crashed": meta.get("server_crashed", False),
                "server_log_tail": meta.get("server_log_tail", ""),

                # diag merge 대상
                "axis": "",
                "reason_code": "",
                "error_detail": "",
                "response_success": None,
                "response_error_code": None,
                "response_msg": None,
                "response_data": None,
                "response_data_error_code": None,
                "response_data_match_score": None,
                "response_data_status": None,
            })
        return out

    def _normalize_allure_results_dir(self, d: Path) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in sorted(d.glob("*-result.json")):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue

            full_name = raw.get("fullName") or raw.get("name") or p.stem
            status = str(raw.get("status", "unknown")).lower()

            start = raw.get("start")
            stop = raw.get("stop")
            duration = 0.0
            if isinstance(start, (int, float)) and isinstance(stop, (int, float)) and stop >= start:
                duration = round((stop - start) / 1000.0, 3)

            details = raw.get("statusDetails", {}) or {}
            longrepr = details.get("message") or details.get("trace") or ""

            out.append({
                "nodeid": full_name,
                "outcome": status,
                "duration": duration,
                "longrepr": str(longrepr),
                "target_type": "",
                "rule_type": "",
                "target_param": "",
                "condition": "",
                "request_method": "",
                "request_url": "",
                "request_path": "",
                "request_path_params": {},
                "request_query": {},
                "request_headers": {},
                "request_body": None,
                "function": "",
                "arguments": None,
                "arguments_repr": "",
                "expected_status": [],
                "expected_status_display": "",
                "actual_status": "",
                "response_text": "",
                "actual_result_repr": "",
                "actual_outcome": "",
                "exception_type": "",
                "exception_message": "",
                "server_crashed": False,
                "server_log_tail": "",
                # ── diag 필드 (build_diag + _apply_diag 로 채워짐) ──────────
                "axis": "",
                "reason_code": "",
                "error_detail": "",
                "response_success": None,
                "response_error_code": None,
                "response_msg": None,
                "response_data": None,
                "response_data_error_code": None,
                "response_data_match_score": None,
                "response_data_status": None,
            })
        return out
    @staticmethod
    def _first_status_code(text: str) -> str:
        parts = str(text or "").strip().split()
        if not parts:
            return ""
        return parts[0] if parts[0].isdigit() else ""
    
    # ─── metadata extraction helpers ──────────────────────────────────────────

    @staticmethod
    def _extract_tc_meta(test_obj: dict[str, Any]) -> dict[str, Any]:
        meta: dict[str, Any] = {}

        # 1) metadata.tc_meta 우선
        md = test_obj.get("metadata") or {}
        if isinstance(md, dict):
            tc_meta = md.get("tc_meta")
            if isinstance(tc_meta, dict):
                meta.update(tc_meta)

        # 2) user_properties fallback
        for up in test_obj.get("user_properties", []) or []:
            if isinstance(up, (list, tuple)) and len(up) == 2 and up[0] == "tc_meta":
                if isinstance(up[1], dict):
                    meta.update(up[1])

        return meta

    @staticmethod
    def _detect_target_type_from_meta(meta: dict[str, Any]) -> str:
        if meta.get("function"):
            return "python"
        if meta.get("request_method") or meta.get("request_body") is not None:
            return "api"
        return ""

    @staticmethod
    def _coerce_expected_display(meta: dict[str, Any]) -> str:
        # 1순위: 직접 기록된 human-readable 문자열
        if meta.get("expected_status_display"):
            return str(meta["expected_status_display"])
        # 2순위: 상태코드 리스트
        if meta.get("expected_status"):
            return ", ".join(map(str, meta["expected_status"]))
        if meta.get("expected_exception_types"):
            return "raises " + ", ".join(map(str, meta["expected_exception_types"]))
        if meta.get("expected"):
            return str(meta["expected"])
        return ""
    

    def _build_expected_display(self, item: dict[str, Any], info: dict[str, str]) -> str:
        if item.get("expected_status_display"):
            return str(item["expected_status_display"])
        if item.get("expected_status"):
            return ", ".join(map(str, item["expected_status"]))
        return info.get("expected_status", "")
    
    def _build_actual_display(self, item: dict[str, Any], actual_status: str) -> str:
        head = str(actual_status) if actual_status else ""

        tail_parts: list[str] = []

        rs = item.get("response_success")
        if rs is not None:
            tail_parts.append(f"success={'true' if rs else 'false'}")

        ec = item.get("response_error_code")
        if ec not in [None, ""]:
            tail_parts.append(f"error_code={ec}")

        tail = ", ".join(tail_parts)

        if head and tail:
            return f"{head} / {tail}"
        return head or tail

    def _pick_request_payload(self, item: dict[str, Any]) -> str:
        if item.get("request_body") is not None:
            return self._format_value(item.get("request_body"))
        if item.get("arguments") is not None:
            return self._format_value(item.get("arguments"))
        if item.get("arguments_repr"):
            return str(item.get("arguments_repr"))
        return ""

    def _pick_response_or_result(self, item: dict[str, Any]) -> str:
        if item.get("response_text"):
            return str(item["response_text"])[:2000]
        if item.get("actual_result_repr"):
            return str(item["actual_result_repr"])[:2000]
        if item.get("actual_outcome"):
            return str(item["actual_outcome"])
        return ""
    

    @staticmethod
    def _infer_target_type(item: dict[str, Any], info: dict[str, str]) -> str:
        if item.get("function"):
            return "python"
        if item.get("request_method") or info.get("method"):
            return "api"
        return ""

    @staticmethod
    def _format_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, indent=2)
        except Exception:
            return repr(value)

    # ─── request_body helpers ────────────────────────────────────────────────

    @staticmethod
    def _get_primary_media_type(req_body: dict | None) -> str | None:
        if not req_body:
            return None

        if "schema" in req_body:
            return "__legacy__"

        content = req_body.get("content", {})
        if not content:
            return None

        if "application/json" in content:
            return "application/json"
        if "multipart/form-data" in content:
            return "multipart/form-data"
        if "application/x-www-form-urlencoded" in content:
            return "application/x-www-form-urlencoded"

        return next(iter(content.keys()), None)

    def _get_request_body_schema(self, req_body: dict | None) -> dict[str, Any]:
        if not req_body:
            return {}

        media_type = self._get_primary_media_type(req_body)
        if not media_type:
            return {}

        if media_type == "__legacy__":
            return req_body.get("schema", {}) or {}

        return (req_body.get("content", {}).get(media_type, {}) or {}).get("schema", {}) or {}

    # ─── generic helpers ──────────────────────────────────────────────────────

    def _title_banner(self, ws, cell_range: str, text: str) -> None:
        ws.merge_cells(cell_range)
        first_cell = ws[cell_range.split(":")[0]]
        first_cell.value = text
        first_cell.font = Font(bold=True, size=13, color="FFFFFF")
        first_cell.fill = PatternFill(fill_type="solid", fgColor=_BLUE_TITLE)
        first_cell.alignment = Alignment(horizontal="center", vertical="center")

    def _header_row(self, ws, row: int, headers: list[str]) -> None:
        for col, h in enumerate(headers, start=1):
            cell = ws.cell(row=row, column=col, value=h)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill(fill_type="solid", fgColor=_BLUE_DARK)
            cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[row].height = 18

    def _parse_nodeid(self, nodeid: str, src_cache: dict) -> dict:
        result: dict[str, str] = {
            "method": "",
            "path": "",
            "rule_type": "",
            "condition": "",
            "test_data": "",
            "expected_status": "",
        }
        if not nodeid:
            return result

        file_part = ""
        func_name = nodeid

        if "::" in nodeid:
            file_part, func_name = nodeid.split("::", 1)

        method, path = self._read_file_header(file_part, src_cache) if file_part else ("", "")
        result["method"] = method
        result["path"] = path

        if not method:
            m = re.search(r"test_(get|post|put|delete|patch)__", func_name, re.IGNORECASE)
            if m:
                result["method"] = m.group(1).upper()

        fn = func_name.lower()

        if fn.endswith("_positive"):
            result["rule_type"] = "positive"
            result["condition"] = "Happy path — all required fields present"
            result["test_data"] = "Representative valid values for all params"
            result["expected_status"] = "2xx"

        elif m2 := re.search(r"_missing_body_(.+)$", fn):
            field = m2.group(1)
            result["rule_type"] = "missing_required"
            result["condition"] = f"Required body field omitted: {field}"
            result["test_data"] = f"Request body without field '{field}'"
            result["expected_status"] = "400 / 422"

        elif fn.endswith("_missing_body"):
            result["rule_type"] = "missing_required"
            result["condition"] = "Required request body omitted"
            result["test_data"] = "No request body"
            result["expected_status"] = "400 / 415 / 422"

        elif m2 := re.search(r"_missing_(.+)$", fn):
            param = m2.group(1)
            result["rule_type"] = "missing_required"
            result["condition"] = f"Required param omitted: {param}"
            result["test_data"] = f"Request without param '{param}'"
            result["expected_status"] = "400 / 422"

        elif m2 := re.search(r"_wrong_type_body_(.+)$", fn):
            field = m2.group(1)
            result["rule_type"] = "wrong_type"
            result["condition"] = f"Body field sent with wrong type: {field}"
            result["test_data"] = f"body.{field} = wrong type"
            result["expected_status"] = "400 / 422"

        elif m2 := re.search(r"_wrong_type_(.+)$", fn):
            param = m2.group(1)
            result["rule_type"] = "wrong_type"
            result["condition"] = f"Param sent with wrong type: {param}"
            result["test_data"] = f"{param} = wrong type"
            result["expected_status"] = "400 / 422"

        elif m2 := re.search(r"_boundary_body_(.+?)_(.+)$", fn):
            field, probe = m2.group(1), m2.group(2)
            result["rule_type"] = "boundary"
            result["condition"] = f"Body field boundary probe: {field} ({probe})"
            result["test_data"] = f"body.{field} = {probe}"
            result["expected_status"] = "< 500"

        elif m2 := re.search(r"_boundary_(.+?)_(.+)$", fn):
            param, probe = m2.group(1), m2.group(2)
            result["rule_type"] = "boundary"
            result["condition"] = f"Param boundary probe: {param} ({probe})"
            result["test_data"] = f"{param} = {probe}"
            result["expected_status"] = "< 500"

        elif m2 := re.search(r"_invalid_enum_body_(.+)$", fn):
            field = m2.group(1)
            result["rule_type"] = "invalid_enum"
            result["condition"] = f"Body field outside allowed enum: {field}"
            result["test_data"] = f"body.{field} = '__INVALID_ENUM_VALUE__'"
            result["expected_status"] = "400 / 422"

        elif m2 := re.search(r"_invalid_enum_(.+)$", fn):
            param = m2.group(1)
            result["rule_type"] = "invalid_enum"
            result["condition"] = f"Param value outside allowed enum: {param}"
            result["test_data"] = f"{param} = '__INVALID_ENUM_VALUE__'"
            result["expected_status"] = "400 / 422"

        elif m2 := re.search(r"_semantic_(.+?)_(.+)$", fn):
            field, probe = m2.group(1), m2.group(2)
            result["rule_type"] = "semantic_probe"
            result["condition"] = f"Semantic probe: {field} ({probe})"
            result["test_data"] = f"{field} = {probe}"
            result["expected_status"] = "< 500"

        return result

    def _read_file_header(self, file_path: str, cache: dict) -> tuple[str, str]:
        if not file_path:
            return "", ""

        if file_path not in cache:
            p = Path(file_path)
            cache[file_path] = p.read_text(encoding="utf-8") if p.exists() else ""

        src = cache[file_path]
        m = re.search(
            r"^#\s*(GET|POST|PUT|DELETE|PATCH)\s+(\S+)",
            src,
            re.MULTILINE | re.IGNORECASE,
        )
        if m:
            return m.group(1).upper(), m.group(2).strip()
        return "", ""

    def _extract_actual_status(self, longrepr: str) -> str:
        patterns = [
            r"\bgot\s+(\d{3})\b",
            r"\bstatus(?:_code)?\D+(\d{3})\b",
            r"\bHTTP[/ ]1\.[01]\"\s+(\d{3})\b",
        ]
        for pat in patterns:
            m = re.search(pat, longrepr, re.IGNORECASE)
            if m:
                return m.group(1)
        return ""
    
    