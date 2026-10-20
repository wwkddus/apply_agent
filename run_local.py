from __future__ import annotations

import os
import secrets
import socket
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx


PROJECT_DIR = Path(__file__).resolve().parent


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_until_ready(base_url: str, process: subprocess.Popen, timeout: float = 15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and process.poll() is None:
        try:
            if httpx.get(f"{base_url}/health", timeout=0.5).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    return False


def main() -> int:
    environment = os.environ.copy()
    token = secrets.token_urlsafe(32)
    environment["JOB_AGENT_API_TOKEN"] = token
    environment.setdefault("JOB_AGENT_DATA_DIR", str(PROJECT_DIR / ".job_agent"))
    environment["PYTHONPATH"] = str(PROJECT_DIR / "src")
    port = available_port()
    base_url = f"http://127.0.0.1:{port}"

    api = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "job_agent_core.api:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
            "--log-level",
            "warning",
        ],
        cwd=PROJECT_DIR,
        env=environment,
    )
    def stop(_signum=None, _frame=None) -> None:
        if api.poll() is None:
            api.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    if not wait_until_ready(base_url, api):
        stop()
        print("로컬 API를 시작하지 못했습니다.", file=sys.stderr)
        return api.returncode or 2
    try:
        from job_agent_core.desktop import run_desktop

        return run_desktop(base_url, token)
    finally:
        stop()
        try:
            api.wait(timeout=5)
        except subprocess.TimeoutExpired:
            api.kill()


if __name__ == "__main__":
    raise SystemExit(main())
