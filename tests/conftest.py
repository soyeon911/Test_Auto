"""
pytest conftest.py — shared fixtures for all generated & manual tests.

Server crash guard 동작:
  - 각 TC의 call 단계가 끝난 직후 서버 상태를 확인
  - 서버가 다운된 경우: 해당 TC의 diag에 server_crash=True + server_log_tail 주입 → JSONL 기록
  - teardown 단계에서 자동 재기동 시도
  - 재기동 성공 시 다음 TC부터 정상 실행 재개
"""

import json
import os
from pathlib import Path

import pytest
import requests

# ─── diag JSONL 경로 ─────────────────────────────────────────────────────────
_DIAG_JSONL = Path("reports/test_diag.jsonl")

# 서버 상태 플래그 (세션 전체 공유)
_server_known_down: bool = False


# ─── JSONL 기록 ──────────────────────────────────────────────────────────────

def _flush_diag(item, outcome: str) -> None:
    """test의 user_properties에서 diag를 꺼내 JSONL에 기록한다."""
    diag = None
    for key, value in getattr(item, "user_properties", []):
        if key == "diag":
            diag = value
            break

    if diag is None:
        return

    _DIAG_JSONL.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "test_id": item.nodeid,
        "outcome": outcome,
        "diag": diag,
    }
    try:
        with _DIAG_JSONL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # JSONL 기록 실패는 테스트 결과에 영향 없음


# ─── 서버 크래시 감지 ─────────────────────────────────────────────────────────

def _get_base_url(item) -> str:
    """item에서 --base-url 옵션 값을 안전하게 꺼낸다."""
    try:
        return item.config.getoption("--base-url") or ""
    except Exception:
        return os.environ.get("BASE_URL", "")


def _inject_crash_into_diag(item, log_tail: str) -> None:
    """user_properties의 diag에 server_crash=True + server_log_tail을 주입한다."""
    for key, value in getattr(item, "user_properties", []):
        if key == "diag" and isinstance(value, dict):
            value["server_crash"] = True
            if log_tail and not value.get("server_log_tail"):
                value["server_log_tail"] = log_tail


def _check_and_update_crash(item) -> bool:
    """
    서버 상태를 확인하고, 크래시면 diag를 업데이트한다.
    반환: 서버가 살아있으면 True, 다운되면 False
    """
    global _server_known_down
    try:
        from tests.helpers.server_manager import is_alive, tail_log
    except ImportError:
        return True

    base_url = _get_base_url(item)
    if not base_url:
        return True

    alive = is_alive(base_url, timeout=2)

    if not alive:
        _server_known_down = True
        log_tail = tail_log(60)
        _inject_crash_into_diag(item, log_tail)
        print(f"\n[ServerCrash] 서버 다운 감지 → diag 업데이트 (test={item.nodeid!r})")
    else:
        _server_known_down = False

    return alive


# ─── 서버 자동 재기동 ─────────────────────────────────────────────────────────

def _try_restart_server(item) -> None:
    """서버가 다운된 경우 자동 재기동을 시도한다."""
    global _server_known_down
    if not _server_known_down:
        return

    try:
        from tests.helpers.server_manager import restart_server, is_alive
    except ImportError:
        return

    base_url = _get_base_url(item)
    if not base_url:
        return

    # 이미 복구됐을 수도 있으니 재확인
    if is_alive(base_url, timeout=2):
        _server_known_down = False
        return

    print(f"\n[ServerManager] 서버 다운 → 자동 재기동 시도...")
    ok = restart_server(base_url)
    if ok:
        _server_known_down = False
        print("[ServerManager] 재기동 성공 ✓ — 다음 TC부터 정상 실행")
    else:
        print("[ServerManager] 재기동 실패 ✗ — 이후 TC는 연결 오류 예상")


# ─── pytest hooks ────────────────────────────────────────────────────────────

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()

    if report.when == "call":
        # 1) 서버 크래시 감지 및 diag 업데이트 (JSONL 기록 전)
        #    PASSED 테스트는 보통 서버가 정상이므로 서버가 이미 다운된 것이
        #    알려진 경우에만 health check 수행 → 불필요한 네트워크 호출 최소화
        if report.outcome != "passed" or _server_known_down:
            _check_and_update_crash(item)

        # 2) JSONL 기록
        _flush_diag(item, report.outcome)

    elif report.when == "teardown":
        # call 단계 완료 후 teardown에서 재기동
        _try_restart_server(item)


def pytest_configure(config):
    """세션 시작 시 이전 JSONL 초기화."""
    global _server_known_down
    _server_known_down = False
    if _DIAG_JSONL.exists():
        _DIAG_JSONL.unlink()


# ─── fixtures ────────────────────────────────────────────────────────────────

def pytest_addoption(parser):
    parser.addoption(
        "--base-url",
        action="store",
        default=os.environ.get("BASE_URL", "http://127.0.0.1:8080"),
        help="Base URL of the local server under test",
    )


@pytest.fixture(scope="session")
def base_url(request) -> str:
    return request.config.getoption("--base-url")


@pytest.fixture(scope="session")
def http_session(base_url) -> requests.Session:
    """Reusable requests session with base URL baked in."""
    session = requests.Session()
    session.base_url = base_url
    return session


@pytest.fixture(autouse=False)
def assert_server_running(base_url):
    """Optional fixture: skip test if server is not reachable."""
    health_url = f"{base_url}/health"
    try:
        resp = requests.get(health_url, timeout=3)
        if resp.status_code >= 500:
            pytest.skip(f"Server returned {resp.status_code} at {health_url}")
    except requests.ConnectionError:
        pytest.skip(f"Server not reachable at {health_url}")
