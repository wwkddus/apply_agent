import asyncio
import unittest

from job_agent_core.mapping import FormField
from job_agent_core.models import PersonalInfo, ResumeProfile
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

    def test_notebook_fallback_maps_name_to_full_name_only(self):
        profile = ResumeProfile(personal=PersonalInfo(name={"full": "홍길동"}))
        fields = [
            FormField(key="family", label="성", group_labels=["성", "이름"]),
            FormField(key="given", label="이름", group_labels=["성", "이름"]),
        ]
        worker = KoreanPlanningWorker()
        plan = asyncio.run(worker.plan(profile, fields))
        self.assertEqual({item.form_field: item.resume_path for item in plan.mappings}, {
            "family": "unmapped",
            "given": "person.name.full",
        })
        self.assertEqual([item.form_field for item in plan.fill_actions], ["given"])


if __name__ == "__main__":
    unittest.main()
