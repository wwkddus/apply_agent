import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from playwright.async_api import Error as PlaywrightError

from job_agent_core.app_paths import default_data_dir, user_data_dir
from job_agent_core.browser_session import BrowserSessionError, BrowserSessionManager, browser_missing


class FakeChromium:
    def __init__(self, missing: set[str | None], other_error: set[str | None] = frozenset()):
        self.missing = missing
        self.other_error = other_error
        self.tried: list[str | None] = []

    async def launch_persistent_context(self, **options):
        channel = options.get("channel")
        self.tried.append(channel)
        if channel in self.missing:
            raise PlaywrightError(f"Chromium distribution '{channel}' is not found at /Applications/x")
        if channel in self.other_error:
            raise PlaywrightError("Target page, context or browser has been closed")
        return f"context:{channel}"


class FakeRuntime:
    def __init__(self, chromium: FakeChromium):
        self.chromium = chromium


class AppPathTests(unittest.TestCase):
    def test_user_data_dir_per_platform(self):
        home = Path("/home/u")
        self.assertEqual(user_data_dir("darwin", home, {}), home / "Library/Application Support/ApplyLocal")
        self.assertEqual(user_data_dir("win32", home, {"APPDATA": "C:/Users/u/AppData/Roaming"}), Path("C:/Users/u/AppData/Roaming/ApplyLocal"))
        self.assertEqual(user_data_dir("linux", home, {}), home / ".local/share/ApplyLocal")

    def test_default_data_dir_prefers_env_then_frozen_app_folder(self):
        with mock.patch.dict(os.environ, {"JOB_AGENT_DATA_DIR": "/tmp/custom"}):
            self.assertEqual(default_data_dir(), Path("/tmp/custom"))
        # 환경 변수를 모두 지우면 Windows에서 홈 폴더(USERPROFILE)를 못 찾으므로 이 값만 뺍니다.
        environ = {key: value for key, value in os.environ.items() if key != "JOB_AGENT_DATA_DIR"}
        with mock.patch.dict(os.environ, environ, clear=True):
            self.assertEqual(default_data_dir(), Path(".job_agent"))
            with mock.patch.object(sys, "frozen", True, create=True):
                self.assertEqual(default_data_dir(), user_data_dir())


class BrowserFallbackTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.manager = BrowserSessionManager(self.root.name)

    def tearDown(self):
        self.root.cleanup()

    def launch(self, chromium: FakeChromium):
        return asyncio.run(self.manager._launch(FakeRuntime(chromium), Path(self.root.name)))

    def test_missing_message_detection(self):
        self.assertTrue(browser_missing(PlaywrightError("Executable doesn't exist at /x/chrome")))
        self.assertTrue(browser_missing(PlaywrightError("Chromium distribution 'chrome' is not found at /x")))
        self.assertFalse(browser_missing(PlaywrightError("Target closed")))

    def test_falls_back_to_installed_chrome_then_edge(self):
        chromium = FakeChromium(missing={None})
        self.assertEqual(self.launch(chromium), "context:chrome")
        chromium = FakeChromium(missing={None, "chrome"})
        self.assertEqual(self.launch(chromium), "context:msedge")
        self.assertEqual(chromium.tried, [None, "chrome", "msedge"])

    def test_no_browser_or_other_error_is_reported(self):
        with self.assertRaisesRegex(BrowserSessionError, "Chrome"):
            self.launch(FakeChromium(missing={None, "chrome", "msedge"}))
        chromium = FakeChromium(missing=set(), other_error={None})
        with self.assertRaisesRegex(BrowserSessionError, "시작하지 못했습니다"):
            self.launch(chromium)
        # 설치는 되어 있는데 실행이 실패한 경우에는 다른 브라우저로 넘어가지 않습니다.
        self.assertEqual(chromium.tried, [None])


if __name__ == "__main__":
    unittest.main()
