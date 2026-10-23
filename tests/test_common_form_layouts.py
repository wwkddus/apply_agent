import asyncio
import tempfile
import unittest
from pathlib import Path

from playwright.async_api import async_playwright

from job_agent_core.attribute_semantics import infer_from_attributes
from job_agent_core.browser_worker import BrowserAutomationWorker
from job_agent_core.korean_scope import KoreanMvpMapper
from job_agent_core.mapping import FormField, FormOption
from job_agent_core.models import EducationRecord, PersonalInfo, ResumeProfile
from job_agent_core.widgets import plan_widget_action
from tests.test_browser_worker import FakeSessions


def mapped(fields):
    return asyncio.run(KoreanMvpMapper().map_fields(fields)).mappings


def options(*texts):
    return [FormOption(value=text, text=text) for text in texts]


class AttributeNameTests(unittest.TestCase):
    def test_a_name_of_something_else_is_not_the_applicants_name(self):
        for name in ("rnaLeduDTO.unvsLeduDTOList[0].schlNm", "testNm", "rnaCarrDTO.expList[0].lastPostNm"):
            hint = infer_from_attributes(name)
            # Not the applicant's name, and 'Post' (직위) is not a postal code either.
            self.assertTrue(hint is None or hint.resume_path == "unmapped", name)

    def test_the_applicants_own_name_attributes_still_map(self):
        self.assertEqual(infer_from_attributes("user_name").resume_path, "person.name.full")
        self.assertEqual(infer_from_attributes("legalName--firstName").resume_path, "person.name.given")
        self.assertEqual(infer_from_attributes("applicantName").resume_path, "person.name.full")

    def test_referral_city_and_address_line_one(self):
        self.assertEqual(infer_from_attributes("referralName").resume_path, "unmapped")
        self.assertEqual(infer_from_attributes("address--city").resume_path, "unmapped")
        self.assertEqual(infer_from_attributes("address--addressLine1").resume_path, "person.address")
        self.assertEqual(infer_from_attributes("address--addressLine2").resume_path, "person.address_detail")
        self.assertEqual(infer_from_attributes("rsdnBaseAdr").resume_path, "person.address")


    def test_service_details_are_not_the_status_fields(self):
        for name in ("military.militaryBranchCode", "military.militaryStartDate", "handicap.handicapGradeCode",
                     "handicap.handicapContentsCode", "patriot.patriotRate", "rnaPrsnDTO.bohunNo", "rnaPrsnDTO.disableContext"):
            self.assertEqual(infer_from_attributes(name).resume_path, "unmapped", name)
        self.assertEqual(infer_from_attributes("prsMilitarySvcYN").resume_path, "person.military_status")
        self.assertEqual(infer_from_attributes("prsDisabledYN").resume_path, "person.disability_status")


    def test_education_attribute_corner_cases(self):
        cases = {
            "college[0].collegeMajor[0].dayOrNight": "unmapped",
            "graduateSchool[0].entranceTypeCode": "education.graduate_school.admission_type",
            "graduateSchool[0].score": "education.graduate_school.gpa",
            "univ[0].subMajor": "unmapped",
            "univ[0].minorName": "unmapped",
            "univ[0].doubleMajor": "education.university.double_major",
            "univ[0].transferYn": "education.university.admission_type",
        }
        for name, path in cases.items():
            self.assertEqual(infer_from_attributes(name).resume_path, path, name)
        self.assertEqual(infer_from_attributes("additionalAddress").resume_path, "person.address_detail")


class MonthFirstDateTests(unittest.TestCase):
    def test_month_first_placeholder_is_filled_month_first(self):
        field = FormField(key="f0-1", label="From", placeholder="MM/YYYY")
        action = plan_widget_action(field, "education.university.admission_date", "2021-03")
        self.assertEqual((action.action, action.value), ("fill", "03/2021"))


