import asyncio
import unittest

from job_agent_core.korean_scope import KoreanMvpMapper, enforce_korean_mvp_profile
from job_agent_core.mapping import FormField
from job_agent_core.models import PersonalInfo, ResumeProfile


class KoreanScopeTests(unittest.TestCase):
    def test_notebook_fallback_rules_cover_real_site_labels(self):
        fields = [
            FormField(key="name", label="이름 *"),
            FormField(key="email", label="이메일주소 *"),
            FormField(key="phone", label="연락처 *"),
            FormField(key="school", label="학교명 *", section="대학교"),
            FormField(key="gpa", label="평점 *", section="대학교"),
        ]
        records = asyncio.run(KoreanMvpMapper().map_fields(fields)).mappings
        paths = {record.form_field: record.resume_path for record in records}
        self.assertEqual(paths["name"], "person.name.full")
        self.assertEqual(paths["email"], "person.email")
        self.assertEqual(paths["phone"], "person.phone")
        self.assertEqual(paths["school"], "education.university.school_name")
        self.assertEqual(paths["gpa"], "education.university.gpa")

    def test_notebook_rules_use_section_to_choose_education_level(self):
        records = asyncio.run(KoreanMvpMapper().map_fields([
            FormField(key="u", label="학교명", section="대학교"),
            FormField(key="g", label="학교명", section="대학원"),
        ])).mappings
        paths = {record.form_field: record.resume_path for record in records}
        self.assertEqual(paths["u"], "education.university.school_name")
        self.assertEqual(paths["g"], "education.graduate_school.school_name")

    def test_invalid_korean_phone_and_postal_code_are_blank_not_invented(self):
        profile = ResumeProfile(
            personal=PersonalInfo(
                name="홍길동",
                phone="02-123-4567",
                postal_code="ABC12",
            )
        )
        safe, warnings = enforce_korean_mvp_profile(profile)
        self.assertEqual(safe.personal.phone, "")
        self.assertEqual(safe.personal.postal_code, "")
        self.assertEqual(len(warnings), 2)

    def test_education_is_preserved_for_notebook_rule_compatibility(self):
        profile = ResumeProfile.model_validate(
            {
                "personal": {"name": "홍길동", "phone": "010-1234-5678", "postal_code": "06236"},
                "education": [{"education_level": "university", "school_name": "예시대학교"}],
            }
        )
        safe, warnings = enforce_korean_mvp_profile(profile)
        self.assertEqual(safe.education[0].school_name, "예시대학교")
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
