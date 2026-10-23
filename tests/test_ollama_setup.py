import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from job_agent_core import ollama_setup
from job_agent_core.desktop import AI_CHOICE_KEY, ApplyLocalWindow, DesktopApiClient
from job_agent_core.ollama_setup import OllamaSetupCancelled, OllamaSetupError, OllamaStatus


def done(stdout: str = "", stderr: str = "", code: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, stdout, stderr)


def tags(*names: str) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(200, json={"models": [{"name": n} for n in names]}))


def offline(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


# 설치·다운로드는 모두 가짜(MockTransport, 가짜 실행 함수)로 확인합니다. 실제로 아무것도 받거나 설치하지 않습니다.
class StatusTests(unittest.TestCase):
    def test_running_ollama_lists_models(self):
        status = ollama_setup.detect(tags("qwen3:8b", "llama3:latest"), location="")
        self.assertTrue(status.running and status.installed)
        self.assertEqual(status.missing_models, ["qwen2.5:7b"])
        self.assertFalse(status.ready)
        self.assertTrue(ollama_setup.detect(tags("qwen3:8b", "qwen2.5:7b"), location="").ready)

    def test_installed_but_not_running(self):
        status = ollama_setup.detect(httpx.MockTransport(offline), location="/Applications/Ollama.app")
        self.assertEqual((status.installed, status.running), (True, False))
        status = ollama_setup.detect(httpx.MockTransport(offline), location="")
        self.assertFalse(status.installed)

    def test_windows_location_and_space_and_macos_version(self):
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder) / "Programs" / "Ollama" / "ollama app.exe"
            app.parent.mkdir(parents=True)
            app.touch()
            self.assertEqual(ollama_setup.installed_location("win32", {"LOCALAPPDATA": folder}), str(app))
        fresh = OllamaStatus()
        self.assertEqual(ollama_setup.needed_space(fresh, "darwin"), 5_200_000_000 + 4_700_000_000 + 440_000_000)
        self.assertEqual(ollama_setup.needed_space(OllamaStatus(installed=True, models={"qwen3:8b", "qwen2.5:7b"})), 0)
        self.assertIn("macOS 14", ollama_setup.unsupported_reason("darwin", "13.6"))
        self.assertEqual(ollama_setup.unsupported_reason("darwin", "14.2"), "")
        self.assertIn("지원하지 않습니다", ollama_setup.unsupported_reason("linux"))


class SignatureTests(unittest.TestCase):
    def mac_runner(self, team="3MU9H2V9Y9", gatekeeper=0, strict=0):
        def run(command, **_):
            if command[:2] == ["codesign", "--verify"]:
                return done(code=strict)
            if command[0] == "codesign":
                return done(stderr=f"Identifier=com.electron.ollama\nTeamIdentifier={team}\n")
            return done(code=gatekeeper)
        return run

    def test_mac_app_must_be_notarized_and_signed_by_ollama(self):
        ollama_setup.verify_mac_app(Path("/tmp/Ollama.app"), self.mac_runner())
        for runner in (self.mac_runner(team="ABCDE12345"), self.mac_runner(gatekeeper=3), self.mac_runner(strict=1)):
            with self.assertRaises(OllamaSetupError):
                ollama_setup.verify_mac_app(Path("/tmp/Ollama.app"), runner)

    def test_windows_installer_must_be_validly_signed_by_ollama(self):
        good = "Valid\nCN=Ollama Inc., O=Ollama Inc., L=Palo Alto, S=California, C=US\n"
        ollama_setup.verify_windows_installer(Path("C:/x.exe"), lambda *a, **k: done(good))
        for output in ("NotSigned\n\n", "Valid\nCN=Someone Else, O=Someone Else\n", "HashMismatch\nCN=Ollama Inc.\n"):
            with self.assertRaises(OllamaSetupError):
                ollama_setup.verify_windows_installer(Path("C:/x.exe"), lambda *a, o=output, **k: done(o))

    def test_installer_is_not_run_when_the_signature_fails(self):
        calls = []

        def run(command, **_):
            calls.append(command)
            return done("NotSigned\n")

        with self.assertRaises(OllamaSetupError):
            ollama_setup.install_windows(Path("C:/OllamaSetup.exe"), run)
        self.assertEqual(len(calls), 1)  # 서명 확인만 하고 설치 파일은 실행하지 않았습니다.


