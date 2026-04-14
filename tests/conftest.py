"""
pytest conftest.py — shared fixtures for all generated & manual tests.
"""

import json
import os
from pathlib import Path

import pytest
import requests

# ─── diag JSONL 경로 ─────────────────────────────────────────────────────────
_DIAG_JSONL = Path("reports/test_diag.jsonl")


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


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()

    if report.when != "call":
        return

    _flush_diag(item, report.outcome)



def pytest_configure(config):
    """세션 시작 시 이전 JSONL 초기화."""
    if _DIAG_JSONL.exists():
        _DIAG_JSONL.unlink()


def pytest_addoption(parser):
    parser.addoption(
        "--base-url",
        action="store",
        default=os.environ.get("BASE_URL", "http://192.168.150.158:8080"),
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
