# [역할] 앱 시작점. 로컬 API 서버를 같은 프로세스의 백그라운드 스레드로 띄우고, 데스크톱 창을 엽니다.
# 설치한 앱(PyInstaller)에서는 'python -m uvicorn'으로 별도 프로세스를 띄울 수 없어서 이 방식을 씁니다.

from __future__ import annotations

import os
import secrets
import socket
import sys
import threading
import time

import uvicorn


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


# 로컬 API 서버. 127.0.0.1에서만 받고, 실행할 때마다 새 토큰을 씁니다.
# loop/http를 직접 지정해 uvicorn이 실행 중에 모듈을 찾아 불러오지 않게 합니다(앱 묶음에서 빠지지 않도록).
def build_server(port: int) -> uvicorn.Server:
    from .api import create_app

    config = uvicorn.Config(
        create_app(),
        host="127.0.0.1",
        port=port,
        loop="asyncio",
        http="h11",
        ws="none",
        lifespan="on",
        access_log=False,
        log_level="warning",
        log_config=None,
    )
    return uvicorn.Server(config)


def wait_until_started(server: uvicorn.Server, thread: threading.Thread, timeout: float = 15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and thread.is_alive():
        if server.started:
            return True
        time.sleep(0.05)
    return False


def main() -> int:
    token = secrets.token_urlsafe(32)
    os.environ["JOB_AGENT_API_TOKEN"] = token
    port = available_port()
    server = build_server(port)
    thread = threading.Thread(target=server.run, name="apply-local-api", daemon=True)
    thread.start()
    if not wait_until_started(server, thread):
        server.should_exit = True
        print("로컬 API를 시작하지 못했습니다.", file=sys.stderr)
        return 2
    try:
        from .desktop import run_desktop

        return run_desktop(f"http://127.0.0.1:{port}", token)
    finally:
        # 서버를 정상 종료해 lifespan 정리(브라우저 닫기, 민감 데이터 삭제)가 실행되게 합니다.
        server.should_exit = True
        thread.join(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