class KoreanLabelTests(unittest.TestCase):
    def test_items_outside_the_resume_are_left_alone(self):
        fields = [
            FormField(key="f0-1", label="1번째 외국어 어학점수", section="외국어"),
            FormField(key="f0-2", label="1번째 대학교 주/야간구분", widget_kind="select", type="select", options=options("주간", "야간")),
            FormField(key="f0-3", label="대학교 이수학점"),
            FormField(key="f0-4", label="대학교 졸업기준 학점"),
            FormField(key="f0-7", label="이름(한문)"),
            FormField(key="f0-8", label="시험명", section="어학"),
        ]
        self.assertEqual([record.resume_path for record in mapped(fields)], ["unmapped"] * len(fields))

    def test_service_details_are_left_alone_but_statuses_map(self):
        details = ["군별", "계급을 선택하세요.", "제대구분", "복무기간 입대년월일", "보훈번호", "보훈비율", "장애정도", "장애내용"]
        statuses = {"병역구분": "military", "군필 여부": "military", "보훈대상구분": "veteran", "장애대상구분": "disability"}
        records = mapped([FormField(key=f"f0-{i}", label=label) for i, label in enumerate([*details, *statuses])])
        self.assertEqual([r.resume_path for r in records[:len(details)]], ["unmapped"] * len(details))
        self.assertEqual([r.resume_path for r in records[len(details):]],
                         [f"person.{kind}_status" for kind in statuses.values()])

    def test_block_markers_minor_and_level_choice(self):
        records = mapped([
            FormField(key="f0-1", label="국내·외 구분", name="univ-region", widget_kind="radio_group", type="radio",
                      options=options("국내", "해외")),
            FormField(key="f0-2", label="최종졸업(예정)대학", name="lastestSchool", widget_kind="radio_group", type="radio",
                      options=options("최종졸업(예정)대학")),
            FormField(key="f0-3", label="1번째 대학교 부전공"),
            FormField(key="f0-4", label="1번째 대학교 편입여부", widget_kind="select", type="select", options=options("선택", "Y")),
            FormField(key="f0-5", label="구분", element_id="schoolType", widget_kind="select", type="select",
                      options=options("구분", "검정고시", "고등학교", "전문대학", "대학교", "대학원(석사)")),
            FormField(key="f0-6", label="영문 이름 (First Name)", element_id="englishName"),
        ])
        self.assertEqual([r.resume_path for r in records], [
            "unmapped", "unmapped", "unmapped", "education.university.admission_type",
            "education.highest_level", "person.name_en.given",
        ])
        transfer = plan_widget_action(FormField(key="f0-4", label="편입여부", widget_kind="select", type="select",
                                                options=options("선택", "Y")), "education.university.admission_type", "편입")
        self.assertEqual((transfer.action, transfer.value), ("select_option", "Y"))

    def test_neighbouring_labels_give_the_block_level(self):
        # No section heading: '고등학교 졸업일자' / '대학원(석사) 입학년월' next to a bare '학교명' name the block.
        records = mapped([
            FormField(key="f0-1", label="학교명", group_labels=["국내", "해외", "고등학교 졸업일자"]),
            FormField(key="f0-2", label="소재지", widget_kind="custom_select", group_labels=["고등학교 졸업일자"]),
            FormField(key="f0-3", label="학교명", group_labels=["대학원(석사) 입학년월", "대학원(석사) 학점"]),
        ])
        self.assertEqual([r.resume_path for r in records], [
            "education.high_school.school_name", "education.high_school.region", "education.graduate_school.school_name",
        ])

    def test_gender_and_career_text_are_not_resume_items(self):
        records = mapped([
            FormField(key="f0-1", label="성별* 필수", name="sex", widget_kind="radio_group", type="radio", options=options("남", "여")),
            FormField(key="f0-2", label="", name="genderFlag", widget_kind="radio_group", type="radio", options=options("성별 남", "여")),
            FormField(key="f0-3", label="상세 경력 기술", name="rnaCarrDTO.plowCarrCntn", widget_kind="textarea", type="textarea"),
            FormField(key="f0-4", label="이름(성별)"),
            FormField(key="f0-5", label="보유 기술"),
        ])
        self.assertEqual([r.resume_path for r in records], ["unmapped"] * 3 + ["person.name.full", "skills"])

    def test_passport_name_and_double_major_wording(self):
        records = mapped([
            FormField(key="f0-1", label="이름(한글)"),
            FormField(key="f0-2", label="이름(영문)_여권명 ex)HONG GILDONG"),
            FormField(key="f0-3", label="1번째 대학교 복수(이중)전공"),
        ])
        self.assertEqual([record.resume_path for record in records],
                         ["person.name.full", "person.name_en.full", "education.university.double_major"])

    def test_year_and_month_selects_get_their_own_part(self):
        fields = [
            FormField(key="f0-1", label="1번째 대학교 입학연도", widget_kind="select", type="select", options=options("선택", "2021년", "2020년")),
            FormField(key="f0-2", label="1번째 대학교 입학월", widget_kind="select", type="select", options=options("선택", "2월", "3월")),
        ]
        records = mapped(fields)
        self.assertEqual([(r.resume_path, r.part, r.status) for r in records],
                         [("education.university.admission_date", part, "auto_accepted") for part in ("year", "month")])
        year = plan_widget_action(fields[0], "education.university.admission_date", "2021-03", "year")
        month = plan_widget_action(fields[1], "education.university.admission_date", "2021-03", "month")
        self.assertEqual((year.action, year.value, month.action, month.value), ("select_option", "2021년", "select_option", "3월"))


