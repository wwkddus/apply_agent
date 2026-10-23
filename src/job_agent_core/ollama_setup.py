# [역할] AI 보조 기능(로컬 Ollama) 준비: 설치 여부 확인 → 사용자가 동의하면 공식 설치 파일을 받아
# 서명을 확인한 뒤 설치 → 실행 → 필요한 모델 받기. 화면(desktop.py)에서 백그라운드로 호출합니다.
#
# 안전 장치
# - 설치 파일은 ollama.com의 고정 주소(공식 GitHub 릴리스로 연결)에서만 받습니다.
# - 실행하거나 옮기기 전에 코드 서명을 확인합니다. macOS는 Apple 공증 + Ollama 개발자 팀 ID,
#   Windows는 Authenticode 서명이 유효하고 서명자가 Ollama Inc.인지 봅니다. 다르면 설치하지 않습니다.
# - Ollama에는 내 컴퓨터 주소(127.0.0.1)로만 연결합니다. 이력서·개인정보는 여기서 다루지 않습니다.

from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from .llm_mapping import DEFAULT_MAPPING_MODEL
from .resume_llm import DEFAULT_OLLAMA_MODEL

OLLAMA_URL = "http://127.0.0.1:11434"
# 칸 연결 모델과 이력서 보조 추출·2차 확인 모델.
REQUIRED_MODELS = (DEFAULT_MAPPING_MODEL.partition(":")[2], DEFAULT_OLLAMA_MODEL)
# 화면 안내용 대략적인 크기(바이트). 실제 크기는 받으면서 Ollama가 알려 줍니다.
MODEL_SIZES = {"qwen3:8b": 5_200_000_000, "qwen2.5:7b": 4_700_000_000}
INSTALLER_SIZES = {"darwin": 220_000_000, "win32": 1_700_000_000}
DOWNLOADS = {
    "darwin": "https://ollama.com/download/Ollama-darwin.zip",
    "win32": "https://ollama.com/download/OllamaSetup.exe",
}
# 2026-10-23 공식 설치 파일에서 확인한 서명자.
MAC_TEAM_ID = "3MU9H2V9Y9"
WINDOWS_SIGNER = "Ollama Inc."
MIN_MACOS = (14, 0)
RECOMMENDED_MEMORY = 16 * 1024**3
DISK_MARGIN = 1_000_000_000

Progress = Callable[[str, float | None], None]
Runner = Callable[..., subprocess.CompletedProcess]


class OllamaSetupError(RuntimeError):
    """사용자에게 그대로 보여 줄 수 있는 한국어 오류."""


class OllamaSetupCancelled(OllamaSetupError):
    pass


@dataclass
class OllamaStatus:
    installed: bool = False
    running: bool = False
    models: set[str] = field(default_factory=set)
    location: str = ""

    @property
    def missing_models(self) -> list[str]:
        return [name for name in REQUIRED_MODELS if not has_model(self.models, name)]

    @property
    def ready(self) -> bool:
        return self.running and not self.missing_models


def has_model(models: set[str], name: str) -> bool:
    return name in models or (":" not in name and f"{name}:latest" in models)


def platform_key(name: str | None = None) -> str:
    name = name or sys.platform
    return "win32" if name.startswith("win") else name


# ---------- 상태 확인 ----------


def installed_location(key: str | None = None, environ: dict[str, str] | None = None, home: Path | None = None) -> str:
    """Ollama 앱 또는 명령어 위치(없으면 빈 문자열). Homebrew로 설치한 명령어도 찾습니다."""
    key = platform_key(key)
    environ = os.environ if environ is None else environ
    home = home or Path.home()
    candidates: list[Path] = []
    if key == "darwin":
        candidates = [Path("/Applications/Ollama.app"), home / "Applications" / "Ollama.app",
                      Path("/opt/homebrew/bin/ollama"), Path("/usr/local/bin/ollama")]
    elif key == "win32":
        local = environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        candidates = [Path(local) / "Programs" / "Ollama" / "ollama app.exe", Path(local) / "Programs" / "Ollama" / "ollama.exe"]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which("ollama") or ""


