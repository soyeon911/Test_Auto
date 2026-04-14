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
from pathlib import Path
from typing import Any


# ─── 진단 레코드 생성 ──────────────────────────────────────────────────────────

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

    # response 스니펫 (500자 이내)
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

    return {
        "axis":               axis,
        "reason_code":        reason_code,
        "target_field":       target_field,
        "test_condition":     test_condition,

        "expected_http":      expected_http,
        "expected_app":       expected_app,
        "actual_status":      getattr(resp, "status_code", None),

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
