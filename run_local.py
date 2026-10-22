from __future__ import annotations

import os
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent


# 소스에서 실행할 때의 시작점. 실행 데이터는 프로젝트의 .job_agent에 둡니다.
def main() -> int:
    os.environ.setdefault("JOB_AGENT_DATA_DIR", str(PROJECT_DIR / ".job_agent"))
    sys.path.insert(0, str(PROJECT_DIR / "src"))
    from job_agent_core.launcher import main as launch

    return launch()


if __name__ == "__main__":
    raise SystemExit(main())
