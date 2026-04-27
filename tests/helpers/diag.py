"""
tests/helpers/diag.py - structured diagnostic data builder

axis values:
    schema   - type/required/format violations
    domain   - range/enum/base64/image relation violations
    state    - registered user/template/DB state dependency
    runtime  - server crash/connection refused/timeout

reason_code values by axis:
    schema  : missing_required | type_mismatch | invalid_json_shape
              path_param_invalid | query_param_invalid | response_format_invalid
    domain  : range_violation | enum_violation | invalid_base64
              invalid_image_relation | invalid_template_relation
              constraint_missing_in_generator
    state   : user_missing | template_missing | seed_not_prepared
              db_state_invalid | precondition_not_met
    runtime : server_crash | connection_refused | timeout
              http_5xx | unexpected_exception
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


# --- server log tail helper --------------------------------------------------

def _read_server_log_tail(n_lines: int = 60) -> str:
    log_path = os.environ.get("SERVER_LOG_FILE", "")
    if not log_path:
        return ""
    try:
        lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n_lines:])
    except Exception:
        return ""


# --- request info extractor --------------------------------------------------

def _extract_request_info(resp) -> tuple[Any, dict, dict]:
    import urllib.parse as _up

    req = getattr(resp, "request", None)
    if req is None:
        return None, {}, {}

    req_body: Any = None
    try:
        raw_body = getattr(req, "body", None)
        if isinstance(raw_body, bytes):
            raw_body = raw_body.decode("utf-8", errors="replace")
        if raw_body:
            try:
                req_body = json.loads(raw_body)
            except Exception:
                req_body = raw_body
    except Exception:
        pass

    req_query: dict = {}
    try:
        url = getattr(req, "url", "") or ""
        parsed = _up.urlparse(url)
        req_query = dict(_up.parse_qsl(parsed.query))
    except Exception:
        pass

    req_headers: dict = {}
    try:
        raw_headers = dict(getattr(req, "headers", {}) or {})
        _keep = {"content-type", "accept", "authorization", "x-api-key"}
        req_headers = {k: v for k, v in raw_headers.items() if k.lower() in _keep}
    except Exception:
        pass

    return req_body, req_query, req_headers


def build_diag(
    axis: str,
    reason_code: str,
    target_field: str = "",
    test_condition: str = "",
    expected_http: "str | int | None" = None,
    expected_app: "str | None" = None,
    resp=None,
    body: "dict | None" = None,
    exc: "Exception | None" = None,
    server_crash: bool = False,
    server_log_tail: "str | None" = None,
    error_detail: "str | None" = None,
    # http_status mode extra fields
    expected_error_codes: "list[int] | None" = None,
    expected_error_family: str = "",
) -> "dict[str, Any]":
    """Build a structured diag dictionary."""

    if body is None:
        body = {}

    snippet: "str | None" = None
    if body:
        try:
            snippet = json.dumps(body, ensure_ascii=False)[:500]
        except Exception:
            snippet = str(body)[:500]
    elif resp is not None:
        try:
            snippet = resp.text[:500]
        except Exception:
            pass

    req_body, req_query, req_headers = _extract_request_info(resp)

    if server_log_tail is None and (exc is not None or server_crash):
        server_log_tail = _read_server_log_tail()

    return {
        "axis":               axis,
        "reason_code":        reason_code,
        "target_field":       target_field,
        "test_condition":     test_condition,

        "expected_http":      expected_http,
        "expected_app":       expected_app,
        "actual_status":      getattr(resp, "status_code", None),

        "request_body":       req_body,
        "request_query":      req_query,
        "request_headers":    req_headers,

        "response_snippet":   snippet,
        "response_success":   body.get("success")    if isinstance(body, dict) else None,
        "response_error_code":body.get("error_code") if isinstance(body, dict) else None,
        "response_msg":       body.get("msg")        if isinstance(body, dict) else None,
        "response_data":      body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else None,
        "response_data_error_code": (
            body.get("data", {}).get("error_code")
            if isinstance(body, dict) and isinstance(body.get("data"), dict)
            else None
        ),
        "response_data_match_score": (
            body.get("data", {}).get("match_score")
            if isinstance(body, dict) and isinstance(body.get("data"), dict)
            else None
        ),
        "response_data_status": (
            body.get("data", {}).get("status")
            if isinstance(body, dict) and isinstance(body.get("data"), dict)
            else None
        ),

        "exception_type":     type(exc).__name__ if exc else None,
        "exception_message":  str(exc)            if exc else None,

        "server_crash":       server_crash,
        "server_log_tail":    server_log_tail,

        "error_detail":       error_detail or f"{axis}.{reason_code}",

        "response_data_verified": (
            body.get("data", {}).get("verified")
            if isinstance(body, dict) and isinstance(body.get("data"), dict)
            else None
        ),

        # http_status mode extra fields
        "expected_error_codes":   expected_error_codes,
        "expected_error_family":  expected_error_family,
    }


def build_probe_diag(
    probe_endpoint: str,
    target_field: str,
    probe_label: str,
    probe_input: Any,
    severity: str,
    classification: str,
    expected_behavior: str,
    resp=None,
    body=None,
    exc=None,
    server_crash=False,
    server_log_tail=None,
    error_detail=None,
):
    diag = build_diag(
        axis="runtime",
        reason_code="probe_runtime",
        target_field=target_field,
        test_condition=f"Crash probe: {probe_label}",
        expected_http="<500",
        expected_app=expected_behavior,
        resp=resp,
        body=body,
        exc=exc,
        server_crash=server_crash,
        server_log_tail=server_log_tail,
        error_detail=error_detail or f"runtime.probe.{probe_label}",
    )
    diag.update({
        "probe_endpoint": probe_endpoint,
        "probe_label": probe_label,
        "probe_input": probe_input,
        "probe_severity": severity,
        "probe_classification": classification,
    })
    return diag


def attach_diag(request, diag: dict) -> None:
    request.node.user_properties.append(("diag", diag))


def attach_probe_diag(request, diag: "dict[str, Any]") -> None:
    request.node.user_properties.append(("probe_diag", diag))


def attach_probe_meta(
    request,
    probe_endpoint: str,
    target_field: str,
    probe_label: str,
    probe_input: Any,
    severity: str,
) -> None:
    request.node.user_properties.append((
        "probe_meta",
        {
            "expected_result_type": "probe_only",
            "probe_endpoint": probe_endpoint,
            "target_field": target_field,
            "probe_label": probe_label,
            "probe_input": probe_input,
            "probe_severity": severity,
        },
    ))


# --- legacy QFE failure cause classifier -------------------------------------

def classify_failure_cause(
    outcome: str,
    axis: str,
    reason_code: str,
    response_success,
    response_error_code,
    server_crash: bool = False,
) -> str:
    outcome = (outcome or "").lower()

    if outcome == "passed":
        return "PASS"

    if server_crash:
        return "SERVER_CRASH_5XX"
    if reason_code == "connection_refused":
        return "SERVER_NO_RESPONSE"
    if reason_code in {"timeout", "http_5xx"}:
        return "SERVER_CRASH_5XX"

    if axis == "state":
        if response_success is False:
            return "STATE_PRECONDITION_NOT_MET"
        if response_success is True:
            return "TC_ASSERTION_ERROR"

    if axis == "schema":
        if response_success is True:
            return "ENDPOINT_BUG_VALIDATION_NOT_PERFORMED"
        if response_success is False:
            return "UNEXPECTED_FAILURE"

    if axis == "domain":
        if response_success is True:
            return "ENDPOINT_BUG_DOMAIN_VALIDATION_SKIPPED"
        if response_success is False:
            return "UNEXPECTED_FAILURE"

    if axis == "runtime":
        return "SERVER_CRASH_5XX"

    return "UNKNOWN"


# --- HTTP status primary axis - 3-level failure classification ----------------

def classify_result(
    outcome: str,
    expected_http: "str | int | None",
    expected_error_codes: "list[int] | frozenset[int] | None",
    actual_status: "int | None",
    actual_error_code: "int | None",
    axis: str = "",
    reason_code: str = "",
    server_crash: bool = False,
    migration_flag: str = "",
) -> "dict[str, str]":
    """
    HTTP status primary axis 3-level failure classification.

    Levels:
        Level 0  PASS / PASS_WITH_LEGACY_HTTP
        Level 1  HTTP_STATUS_MISMATCH   - actual HTTP != expected HTTP
        Level 2  ERROR_CODE_MISMATCH    - HTTP matches but error_code set mismatch
        Level 3  BODY_SCHEMA_MISMATCH   - HTTP+error_code match but body structure wrong
        Extra    SERVER_CRASH           - 5xx / server_crash=True
                 CONNECTION_REFUSED     - connection refused / timeout
                 UNKNOWN                - unclassifiable

    Returns dict with keys: level, cause, migration_flag
    """
    outcome = (outcome or "").lower()
    _mf = migration_flag or ""

    # server crash / connection refused
    if server_crash or reason_code in {"http_5xx", "unexpected_exception"}:
        return {
            "level":          "SERVER_CRASH",
            "cause":          "Server Crash (5xx) or unexpected server exception",
            "migration_flag": _mf,
        }
    if reason_code == "connection_refused":
        return {
            "level":          "CONNECTION_REFUSED",
            "cause":          "Server not responding - connection refused / timeout",
            "migration_flag": _mf,
        }

    # PASS (including hybrid migration PASS)
    if outcome == "passed":
        return {
            "level":          "PASS",
            "cause":          "Test passed",
            "migration_flag": _mf,
        }

    # Level 1: HTTP STATUS MISMATCH
    try:
        exp_status_int = int(str(expected_http).strip()) if expected_http is not None else None
    except (ValueError, TypeError):
        exp_status_int = None

    if exp_status_int is not None and actual_status is not None:
        if actual_status != exp_status_int:
            # hybrid migration: expected 4xx/422 but legacy 200 + correct error_code?
            _is_legacy_pass = (
                actual_status == 200
                and exp_status_int in {400, 404, 422}
                and actual_error_code is not None
                and expected_error_codes
                and actual_error_code in frozenset(expected_error_codes)
            )
            if _is_legacy_pass:
                return {
                    "level":          "PASS",
                    "cause":          f"Legacy HTTP 200 but error_code({actual_error_code}) matches - migration tolerated",
                    "migration_flag": "PASS_WITH_LEGACY_HTTP",
                }
            return {
                "level":          "HTTP_STATUS_MISMATCH",
                "cause":          (
                    f"HTTP status mismatch - expected {exp_status_int}, actual {actual_status}"
                    + (f" (axis={axis}, reason={reason_code})" if axis else "")
                ),
                "migration_flag": _mf,
            }

    # Level 2: ERROR_CODE MISMATCH
    if expected_error_codes and actual_error_code is not None:
        _ec_set = frozenset(expected_error_codes)
        if actual_error_code not in _ec_set:
            return {
                "level":          "ERROR_CODE_MISMATCH",
                "cause":          (
                    f"error_code mismatch - expected {sorted(_ec_set)}, actual {actual_error_code}"
                    + (f" (axis={axis}, reason={reason_code})" if axis else "")
                ),
                "migration_flag": _mf,
            }

    # Level 3: BODY SCHEMA MISMATCH
    # HTTP and error_code match but assertion failed -> body structure issue
    if outcome != "passed":
        return {
            "level":          "BODY_SCHEMA_MISMATCH",
            "cause":          "HTTP/error_code match but body structure or data field assertion failed",
            "migration_flag": _mf,
        }

    return {
        "level":          "UNKNOWN",
        "cause":          "Unclassifiable - insufficient outcome / status info",
        "migration_flag": _mf,
    }
