import tempfile
import unittest
from pathlib import Path

from playwright.async_api import async_playwright

from job_agent_core.desktop import TEMPLATE_PATH
from job_agent_core.resume_parser import extract_resume_profile, inspect_resume_file


# 앱에 들어 있는 템플릿을 브라우저에서 고치고 PDF로 저장한 결과를 이력서 해석기가 그대로 읽는지 확인합니다.
class ResumeTemplateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        await self.page.goto(TEMPLATE_PATH.resolve().as_uri())

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()
        self.temp.cleanup()

    async def test_saved_pdf_parses_without_the_toolbar(self):
        pdf = Path(self.temp.name) / "resume.pdf"
        await self.page.pdf(path=str(pdf), format="A4")
        text = str(inspect_resume_file(pdf)["text"])
        self.assertNotIn("PDF로 저장", text)
        parsed = extract_resume_profile(text)
        profile = parsed["resume_profile"]
        self.assertEqual(parsed["warnings"], [])
        self.assertEqual(profile.personal.name.full, "홍길동")
        self.assertEqual(profile.personal.name_en.full, "Hong Gildong")
        levels = {record.education_level: record.school_name for record in profile.education}
        self.assertEqual(levels, {"high_school": "서울고등학교", "university": "서울대학교", "graduate_school": "서울대학교 대학원"})

    async def test_edits_are_kept_in_this_browser_only(self):
        cell = self.page.locator("td.value").first
        await cell.click()
        await self.page.keyboard.press("Meta+A" if await self.page.evaluate("navigator.platform.startsWith('Mac')") else "Control+A")
        await self.page.keyboard.type("김예시")
        await self.page.reload()
        self.assertEqual(await self.page.locator("td.value").first.text_content(), "김예시")
