# [역할] 실행 데이터(작업 DB, 임시 브라우저 프로필)를 둘 폴더를 정합니다.
# 소스에서 실행하면 프로젝트의 .job_agent, 설치한 앱으로 실행하면 운영체제의 사용자 데이터 폴더를 씁니다.

from __future__ import annotations

import os
import sys
from pathlib import Path


APP_DIR_NAME = "ApplyLocal"


# 설치한 앱(PyInstaller로 묶은 실행 파일)으로 실행 중인지.
def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


# 운영체제별 사용자 데이터 폴더. 앱 안쪽은 쓰기 권한이 없을 수 있어 여기에 저장합니다.
def user_data_dir(platform: str | None = None, home: Path | None = None, environ: dict[str, str] | None = None) -> Path:
    platform = platform or sys.platform
    home = home or Path.home()
    environ = os.environ if environ is None else environ
    if platform == "darwin":
        return home / "Library" / "Application Support" / APP_DIR_NAME
    if platform.startswith("win"):
        base = environ.get("APPDATA")
        return Path(base) / APP_DIR_NAME if base else home / "AppData" / "Roaming" / APP_DIR_NAME
    base = environ.get("XDG_DATA_HOME")
    return Path(base) / APP_DIR_NAME if base else home / ".local" / "share" / APP_DIR_NAME


# JOB_AGENT_DATA_DIR가 있으면 그 값, 없으면 설치 앱은 사용자 데이터 폴더, 소스 실행은 .job_agent.
def default_data_dir() -> Path:
    configured = os.environ.get("JOB_AGENT_DATA_DIR")
    if configured:
        return Path(configured)
    if is_frozen():
        return user_data_dir()
    return Path(".job_agent")
