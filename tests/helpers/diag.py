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
    """pytest request fixture의 user_properties에 diag를 첨부한다."""
    request.node.user_properties.append(("diag", diag))


# ─── 실패 원인 분류 ────────────────────────────────────────────────────────────

def classify_failure_cause(
    outcome: str,
    axis: str,
    reason_code: str,
    response_success,       # bool | None
    response_error_code,    # int | None
    server_crash: bool = False,
) -> str:
    """
    테스트 결과(outcome)와 diag 정보를 조합하여 실패 원인을 분류한다.

    반환값 카테고리:
        PASS                    — 테스트 통과
        서버 Crash (5xx)        — 서버가 5xx로 응답하거나 crash
        서버 미응답              — 연결 거부 / 타임아웃
        상태 미충족 (DB 없음)   — state 축 + API가 "데이터 없음" 오류 반환
        엔드포인트 버그          — schema/domain 축에서 validation이 수행되지 않음
                                  (잘못된 입력에도 success=true 반환)
        예상치 못한 실패         — 정상 입력인데 API가 오류 반환 (positive 실패 등)
        TC 실패 (단언 오류)      — 위 분류에 해당하지 않는 assertion 실패
        알 수 없음               — outcome이 passed가 아닌데 분류 불가
    """
    outcome = (outcome or "").lower()

    if outcome == "passed":
        return "PASS"

    # 서버 crash / 연결 거부
    if server_crash:
        return "서버 Crash (5xx)"
    if reason_code == "connection_refused":
        return "서버 미응답"
    if reason_code in {"timeout", "http_5xx"}:
        return "서버 Crash (5xx)"

    # state 축 — positive TC 실패의 원인 구분
    if axis == "state":
        # API가 success=false 로 응답 → DB/상태 사전 조건 미충족 (서버 자체 버그 X)
        if response_success is False:
            return "상태 미충족 (DB 없음)"
        # API가 success=true 인데 TC assertion 실패 → TC 로직 오류 가능성
        if response_success is True:
            return "TC 실패 (단언 오류)"

    # schema 축 — 잘못된 입력을 서버가 묵인
    if axis == "schema":
        if response_success is True:
            return "엔드포인트 버그 (Validation 미수행)"
        if response_success is False:
            # expected pass인데 fail → positive TC가 상태 문제로 실패했을 수도
            return "예상치 못한 실패"

    # domain 축 — enum/range/semantic 검증 미수행
    if axis == "domain":
        if response_success is True:
            return "엔드포인트 버그 (도메인 검증 미수행)"
        if response_success is False:
            return "예상치 못한 실패"

    # runtime 축
    if axis == "runtime":
        return "서버 Crash (5xx)"

    return "알 수 없음"