def detect(transport: httpx.BaseTransport | None = None, location: str | None = None) -> OllamaStatus:
    location = installed_location() if location is None else location
    status = OllamaStatus(installed=bool(location), location=location)
    try:
        with httpx.Client(base_url=OLLAMA_URL, timeout=3, transport=transport, trust_env=False) as client:
            response = client.get("/api/tags")
            response.raise_for_status()
            status.models = {item.get("name", "") for item in response.json().get("models", [])}
    except (httpx.HTTPError, ValueError):
        return status
    status.running = status.installed = True
    return status


def total_memory() -> int:
    """설치된 메모리(바이트). 알 수 없으면 0."""
    try:
        if sys.platform == "darwin":
            return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout)
        if sys.platform.startswith("win"):
            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("available", ctypes.c_ulonglong), ("page_total", ctypes.c_ulonglong),
                            ("page_available", ctypes.c_ulonglong), ("virtual_total", ctypes.c_ulonglong),
                            ("virtual_available", ctypes.c_ulonglong), ("extended", ctypes.c_ulonglong)]

            memory = MemoryStatus()
            memory.length = ctypes.sizeof(MemoryStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory))  # type: ignore[attr-defined]
            return int(memory.total)
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return 0


def needed_space(status: OllamaStatus, key: str | None = None) -> int:
    key = platform_key(key)
    models = sum(MODEL_SIZES.get(name, 5_000_000_000) for name in status.missing_models)
    installer = 0 if status.installed else INSTALLER_SIZES.get(key, 0) * 2
    return models + installer


def unsupported_reason(key: str | None = None, mac_version: str | None = None) -> str:
    """이 컴퓨터에 자동 설치를 할 수 없는 이유(없으면 빈 문자열)."""
    key = platform_key(key)
    if key not in DOWNLOADS:
        return "이 운영체제에서는 자동 설치를 지원하지 않습니다. ollama.com에서 직접 설치해 주세요."
    if key == "darwin":
        version = mac_version if mac_version is not None else platform.mac_ver()[0]
        parts = tuple(int(part) for part in version.split(".")[:2] if part.isdigit())
        if parts and parts < MIN_MACOS:
            return "Ollama는 macOS 14(Sonoma) 이상에서 설치할 수 있습니다."
    return ""


def gigabytes(size: int) -> str:
    return f"{size / 1_000_000_000:.1f}GB"


# ---------- 설치 ----------


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise OllamaSetupCancelled("AI 기능 설치를 취소했습니다.")


def download(url: str, target: Path, progress: Progress, cancel: threading.Event | None = None,
             transport: httpx.BaseTransport | None = None) -> Path:
    """설치 파일을 받습니다. https 주소만 따라갑니다."""
    try:
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30, read=120), transport=transport) as client:
            with client.stream("GET", url) as response:
                if response.url.scheme != "https":
                    raise OllamaSetupError("설치 파일 주소가 안전하지 않아 받지 않았습니다.")
                response.raise_for_status()
                total = int(response.headers.get("content-length") or 0)
                done = 0
                with target.open("wb") as handle:
                    for chunk in response.iter_bytes(1024 * 256):
                        _check_cancel(cancel)
                        handle.write(chunk)
                        done += len(chunk)
                        progress(f"Ollama 받는 중 · {gigabytes(done)} / {gigabytes(total)}" if total else "Ollama 받는 중",
                                 done / total if total else None)
    except httpx.HTTPError as error:
        raise OllamaSetupError(f"Ollama 설치 파일을 받지 못했습니다. 인터넷 연결을 확인해 주세요. ({type(error).__name__})") from None
    return target


