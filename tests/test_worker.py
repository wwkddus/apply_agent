import asyncio
import unittest

from job_agent_core.mapping import FormField, FormOption
from job_agent_core.models import PersonalInfo, ResumeProfile
from job_agent_core.names import infer_latin_name, infer_name_parts
from job_agent_core.worker import KoreanPlanningWorker


class WorkerTests(unittest.TestCase):
    def test_worker_plans_notebook_personal_and_education_fields(self):
        profile = ResumeProfile.model_validate({
            "personal": PersonalInfo(
                name={"full": "홍길동"}, phone="010-1234-5678", email="hong@example.com"
            ).model_dump(),
            "education": [{"education_level": "university", "school_name": "예시대학교"}],
        })
        fields = [
            FormField(key="name", label="이름", widget_kind="text"),
            FormField(key="phone", label="휴대폰번호", widget_kind="text"),
            FormField(key="school", label="학교명", widget_kind="text"),
        ]
        plan = asyncio.run(KoreanPlanningWorker().plan(profile, fields))
        self.assertEqual({item.form_field for item in plan.fill_actions}, {"name", "phone", "school"})
        self.assertEqual(plan.skipped_field_count, 0)
        self.assertEqual(len(plan.widget_actions), 3)

    def test_adjacent_family_and_name_fields_are_split_without_asking(self):
        profile = ResumeProfile(personal=PersonalInfo(name=infer_name_parts("홍길동")))
        fields = [
            FormField(key="al-f0-0", label="성 *"),
            FormField(key="al-f0-1", label="이름 *"),
        ]
        plan = asyncio.run(KoreanPlanningWorker().plan(profile, fields))
        self.assertEqual({item.form_field: item.resume_path for item in plan.mappings}, {
            "al-f0-0": "person.name.family",
            "al-f0-1": "person.name.given",
        })
        self.assertEqual({item.form_field: item.value for item in plan.widget_actions}, {
            "al-f0-0": "홍",
            "al-f0-1": "길동",
        })

    def test_lone_name_field_stays_full_name(self):
        profile = ResumeProfile(personal=PersonalInfo(name=infer_name_parts("홍길동")))
        fields = [
            FormField(key="al-f0-0", label="이름 *"),
            FormField(key="al-f0-1", label="이메일"),
            FormField(key="al-f0-2", label="주소"),
            FormField(key="al-f0-3", label="성"),
        ]
        plan = asyncio.run(KoreanPlanningWorker().plan(profile, fields))
        paths = {item.form_field: item.resume_path for item in plan.mappings}
        self.assertEqual(paths["al-f0-0"], "person.name.full")

    def test_compound_family_name_is_not_auto_split(self):
        profile = ResumeProfile(personal=PersonalInfo(name=infer_name_parts("남궁민수")))
        fields = [FormField(key="al-f0-0", label="성"), FormField(key="al-f0-1", label="이름")]
        plan = asyncio.run(KoreanPlanningWorker().plan(profile, fields))
        self.assertEqual({item.status for item in plan.fill_actions}, {"name_confirmation_required"})
        self.assertEqual(plan.widget_actions, [])

    def test_english_name_field_is_not_filled_with_korean_name(self):
        profile = ResumeProfile(personal=PersonalInfo(name=infer_name_parts("홍길동")))
        fields = [
            FormField(key="al-f0-0", label="영문 이름"),
            FormField(key="al-f0-1", label="성명(한글)"),
            FormField(key="al-f0-2", label="한자 성명"),
        ]
        plan = asyncio.run(KoreanPlanningWorker().plan(profile, fields))
        paths = {item.form_field: item.resume_path for item in plan.mappings}
        self.assertEqual(paths, {"al-f0-0": "person.name_en.full", "al-f0-1": "person.name.full", "al-f0-2": "unmapped"})
        self.assertEqual([item.value for item in plan.widget_actions], ["홍길동"])

    def test_english_family_and_given_fields_use_the_romanized_name(self):
        name = infer_name_parts("홍길동")
        profile = ResumeProfile(personal=PersonalInfo(name=name, name_en=infer_latin_name("Gildong Hong", name)))
        fields = [
            FormField(key="al-f0-0", label="성(영문)"),
            FormField(key="al-f0-1", label="이름(영문)"),
            FormField(key="al-f0-2", label="성명"),
        ]
        plan = asyncio.run(KoreanPlanningWorker().plan(profile, fields))
        self.assertEqual(
            [item.resume_path for item in plan.mappings],
            ["person.name_en.family", "person.name_en.given", "person.name.full"],
        )
        self.assertEqual([item.value for item in plan.widget_actions], ["Hong", "Gildong", "홍길동"])

    def test_labels_with_romanized_examples_and_secondary_email(self):
        name = infer_name_parts("장연")
        profile = ResumeProfile(
            personal=PersonalInfo(name=name, name_en=infer_latin_name("ZHANG YAN", name), email="primary@example.com")
        )
        fields = [
            FormField(key="al-f0-0", label="(영문 이름) Gildong"),
            FormField(key="al-f0-1", label="(영문 성) Hong"),
            FormField(key="al-f0-2", label="보조 이메일"),
            FormField(key="al-f0-3", label="이메일"),
        ]
        plan = asyncio.run(KoreanPlanningWorker().plan(profile, fields))
        self.assertEqual(
            [item.resume_path for item in plan.mappings],
            ["person.name_en.given", "person.name_en.family", "person.email_secondary", "person.email"],
        )
        self.assertEqual([item.value for item in plan.widget_actions], ["YAN", "ZHANG", "primary@example.com"])

    def test_job_preference_fields_are_not_education(self):
        profile = ResumeProfile(personal=PersonalInfo(name={"full": "홍길동"}))
        fields = [
            FormField(key="al-f0-0", label="appWorkingRegion", name="appWorkingRegion"),
            FormField(key="al-f0-1", label="희망 근무 지역"),
            FormField(key="al-f0-2", label="지역", section="학력사항"),
        ]
        plan = asyncio.run(KoreanPlanningWorker().plan(profile, fields))
        self.assertEqual(
            [item.resume_path for item in plan.mappings], ["unmapped", "unmapped", "education.university.region"]
        )

    def test_english_name_with_unknown_order_is_not_split(self):
        name = infer_name_parts("홍길동")
        profile = ResumeProfile(personal=PersonalInfo(name=name, name_en=infer_latin_name("Alex Smith", name)))
        fields = [FormField(key="al-f0-0", label="영문 성"), FormField(key="al-f0-1", label="영문 이름")]
        plan = asyncio.run(KoreanPlanningWorker().plan(profile, fields))
        self.assertEqual({item.status for item in plan.fill_actions}, {"name_confirmation_required"})
        self.assertEqual(plan.widget_actions, [])

    def test_three_box_phone_input_is_split_into_segments(self):
        profile = ResumeProfile(personal=PersonalInfo(name={"full": "홍길동"}, phone="010-1234-5678"))
        fields = [
            FormField(
                key="al-f0-0",
                label="휴대폰",
                widget_kind="select",
                options=[FormOption(value="", text="선택"), FormOption(value="010", text="010"), FormOption(value="011", text="011")],
            ),
            FormField(key="al-f0-1", label="휴대폰", maxlength=4),
            FormField(key="al-f0-2", label="", maxlength=4),
        ]
        plan = asyncio.run(KoreanPlanningWorker().plan(profile, fields))
        self.assertEqual({item.status for item in plan.mappings}, {"auto_accepted"})
        self.assertEqual([item.value for item in plan.widget_actions], ["010", "1234", "5678"])

    def test_third_party_contact_and_duplicate_phone_are_not_auto_filled(self):
        fields = [
            FormField(key="al-f0-0", label="휴대폰", widget_kind="select", options=[FormOption(value="010", text="010")]),
            FormField(key="al-f0-1", label="휴대폰", maxlength=4),
            FormField(key="al-f0-2", label="휴대폰", maxlength=4),
            FormField(key="al-f0-3", label="연락처"),
            FormField(key="al-f0-4", label="비상 연락처"),
            FormField(key="al-f0-5", label="이메일", section="추천인 정보"),
        ]
        plan = asyncio.run(KoreanPlanningWorker().plan(ResumeProfile(), fields))
        status = {item.form_field: (item.resume_path, item.status) for item in plan.mappings}
        self.assertEqual(status["al-f0-3"], ("person.phone", "needs_human_review"))
        self.assertEqual(status["al-f0-4"][0], "unmapped")
        self.assertEqual(status["al-f0-5"][0], "unmapped")


if __name__ == "__main__":
    unittest.main()
