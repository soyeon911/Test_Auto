"""
pytest conftest.py — shared fixtures for all generated & manual tests.
"""

import os
import pytest
import requests


def pytest_addoption(parser):
    parser.addoption(
        "--base-url",
        action="store",
        default=os.environ.get("BASE_URL", "http://localhost:8000"),
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