def verify_mac_app(app: Path, run: Runner = subprocess.run) -> None:
    """서명이 온전하고, Apple 공증을 받았고, Ollama 개발자 팀이 서명했는지 확인합니다."""
    strict = run(["codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True, text=True)
    details = run(["codesign", "-dv", "--verbose=2", str(app)], capture_output=True, text=True)
    gatekeeper = run(["spctl", "--assess", "--type", "execute", str(app)], capture_output=True, text=True)
    team = next((line.split("=", 1)[1].strip() for line in (details.stderr or "").splitlines()
                 if line.startswith("TeamIdentifier=")), "")
    if strict.returncode != 0 or gatekeeper.returncode != 0 or team != MAC_TEAM_ID:
        raise OllamaSetupError("받은 Ollama 앱의 서명을 확인하지 못해 설치하지 않았습니다.")


def verify_windows_installer(installer: Path, run: Runner = subprocess.run) -> None:
    script = (
        "$s = Get-AuthenticodeSignature -LiteralPath $args[0]; "
        "Write-Output $s.Status; Write-Output $s.SignerCertificate.Subject"
    )
    result = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script, str(installer)],
                 capture_output=True, text=True)
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    subject = lines[1] if len(lines) > 1 else ""
    signed_by_ollama = any(part.strip() in {f"O={WINDOWS_SIGNER}", f"CN={WINDOWS_SIGNER}"} for part in subject.split(","))
    if result.returncode != 0 or not lines or lines[0] != "Valid" or not signed_by_ollama:
        raise OllamaSetupError("받은 Ollama 설치 파일의 서명을 확인하지 못해 설치하지 않았습니다.")


def install_mac(archive: Path, work: Path, run: Runner = subprocess.run, home: Path | None = None) -> Path:
    extracted = work / "extracted"
    result = run(["ditto", "-x", "-k", str(archive), str(extracted)], capture_output=True, text=True)
    app = extracted / "Ollama.app"
    if result.returncode != 0 or not app.is_dir():
        raise OllamaSetupError("Ollama 설치 파일을 풀지 못했습니다.")
    verify_mac_app(app, run)
    # 관리자 권한 없이 쓸 수 있는 곳에 둡니다: /Applications가 막혀 있으면 내 홈의 Applications.
    folder = Path("/Applications")
    if not os.access(folder, os.W_OK):
        folder = (home or Path.home()) / "Applications"
        folder.mkdir(exist_ok=True)
    target = folder / "Ollama.app"
    if target.exists():
        raise OllamaSetupError(f"{target}가 이미 있습니다. 그 앱을 한 번 실행한 뒤 다시 시도해 주세요.")
    result = run(["ditto", str(app), str(target)], capture_output=True, text=True)
    if result.returncode != 0:
        raise OllamaSetupError("Ollama 앱을 응용 프로그램 폴더로 옮기지 못했습니다.")
    return target


def install_windows(installer: Path, run: Runner = subprocess.run) -> None:
    verify_windows_installer(installer, run)
    # Ollama 자체 설치 창이 뜹니다(관리자 권한 필요 없음). 설치가 끝날 때까지 기다립니다.
    result = run([str(installer)], capture_output=True, text=True)
    if result.returncode != 0:
        raise OllamaSetupError("Ollama 설치가 끝나지 않았습니다. 다시 시도해 주세요.")


def start(location: str, popen: Callable[..., object] = subprocess.Popen) -> None:
    """설치된 Ollama를 백그라운드로 실행합니다(이미 실행 중이면 호출하지 않음)."""
    if not location:
        raise OllamaSetupError("Ollama가 설치되어 있지 않습니다.")
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if location.endswith(".app"):
        popen(["open", "-g", "-a", location], **quiet)
    elif location.endswith("ollama app.exe"):
        popen([location], **quiet)
    else:
        # 명령어만 있는 설치(Homebrew 등): 앱을 꺼도 계속 돌도록 따로 띄웁니다.
        extra = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform.startswith("win") \
            else {"start_new_session": True}
        popen([location, "serve"], **quiet, **extra)


