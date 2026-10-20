import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from playwright.async_api import async_playwright

from job_agent_core.browser_session import BrowserLoginNotComplete, BrowserStatus
from job_agent_core.browser_worker import BrowserAutomationWorker
from job_agent_core.models import PersonalInfo, ResumeProfile


class FakeSessions:
    def __init__(self, page, folder: Path):
        self.page = page
        self.session = SimpleNamespace(
            blocked_hostname=None,
            approved_domains={"example.test"},
            discovered_frame_hosts=set(),
            page=page,
            target_hostname="example.test",
            profile_dir=folder,
        )

    def get(self, _task_id):
        return self.session

    async def application_page(self, _task_id):
        return self.page

    async def status(self, _task_id):
        return BrowserStatus(
            state="open",
            current_hostname="example.test",
            display_url="https://example.test/apply",
            page_title=await self.page.title(),
            can_inspect=True,
            discovered_frame_hosts=sorted(self.session.discovered_frame_hosts),
        )


class BrowserWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page(viewport={"width": 1280, "height": 900})
        self.sessions = FakeSessions(self.page, Path(self.temp.name))
        self.worker = BrowserAutomationWorker(self.sessions)
        self.profile = ResumeProfile(
            personal=PersonalInfo(
                name={"full": "홍길동"},
                birth_date="1990-02-03",
                phone="010-1234-5678",
                email="hong@example.com",
            ),
            skills="Python",
        )

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()
        self.temp.cleanup()

    async def test_inspect_fill_validate_custom_controls_and_never_submit(self):
        await self.page.set_content(
            """
            <!doctype html><html lang="ko"><head><title>지원서</title></head><body>
            <form id="application">
              <label for="name">성명</label><input id="name" value="기존 이름">
              <label for="phone">휴대폰번호</label><input id="phone" type="tel">
              <label for="email">이메일</label><input id="email" type="email">
              <div class="row">
                <label for="birth">생년월일</label>
                <input id="birth" readonly aria-haspopup="dialog" placeholder="날짜 선택">
                <button id="calendar-open" type="button">달력</button>
              </div>
              <label for="skills">보유기술</label><input id="skills" role="combobox" aria-autocomplete="list">
              <div id="skill-options" role="listbox" hidden><button type="button" role="option">Python</button></div>
              <button id="next" type="button">다음</button>
              <button id="submit" type="submit">최종 제출</button>
            </form>
            <div id="calendar" role="dialog" hidden>
              <select aria-label="연도"><option value="">연도</option><option value="1990">1990년</option></select>
              <select aria-label="월"><option value="">월</option><option value="2">2월</option></select>
              <div role="grid"><button type="button" role="gridcell">3</button></div>
            </div>
            <script>
              window.submitted = 0; window.nexted = 0;
              document.getElementById('application').addEventListener('submit', (event) => { event.preventDefault(); window.submitted += 1; });
              document.getElementById('next').addEventListener('click', () => { window.nexted += 1; });
              document.getElementById('skills').addEventListener('input', () => { document.getElementById('skill-options').hidden = false; });
              document.getElementById('skill-options').querySelector('[role=option]').addEventListener('click', (event) => {
                document.getElementById('skills').value = event.target.textContent.trim(); document.getElementById('skill-options').hidden = true;
              });
              document.getElementById('calendar-open').addEventListener('click', () => { document.getElementById('calendar').hidden = false; });
              document.getElementById('calendar').querySelector('[role=gridcell]').addEventListener('click', () => {
                document.getElementById('birth').value = '1990-02-03'; document.getElementById('calendar').hidden = true;
              });
            </script>
            </body></html>
            """
        )
        inspection = await self.worker.inspect("task", self.profile)
        self.assertEqual(inspection.submit_control_count, 1)
        self.assertTrue(inspection.safe_next_available)
        self.assertGreaterEqual(inspection.field_count, 5)

        report = await self.worker.fill("task", self.profile)
        self.assertGreaterEqual(report.filled_count, 4, report.model_dump())
        self.assertEqual(report.kept_existing_count, 1)
        self.assertEqual(await self.page.locator("#name").input_value(), "기존 이름")
        self.assertEqual(await self.page.locator("#phone").input_value(), "010-1234-5678")
        self.assertEqual(await self.page.locator("#email").input_value(), "hong@example.com")
        self.assertEqual(await self.page.locator("#birth").input_value(), "1990-02-03")
        self.assertEqual(await self.page.locator("#skills").input_value(), "Python")
        self.assertEqual(await self.page.evaluate("window.submitted"), 0)

        await self.worker.advance("task", self.profile)
        self.assertEqual(await self.page.evaluate("window.nexted"), 1)
        self.assertEqual(await self.page.evaluate("window.submitted"), 0)

    async def test_login_page_is_never_inspected_for_resume_filling(self):
        await self.page.set_content(
            """
            <label for="email">이메일</label><input id="email" autocomplete="username">
            <label for="password">비밀번호</label><input id="password" type="password" autocomplete="current-password">
            """
        )
        with self.assertRaises(BrowserLoginNotComplete):
            await self.worker.inspect("task", self.profile)


if __name__ == "__main__":
    unittest.main()
