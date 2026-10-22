# PyInstaller가 묶는 앱 시작 파일. 실제 시작 로직은 job_agent_core.launcher에 있습니다.
from job_agent_core.launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
