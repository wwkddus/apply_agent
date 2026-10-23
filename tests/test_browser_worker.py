import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from playwright.async_api import async_playwright

from job_agent_core.browser_session import BrowserLoginNotComplete, BrowserSessionError, BrowserStatus
from job_agent_core.browser_worker import BrowserAutomationWorker
from job_agent_core.models import PersonalInfo, ResumeProfile
from job_agent_core.names import infer_name_parts
from job_agent_core.resume_llm import LlmUnavailable


def second_check(report):
    return [
        (item.fingerprint, item.status)
        for item in report.results
        if item.status in {"pending_lookup", "missing_resume_value", "needs_human"}
    ]


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
        progress = self.worker.fill_progress("task")
        self.assertFalse(progress.running)
        self.assertEqual(progress.done, progress.total)
        self.assertGreater(progress.total, 0)
        # 진행 상황에는 칸 이름과 상태만 있고 입력한 값은 없습니다.
        self.assertNotIn("hong@example.com", progress.model_dump_json())

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
        self.assertEqual(by_label["비상 연락처"].status, "user_only")
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
                    {"education_level": "high_school", "school_name": "예시고등학교"},
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
        by_path = {item.resume_path: item for item in report.results if item.resume_path}
        failures = {path: (item.status, item.reason) for path, item in by_path.items() if item.status != "filled_verified"}
        self.assertEqual(failures, {})
        # Every detected field has a row; out-of-scope ones say why instead of disappearing.
        self.assertEqual(len(report.results), report.field_count)
        others = sorted((item.label, item.status) for item in report.results if not item.resume_path)
        self.assertIn(("부서 *", "pending_lookup"), others)
        self.assertIn(("개인정보 수집 및 이용 동의 (필수)", "user_only"), others)
        self.assertEqual(
            set(by_path),
            {
                "person.name.full",
                "person.email",
                "person.phone",
                "person.birth_date",
                "education.high_school.school_name",
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
        self.assertEqual(await page.locator('[name$="highSchool.schoolName"]').input_value(), "예시고등학교")
        # Out of scope: work history, day/night, consent, submit.
        self.assertEqual(await page.locator('[name$="department"]').input_value(), "")
        self.assertFalse(await page.locator("#agree").is_checked())
        self.assertEqual(await page.evaluate("window.submitted"), 0)

    async def test_unnamed_button_group_follows_its_graduate_school_block(self):
        await self.page.set_content(
            """
            <section><h2>학력사항</h2>
              <div class="block">
                <div role="group"><label>입학구분 *</label>
                  <div role="radiogroup"><button type="button" role="radio" aria-checked="false">입학</button>
                  <button type="button" role="radio" aria-checked="false">편입</button></div></div>
                <div role="group"><label>학교명 *</label><input name="educationalBackground.universities.0.schoolName"></div>
              </div>
              <div class="block">
                <div role="group"><label>입학구분 *</label>
                  <div role="radiogroup"><button type="button" role="radio" aria-checked="false">입학</button>
                  <button type="button" role="radio" aria-checked="false">편입</button></div></div>
                <div role="group"><label>학교명 *</label><input name="educationalBackground.graduateSchools.0.schoolName"></div>
                <div role="group"><label>대학원 논문&amp;졸업작품</label><input id="thesis"></div>
              </div>
            </section>
            <script>
              document.querySelectorAll('[role=radio]').forEach((radio) => radio.addEventListener('click', () => {
                radio.parentElement.querySelectorAll('[role=radio]').forEach((item) => item.setAttribute('aria-checked', 'false'));
                radio.setAttribute('aria-checked', 'true');
              }));
            </script>
            """
        )
        profile = ResumeProfile.model_validate(
            {
                "education": [
                    {"education_level": "university", "school_name": "예시대학교", "admission_type": "입학",
                     "graduation_date": "2020-02"},
                    {"education_level": "graduate_school", "school_name": "예시대학원", "admission_type": "편입",
                     "graduation_date": "2022-02"},
                ]
            }
        )
        report = await self.worker.fill("task", profile)
        by_path = {item.resume_path: item.status for item in report.results if item.resume_path}
        self.assertEqual(by_path["education.university.admission_type"], "filled_verified")
        self.assertEqual(by_path["education.graduate_school.admission_type"], "filled_verified")
        blocks = self.page.locator(".block")
        self.assertEqual(await blocks.nth(0).locator("[aria-checked=true]").all_inner_texts(), ["입학"])
        self.assertEqual(await blocks.nth(1).locator("[aria-checked=true]").all_inner_texts(), ["편입"])
        [thesis] = [item for item in report.results if item.label.startswith("대학원 논문")]
        self.assertEqual(thesis.status, "user_only")
        self.assertEqual(await self.page.locator("#thesis").input_value(), "")

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

    GRADUATE_PAGE = """
        <section><h2>대학원</h2>
        <div role="group"><label>재학기간 *</label>
          <button type="button" name="educationalBackground.graduateSchools.0.enrollmentPeriod.endDate"
                  aria-haspopup="grid" data-placeholder="true" data-format="2026. 10" disabled>졸업일</button></div>
        <div role="group"><label id="st">졸업구분 *</label>
          <button type="button" role="combobox" aria-haspopup="listbox" aria-labelledby="st" data-placeholder-shown=""
                  name="educationalBackground.graduateSchools.0.completionStatus">선택</button></div>
        <div role="group"><label>학위구분 *</label>
          <div role="radiogroup"><button type="button" role="radio" aria-checked="false">석사</button>
          <button type="button" role="radio" aria-checked="false">박사</button></div></div>
        <div role="group"><label>전공</label>
          <input role="combobox" aria-autocomplete="list" name="educationalBackground.graduateSchools.0.majors.0"></div>
        </section>
        <div id="portal"></div>
        <script>
          const portal = document.getElementById('portal');
          const list = (labels, pick) => {
            portal.innerHTML = '';
            const box = document.createElement('div'); box.setAttribute('role', 'listbox');
            labels.forEach((text) => { const item = document.createElement('div'); item.setAttribute('role', 'option');
              item.textContent = text; item.onclick = () => { pick(text); portal.innerHTML = ''; }; box.appendChild(item); });
            portal.appendChild(box);
          };
          const date = document.querySelector('[aria-haspopup=grid]');
          const status = document.querySelector('[aria-haspopup=listbox]');
          status.onclick = () => list(['졸업', '재학'], (text) => {
            status.textContent = text; status.removeAttribute('data-placeholder-shown');
            date.disabled = text !== '졸업';
          });
          date.onclick = () => {
            portal.innerHTML = '';
            const input = document.createElement('input'); input.setAttribute('data-part', 'input');
            input.placeholder = date.dataset.format;
            input.onkeydown = (event) => { if (event.key === 'Enter') { date.textContent = input.value;
              date.removeAttribute('data-placeholder'); portal.innerHTML = ''; } };
            const content = document.createElement('div'); content.setAttribute('role', 'application');
            content.appendChild(input); portal.appendChild(content);
          };
          const major = document.querySelector('input[role=combobox]');
          major.oninput = () => { if (major.value) setTimeout(() => list(['컴퓨터공학', '컴퓨터공학'], (text) => { major.value = text; }), 200); };
          major.onkeydown = (event) => { if (event.key === 'Escape') portal.innerHTML = ''; };
        </script>
    """

    def graduate_profile(self, status):
        return ResumeProfile.model_validate(
            {
                "education": [
                    {
                        "education_level": "graduate_school",
                        "school_name": "예시대학교 대학원",
                        "graduation_status": status,
                        "graduation_date": "2027-02",
                        "major": "컴퓨터공학",
                    }
                ]
            }
        )

    async def test_field_enabled_by_a_later_choice_is_filled_in_a_second_pass(self):
        await self.page.set_content(self.GRADUATE_PAGE)
        report = await self.worker.fill("task", self.graduate_profile("졸업"))
        by_path = {item.resume_path: item for item in report.results}
        self.assertEqual(by_path["education.graduate_school.graduation_date"].status, "filled_verified")
        self.assertEqual(await self.page.locator("[aria-haspopup=grid]").inner_text(), "2027. 02")

    async def test_field_that_stays_disabled_is_not_counted_as_needing_the_user(self):
        await self.page.set_content(self.GRADUATE_PAGE)
        report = await self.worker.fill("task", self.graduate_profile("재학"))
        by_path = {item.resume_path: item for item in report.results}
        self.assertEqual(by_path["education.graduate_school.graduation_status"].status, "filled_verified")
        self.assertEqual(by_path["education.graduate_school.graduation_date"].status, "disabled_on_page")
        self.assertEqual(report.disabled_count, 1)
        # Missing degree: no guess, but the page's own options are offered for the review dialog.
        degree = by_path["education.graduate_school.degree"]
        self.assertEqual((degree.status, degree.candidates), ("missing_resume_value", ["석사", "박사"]))
        self.assertEqual(await self.page.locator("[role=radio][aria-checked=true]").count(), 0)

    async def test_identical_duplicate_options_are_accepted_only_when_the_user_picks_one(self):
        await self.page.set_content(self.GRADUATE_PAGE)
        profile = self.graduate_profile("졸업")
        report = await self.worker.fill("task", profile)
        [major] = [item for item in report.results if item.resume_path == "education.graduate_school.major"]
        self.assertEqual((major.status, major.candidates), ("needs_human", ["컴퓨터공학", "컴퓨터공학"]))
        result = await self.worker.fill_field(
            "task", profile, major.form_field, major.label, "컴퓨터공학", fingerprint=major.fingerprint, chosen=True
        )
        self.assertEqual(result.status, "filled_verified")
        self.assertEqual(await self.page.locator("input[role=combobox]").input_value(), "컴퓨터공학")

    async def test_review_fill_follows_the_fingerprint_when_positions_shift(self):
        await self.page.set_content(
            """
            <form id="f">
              <label for="a">캠퍼스 메모</label><input id="a" name="memo.first">
              <label for="b">캠퍼스 메모</label><input id="b" name="memo.second" value="keep">
            </form>
            """
        )
        inspection = await self.worker._prepare("task", self.profile)
        first = next(item for item in inspection.inspection.fields if item.name == "memo.first")
        # The page inserts a field above, so the old positional key now points at another field.
        await self.page.evaluate(
            "() => document.getElementById('f').insertAdjacentHTML('afterbegin', '<label for=n>캠퍼스 메모</label><input id=n>')"
        )
        result = await self.worker.fill_field(
            "task", self.profile, first.key, first.label, "서울캠퍼스", fingerprint=first.fingerprint
        )
        self.assertEqual(result.status, "filled_verified")
        values = [await self.page.locator(selector).input_value() for selector in ("#n", "#a", "#b")]
        self.assertEqual(values, ["", "서울캠퍼스", "keep"])

    async def test_lookup_fills_extra_fields_but_marks_them_for_review(self):
        await self.page.set_content(
            """
            <section><h2>경력사항</h2>
              <label for="company">회사명</label><input id="company" name="workHistory.workExperiences.0.companyName">
              <label for="army">병역구분</label>
              <select id="army"><option value="">선택</option><option value="D">군필</option><option value="N">미필</option></select>
              <label for="intro">자기소개</label><textarea id="intro"></textarea>
            </section>
            """
        )
        asked = []

        async def lookup(items):
            asked.extend(item.field.label for item in items)
            keys = {item.field.label: item.field.key for item in items}
            return {keys["회사명"]: "예시주식회사", keys["병역구분"]: "군필"}, {}

        first = await self.worker.fill("task", self.profile)
        self.assertEqual(await self.page.locator("#company").input_value(), "")
        self.assertEqual({item.label: item.status for item in first.results}["자기소개"], "user_only")
        report = await self.worker.fill_lookup("task", self.profile, lookup, second_check(first))
        # 병역 is now a resume item; with no value in this resume it is still asked about.
        self.assertEqual(sorted(asked), sorted(["회사명", "병역구분"]))
        by_label = {item.label: item for item in report.results}
        self.assertEqual(by_label["회사명"].status, "filled_llm_unconfirmed")
        self.assertEqual(by_label["병역구분"].status, "filled_llm_unconfirmed")
        self.assertEqual(report.lookup_filled_count, 2)
        self.assertEqual(await self.page.locator("#company").input_value(), "예시주식회사")
        self.assertEqual(await self.page.locator("#army").input_value(), "D")
        self.assertEqual(await self.page.locator("#intro").input_value(), "")

    async def test_unavailable_lookup_only_adds_a_notice(self):
        await self.page.set_content('<label for="company">회사명</label><input id="company">')

        async def lookup(items):
            raise LlmUnavailable("로컬 Ollama에 연결할 수 없습니다.")

        first = await self.worker.fill("task", self.profile)
        report = await self.worker.fill_lookup("task", self.profile, lookup, second_check(first))
        self.assertIn("건너뛰었습니다", report.notice)
        # The waiting row still gets a final state with the reason.
        [row] = report.results
        self.assertEqual(row.status, "not_in_resume")
        self.assertIn("건너뛰었습니다", row.reason)
        self.assertEqual(await self.page.locator("#company").input_value(), "")

    async def test_second_check_reads_broken_labels_and_converts_options(self):
        await self.page.set_content(
            """
            <section><h2>병역 / 취업우대</h2>
              <div data-scope="field"><label id="field::a::label">병역사항 *</label>
                <div role="group"><button type="button" role="combobox" aria-haspopup="listbox"
                  aria-labelledby="field::missing::label" data-placeholder-shown=""
                  name="militaryServicePreferentialEmploymentStatus.militaryService.militaryServiceStatus">선택</button></div></div>
              <div data-scope="field"><label>장애여부</label>
                <div role="radiogroup" id="toggle-group:x"><button type="button" role="radio" aria-checked="false">비대상</button>
                <button type="button" role="radio" aria-checked="false">대상</button></div></div>
            </section>
            <section><h2>학력</h2>
              <label for="status">졸업구분</label>
              <select id="status" name="educationalBackground.universities.0.completionStatus">
                <option value="">Select</option><option value="G">Finished</option><option value="E">Currently studying</option></select>
            </section>
            <div id="portal"></div>
            <script>
              const trigger = document.querySelector('[aria-haspopup=listbox]');
              trigger.onclick = () => {
                const portal = document.getElementById('portal'); portal.innerHTML = '';
                const box = document.createElement('div'); box.setAttribute('role', 'listbox');
                ['비대상', '군필', '미필', '면제', '복무중'].forEach((text) => {
                  const item = document.createElement('div'); item.setAttribute('role', 'option'); item.textContent = text;
                  item.onclick = () => { trigger.textContent = text; trigger.removeAttribute('data-placeholder-shown'); portal.innerHTML = ''; };
                  box.appendChild(item);
                });
                portal.appendChild(box);
              };
              document.addEventListener('keydown', (event) => { if (event.key === 'Escape') document.getElementById('portal').innerHTML = ''; });
              document.querySelectorAll('[role=radio]').forEach((radio) => radio.onclick = () => {
                document.querySelectorAll('[role=radio]').forEach((item) => item.setAttribute('aria-checked', 'false'));
                radio.setAttribute('aria-checked', 'true');
              });
            </script>
            """
        )
        profile = ResumeProfile.model_validate(
            {"education": [{"education_level": "university", "school_name": "예시대학교", "graduation_status": "재학"}]}
        )
        first = await self.worker.fill("task", profile)
        labels = {item.label: item for item in first.results}
        self.assertEqual(set(labels), {"병역사항 *", "장애여부", "졸업구분"})
        self.assertEqual(labels["졸업구분"].status, "needs_human")
        seen = {}

        async def lookup(items):
            seen.update({item.field.label: item for item in items})
            keys = {item.field.label: item.field.key for item in items}
            return {keys["병역사항 *"]: "비대상", keys["장애여부"]: "비대상", keys["졸업구분"]: "Currently studying"}, {}

        report = await self.worker.fill_lookup("task", profile, lookup, second_check(first))
        # The closed listbox was opened once to learn its options; the rule value is offered for conversion.
        self.assertEqual([option.text for option in seen["병역사항 *"].field.options][:2], ["비대상", "군필"])
        self.assertEqual(seen["졸업구분"].known_value, "재학")
        statuses = {item.label: (item.status, item.resume_path) for item in report.results}
        self.assertEqual(statuses["졸업구분"], ("filled_llm_unconfirmed", "education.university.graduation_status"))
        self.assertEqual(statuses["병역사항 *"][0], "filled_llm_unconfirmed")
        self.assertEqual(statuses["장애여부"][0], "filled_llm_unconfirmed")
        self.assertEqual(await self.page.locator("#status").input_value(), "E")
        self.assertEqual(await self.page.locator("[aria-haspopup=listbox]").inner_text(), "비대상")
        self.assertEqual(await self.page.locator("[role=radio][aria-checked=true]").inner_text(), "비대상")

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
