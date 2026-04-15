"""
tests/helpers/diag.py — 구조화된 진단 데이터 생성/기록 헬퍼

사용법:
    from tests.helpers.diag import build_diag, attach_diag

    diag = build_diag(
        axis="schema",
        reason_code="type_mismatch",
        target_field="user_id",
        test_condition="Body field 'user_id' sent with wrong type",
        expected_http="200",
        expected_app="success=false, error_code<0",
        resp=resp,
        body=body,
        error_detail="schema.type_mismatch.user_id",
    )
    attach_diag(request, diag)

axis 값:
    schema   — 타입·필수값·포맷 위반
    domain   — 범위·enum·base64·이미지 관계 위반
    state    — 등록 사용자·템플릿·DB 상태 의존
    runtime  — 서버 크래시·연결 거부·타임아웃

reason_code 값 (axis별):
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


# ─── 서버 로그 tail 헬퍼 ────────────────────────────────────────────────────────

def _read_server_log_tail(n_lines: int = 60) -> str:
    """
    SERVER_LOG_FILE 환경변수가 설정된 경우 서버 로그 마지막 N줄을 반환한다.
    설정 안 됐거나 파일이 없으면 빈 문자열 반환.
    """
    log_path = os.environ.get("SERVER_LOG_FILE", "")
    if not log_path:
        return ""
    try:
        lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n_lines:])
    except Exception:
        return ""


# ─── 진단 레코드 생성 ──────────────────────────────────────────────────────────

def _extract_request_info(resp) -> tuple[Any, dict, dict]:
    """
    resp.request (PreparedRequest) 에서 실제로 전송된 request 데이터를 추출한다.

    반환:
        (request_body, request_query, request_headers)
        - request_body : dict (JSON 파싱 성공 시) 또는 str / None
        - request_query: {param: value} — URL 쿼리스트링 파싱 결과
        - request_headers: 주요 헤더만 (Content-Type 등)
    """
    import urllib.parse as _up

    req = getattr(resp, "request", None)
    if req is None:
        return None, {}, {}

    # ── request body ──────────────────────────────────────────────
    req_body: Any = None
    try:
        raw_body = getattr(req, "body", None)
        if isinstance(raw_body, bytes):
            raw_body = raw_body.decode("utf-8", errors="replace")
        if raw_body:
            try:
                req_body = json.loads(raw_body)
            except Exception:
                req_body = raw_body  # JSON 파싱 실패 시 문자열 그대로
    except Exception:
        pass

    # ── query params ──────────────────────────────────────────────
    req_query: dict = {}
    try:
        url = getattr(req, "url", "") or ""
        parsed = _up.urlparse(url)
        req_query = dict(_up.parse_qsl(parsed.query))
    except Exception:
        pass

    # ── headers (Content-Type 등 주요 항목만) ─────────────────────
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
    expected_http: str | None = None,
    expected_app: str | None = None,
    resp=None,
    body: dict | None = None,
    exc: Exception | None = None,
    server_crash: bool = False,
    server_log_tail: str | None = None,
    error_detail: str | None = None,
) -> dict[str, Any]:
    """구조화된 diag 딕셔너리를 생성한다."""

    if body is None:
        body = {}

    # ── response 스니펫 (500자 이내) ──────────────────────────────
    snippet: str | None = None
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

    # ── request 데이터 추출 (resp.request / PreparedRequest) ──────
    req_body, req_query, req_headers = _extract_request_info(resp)

    # ── server crash 시 log tail 자동 수집 ────────────────────────
    # exc가 있거나 server_crash=True인데 server_log_tail이 전달 안 됐을 때
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

        # ── request ───────────────────────────────────────────────
        "request_body":       req_body,
        "request_query":      req_query,
        "request_headers":    req_headers,

        # ── response ──────────────────────────────────────────────
        "response_snippet":   snippet,
        "response_success":   body.get("success")    if isinstance(body, dict) else None,
        "response_error_code":body.get("error_code") if isinstance(body, dict) else None,
        "response_msg":       body.get("msg")        if isinstance(body, dict) else None,

        "exception_type":     type(exc).__name__ if exc else None,
        "exception_message":  str(exc)            if exc else None,

        "server_crash":       server_crash,
        "server_log_tail":    server_log_tail,

        "error_detail":       error_detail or f"{axis}.{reason_code}",
    }


def attach_diag(request, diag: dict) -> None:
    """pytest request fixture의 user_properties에 diag를 첨부한다."""
    request.node.user_properties.append(("diag", diag))
