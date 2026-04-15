"""
tests/helpers/server_manager.py — 서버 생사 확인 · 로그 tail · 자동 재기동

필요 환경변수:
    SERVER_LOG_FILE       서버 stderr 로그 파일 경로 (log tail 읽기용)
    BASE_URL              서버 base URL (health check용, 없으면 --base-url 옵션 사용)

    [재기동에 필요한 추가 환경변수]
    SERVER_DIR            서버 실행 파일 디렉터리
    SERVER_EXE_NAME       서버 실행 파일 이름 (기본: qfe-server.exe)
    SERVER_LICENSE_KEY    stdin 응답: 라이선스 키 (기본: 1)
    SERVER_MODE_CHOICE    stdin 응답: 처리 모드 (기본: 1 = CPU)
    SERVER_INSTANCE_COUNT stdin 응답: 인스턴스 수 (기본: 1)
    SERVER_MODEL_PATH     stdin 응답: 모델 경로
    SERVER_DB_PATH        stdin 응답: DB 경로
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


# ─── 내부 유틸 ───────────────────────────────────────────────────────────────

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# ─── 로그 tail ────────────────────────────────────────────────────────────────

def tail_log(n_lines: int = 60) -> str:
    """
    SERVER_LOG_FILE 환경변수 경로에서 서버 로그 마지막 N줄을 반환한다.
    경로가 설정되지 않았거나 파일이 없으면 빈 문자열 반환.
    """
    log_path = _env("SERVER_LOG_FILE")
    if not log_path:
        return ""
    p = Path(log_path)
    if not p.exists():
        return f"[log file not found: {log_path}]"
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n_lines:])
    except Exception as exc:
        return f"[log read error: {exc}]"


# ─── health check ─────────────────────────────────────────────────────────────

def is_alive(base_url: str, timeout: int = 3) -> bool:
    """서버 /health 또는 /api/health 에 응답하면 True."""
    try:
        import requests
    except ImportError:
        return True  # requests 없으면 판단 불가 → 살아있다고 가정

    for ep in ("/health", "/api/health"):
        try:
            r = requests.get(f"{base_url}{ep}", timeout=timeout)
            if r.status_code < 500:
                return True
        except Exception:
            pass
    return False


# ─── 서버 재기동 ──────────────────────────────────────────────────────────────

def _kill_existing_by_name(exe_name: str) -> None:
    """같은 이름의 기존 프로세스를 강제 종료한다 (플랫폼 대응)."""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/IM", exe_name],
                capture_output=True, timeout=5,
            )
        else:
            subprocess.run(
                ["pkill", "-f", exe_name],
                capture_output=True, timeout=5,
            )
        time.sleep(1)  # 포트 반환 대기
    except Exception:
        pass


def _kill_existing_by_port(port: int = 8080) -> None:
    """포트를 점유 중인 프로세스를 종료한다 (Windows netstat 기반)."""
    if sys.platform != "win32":
        return
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                pid = parts[-1]
                if pid.isdigit() and int(pid) > 0:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", pid],
                        capture_output=True, timeout=3,
                    )
        time.sleep(1)
    except Exception:
        pass


def restart_server(base_url: str, wait_sec: int = 25) -> bool:
    """
    환경변수에서 서버 실행 정보를 읽어 서버를 재기동한다.

    반환값:
        True  — wait_sec 안에 health check 성공
        False — 재기동 실패 또는 timeout
    """
    exe_dir  = _env("SERVER_DIR")
    exe_name = _env("SERVER_EXE_NAME", "qfe-server.exe")
    if not exe_dir:
        print("[ServerManager] SERVER_DIR 환경변수가 설정되지 않아 재기동 불가")
        return False

    exe_path = Path(exe_dir) / exe_name
    if not exe_path.exists():
        print(f"[ServerManager] 실행 파일 없음: {exe_path}")
        return False

    # ── 기존 프로세스 정리 ─────────────────────────────────────────
    print(f"[ServerManager] 기존 프로세스 정리 중...")
    _kill_existing_by_name(exe_name)
    _kill_existing_by_port(8080)

    # ── stdin 응답 시퀀스 ──────────────────────────────────────────
    model_default = str(Path(exe_dir) / "model")
    db_default    = str(Path(exe_dir) / "face_database.db")

    stdin_payload = "\n".join([
        _env("SERVER_LICENSE_KEY",    "1"),
        _env("SERVER_MODE_CHOICE",    "1"),
        _env("SERVER_INSTANCE_COUNT", "1"),
        _env("SERVER_MODEL_PATH",     model_default),
        _env("SERVER_DB_PATH",        db_default),
        "",  # 마지막 개행
    ]).encode("utf-8")

    # ── 로그 파일 준비 ────────────────────────────────────────────
    log_path = _env("SERVER_LOG_FILE")
    if log_path:
        log_out = open(log_path, "a", encoding="utf-8", errors="replace")
        log_err = log_out
    else:
        log_out = subprocess.DEVNULL
        log_err = subprocess.DEVNULL

    # ── 프로세스 기동 ─────────────────────────────────────────────
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

    try:
        proc = subprocess.Popen(
            [str(exe_path), "-host", "0.0.0.0"],
            cwd=str(exe_dir),
            stdin=subprocess.PIPE,
            stdout=log_out,
            stderr=log_err,
            creationflags=creation_flags,
        )
        proc.stdin.write(stdin_payload)
        proc.stdin.flush()
        proc.stdin.close()
    except Exception as exc:
        print(f"[ServerManager] 프로세스 기동 실패: {exc}")
        return False

    # 새 PID를 환경변수에 저장 (이후 정리에 사용)
    os.environ["SERVER_PID"] = str(proc.pid)
    print(f"[ServerManager] 서버 기동 중 (PID={proc.pid})...")

    # ── health check 대기 ────────────────────────────────────────
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        time.sleep(2)
        if proc.poll() is not None:
            print(f"[ServerManager] 서버 프로세스가 조기 종료됨 (exit={proc.returncode})")
            return False
        if is_alive(base_url, timeout=2):
            print(f"[ServerManager] 서버 재기동 성공 ✓ (PID={proc.pid})")
            return True

    print(f"[ServerManager] 재기동 timeout ({wait_sec}s 초과)")
    return False
