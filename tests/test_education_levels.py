import asyncio
import tempfile
import unittest
from pathlib import Path

from playwright.async_api import async_playwright

from job_agent_core.browser_worker import BrowserAutomationWorker
from job_agent_core.korean_scope import KoreanMvpMapper
from job_agent_core.mapping import FormField, FormOption
from job_agent_core.models import EducationRecord, PersonalInfo, ResumeProfile
from job_agent_core.planning import PREVIOUS_UNIVERSITY_REVIEW, build_fill_plan, highest_education
from job_agent_core.resume_parser import extract_resume_profile
from tests.test_browser_worker import FakeSessions


def mapped(fields):
    return asyncio.run(KoreanMvpMapper().map_fields(fields)).mappings


def school(level, name, **values):
    return EducationRecord(education_level=level, school_name=name, **values)


TRANSFER = ResumeProfile(education=[
    school("high_school", "예시고등학교", major="이과", graduation_date="2016-02"),
    school("previous_university", "예시전문대학", graduation_date="2018-02"),
    school("university", "예시대학교", admission_type="편입", graduation_date="2020-02"),
])


class ResumeLevelTests(unittest.TestCase):
    def test_high_school_and_pre_transfer_sections_are_read(self):
        text = "\n".join([
            "이름 홍길동", "이메일 hong@example.com", "휴대전화 010-1234-5678",
            "고등학교", "학교명 예시고등학교", "계열 이과", "졸업년월 2016-02",
            "편입 전 대학교", "학교명 예시전문대학", "졸업구분 졸업",
            "대학교", "학교명 예시대학교", "입학구분 편입", "소재지 서울특별시",
            "병역 · 보훈 · 장애", "병역사항 군필", "영문 성 Hong", "영문 이름 Gildong",
        ])
        profile = extract_resume_profile(text)["resume_profile"]
        levels = {record.education_level: record for record in profile.education}
        self.assertEqual((levels["high_school"].school_name, levels["high_school"].major), ("예시고등학교", "이과"))
        self.assertEqual(levels["previous_university"].school_name, "예시전문대학")
        self.assertEqual((levels["university"].admission_type, levels["university"].region), ("편입", "서울특별시"))
        self.assertEqual(profile.personal.military_status, "군필")
        name_en = profile.personal.name_en
        self.assertEqual((name_en.full, name_en.family, name_en.given, name_en.order),
                         ("Hong Gildong", "Hong", "Gildong", "family_given"))

    def test_highest_level_falls_back_to_high_school(self):
        self.assertEqual(highest_education(ResumeProfile(education=[school("high_school", "예시고등학교")])), "고등학교")
        self.assertEqual(highest_education(TRANSFER), "학사")


class MappingTests(unittest.TestCase):
    def test_high_school_block_and_track(self):
        records = mapped([
            FormField(key="f0-1", label="학교명", section="고등학교"),
            FormField(key="f0-2", label="계열", section="고등학교", widget_kind="select", type="select",
                      options=[FormOption(value=t, text=t) for t in ("인문계", "자연계")]),
            FormField(key="f0-3", label="학과계열", section="대학교"),
        ])
        self.assertEqual([r.resume_path for r in records],
                         ["education.high_school.school_name", "education.high_school.major", "unmapped"])

    def test_university_blocks_are_numbered_not_duplicates(self):
        by_name = mapped([
            FormField(key="f0-1", label="학교명", name="edu.unvsList[0].schlNm", section="대학교"),
            FormField(key="f0-2", label="학교명", name="edu.unvsList[1].schlNm", section="대학교"),
        ])
        by_label = mapped([
            FormField(key="f0-1", label="1번째 대학교 학교명"),
            FormField(key="f0-2", label="2번째 대학교 학교명"),
        ])
        # 복수전공·부전공은 같은 항목이 블록 안에서 겹칠 뿐 새 블록이 아닙니다. 학교명이 다시 나와야 새 블록입니다.
        by_repeat = mapped([
            FormField(key="f0-1", label="학교명", section="대학교"),
            FormField(key="f0-2", label="복수전공", section="대학교"),
            FormField(key="f0-3", label="입학년월", section="대학교"),
            FormField(key="f0-4", label="학교명", section="대학교"),
        ])
        for records, expected in ((by_name, [0, 1]), (by_label, [0, 1]), (by_repeat, [0, 0, 0, 1])):
            self.assertEqual([r.instance for r in records], expected)
            self.assertTrue(all(r.status == "auto_accepted" for r in records))