def wait_until_running(timeout: float = 60, cancel: threading.Event | None = None,
                       transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep) -> OllamaStatus:
    deadline = time.monotonic() + timeout
    while True:
        _check_cancel(cancel)
        status = detect(transport, location=installed_location())
        if status.running or time.monotonic() >= deadline:
            return status
        sleep(1)


def pull_model(name: str, progress: Progress, cancel: threading.Event | None = None,
               transport: httpx.BaseTransport | None = None, label: str = "") -> None:
    """Ollama에 모델을 받게 하고 진행률을 전합니다."""
    label = label or name
    layers: dict[str, tuple[int, int]] = {}
    try:
        with httpx.Client(base_url=OLLAMA_URL, timeout=httpx.Timeout(30, read=300), transport=transport, trust_env=False) as client:
            with client.stream("POST", "/api/pull", json={"model": name, "stream": True}) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    _check_cancel(cancel)
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if event.get("error"):
                        raise OllamaSetupError(f"모델 {name}을 받지 못했습니다: {event['error']}")
                    if event.get("digest") and event.get("total"):
                        layers[event["digest"]] = (int(event.get("completed") or 0), int(event["total"]))
                    done = sum(item[0] for item in layers.values())
                    total = sum(item[1] for item in layers.values())
                    if total:
                        progress(f"{label} 받는 중 · {gigabytes(done)} / {gigabytes(total)}", done / total)
                    else:
                        progress(f"{label} 준비 중", None)
                    if event.get("status") == "success":
                        return
    except httpx.HTTPError as error:
        raise OllamaSetupError(f"모델 {name}을 받지 못했습니다. 인터넷 연결을 확인해 주세요. ({type(error).__name__})") from None
    except ValueError:
        raise OllamaSetupError(f"모델 {name}을 받는 중 Ollama 응답을 읽지 못했습니다.") from None
    raise OllamaSetupError(f"모델 {name}을 끝까지 받지 못했습니다. 다시 시도해 주세요.")


def prepare(progress: Progress, cancel: threading.Event | None = None) -> OllamaStatus:
    """없으면 설치 → 실행 → 빠진 모델 받기. 이미 준비된 단계는 건너뜁니다."""
    progress("이 컴퓨터의 Ollama 확인 중", None)
    status = detect()
    if not status.installed:
        reason = unsupported_reason()
        if reason:
            raise OllamaSetupError(reason)
    free = shutil.disk_usage(Path.home()).free
    if free < needed_space(status) + DISK_MARGIN:
        raise OllamaSetupError(
            f"저장 공간이 부족합니다. {gigabytes(needed_space(status) + DISK_MARGIN)}가 필요하지만 {gigabytes(free)}만 남아 있습니다."
        )
    if not status.installed:
        key = platform_key()
        with tempfile.TemporaryDirectory(prefix="applylocal-ollama-") as folder:
            work = Path(folder)
            target = work / ("Ollama-darwin.zip" if key == "darwin" else "OllamaSetup.exe")
            download(DOWNLOADS[key], target, progress, cancel)
            _check_cancel(cancel)
            progress("서명 확인 후 설치 중", None)
            if key == "darwin":
                install_mac(target, work)
            else:
                install_windows(target)
        status = detect()
    if not status.running:
        progress("Ollama 실행 중", None)
        start(status.location or installed_location())
        status = wait_until_running(cancel=cancel)
        if not status.running:
            raise OllamaSetupError("Ollama를 실행하지 못했습니다. Ollama 앱을 직접 한 번 열어 본 뒤 다시 시도해 주세요.")
    missing = status.missing_models
    for index, name in enumerate(missing, 1):
        pull_model(name, progress, cancel, label=f"AI 모델 {index}/{len(missing)}")
    return detect()
