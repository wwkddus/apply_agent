import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from playwright.async_api import async_playwright

from job_agent_core.browser_session import BrowserLoginNotComplete, BrowserSessionError, BrowserStatus
from job_agent_core.browser_worker import BrowserAutomationWorker
from job_agent_core.models import PersonalInfo, ResumeProfile
from job_agent_core.names import infer_name_parts


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

    async def test_split_name_phone_boxes_retry_and_cleanup(self):
        await self.page.set_content(
            """
            <!doctype html><html lang="ko"><head><title>지원서</title></head><body>
            <form>
              <label for="family">성</label><input id="family">
              <label for="given">이름</label><input id="given">
              <div class="row">
                <label for="p1">휴대폰</label>
                <select id="p1"><option value="">선택</option><option>010</option><option>011</option></select>
                <input id="p2" maxlength="4" aria-label="휴대폰 가운데"><input id="p3" maxlength="4">
              </div>
              <label for="tel">비상 연락처</label><input id="tel">
              <label for="email">이메일</label><input id="email">
            </form>
            <script>
              const tel = document.getElementById('tel');
              tel.addEventListener('input', () => tel.setCustomValidity(tel.value.includes('-') ? 'no hyphen' : ''));
              const email = document.getElementById('email');
              email.addEventListener('input', () => { if (email.value && !email.value.endsWith('#')) email.value += '#'; });
            </script>
            </body></html>
            """
        )
        profile = ResumeProfile(
            personal=PersonalInfo(name=infer_name_parts("홍길동"), phone="010-1234-5678", email="hong@example.com")
        )
        report = await self.worker.fill("task", profile)
        by_label = {item.label: item for item in report.results}
        values = {
            selector: await self.page.locator(selector).input_value()
            for selector in ["#family", "#given", "#p1", "#p2", "#p3", "#email"]
        }
        self.assertEqual(values, {"#family": "홍", "#given": "길동", "#p1": "010", "#p2": "1234", "#p3": "5678", "#email": ""})
        # An emergency contact belongs to someone else and is never planned for the applicant's phone.
        self.assertNotIn("비상 연락처", by_label)
        self.assertEqual(await self.page.locator("#tel").input_value(), "")
        self.assertEqual(by_label["이메일"].status, "failed_validation")
        self.assertEqual(by_label["이메일"].attempts, 2)
        self.assertIn("지웠습니다", by_label["이메일"].reason)

    async def test_alternative_phone_format_is_tried_once(self):
        await self.page.set_content(
            """
            <label for="tel">휴대폰번호</label><input id="tel">
            <script>
              const tel = document.getElementById('tel');
              tel.addEventListener('input', () => tel.setCustomValidity(tel.value.includes('-') ? 'no hyphen' : ''));
            </script>
            """
        )
        report = await self.worker.fill("task", self.profile)
        [result] = [item for item in report.results if item.resume_path == "person.phone"]
        self.assertEqual((result.status, result.attempts), ("filled_verified", 2))
        self.assertEqual(await self.page.locator("#tel").input_value(), "01012345678")

    async def test_component_library_form_with_aria_widgets(self):
        await self.page.set_content((Path(__file__).parent / "fixtures" / "aria_application_form.html").read_text(encoding="utf-8"))
        profile = ResumeProfile.model_validate(
            {
                "personal": {
                    "name": infer_name_parts("홍길동").model_dump(),
                    "phone": "010-1234-5678",
                    "email": "hong@example.com",
                    "birth_date": "1990-02-03",
                },
                "education": [
                    {
                        "education_level": "university",
                        "school_name": "예시대학교",
                        "degree": "학사",
                        "major": "컴퓨터공학",
                        "admission_type": "입학",
                        "admission_date": "2016-03",
                        "graduation_status": "졸업",
                        "graduation_date": "2020-02",
                        "gpa": "3.77",
                        "gpa_scale": "4.5",
                    }
                ],
            }
        )
        report = await self.worker.fill("task", profile)
        by_path = {item.resume_path: item for item in report.results}
        failures = {path: (item.status, item.reason) for path, item in by_path.items() if item.status != "filled_verified"}
        self.assertEqual(failures, {})
        self.assertEqual(
            set(by_path),
            {
                "person.name.full",
                "person.email",
                "person.phone",
                "person.birth_date",
                "education.university.school_name",
                "education.university.degree",
                "education.university.admission_date",
                "education.university.graduation_date",
                "education.university.admission_type",
                "education.university.graduation_status",
                "education.university.gpa",
                "education.university.gpa_scale",
                "education.university.major",
            },
        )
        page = self.page
        self.assertEqual(await page.locator('[name="basicInformation.birthdate"]').inner_text(), "1990. 02. 03")
        self.assertEqual(await page.locator('[name$="startDate"]').inner_text(), "2016. 03")
        self.assertEqual(await page.locator('[name$="endDate"]').inner_text(), "2020. 02")
        self.assertEqual(await page.locator('[name$="universities.0.schoolName"]').input_value(), "예시대학교")
        self.assertEqual(await page.locator('[name$="completionStatus"]').inner_text(), "졸업")
        self.assertEqual(await page.locator('[name$="scoreScale"]').inner_text(), "4.5")
        self.assertEqual(await page.locator('[role=radio][aria-checked=true]').all_inner_texts(), ["학사", "입학"])
        # Out of scope: high school, work history, day/night, consent, submit.
        self.assertEqual(await page.locator('[name$="highSchool.schoolName"]').input_value(), "")
        self.assertEqual(await page.locator('[name$="department"]').input_value(), "")
        self.assertFalse(await page.locator("#agree").is_checked())
        self.assertEqual(await page.evaluate("window.submitted"), 0)

    async def test_calendar_button_that_already_shows_a_date_is_kept(self):
        await self.page.set_content((Path(__file__).parent / "fixtures" / "aria_application_form.html").read_text(encoding="utf-8"))
        await self.page.evaluate(
            """() => {
                const button = document.querySelector('[name="basicInformation.birthdate"]');
                button.removeAttribute('data-placeholder');
                button.textContent = '1985. 01. 01';
            }"""
        )
        profile = ResumeProfile.model_validate({"personal": {"birth_date": "1990-02-03"}})
        report = await self.worker.fill("task", profile)
        [result] = [item for item in report.results if item.resume_path == "person.birth_date"]
        self.assertEqual(result.status, "kept_existing")
        self.assertEqual(await self.page.locator('[name="basicInformation.birthdate"]').inner_text(), "1985. 01. 01")

    async def test_ambiguous_search_option_returns_candidates(self):
        await self.page.set_content(
            """
            <div role="group"><label>학교명</label>
            <input role="combobox" aria-autocomplete="list" name="educationalBackground.universities.0.schoolName"></div>
            <div id="portal"></div>
            <script>
              const input = document.querySelector('input');
              input.addEventListener('input', () => setTimeout(() => {
                document.getElementById('portal').innerHTML =
                  '<div role="listbox"><div role="option">예시대학교 서울캠퍼스</div><div role="option">예시대학교 지방캠퍼스</div></div>';
              }, 300));
            </script>
            """
        )
        profile = ResumeProfile.model_validate(
            {"education": [{"education_level": "university", "school_name": "예시대학교"}]}
        )
        report = await self.worker.fill("task", profile)
        [result] = report.results
        self.assertEqual(result.status, "needs_human")
        self.assertEqual(result.candidates, ["예시대학교 서울캠퍼스", "예시대학교 지방캠퍼스"])
        self.assertEqual(await self.page.locator("input").input_value(), "")

    async def test_user_chosen_value_is_filled_and_consent_is_refused(self):
        await self.page.set_content(
            """
            <label for="campus">캠퍼스 메모</label><input id="campus" value="old">
            <label for="agree">개인정보 수집 동의</label><input id="agree" type="checkbox">
            """
        )
        inspection = await self.worker.inspect("task", self.profile)
        keys = {item.label: item.form_field for item in inspection.decisions}
        result = await self.worker.fill_field("task", self.profile, keys["캠퍼스 메모"], "캠퍼스 메모", "서울캠퍼스")
        self.assertEqual(result.status, "filled_verified")
        self.assertEqual(await self.page.locator("#campus").input_value(), "서울캠퍼스")
        with self.assertRaises(BrowserSessionError):
            await self.worker.fill_field("task", self.profile, keys["개인정보 수집 동의"], "개인정보 수집 동의", "yes")
        with self.assertRaises(BrowserSessionError):
            await self.worker.fill_field("task", self.profile, keys["캠퍼스 메모"], "다른 라벨", "x")
        self.assertFalse(await self.page.locator("#agree").is_checked())

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
