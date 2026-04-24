"""
Server Crash Probe Tests
========================
CGO 를 통해 C 라이브러리를 직접 호출하는 엔드포인트에 비정상 입력을 보내
서버가 죽는지(Exception 0xc0000005 / SIGSEGV) 또는 정상적으로 거부하는지 탐지.

탐지 결과 분류 (CLAUDE.md Pass/Fail Classification Rules):
  CRASH_DETECTED       서버 프로세스가 죽음 → 최고 심각도
  VALIDATION_GAP       서버가 에러 반환해야 하는데 success=True 반환 → 잠재적 크래시 경로
  GRACEFUL_REJECTION   서버가 올바르게 에러 반환 (정상, PASS)
  UNEXPECTED_SUCCESS   비정상 입력인데 성공 응답 → 도메인 버그

대상 엔드포인트 (CGO 호출 경로):
  POST /api/v2/enroll-template    QFE_SetTemplateBase64 → QFE_EnrollTemplate
  POST /api/v2/match              QFE_SetTemplateBase64 × 2 → QFE_MatchTemplates
  POST /api/v2/verify-template    QFE_SetTemplateBase64 → QFE_VerifyTemplate
  POST /api/v2/enroll             QFE_ExtractFeature (이미지 → 템플릿)
  POST /api/v2/verify             QFE_ExtractFeature + QFE_VerifyTemplate
  POST /api/v2/match-images       QFE_ExtractFeature × 2 + QFE_MatchTemplates
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any

import pytest
import requests
from tests.helpers.diag import build_probe_diag, attach_probe_diag, attach_probe_meta

# ─── 설정 ─────────────────────────────────────────────────────────────────────

_HEALTH_TIMEOUT = 3.0
_PROBE_TIMEOUT = 6.0
_RESTART_WAIT = 12
_MAX_RESTART_WAIT = 30

# ─── 결과 분류 상수 ────────────────────────────────────────────────────────────

CRASH_DETECTED = "CRASH_DETECTED"
VALIDATION_GAP = "VALIDATION_GAP"
GRACEFUL_REJECTION = "GRACEFUL_REJECTION"
UNEXPECTED_SUCCESS = "UNEXPECTED_SUCCESS"
TIMEOUT = "TIMEOUT"
CONNECTION_ERROR = "CONNECTION_ERROR"


# ─── 서버 생존 관리 ───────────────────────────────────────────────────────────

def _is_alive(base_url: str, timeout: float = _HEALTH_TIMEOUT) -> bool:
    try:
        r = requests.get(f"{base_url}/health", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _restart_server(base_url: str) -> bool:
    server_dir = os.environ.get("SERVER_DIR", "")
    server_exe = os.environ.get("SERVER_EXE_NAME", "qfe-server.exe")
    if not server_dir:
        print("[CrashProbe] SERVER_DIR 미설정 — 자동 재기동 불가")
        return False

    exe_path = os.path.join(server_dir, server_exe)
    stdin_file = os.path.join(server_dir, "stdin.txt")
    log_file = os.path.join(server_dir, "server.log")

    print(f"[CrashProbe] 서버 재기동: {exe_path}")

    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", server_exe],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1)

        with open(stdin_file, "w", encoding="utf-8") as f:
            f.write(
                "\n".join(
                    [
                        os.environ.get("SERVER_LICENSE_KEY", "1"),
                        os.environ.get("SERVER_MODE_CHOICE", "1"),
                        os.environ.get("SERVER_INSTANCE_COUNT", "1"),
                        os.environ.get("SERVER_MODEL_PATH", ""),
                        os.environ.get("SERVER_DB_PATH", ""),
                        "",
                    ]
                )
            )

        subprocess.Popen(
            f'"{exe_path}" -host 0.0.0.0 < "{stdin_file}" > "{log_file}" 2>&1',
            cwd=server_dir,
            shell=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )

    except Exception as e:
        print(f"[CrashProbe] 재기동 실패: {e}")
        return False

    for _ in range(_MAX_RESTART_WAIT):
        time.sleep(1)
        if _is_alive(base_url):
            print("[CrashProbe] 서버 재기동 완료")
            return True

    print("[CrashProbe] 서버 재기동 타임아웃")
    return False


# ─── 프로브 헬퍼 ──────────────────────────────────────────────────────────────

def _probe(
    base_url: str,
    method: str,
    path: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """
    단일 프로브 요청을 보내고 결과를 분류한다.

    반환:
      {
        "http_status":   int | None,
        "success":       bool | None,
        "error_code":    int | None,
        "msg":           str,
        "outcome":       CRASH_DETECTED | GRACEFUL_REJECTION | UNEXPECTED_SUCCESS | ...
        "server_alive":  bool,
      }
    """
    url = f"{base_url}{path}"
    result: dict[str, Any] = {
        "http_status": None,
        "success": None,
        "error_code": None,
        "msg": "",
        "outcome": None,
        "server_alive": True,
    }

    try:
        resp = getattr(requests, method.lower())(url, timeout=_PROBE_TIMEOUT, **kwargs)
        result["http_status"] = resp.status_code
        try:
            body = resp.json()
            result["success"] = body.get("success")
            result["error_code"] = body.get("error_code")
            result["msg"] = body.get("msg", "")
        except Exception:
            result["msg"] = resp.text[:200]

    except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
        result["outcome"] = CONNECTION_ERROR
        result["msg"] = str(e)[:200]
    except requests.exceptions.Timeout:
        result["outcome"] = TIMEOUT
    except Exception as e:
        result["outcome"] = CONNECTION_ERROR
        result["msg"] = str(e)[:200]

    alive = _is_alive(base_url)
    result["server_alive"] = alive

    # ConnectionError 후 서버가 죽어있으면 → 서버 Crash로 재분류
    if result["outcome"] == CONNECTION_ERROR and not alive:
        result["outcome"] = CRASH_DETECTED

    if result["outcome"] is None:
        if not alive:
            result["outcome"] = CRASH_DETECTED
        elif result["http_status"] is None:
            result["outcome"] = CONNECTION_ERROR
        elif result["success"] is True:
            result["outcome"] = UNEXPECTED_SUCCESS
        else:
            result["outcome"] = GRACEFUL_REJECTION

    return result


def _assert_no_crash(
    request,
    result: dict[str, Any],
    *,
    endpoint: str,
    target_field: str,
    probe_input: Any,
    label: str,
    severity: str,
) -> None:
    """
    크래시 감지 시 CRASH_DETECTED 로 실패,
    검증 누락(success=True) 시 VALIDATION_GAP 으로 실패.
    """
    classification = VALIDATION_GAP if result["outcome"] == UNEXPECTED_SUCCESS else result["outcome"]

    attach_probe_meta(
        request=request,
        probe_endpoint=endpoint,
        target_field=target_field,
        probe_label=label,
        probe_input=probe_input,
        severity=severity,
    )

    probe_diag = build_probe_diag(
        probe_endpoint=endpoint,
        target_field=target_field,
        probe_label=label,
        probe_input=probe_input,
        severity=severity,
        classification=classification,
        expected_behavior="invalid input should be rejected without crash",
        resp=None,
        body={
            "success": result.get("success"),
            "error_code": result.get("error_code"),
            "msg": result.get("msg"),
        },
        error_detail=f"runtime.probe.{endpoint.strip('/').replace('/', '.')}.{label}",
    )
    probe_diag["actual_status"] = result.get("http_status")
    attach_probe_diag(request, probe_diag)

    if result["outcome"] == CRASH_DETECTED:
        pytest.fail(
            f"\n{'='*60}\n"
            f"[{CRASH_DETECTED}] severity={severity}\n"
            f"probe    : {label}\n"
            f"→ 서버 프로세스가 죽었습니다 (Exception 0xc0000005 / SIGSEGV 의심)\n"
            f"  C 라이브러리 호출 전 입력값 검증이 없습니다.\n"
            f"{'='*60}"
        )

    if result["outcome"] == UNEXPECTED_SUCCESS:
        pytest.fail(
            f"\n{'='*60}\n"
            f"[{VALIDATION_GAP}] severity={severity}\n"
            f"probe    : {label}\n"
            f"→ 비정상 입력인데 success=True 반환\n"
            f"  error_code={result['error_code']}  msg={result['msg'][:80]}\n"
            f"  C 라이브러리가 이 값을 처리했다는 의미 — 다른 값에서 크래시 가능\n"
            f"{'='*60}"
        )


# ─── Fixture ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _guard_server_restart(base_url, request):
    """
    각 크래시 프로브 테스트 후 서버가 죽었으면 재기동을 시도한다.
    재기동 성공 여부와 무관하게 다음 테스트를 계속 진행한다.
    """
    yield
    if not _is_alive(base_url):
        print(f"\n[CrashProbe] 테스트 '{request.node.name}' 후 서버 다운 감지 → 재기동 시도")
        ok = _restart_server(base_url)
        if not ok:
            pytest.fail("서버 재기동 실패 (CRASH 상태 유지)")


# ─── 공통 유효하지 않은 base64 / 템플릿 입력 목록 ────────────────────────────

_INVALID_TEMPLATE_CASES = [
    ("empty_string", "", "CRITICAL", "빈 문자열 → C 함수에 null/empty 버퍼"),
    ("single_char", "a", "CRITICAL", "base64 패딩 불완전"),
    ("two_chars", "ab", "CRITICAL", "base64 패딩 불완전"),
    ("three_chars_no_pad", "abc", "HIGH", "패딩 없는 base64"),
    ("valid_b64_1byte", "YQ==", "HIGH", "유효 base64지만 1바이트 → 템플릿으로 너무 짧음"),
    ("valid_b64_3bytes", "AAEC", "HIGH", "유효 base64지만 3바이트"),
    ("null_byte_b64", "AA==", "HIGH", "0x00 단일 바이트"),
    ("binary_garbage_short", "////", "HIGH", "유효 base64 → 이진 쓰레기"),
    ("binary_garbage_long", "AAAA" * 128, "MEDIUM", "512바이트 이진 쓰레기"),
    ("very_long_garbage", "A" * 8192, "MEDIUM", "8KB 쓰레기 (스택 오버플로우 유발 가능)"),
    ("whitespace_only", "   ", "MEDIUM", "공백만"),
    ("newline_chars", "\n\n\n", "MEDIUM", "개행문자"),
    ("unicode_string", "テンプレートデータ", "MEDIUM", "유니코드 (ASCII 아님)"),
    ("sql_injection", "'; DROP TABLE--", "MEDIUM", "SQL 인젝션"),
    ("path_traversal", "../../../etc/passwd", "MEDIUM", "경로 탐색"),
    ("html_injection", "<script>alert(1)</script>", "LOW", "XSS 프로브"),
    ("format_string", "%s%s%s%s%s", "LOW", "형식 문자열"),
    ("null_json_value", None, "MEDIUM", "null 값"),
]

_INVALID_IMAGE_CASES = [
    ("empty_string", "", "CRITICAL", "빈 이미지"),
    ("single_char", "a", "CRITICAL", "너무 짧은 base64"),
    ("valid_b64_3bytes", "AAEC", "HIGH", "3바이트 이진 → 이미지 아님"),
    ("binary_garbage_short", "////", "HIGH", "이진 쓰레기"),
    ("binary_garbage_long", "AAAA" * 128, "MEDIUM", "512바이트 이진 쓰레기"),
    ("very_long_garbage", "A" * 8192, "MEDIUM", "8KB 이진 쓰레기"),
    ("whitespace_only", "   ", "MEDIUM", "공백만"),
    ("unicode_string", "画像データ", "MEDIUM", "유니코드"),
    ("null_json_value", None, "MEDIUM", "null 값"),
]


# ═══════════════════════════════════════════════════════════════════════════════
# 1. POST /api/v2/enroll-template
# ═══════════════════════════════════════════════════════════════════════════════

class TestEnrollTemplateCrashProbe:
    @pytest.mark.parametrize(
        "label,template_data,severity,note",
        _INVALID_TEMPLATE_CASES,
        ids=[c[0] for c in _INVALID_TEMPLATE_CASES],
    )
    def test_invalid_template_data(self, base_url, request, label, template_data, severity, note):
        payload = {"template_data": template_data, "user_id": 1}
        result = _probe(base_url, "POST", "/api/v2/enroll-template", json=payload)

        print(
            f"\n  [{result['outcome']}] {label} | severity={severity}\n"
            f"  template_data={str(template_data)[:40]!r}  note={note}\n"
            f"  http={result['http_status']}  success={result['success']}"
            f"  error_code={result['error_code']}  server_alive={result['server_alive']}"
        )
        _assert_no_crash(
            request,
            result,
            endpoint="/api/v2/enroll-template",
            target_field="template_data",
            probe_input=template_data,
            label=f"enroll-template/{label}",
            severity=severity,
        )

    @pytest.mark.parametrize(
        "user_id,severity,note",
        [
            (-1, "MEDIUM", "음수 user_id"),
            (0, "MEDIUM", "user_id=0"),
            (2**31, "MEDIUM", "int32 최대 초과"),
            (2**63, "MEDIUM", "int64 최대 초과"),
            ("string_id", "MEDIUM", "문자열 user_id"),
            (None, "MEDIUM", "null user_id"),
        ],
        ids=["neg", "zero", "int32max", "int64max", "string", "null"],
    )
    def test_invalid_user_id_with_garbage_template(self, base_url, request, user_id, severity, note):
        result = _probe(
            base_url,
            "POST",
            "/api/v2/enroll-template",
            json={"template_data": "abc", "user_id": user_id},
        )
        print(
            f"\n  [{result['outcome']}] user_id={user_id}  note={note}\n"
            f"  http={result['http_status']}  error_code={result['error_code']}"
            f"  server_alive={result['server_alive']}"
        )
        _assert_no_crash(
            request,
            result,
            endpoint="/api/v2/enroll-template",
            target_field="user_id",
            probe_input=user_id,
            label=f"enroll-template/user_id={user_id}",
            severity=severity,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 2. POST /api/v2/match
# ═══════════════════════════════════════════════════════════════════════════════

class TestMatchCrashProbe:
    @pytest.mark.parametrize(
        "label,template_data,severity,note",
        _INVALID_TEMPLATE_CASES,
        ids=[f"t1_{c[0]}" for c in _INVALID_TEMPLATE_CASES],
    )
    def test_invalid_template1(self, base_url, request, label, template_data, severity, note):
        result = _probe(
            base_url,
            "POST",
            "/api/v2/match",
            json={"template1": template_data, "template2": "AAAA"},
        )
        print(
            f"\n  [{result['outcome']}] template1={label}  severity={severity}\n"
            f"  http={result['http_status']}  success={result['success']}"
            f"  error_code={result['error_code']}  server_alive={result['server_alive']}"
        )
        _assert_no_crash(
            request,
            result,
            endpoint="/api/v2/match",
            target_field="template1",
            probe_input=template_data,
            label=f"match/template1={label}",
            severity=severity,
        )

    @pytest.mark.parametrize(
        "label,template_data,severity,note",
        _INVALID_TEMPLATE_CASES,
        ids=[f"t2_{c[0]}" for c in _INVALID_TEMPLATE_CASES],
    )
    def test_invalid_template2(self, base_url, request, label, template_data, severity, note):
        result = _probe(
            base_url,
            "POST",
            "/api/v2/match",
            json={"template1": "AAAA", "template2": template_data},
        )
        print(
            f"\n  [{result['outcome']}] template2={label}  severity={severity}\n"
            f"  http={result['http_status']}  success={result['success']}"
            f"  error_code={result['error_code']}  server_alive={result['server_alive']}"
        )
        _assert_no_crash(
            request,
            result,
            endpoint="/api/v2/match",
            target_field="template2",
            probe_input=template_data,
            label=f"match/template2={label}",
            severity=severity,
        )

    def test_both_templates_invalid(self, base_url, request):
        result = _probe(
            base_url,
            "POST",
            "/api/v2/match",
            json={"template1": "", "template2": ""},
        )
        print(
            f"\n  [{result['outcome']}] both templates empty\n"
            f"  http={result['http_status']}  success={result['success']}"
            f"  error_code={result['error_code']}  server_alive={result['server_alive']}"
        )
        _assert_no_crash(
            request,
            result,
            endpoint="/api/v2/match",
            target_field="template1,template2",
            probe_input={"template1": "", "template2": ""},
            label="match/both_empty",
            severity="CRITICAL",
        )

    def test_concurrent_invalid_templates(self, base_url, request):
        import concurrent.futures

        def _send():
            return _probe(
                base_url,
                "POST",
                "/api/v2/match",
                json={"template1": "", "template2": ""},
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            futures = [ex.submit(_send) for _ in range(5)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        crashes = [r for r in results if r["outcome"] == CRASH_DETECTED]
        alive = _is_alive(base_url)

        classification = CRASH_DETECTED if (not alive or crashes) else GRACEFUL_REJECTION
        attach_probe_meta(
            request=request,
            probe_endpoint="/api/v2/match",
            target_field="template1,template2",
            probe_label="match/concurrent_both_empty",
            probe_input={"template1": "", "template2": "", "workers": 5},
            severity="CRITICAL",
        )
        probe_diag = build_probe_diag(
            probe_endpoint="/api/v2/match",
            target_field="template1,template2",
            probe_label="match/concurrent_both_empty",
            probe_input={"template1": "", "template2": "", "workers": 5},
            severity="CRITICAL",
            classification=classification,
            expected_behavior="invalid concurrent inputs should be rejected without crash",
            resp=None,
            body={
                "success": None,
                "error_code": None,
                "msg": f"concurrent_results={len(results)}, crashes={len(crashes)}, server_alive={alive}",
            },
            error_detail="runtime.probe.api.v2.match.concurrent_both_empty",
        )
        attach_probe_diag(request, probe_diag)

        print(
            f"\n  concurrent match probes: {len(results)} sent, "
            f"{len(crashes)} crash(es), server_alive={alive}"
        )

        if not alive or crashes:
            pytest.fail(
                f"[{CRASH_DETECTED}] severity=CRITICAL\n"
                f"동시 비정상 요청 {len(results)}건 중 크래시 {len(crashes)}건\n"
                f"→ Race condition 또는 메모리 손상 의심"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 3. POST /api/v2/verify-template
# ═══════════════════════════════════════════════════════════════════════════════

class TestVerifyTemplateCrashProbe:
    @pytest.mark.parametrize(
        "label,template_data,severity,note",
        _INVALID_TEMPLATE_CASES,
        ids=[c[0] for c in _INVALID_TEMPLATE_CASES],
    )
    def test_invalid_template_data(self, base_url, request, label, template_data, severity, note):
        result = _probe(
            base_url,
            "POST",
            "/api/v2/verify-template",
            json={"template_data": template_data, "user_id": 1},
        )
        print(
            f"\n  [{result['outcome']}] {label}  severity={severity}\n"
            f"  http={result['http_status']}  error_code={result['error_code']}"
            f"  server_alive={result['server_alive']}"
        )
        _assert_no_crash(
            request,
            result,
            endpoint="/api/v2/verify-template",
            target_field="template_data",
            probe_input=template_data,
            label=f"verify-template/{label}",
            severity=severity,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 4. POST /api/v2/enroll
# ═══════════════════════════════════════════════════════════════════════════════

class TestEnrollImageCrashProbe:
    @pytest.mark.parametrize(
        "label,image_data,severity,note",
        _INVALID_IMAGE_CASES,
        ids=[c[0] for c in _INVALID_IMAGE_CASES],
    )
    def test_invalid_image_base64(self, base_url, request, label, image_data, severity, note):
        result = _probe(
            base_url,
            "POST",
            "/api/v2/enroll",
            json={"image_base64": image_data, "user_id": 1},
        )
        print(
            f"\n  [{result['outcome']}] {label}  severity={severity}\n"
            f"  http={result['http_status']}  error_code={result['error_code']}"
            f"  server_alive={result['server_alive']}"
        )
        _assert_no_crash(
            request,
            result,
            endpoint="/api/v2/enroll",
            target_field="image_base64",
            probe_input=image_data,
            label=f"enroll/{label}",
            severity=severity,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 5. POST /api/v2/verify
# ═══════════════════════════════════════════════════════════════════════════════

class TestVerifyImageCrashProbe:
    @pytest.mark.parametrize(
        "label,image_data,severity,note",
        _INVALID_IMAGE_CASES,
        ids=[c[0] for c in _INVALID_IMAGE_CASES],
    )
    def test_invalid_image_base64(self, base_url, request, label, image_data, severity, note):
        result = _probe(
            base_url,
            "POST",
            "/api/v2/verify",
            json={"image_base64": image_data, "user_id": 1},
        )
        print(
            f"\n  [{result['outcome']}] {label}  severity={severity}\n"
            f"  http={result['http_status']}  error_code={result['error_code']}"
            f"  server_alive={result['server_alive']}"
        )
        _assert_no_crash(
            request,
            result,
            endpoint="/api/v2/verify",
            target_field="image_base64",
            probe_input=image_data,
            label=f"verify/{label}",
            severity=severity,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 6. POST /api/v2/match-images
# ═══════════════════════════════════════════════════════════════════════════════

class TestMatchImagesCrashProbe:
    @pytest.mark.parametrize(
        "label,image_data,severity,note",
        _INVALID_IMAGE_CASES,
        ids=[f"img1_{c[0]}" for c in _INVALID_IMAGE_CASES],
    )
    def test_invalid_image1(self, base_url, request, label, image_data, severity, note):
        result = _probe(
            base_url,
            "POST",
            "/api/v2/match-images",
            json={"image1": image_data, "image2": "AAAA"},
        )
        print(
            f"\n  [{result['outcome']}] image1={label}  severity={severity}\n"
            f"  http={result['http_status']}  error_code={result['error_code']}"
            f"  server_alive={result['server_alive']}"
        )
        _assert_no_crash(
            request,
            result,
            endpoint="/api/v2/match-images",
            target_field="image1",
            probe_input=image_data,
            label=f"match-images/image1={label}",
            severity=severity,
        )

    def test_both_images_empty(self, base_url, request):
        result = _probe(
            base_url,
            "POST",
            "/api/v2/match-images",
            json={"image1": "", "image2": ""},
        )
        _assert_no_crash(
            request,
            result,
            endpoint="/api/v2/match-images",
            target_field="image1,image2",
            probe_input={"image1": "", "image2": ""},
            label="match-images/both_empty",
            severity="CRITICAL",
        )