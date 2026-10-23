import tempfile
import unittest
from pathlib import Path

from playwright.async_api import async_playwright

from job_agent_core.browser_worker import BrowserAutomationWorker
from job_agent_core.models import EducationRecord, PersonalInfo, ResumeProfile
from job_agent_core.resume_lookup import LookupAnswers, LookupItem, validate_answers
from job_agent_core.mapping import FormField
from job_agent_core.resume_parser import extract_resume_profile
from tests.test_browser_worker import FakeSessions

# Synthetic copy of a large Korean group's form structure (no real values): radios hidden at 0x0
# behind styled labels, a question written as the text of a wrapping div, and education blocks
# that only appear after an 'add' button clones a hidden template.
FORM = """
<!doctype html><html lang="ko"><head><style>
  .form-btn-selector { display: inline-block; padding: 8px 20px; border: 1px solid #ccc; position: relative; }
  .form-btn-selector input { position: absolute; width: 0; height: 0; margin: 0; padding: 0; border: 0; }
  #EducationUniv_Item { display: none; }
</style><title>지원서</title></head><body>
<div class="apply-form-box">
  <div class="btn-selector-container">
    <div class="form-label">보훈 정보</div>
    <div class="btn-selector-wrapper">
      <label class="form-btn-selector"><input type="radio" name="prsVeteranBenefitYN" value="N"><span class="label">비대상</span></label>
      <label class="form-btn-selector"><input type="radio" name="prsVeteranBenefitYN" value="Y"><span class="label">대상</span></label>
    </div>
  </div>
  <div class="btn-selector-container">
    <div class="form-label">병역 사항</div>
    <div class="btn-selector-wrapper">
      <label class="form-btn-selector"><input type="radio" name="prsMilitarySvcYN" value="N" checked><span class="label">비대상</span></label>
      <label class="form-btn-selector"><input type="radio" name="prsMilitarySvcYN" value="Y"><span class="label">대상</span></label>
    </div>
  </div>
  <div class="btn-selector-container">
    <div class="form-label">장애정보</div>
    <div class="btn-selector-wrapper">
      <label class="form-btn-selector"><input type="radio" name="prsDisabledYN" value="N"><span class="label">비대상</span></label>
      <label class="form-btn-selector"><input type="radio" name="prsDisabledYN" value="Y"><span class="label">대상</span></label>
    </div>
  </div>
  <div class="form-item-group">
    <div class="form-item-sub-title">입사가능일</div>
    <div class="form-label">현 시점 기준 입사가능일자를 기재 부탁드립니다. (YYYY-MM-DD)
      <div class="input-cover"><div class="cover-inner">
        <input type="text" name="prsAnswerDesc_12" title="관리자에서 주관식 / 한 줄 입력 선택으로 등록한 질문입니다">
      </div></div>
    </div>
  </div>
</div>
<div class="apply-form-box education-root" id="applyContentAcademic">
  <h2 class="title">학력</h2>
  <div class="selectric-container"><div class="select-label">최종 학력 *</div>
    <select name="eduLastestEducationType" id="eduLastestEducationType">
      <option value="">Highest level of education *</option><option value="H">High school</option>
      <option value="C">College (diploma)</option><option value="U">University (bachelor’s)</option>
      <option value="M">Postgraduate school (master’s)</option><option value="D">Postgraduate school (doctoral)</option>
    </select></div>
  <div id="blocks"></div>
  <button type="button" class="btnAddEducationHigh">고등학교 학력 정보 추가 +</button>
  <button type="button" class="btnAddEducationUniv">대학 학력 정보 추가 +</button>
  <button type="button" class="btnAddEducationGrad">대학원 학력 정보 추가 +</button>
  <button type="button" id="save">임시저장</button>
  <button type="submit" id="submit">제출하기</button>
</div>
<div id="EducationUniv_Item"><div class="form-item-group educationuniv-item">
  <div class="form-item"><div class="form-label">University Name 1 *</div><input type="text" name="eduEducationName"></div>
  <div class="form-item"><div class="form-label">University Major 1 *</div><input type="text" name="eduMajor"></div>
  <div class="form-item"><div class="form-label">Admission date 1 *</div><input type="text" name="eduFromDate" placeholder="예) 2015.03"></div>
  <div class="form-item"><div class="form-label">Graduate date 1 *</div><input type="text" name="eduToDate" placeholder="예) 2015.03"></div>
  <div class="btn-selector-container"><div class="form-label">Transfer</div><div class="btn-selector-wrapper">
    <label class="form-btn-selector"><input type="radio" name="eduMajorTransferYN" value="N"><span class="label">Not applicable</span></label>
    <label class="form-btn-selector"><input type="radio" name="eduMajorTransferYN" value="Y"><span class="label">Applicable</span></label>
  </div></div>
  <button type="button" class="delete">Delete</button>
</div></div>
<script>
  window.clicked = [];
  document.querySelectorAll('button').forEach((button) => button.addEventListener('click', () => window.clicked.push(button.textContent.trim())));
  document.querySelector('.btnAddEducationUniv').addEventListener('click', () => {
    document.getElementById('blocks').appendChild(document.querySelector('#EducationUniv_Item > div').cloneNode(true));
  });
</script></body></html>
"""


class SkLikeFormTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        self.worker = BrowserAutomationWorker(FakeSessions(self.page, Path(self.temp.name)))
        self.profile = ResumeProfile(
            personal=PersonalInfo(name={"full": "홍길동"}, email="hong@example.com", disability_status="비대상"),
            education=[
                EducationRecord(
                    education_level="university",
                    school_name="한국대학교",
                    major="컴퓨터공학",
                    admission_date="2015-03",
                    graduation_date="2019-02",
                )
            ],
        )

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()
        self.temp.cleanup()

    async def test_hidden_radios_question_label_and_education_block(self):
        await self.page.set_content(FORM)
        report = await self.worker.fill("task", self.profile)
        rows = {item.label: item for item in report.results}

        # Radios hidden behind styled labels are detected and named by their heading.
        self.assertEqual(rows["장애정보"].status, "filled_review", rows["장애정보"].reason)
        self.assertTrue(await self.page.locator("input[name=prsDisabledYN][value=N]").is_checked())
        self.assertEqual(rows["병역 사항"].status, "kept_existing")
        self.assertEqual(rows["보훈 정보"].status, "missing_resume_value")

        # The start-date question is read as its label and left to the applicant.
        question = next(item for item in report.results if "입사가능일" in item.label)
        self.assertEqual(question.status, "user_only")
        self.assertEqual(await self.page.locator("input[name=prsAnswerDesc_12]").input_value(), "")

        # The university block is opened with its add button and filled; 최종 학력 is computed.
        self.assertIn("대학 학력", report.notice)
        self.assertEqual(await self.page.locator("#blocks input[name=eduEducationName]").input_value(), "한국대학교")
        self.assertEqual(await self.page.locator("#blocks input[name=eduMajor]").input_value(), "컴퓨터공학")
        self.assertEqual(await self.page.locator("#blocks input[name=eduFromDate]").input_value(), "2015.03")
        self.assertEqual(await self.page.locator("#blocks input[name=eduToDate]").input_value(), "2019.02")
        self.assertEqual(await self.page.locator("#eduLastestEducationType").input_value(), "U")
        # Yes/no choices inside the block are the applicant's.
        self.assertFalse(await self.page.locator("#blocks input[name=eduMajorTransferYN]:checked").count())

        # Only the university add button was clicked: no high school, graduate, save or submit.
        self.assertEqual(await self.page.evaluate("window.clicked"), ["대학 학력 정보 추가 +"])

    async def test_graduate_school_without_degree_is_not_guessed(self):
        profile = self.profile.model_copy(
            update={
                "education": [
                    *self.profile.education,
                    EducationRecord(education_level="graduate_school", school_name="한국대학원"),
                ]
            }
        )
        await self.page.set_content(FORM)
        report = await self.worker.fill("task", profile)
        highest = next(item for item in report.results if item.resume_path == "education.highest_level")
        self.assertEqual(highest.status, "needs_human")
        self.assertEqual(await self.page.locator("#eduLastestEducationType").input_value(), "")


    async def test_jquery_ui_school_search_commits_a_listed_school(self):
        # 실측(SK): 학교명·전공은 jQuery UI autocomplete. ARIA 역할 없이 class만 있고, 목록은 입력 뒤 서버에서 옵니다.
        await self.page.set_content(
            """
            <div class="form-item"><label for="school" class="form-label">대학원 학교명</label>
              <input type="text" id="school" name="edugdEducationName" class="form-input ui-autocomplete-input" autocomplete="off"></div>
            <ul id="menu" class="ui-menu ui-widget ui-autocomplete" style="display:none"></ul>
            <script>
              const input = document.getElementById('school');
              const menu = document.getElementById('menu');
              const schools = ['한국대학교 대학원', '한국대학교 교육대학원', '서울대학교 대학원'];
              window.picked = 0;
              input.addEventListener('input', () => setTimeout(() => {
                menu.innerHTML = '';
                for (const name of schools.filter((item) => item.includes(input.value.slice(0, 2)))) {
                  const li = document.createElement('li');
                  li.className = 'ui-menu-item';
                  li.innerHTML = '<div class="ui-menu-item-wrapper"></div>';
                  li.firstChild.textContent = name;
                  li.addEventListener('click', () => { input.value = name; menu.style.display = 'none'; window.picked += 1; });
                  menu.appendChild(li);
                }
                menu.style.display = menu.children.length ? 'block' : 'none';
              }, 400));
            </script>
            """
        )
        profile = ResumeProfile(education=[EducationRecord(education_level="graduate_school", school_name="한국대학교 대학원")])
        report = await self.worker.fill("task", profile)
        [row] = [item for item in report.results if item.resume_path == "education.graduate_school.school_name"]
        self.assertEqual(row.status, "filled_verified", row.reason)
        self.assertEqual(await self.page.locator("#school").input_value(), "한국대학교 대학원")
        # Chosen from the list (the site's select handler ran), not just typed.
        self.assertEqual(await self.page.evaluate("window.picked"), 1)


    async def test_application_choice_selects_are_named_by_their_caption_and_left_to_the_user(self):
        # 실측(SK): select에 label이 없고 옆 div.select-label에 문구, title에도 같은 뜻이 있습니다.
        # 지원 직무·근무 지역·고용형태·지원 경로는 이번 지원에 대한 본인의 선택이라 Agent가 고르지 않습니다.
        rows = []
        for name, caption, title in (
            ("appJobRoleLevel1", "지원 직무", "지원 직무(1지망)"),
            ("appWorkingRegion", "근무 지역", "근무 지역(1지망)"),
            ("appWorkingType", "고용형태", "고용형태"),
            ("appApplyPath", "지원 경로", "지원 경로"),
        ):
            rows.append(
                f'<div class="form-item"><div class="selectric-container">'
                f'<div class="select-label">{caption}<span class="required">*</span></div>'
                f'<select name="{name}" class="selectric medium float required" title="{title}">'
                f'<option value="">선택</option><option value="A">A</option><option value="B">B</option></select>'
                f'</div></div>'
            )
        await self.page.set_content("<div class='apply-form-box'>" + "".join(rows) + "</div>")
        report = await self.worker.fill("task", self.profile)
        by_label = {item.label.replace(" ", ""): item.status for item in report.results}
        self.assertEqual(
            by_label,
            {"지원직무*": "user_only", "근무지역*": "user_only", "고용형태*": "user_only", "지원경로*": "user_only"},
        )
        self.assertEqual(await self.page.locator("select").evaluate_all("(items) => items.map((item) => item.value)"), ["", "", "", ""])


class SensitiveStatusParsingTests(unittest.TestCase):
    def test_status_labels_by_keyword(self):
        profile = extract_resume_profile("이름 홍길동\n병역 비대상\n장애 비대상\n보훈여부: 해당없음\n")["resume_profile"].personal
        self.assertEqual((profile.military_status, profile.disability_status, profile.veteran_status), ("비대상", "비대상", "해당없음"))
        other = extract_resume_profile("이름 홍길동\n장애여부 비대상\n")["resume_profile"].personal
        self.assertEqual(other.disability_status, "비대상")


class LookupValueKindTests(unittest.TestCase):
    def test_email_answer_is_refused_for_a_non_email_question(self):
        field = FormField(key="al-f0-0", label="현 시점 기준 입사 일자 (YYYY-MM-DD)", name="prsAnswerDesc_12")
        answers = LookupAnswers.model_validate(
            {"answers": [{"id": "al-f0-0", "value": "hong@example.com", "evidence": "이메일 hong@example.com"}]}
        )
        accepted, rejected = validate_answers(answers, [LookupItem(field)], "이메일 hong@example.com")
        self.assertEqual(accepted, {})
        self.assertIn("이메일 항목에만", rejected["al-f0-0"])


if __name__ == "__main__":
    unittest.main()