class DownloadAndPullTests(unittest.TestCase):
    def test_download_reports_progress_and_can_be_cancelled(self):
        body = b"x" * 1000
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body, headers={"content-length": "1000"}))
        updates = []
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "OllamaSetup.exe"
            ollama_setup.download("https://ollama.com/download/OllamaSetup.exe", target, lambda *u: updates.append(u),
                                  transport=transport)
            self.assertEqual(target.read_bytes(), body)
            self.assertEqual(updates[-1][1], 1.0)
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(OllamaSetupCancelled):
                ollama_setup.download("https://ollama.com/download/OllamaSetup.exe", target, lambda *u: None, cancel, transport)

    def test_plain_http_download_is_refused(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x"))
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(OllamaSetupError):
            ollama_setup.download("http://ollama.com/download/x", Path(folder) / "x", lambda *u: None, transport=transport)

    def pull(self, lines, cancel=None):
        body = "\n".join(json.dumps(line) for line in lines).encode()
        updates = []
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
        ollama_setup.pull_model("qwen3:8b", lambda *u: updates.append(u), cancel, transport, label="AI 모델 1/2")
        return updates

    def test_pull_model_progress_success_and_errors(self):
        updates = self.pull([
            {"status": "pulling manifest"},
            {"status": "pulling a", "digest": "a", "total": 100, "completed": 50},
            {"status": "pulling a", "digest": "a", "total": 100, "completed": 100},
            {"status": "success"},
        ])
        self.assertEqual([value for _, value in updates], [None, 0.5, 1.0, 1.0])
        self.assertTrue(updates[1][0].startswith("AI 모델 1/2 받는 중"))
        with self.assertRaises(OllamaSetupError):
            self.pull([{"error": "pull model manifest: file does not exist"}])
        with self.assertRaises(OllamaSetupError):
            self.pull([{"status": "pulling manifest"}])  # 끝(success)까지 오지 않음

    def test_start_uses_the_app_or_the_command(self):
        calls = []
        ollama_setup.start("/Applications/Ollama.app", lambda command, **_: calls.append(command))
        ollama_setup.start("/opt/homebrew/bin/ollama", lambda command, **_: calls.append(command))
        self.assertEqual(calls, [["open", "-g", "-a", "/Applications/Ollama.app"], ["/opt/homebrew/bin/ollama", "serve"]])


class PrepareTests(unittest.TestCase):
    def test_installed_and_running_only_pulls_missing_models(self):
        statuses = [OllamaStatus(True, True, {"qwen3:8b"}, "/x"), OllamaStatus(True, True, {"qwen3:8b", "qwen2.5:7b"}, "/x")]
        with mock.patch.object(ollama_setup, "detect", side_effect=statuses), \
                mock.patch.object(ollama_setup, "pull_model") as pull, \
                mock.patch.object(ollama_setup, "download") as download:
            result = ollama_setup.prepare(lambda *u: None)
        self.assertTrue(result.ready)
        download.assert_not_called()
        self.assertEqual([call.args[0] for call in pull.call_args_list], ["qwen2.5:7b"])

    def test_unsupported_computer_is_explained_before_downloading(self):
        with mock.patch.object(ollama_setup, "detect", return_value=OllamaStatus()), \
                mock.patch.object(ollama_setup, "unsupported_reason", return_value="macOS 14 이상"), \
                mock.patch.object(ollama_setup, "download") as download, self.assertRaises(OllamaSetupError):
            ollama_setup.prepare(lambda *u: None)
        download.assert_not_called()


class FakeClient(DesktopApiClient):
    def __init__(self):
        super().__init__("http://127.0.0.1:1", "x" * 24)
        self.ai: list[bool] = []

    def set_ai(self, enabled):
        self.ai.append(enabled)
        return {"enabled": enabled}


class FirstRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.folder.name) / "settings.ini"), QSettings.Format.IniFormat)
        self.client = FakeClient()
        self.window = ApplyLocalWindow(self.client)
        # 백그라운드 확인(실제 Ollama 연결)은 하지 않고, 무엇을 부탁했는지만 기록합니다.
        self.background: list[object] = []
        self.window._background = lambda function, success: self.background.append(function)

    def tearDown(self):
        self.window.close()
        self.folder.cleanup()

    def test_first_run_opens_the_ai_page_and_rules_only_is_remembered(self):
        self.window.start_ai(self.settings)
        self.assertEqual(self.window.stage, "ai")
        self.assertEqual(len(self.background), 1)  # Ollama 상태 확인
        self.assertFalse(self.window.stepper.isVisibleTo(self.window))
        self.window._ai_detected((OllamaStatus(), 8 * 1024**3))
        self.assertIn("Ollama 앱", self.window.ai_detail.text())
        self.assertIn("16GB 이상 권장", self.window.ai_detail.text())
        self.assertEqual(self.window.ai_install_button.text(), "설치하고 사용 (권장)")
        self.window._choose_rules_only()
        self.assertEqual(self.settings.value(AI_CHOICE_KEY), "off")
        self.assertEqual(self.window.stage, "upload")

    def test_a_finished_install_turns_ai_on_and_returns(self):
        self.window.ai_settings = self.settings
        self.window.open_ai_setup()
        self.window._ai_ready(OllamaStatus(True, True, {"qwen3:8b", "qwen2.5:7b"}, "/x"))
        self.assertEqual(self.settings.value(AI_CHOICE_KEY), "on")
        self.assertEqual(self.window.stage, "upload")

    def test_a_failed_install_keeps_the_choice_open(self):
        self.window.ai_settings = self.settings
        self.window.open_ai_setup()
        self.window._ai_detected((OllamaStatus(), 0))
        self.window._ai_failed("받은 Ollama 앱의 서명을 확인하지 못해 설치하지 않았습니다.")
        self.assertIsNone(self.settings.value(AI_CHOICE_KEY))
        self.assertEqual(self.window.ai_install_button.text(), "다시 시도")
        self.assertIn("서명", self.window.ai_progress_label.text())

    def test_saved_choice_skips_the_page(self):
        self.settings.setValue(AI_CHOICE_KEY, "off")
        self.window.start_ai(self.settings)
        self.assertEqual(self.window.stage, "upload")
        self.background[0]()  # 저장된 선택(끄기)을 로컬 API에 알립니다.
        self.assertEqual(self.client.ai, [False])