class PlanTests(unittest.TestCase):
    FIELDS = [
        FormField(key="f0-1", label="1번째 대학교 학교명"),
        FormField(key="f0-2", label="2번째 대학교 학교명"),
    ]

    def plan(self, profile, fields=None):
        fields = fields or self.FIELDS
        return {a.form_field: a for a in build_fill_plan(profile, fields, mapped(fields))}

    def test_first_block_gets_the_pre_transfer_school_for_review(self):
        actions = self.plan(TRANSFER)
        self.assertEqual((actions["f0-1"].resume_path, actions["f0-1"].value, actions["f0-1"].review),
                         ("education.previous_university.school_name", "예시전문대학", PREVIOUS_UNIVERSITY_REVIEW))
        self.assertEqual((actions["f0-2"].resume_path, actions["f0-2"].value, actions["f0-2"].review),
                         ("education.university.school_name", "예시대학교", ""))

    def test_without_a_transfer_only_the_first_block_is_filled(self):
        actions = self.plan(ResumeProfile(education=[school("university", "예시대학교")]))
        self.assertEqual((actions["f0-1"].status, actions["f0-1"].value), ("planned", "예시대학교"))
        self.assertEqual(actions["f0-2"].status, "missing_optional")

    def test_junior_college_and_university_lists_are_separate_blocks(self):
        # Both lists start at [0]; the junior-college block takes only a junior-college school.
        fields = [
            FormField(key="f0-1", label="1번째 전문대학 학교명", name="edu.cllgList[0].schlNm"),
            FormField(key="f0-2", label="학교명", name="edu.unvsList[0].schlNm"),
            FormField(key="f0-3", label="1번째 대학교 주전공", name="edu.unvsList[0].mjrNm"),
        ]
        self.assertEqual([r.instance for r in mapped(fields)], [0, 1, 1])
        plain = ResumeProfile(education=[school("university", "예시대학교", major="경영학")])
        actions = self.plan(plain, fields)
        self.assertEqual(actions["f0-1"].status, "missing_optional")
        self.assertEqual((actions["f0-2"].value, actions["f0-3"].value), ("예시대학교", "경영학"))
        actions = self.plan(TRANSFER, fields)
        self.assertEqual((actions["f0-1"].value, actions["f0-1"].review), ("예시전문대학", PREVIOUS_UNIVERSITY_REVIEW))
        self.assertEqual(actions["f0-2"].value, "예시대학교")

    def test_a_single_block_is_the_final_university(self):
        actions = self.plan(TRANSFER, [FormField(key="f0-1", label="학교명", section="대학교")])
        self.assertEqual(actions["f0-1"].value, "예시대학교")


# High school and university blocks only appear after their '추가' buttons clone a hidden template.
ADD_FORM = """
<!doctype html><html lang="ko"><head><title>지원서</title></head><body>
<h3>학력사항</h3>
<button type="button" id="add-hs" onclick="add('hs')">고등학교 추가</button>
<button type="button" id="add-univ" onclick="add('univ')">대학교 학력 추가</button>
<div id="blocks"></div>
<template id="hs"><div class="hs"><h4>고등학교</h4><dl>
  <dt>학교명</dt><dd><input class="name"></dd>
  <dt>계열</dt><dd><select class="track"><option value="">선택</option><option>인문계</option><option>자연계</option></select></dd>
</dl></div></template>
<template id="univ"><div class="univ"><h4>대학교</h4><dl>
  <dt>학교명</dt><dd><input class="name"></dd>
  <dt>입학구분</dt><dd><select class="type"><option value="">선택</option><option>입학</option><option>편입</option></select></dd>
</dl></div></template>
<button type="button" id="save">임시저장</button><button type="submit" id="submit">제출</button>
<script>
  window.clicks = [];
  function add(id) { document.getElementById('blocks').append(document.getElementById(id).content.cloneNode(true)); }
  document.querySelectorAll('button').forEach(b => b.addEventListener('click', () => window.clicks.push(b.id)));
</script>
</body></html>
"""


class AddBlockFillTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_high_school_and_both_universities_are_added_and_filled(self):
        await self.page.set_content(ADD_FORM)
        profile = TRANSFER.model_copy(update={"personal": PersonalInfo(name={"full": "홍길동"})})
        report = await self.worker.fill("task", profile)
        values = await self.page.evaluate("""() => [...document.querySelectorAll('#blocks input, #blocks select')]
            .map(e => e.tagName === 'SELECT' ? e.options[e.selectedIndex].text : e.value)""")
        # Blocks in page order: high school, pre-transfer school, final university.
        self.assertEqual(values, ["예시고등학교", "자연계", "예시전문대학", "선택", "예시대학교", "편입"])
        self.assertEqual(await self.page.evaluate("window.clicks"), ["add-hs", "add-univ", "add-univ"])
        previous = [r for r in report.results if r.resume_path == "education.previous_university.school_name"]
        self.assertEqual([r.status for r in previous], ["filled_review"])
        self.assertIn("고등학교 학력", report.notice)


if __name__ == "__main__":
    unittest.main()
