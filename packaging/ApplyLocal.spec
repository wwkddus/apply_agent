# PyInstaller 빌드 설정. 프로젝트 루트에서 실행합니다:
#   .venv/bin/pyinstaller packaging/ApplyLocal.spec --noconfirm
# 결과: macOS는 dist/Apply Local.app, Windows는 dist/Apply Local/Apply Local.exe
# 브라우저(Chromium)는 넣지 않습니다. 실행할 때 사용자의 Chrome 또는 Edge를 씁니다.

import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
APP_NAME = "Apply Local"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]

# Playwright 드라이버(node + JS 패키지)는 파이썬 import로 잡히지 않아 데이터로 함께 넣습니다.
datas = collect_data_files("playwright")
# 메뉴 막대 아이콘(SVG).
datas += [(str(ROOT / "src" / "job_agent_core" / "assets"), "job_agent_core/assets")]
hiddenimports = collect_submodules("uvicorn") + collect_submodules("job_agent_core")

a = Analysis(
    [str(ROOT / "packaging" / "apply_local_entry.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "uvloop", "httptools", "watchfiles", "websockets", "PySide6.QtWebEngineCore", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore", "PySide6.QtMultimedia"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME, upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        bundle_identifier="io.github.wwkddus.applylocal",
        version=VERSION,
        info_plist={
            "CFBundleShortVersionString": VERSION,
            "NSHighResolutionCapable": True,
        },
    )
