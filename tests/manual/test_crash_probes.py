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

# ─── 설정 ─────────────────────────────────────────────────────────────────────

_HEALTH_TIMEOUT   = 3.0     # /health 응답 대기 (초)
_PROBE_TIMEOUT    = 6.0     # 개별 프로브 요청 타임아웃
_RESTART_WAIT     = 12      # 서버 재기동 후 대기 (초)
_MAX_RESTART_WAIT = 30      # 재기동 최대 대기

# ─── 결과 분류 상수 ────────────────────────────────────────────────────────────

CRASH_DETECTED    = "CRASH_DETECTED"
VALIDATION_GAP    = "VALIDATION_GAP"
GRACEFUL_REJECTION = "GRACEFUL_REJECTION"
UNEXPECTED_SUCCESS = "UNEXPECTED_SUCCESS"
TIMEOUT           = "TIMEOUT"
CONNECTION_ERROR  = "CONNECTION_ERROR"


# ─── 서버 생존 관리 ───────────────────────────────────────────────────────────

def _is_alive(base_url: str, timeout: float = _HEALTH_TIMEOUT) -> bool:
    try:
        r = requests.get(f"{base_url}/health", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _restart_server(base_url: str) -> bool:
    """
    환경변수(SERVER_DIR, SERVER_EXE_NAME 등)를 읽어 서버를 재기동한다.
    GitHub Actions self-hosted 환경 전용. 로컬 직접 실행 시에는 skip.
    """
    server_dir = os.environ.get("SERVER_DIR", "")
    server_exe = os.environ.get("SERVER_EXE_NAME", "qfe-server.exe")
    if not server_dir:
        print("[CrashProbe] SERVER_DIR 미설정 — 자동 재기동 불가")
        return False

    exe_path  = os.path.join(server_dir, server_exe)
    stdin_str = "\n".join([
        os.environ.get("SERVER_LICENSE_KEY",    "1"),
        os.environ.get("SERVER_MODE_CHOICE",    "1"),
        os.environ.get("SERVER_INSTANCE_COUNT", "1"),
        os.environ.get("SERVER_MODEL_PATH",     ""),
        os.environ.get("SERVER_DB_PATH",        ""),
        "",
    ]).encode()

    print(f"[CrashProbe] 서버 재기동: {exe_path}")
    try:
        proc = subprocess.Popen(
            [exe_path, "-host", "0.0.0.0"],
            cwd=server_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        proc.stdin.write(stdin_str)
        proc.stdin.flush()
        proc.stdin.close()
    except Exception as e:
        print(f"[CrashProbe] 재기동 실패: {e}")
        return False

    # 준비될 때까지 대기
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
    method:   str,
    path:     str,
    **kwargs: Any,
) -> dict:
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
    result: dict = {
        "http_status":  None,
        "success":      None,
        "error_code":   None,
        "msg":          "",
        "outcome":      None,
        "server_alive": True,
    }

    try:
        resp = getattr(requests, method.lower())(url, timeout=_PROBE_TIMEOUT, **kwargs)
        result["http_status"] = resp.status_code
        try:
            body = resp.json()
            result["success"]    = body.get("success")
            result["error_code"] = body.get("error_code")
            result["msg"]        = body.get("msg", "")
        except Exception:
            result["msg"] = resp.text[:200]

    except (requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError) as e:
        result["outcome"] = CONNECTION_ERROR
        result["msg"]     = str(e)[:200]
    except requests.exceptions.Timeout:
        result["outcome"] = TIMEOUT
    except Exception as e:
        result["outcome"] = CONNECTION_ERROR
        result["msg"]     = str(e)[:200]

    # 서버 생존 여부 확인
    alive = _is_alive(base_url)
    result["server_alive"] = alive

    # outcome 이 아직 미분류면 결정
    if result["outcome"] is None:
        if not alive:
            result["outcome"] = CRASH_DETECTED
        elif result["success"] is True:
            result["outcome"] = UNEXPECTED_SUCCESS
        else:
            result["outcome"] = GRACEFUL_REJECTION

    return result


def _assert_no_crash(result: dict, label: str, severity: str) -> None:
    """크래시 감지 시 CRASH_DETECTED 로 실패, 검증 누락 시 VALIDATION_GAP 으로 실패."""
    if result["outcome"] == CRASH_DETECTED:
        pytest.fail(
            f"\n{'='*60}\n"
            f"[{CRASH_DETECTED}] severity={severity}\n"
            f"probe    : {label}\n"
            f"→ 서버 프로세스가 죽었습니다 (Exception 0xc0000005 / SIGSEGV 의심)\n"
            f"  C 라이브러리 호출 전 입력값 검증이 없습니다.\n"
            f"{'='*60}"
        )
    elif result["outcome"] == UNEXPECTED_SUCCESS:
        # success=True 인데 비정상 입력 → 잠재적 크래시 경로
        pytest.fail(
            f"\n{'='*60}\n"
            f"[{VALIDATION_GAP}] severity={severity}\n"
            f"probe    : {label}\n"
            f"→ 비정상 입력인데 success=True 반환\n"
            f"  error_code={result['error_code']}  msg={result['msg'][:80]}\n"
            f"  C 라이브러리가 이 값을 처리했다는 의미 — 다른 값에서 크래시 가능\n"
            f"{'='*60}"
        )
    # GRACEFUL_REJECTION → PASS (서버가 올바르게 에러 반환)


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
            pytest.skip("서버 재기동 실패 — 이후 테스트 스킵")


# ─── 공통 유효하지 않은 base64 / 템플릿 입력 목록 ────────────────────────────

# (label, value, severity, note)
_INVALID_TEMPLATE_CASES = [
    ("empty_string",           "",                      "CRITICAL", "빈 문자열 → C 함수에 null/empty 버퍼"),
    ("single_char",            "a",                     "CRITICAL", "base64 패딩 불완전"),
    ("two_chars",              "ab",                    "CRITICAL", "base64 패딩 불완전"),
    ("three_chars_no_pad",     "abc",                   "HIGH",     "패딩 없는 base64"),
    ("valid_b64_1byte",        "YQ==",                  "HIGH",     "유효 base64지만 1바이트 → 템플릿으로 너무 짧음"),
    ("valid_b64_3bytes",       "AAEC",                  "HIGH",     "유효 base64지만 3바이트"),
    ("null_byte_b64",          "AA==",                  "HIGH",     "0x00 단일 바이트"),
    ("binary_garbage_short",   "////",                  "HIGH",     "유효 base64 → 이진 쓰레기"),
    ("binary_garbage_long",    "AAAA" * 128,            "MEDIUM",   "512바이트 이진 쓰레기"),
    ("very_long_garbage",      "A" * 8192,              "MEDIUM",   "8KB 쓰레기 (스택 오버플로우 유발 가능)"),
    ("whitespace_only",        "   ",                   "MEDIUM",   "공백만"),
    ("newline_chars",          "\n\n\n",                "MEDIUM",   "개행문자"),
    ("unicode_string",         "テンプレートデータ",      "MEDIUM",   "유니코드 (ASCII 아님)"),
    ("sql_injection",          "'; DROP TABLE--",       "MEDIUM",   "SQL 인젝션"),
    ("path_traversal",         "../../../etc/passwd",   "MEDIUM",   "경로 탐색"),
    ("html_injection",         "<script>alert(1)</script>", "LOW",  "XSS 프로브"),
    ("format_string",          "%s%s%s%s%s",            "LOW",      "형식 문자열"),
    ("null_json_value",        None,                    "MEDIUM",   "null 값"),
]

_INVALID_IMAGE_CASES = [
    ("empty_string",           "",                      "CRITICAL", "빈 이미지"),
    ("single_char",            "a",                     "CRITICAL", "너무 짧은 base64"),
    ("valid_b64_3bytes",       "AAEC",                  "HIGH",     "3바이트 이진 → 이미지 아님"),
    ("binary_garbage_short",   "////",                  "HIGH",     "이진 쓰레기"),
    ("binary_garbage_long",    "AAAA" * 128,            "MEDIUM",   "512바이트 이진 쓰레기"),
    ("very_long_garbage",      "A" * 8192,              "MEDIUM",   "8KB 이진 쓰레기"),
    ("whitespace_only",        "   ",                   "MEDIUM",   "공백만"),
    ("unicode_string",         "画像データ",              "MEDIUM",   "유니코드"),
    ("null_json_value",        None,                    "MEDIUM",   "null 값"),
]


# ═══════════════════════════════════════════════════════════════════════════════
# 1. POST /api/v2/enroll-template
#    CGO 호출: QFE_SetTemplateBase64 → QFE_EnrollTemplate
# ═══════════════════════════════════════════════════════════════════════════════

class TestEnrollTemplateCrashProbe:
    """
    template_data 에 비정상 값을 넣어 크래시 또는 검증 누락을 탐지.
    스크린샷 근거: 'abc' → error_code=-50 (graceful), 다른 값에서 segfault 가능.
    """

    @pytest.mark.parametrize(
        "label,template_data,severity,note",
        _INVALID_TEMPLATE_CASES,
        ids=[c[0] for c in _INVALID_TEMPLATE_CASES],
    )
    def test_invalid_template_data(
        self, base_url, label, template_data, severity, note
    ):
        """[crash_probe] enroll-template: 비정상 template_data → 크래시 or 검증 누락"""
        payload = {"template_data": template_data, "user_id": 1}
        # None 이면 JSON에 null 로 직렬화됨
        result = _probe(base_url, "POST", "/api/v2/enroll-template", json=payload)

        print(
            f"\n  [{result['outcome']}] {label} | severity={severity}\n"
            f"  template_data={str(template_data)[:40]!r}  note={note}\n"
            f"  http={result['http_status']}  success={result['success']}"
            f"  error_code={result['error_code']}  server_alive={result['server_alive']}"
        )
        _assert_no_crash(result, f"enroll-template/{label}", severity)

    @pytest.mark.parametrize("user_id,severity,note", [
        (-1,          "MEDIUM",   "음수 user_id"),
        (0,           "MEDIUM",   "user_id=0"),
        (2**31,       "MEDIUM",   "int32 최대 초과"),
        (2**63,       "MEDIUM",   "int64 최대 초과"),
        ("string_id", "MEDIUM",   "문자열 user_id"),
        (None,        "MEDIUM",   "null user_id"),
    ], ids=["neg", "zero", "int32max", "int64max", "string", "null"])
    def test_invalid_user_id_with_garbage_template(
        self, base_url, user_id, severity, note
    ):
        """[crash_probe] enroll-template: 비정상 user_id + 쓰레기 template → 크래시 탐지"""
        result = _probe(
            base_url, "POST", "/api/v2/enroll-template",
            json={"template_data": "abc", "user_id": user_id},
        )
        print(
            f"\n  [{result['outcome']}] user_id={user_id}  note={note}\n"
            f"  http={result['http_status']}  error_code={result['error_code']}"
            f"  server_alive={result['server_alive']}"
        )
        _assert_no_crash(result, f"enroll-template/user_id={user_id}", severity)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. POST /api/v2/match
#    CGO 호출: QFE_SetTemplateBase64 × 2 → QFE_MatchTemplates
#    기존 보고: invalid_b64 → success=True (VALIDATION_GAP)
# ═══════════════════════════════════════════════════════════════════════════════

class TestMatchCrashProbe:
    """
    template1 / template2 에 비정상 값을 보내 크래시 또는 검증 누락 탐지.
    이미 'C. 서버 검증 누락' 로 보고된 버그의 더 aggressive한 재현 시도.
    """

    @pytest.mark.parametrize(
        "label,template_data,severity,note",
        _INVALID_TEMPLATE_CASES,
        ids=[f"t1_{c[0]}" for c in _INVALID_TEMPLATE_CASES],
    )
    def test_invalid_template1(self, base_url, label, template_data, severity, note):
        """[crash_probe] match: template1 비정상 → 크래시 탐지"""
        result = _probe(
            base_url, "POST", "/api/v2/match",
            json={"template1": template_data, "template2": "AAAA"},
        )
        print(
            f"\n  [{result['outcome']}] template1={label}  severity={severity}\n"
            f"  http={result['http_status']}  success={result['success']}"
            f"  error_code={result['error_code']}  server_alive={result['server_alive']}"
        )
        _assert_no_crash(result, f"match/template1={label}", severity)

    @pytest.mark.parametrize(
        "label,template_data,severity,note",
        _INVALID_TEMPLATE_CASES,
        ids=[f"t2_{c[0]}" for c in _INVALID_TEMPLATE_CASES],
    )
    def test_invalid_template2(self, base_url, label, template_data, severity, note):
        """[crash_probe] match: template2 비정상 → 크래시 탐지"""
        result = _probe(
            base_url, "POST", "/api/v2/match",
            json={"template1": "AAAA", "template2": template_data},
        )
        print(
            f"\n  [{result['outcome']}] template2={label}  severity={severity}\n"
            f"  http={result['http_status']}  success={result['success']}"
            f"  error_code={result['error_code']}  server_alive={result['server_alive']}"
        )
        _assert_no_crash(result, f"match/template2={label}", severity)

    def test_both_templates_invalid(self, base_url):
        """[crash_probe] match: template1, template2 모두 비정상 → 조합 크래시"""
        result = _probe(
            base_url, "POST", "/api/v2/match",
            json={"template1": "", "template2": ""},
        )
        print(
            f"\n  [{result['outcome']}] both templates empty\n"
            f"  http={result['http_status']}  success={result['success']}"
            f"  error_code={result['error_code']}  server_alive={result['server_alive']}"
        )
        _assert_no_crash(result, "match/both_empty", "CRITICAL")

    def test_concurrent_invalid_templates(self, base_url):
        """[crash_probe] match: 동시 다중 비정상 요청 → 경쟁 조건(race condition) 크래시"""
        import concurrent.futures

        def _send():
            return _probe(
                base_url, "POST", "/api/v2/match",
                json={"template1": "", "template2": ""},
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            futures = [ex.submit(_send) for _ in range(5)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        crashes = [r for r in results if r["outcome"] == CRASH_DETECTED]
        alive = _is_alive(base_url)

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
#    CGO 호출: QFE_SetTemplateBase64 → QFE_VerifyTemplate (DB 비교)
# ═══════════════════════════════════════════════════════════════════════════════

class TestVerifyTemplateCrashProbe:

    @pytest.mark.parametrize(
        "label,template_data,severity,note",
        _INVALID_TEMPLATE_CASES,
        ids=[c[0] for c in _INVALID_TEMPLATE_CASES],
    )
    def test_invalid_template_data(
        self, base_url, label, template_data, severity, note
    ):
        """[crash_probe] verify-template: 비정상 template_data → 크래시 탐지"""
        result = _probe(
            base_url, "POST", "/api/v2/verify-template",
            json={"template_data": template_data, "user_id": 1},
        )
        print(
            f"\n  [{result['outcome']}] {label}  severity={severity}\n"
            f"  http={result['http_status']}  error_code={result['error_code']}"
            f"  server_alive={result['server_alive']}"
        )
        _assert_no_crash(result, f"verify-template/{label}", severity)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. POST /api/v2/enroll
#    CGO 호출: QFE_ExtractFeature (이미지 base64 → 템플릿 추출)
# ═══════════════════════════════════════════════════════════════════════════════

class TestEnrollImageCrashProbe:

    @pytest.mark.parametrize(
        "label,image_data,severity,note",
        _INVALID_IMAGE_CASES,
        ids=[c[0] for c in _INVALID_IMAGE_CASES],
    )
    def test_invalid_image_base64(
        self, base_url, label, image_data, severity, note
    ):
        """[crash_probe] enroll: 비정상 image_base64 → 크래시 탐지"""
        result = _probe(
            base_url, "POST", "/api/v2/enroll",
            json={"image_base64": image_data, "user_id": 1},
        )
        print(
            f"\n  [{result['outcome']}] {label}  severity={severity}\n"
            f"  http={result['http_status']}  error_code={result['error_code']}"
            f"  server_alive={result['server_alive']}"
        )
        _assert_no_crash(result, f"enroll/{label}", severity)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. POST /api/v2/verify
#    CGO 호출: QFE_ExtractFeature → QFE_VerifyTemplate
# ═══════════════════════════════════════════════════════════════════════════════

class TestVerifyImageCrashProbe:

    @pytest.mark.parametrize(
        "label,image_data,severity,note",
        _INVALID_IMAGE_CASES,
        ids=[c[0] for c in _INVALID_IMAGE_CASES],
    )
    def test_invalid_image_base64(
        self, base_url, label, image_data, severity, note
    ):
        """[crash_probe] verify: 비정상 image_base64 → 크래시 탐지"""
        result = _probe(
            base_url, "POST", "/api/v2/verify",
            json={"image_base64": image_data, "user_id": 1},
        )
        print(
            f"\n  [{result['outcome']}] {label}  severity={severity}\n"
            f"  http={result['http_status']}  error_code={result['error_code']}"
            f"  server_alive={result['server_alive']}"
        )
        _assert_no_crash(result, f"verify/{label}", severity)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. POST /api/v2/match-images
#    CGO 호출: QFE_ExtractFeature × 2 → QFE_MatchTemplates
# ═══════════════════════════════════════════════════════════════════════════════

class TestMatchImagesCrashProbe:

    @pytest.mark.parametrize(
        "label,image_data,severity,note",
        _INVALID_IMAGE_CASES,
        ids=[f"img1_{c[0]}" for c in _INVALID_IMAGE_CASES],
    )
    def test_invalid_image1(self, base_url, label, image_data, severity, note):
        """[crash_probe] match-images: image1 비정상 → 크래시 탐지"""
        result = _probe(
            base_url, "POST", "/api/v2/match-images",
            json={"image1": image_data, "image2": "AAAA"},
        )
        print(
            f"\n  [{result['outcome']}] image1={label}  severity={severity}\n"
            f"  http={result['http_status']}  error_code={result['error_code']}"
            f"  server_alive={result['server_alive']}"
        )
        _assert_no_crash(result, f"match-images/image1={label}", severity)

    def test_both_images_empty(self, base_url):
        """[crash_probe] match-images: image1, image2 모두 빈 문자열 → 크래시 탐지"""
        result = _probe(
            base_url, "POST", "/api/v2/match-images",
            json={"image1": "", "image2": ""},
        )
        _assert_no_crash(result, "match-images/both_empty", "CRITICAL")