# Common Korean layouts: dt/dd lists, two label/field pairs per table row, title-only inputs,
# '<option>선택</option>' prompts without a value, and an <h4> heading per education block.
LAYOUTS = """
<!doctype html><html lang="ko"><head><title>지원서</title></head><body>
<h3>기본정보</h3>
<dl><dt>이름(한글)</dt><dd><input id="nm"></dd><dt>이름(한문)</dt><dd><input id="nmchin"></dd>
<dt>성별</dt><dd><label><input type="radio" name="sex" value="M">남</label><label><input type="radio" name="sex" value="F">여</label></dd>
<dt>장애여부</dt><dd><label><input type="radio" name="defectyn" value="N">비대상</label><label><input type="radio" name="defectyn" value="Y">대상</label></dd></dl>
<table><tr><th>이메일</th><td><input id="mail"></td><th>생년월일</th><td><input id="birth" placeholder="YYYY-MM-DD"></td></tr></table>
<div><span>휴대폰</span><select id="hp1" title="휴대폰 번호 앞자리"><option>010</option><option>011</option></select>
<input id="hp2" maxlength="4" title="휴대폰 번호 중간자리"><input id="hp3" maxlength="4" title="휴대폰 번호 끝자리"></div>
<label for="vet">보훈대상구분</label><select id="vet"><option>선택</option><option>비대상</option><option>대상</option></select>
<h4>고등학교</h4><dl><dt>학교명</dt><dd><input id="hs" name="schnmfull"></dd></dl>
<h4>대학교</h4><dl><dt>학교명</dt><dd><input id="univ" name="schnmfull"></dd>
<dt>입학연도</dt><dd><select id="yy"><option value="">선택</option><option>2021년</option><option>2020년</option></select></dd>
<dt>입학월</dt><dd><select id="mm"><option value="">선택</option><option>3월</option><option>9월</option></select></dd></dl>
<h4>어학</h4><dl><dt>시험명</dt><dd><input id="test" name="testNm"></dd></dl>
<button type="submit">제출</button>
</body></html>
"""


class CommonLayoutFillTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        self.worker = BrowserAutomationWorker(FakeSessions(self.page, Path(self.temp.name)))

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()
        self.temp.cleanup()

    async def test_layouts_are_read_and_filled_with_the_right_values(self):
        await self.page.set_content(LAYOUTS)
        profile = ResumeProfile(
            personal=PersonalInfo(
                name={"full": "홍길동"}, birth_date="2001-01-01", email="hong@example.com", phone="010-1234-5678",
                veteran_status="비대상", disability_status="비대상",
            ),
            education=[EducationRecord(education_level="university", school_name="서울대학교", admission_date="2021-03")],
        )
        await self.worker.fill("task", profile)
        values = await self.page.evaluate(
            "() => Object.fromEntries([...document.querySelectorAll('input:not([type=radio]), select')].map(e => [e.id, e.value]))"
        )
        self.assertEqual(values, {
            "nm": "홍길동", "nmchin": "", "mail": "hong@example.com", "birth": "2001-01-01",
            "hp1": "010", "hp2": "1234", "hp3": "5678", "vet": "비대상",
            "hs": "", "univ": "서울대학교", "yy": "2021년", "mm": "3월", "test": "",
        })
        self.assertEqual(await self.page.evaluate("document.querySelector('[name=defectyn]:checked')?.value"), "N")
        self.assertIsNone(await self.page.evaluate("document.querySelector('[name=sex]:checked')?.value"))


if __name__ == "__main__":
    unittest.main()
