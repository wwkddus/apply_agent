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

    def test_unnamed_education_field_takes_the_level_of_its_block_neighbour(self):
        # 실측(그리팅): 대학원 블록의 '입학구분' 버튼 묶음은 name이 없고 영역 제목도 '학력사항'뿐입니다.
        records = asyncio.run(KoreanMvpMapper().map_fields([
            FormField(key="u", label="입학구분 *", section="학력사항", widget_kind="aria_radio_group",
                      context_names=["educationalBackground.universities.0.completionStatus"]),
            FormField(key="g", label="입학구분 *", section="학력사항", widget_kind="aria_radio_group",
                      context_names=["educationalBackground.graduateSchools.0.completionStatus"]),
            FormField(key="h", label="입학구분 *", section="학력사항", widget_kind="aria_radio_group",
                      context_names=["educationalBackground.highSchool.schoolName"]),
            # 여러 블록이 섞인 넓은 영역이면 판단하지 않고 기존처럼 대학으로 둡니다(사람 확인 대상 아님).
            FormField(key="m", label="학위구분 *", section="학력사항", widget_kind="aria_radio_group",
                      context_names=["educationalBackground.highSchool.schoolName", "educationalBackground.universities.0.schoolName"]),
        ])).mappings
        by_key = {record.form_field: record for record in records}
        self.assertEqual(by_key["u"].resume_path, "education.university.admission_type")
        self.assertEqual(by_key["g"].resume_path, "education.graduate_school.admission_type")
        self.assertEqual((by_key["u"].status, by_key["g"].status), ("auto_accepted", "auto_accepted"))
        self.assertEqual(by_key["h"].resume_path, "education.high_school.admission_type")
        self.assertEqual(by_key["m"].resume_path, "education.university.degree")

    def test_thesis_or_graduation_work_is_not_the_graduation_date(self):
        # 실측(SK): '대학원 논문&졸업작품'이 '졸업' 때문에 졸업일로 연결되던 문제.
        records = asyncio.run(KoreanMvpMapper().map_fields([
            FormField(key="t", label="대학원 논문&졸업작품"),
            FormField(key="w", label="졸업 작품"),
            FormField(key="d", label="졸업년월", section="대학원"),
        ])).mappings
        paths = {record.form_field: record.resume_path for record in records}
        self.assertEqual(paths, {"t": "unmapped", "w": "unmapped", "d": "education.graduate_school.graduation_date"})

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
