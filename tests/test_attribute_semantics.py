import asyncio
import unittest

from job_agent_core.attribute_semantics import attribute_tokens, infer_from_attributes
from job_agent_core.korean_scope import KoreanMvpMapper
from job_agent_core.mapping import FormField
from job_agent_core.widgets import format_like_example, pick_option


def path(name):
    hint = infer_from_attributes(name)
    return hint.resume_path if hint else None


class AttributeSemanticsTests(unittest.TestCase):
    def test_tokens_split_dots_camel_case_and_snake_case(self):
        self.assertEqual(
            attribute_tokens("educationalBackground.universities.0.gpa.scoreScale"),
            ["educational", "background", "universities", "gpa", "score", "scale"],
        )
        self.assertEqual(attribute_tokens("edu_school_nm"), ["edu", "school", "nm"])

    def test_education_level_and_field_come_from_the_data_model_path(self):
        self.assertEqual(path("educationalBackground.universities.0.schoolName"), "education.university.school_name")
        self.assertEqual(path("educationalBackground.graduateSchools.0.schoolName"), "education.graduate_school.school_name")
        self.assertEqual(path("educationalBackground.highSchool.schoolName"), "unmapped")
        self.assertEqual(path("educationalBackground.universities.0.enrollmentPeriod.startDate"), "education.university.admission_date")
        self.assertEqual(path("educationalBackground.universities.0.enrollmentPeriod.endDate"), "education.university.graduation_date")
        self.assertEqual(path("educationalBackground.universities.0.gpa.score"), "education.university.gpa")
        self.assertEqual(path("educationalBackground.universities.0.gpa.scoreScale"), "education.university.gpa_scale")
        self.assertEqual(path("educationalBackground.universities.0.majors.0"), "education.university.major")
        self.assertEqual(path("educationalBackground.universities.0.majors.0.majorField"), "unmapped")

    def test_out_of_scope_sections_are_never_mapped(self):
        for name in (
            "workHistory.workExperiences.0.department",
            "militaryServicePreferentialEmploymentStatus.militaryService",
            "coverLetter.customAttributeAnswers.60400154",
            "emergencyContact.phone",
        ):
            self.assertEqual(path(name), "unmapped", name)

    def test_personal_paths_and_unstructured_names(self):
        self.assertEqual(path("basicInformation.phoneNumber.nationalNumber"), "person.phone")
        self.assertEqual(path("basicInformation.phoneNumber.countryCode"), "unmapped")
        self.assertEqual(path("basicInformation.birthdate"), "person.birth_date")
        self.assertEqual(path("hp_no"), "person.phone")
        self.assertEqual(path("basicInformation.englishName"), "person.name_en.full")
        self.assertEqual(path("eng_last_nm"), "person.name_en.family")
        self.assertEqual(path("basicInformation.englishName.firstName"), "person.name_en.given")
        self.assertIsNone(path("name"))
        self.assertIsNone(path("q1"))

    def test_attribute_path_outranks_a_misleading_label(self):
        fields = [
            FormField(key="al-f0-0", label="평점 *", name="educationalBackground.universities.0.enrollmentPeriod.endDate"),
            FormField(key="al-f0-1", label="전공 *", name="workHistory.workExperiences.0.department"),
            FormField(key="al-f0-2", label="학교명 *", name="educationalBackground.highSchool.schoolName"),
            FormField(key="al-f0-3", label="학교명 *", name="educationalBackground.universities.0.schoolName"),
        ]
        records = asyncio.run(KoreanMvpMapper().map_fields(fields)).mappings
        self.assertEqual(
            [(record.resume_path, record.status) for record in records],
            [
                ("education.university.graduation_date", "auto_accepted"),
                ("unmapped", "unmapped_optional"),
                ("unmapped", "unmapped_optional"),
                ("education.university.school_name", "auto_accepted"),
            ],
        )


class OptionPickingTests(unittest.TestCase):
    def test_synonyms_and_notes_for_enumerated_values(self):
        self.assertEqual(pick_option(["전문학사", "학사(4년제)"], "학사", "education.university.degree"), (1, []))
        self.assertEqual(pick_option(["Graduated", "Expected"], "졸업", "education.university.graduation_status"), (0, []))
        self.assertEqual(pick_option(["4.3", "4.50점 만점"], "4.5", "education.university.gpa_scale"), (1, []))

    def test_free_text_does_not_strip_campus_notes(self):
        index, candidates = pick_option(["예시대학교", "예시대학교(분교)"], "예시대학교", "education.university.school_name")
        self.assertEqual(index, 0)
        index, candidates = pick_option(["예시대학교 A캠퍼스", "예시대학교 B캠퍼스"], "예시대학교", "education.university.school_name")
        self.assertIsNone(index)
        self.assertEqual(len(candidates), 2)

    def test_date_written_in_the_shape_of_an_example(self):
        self.assertEqual(format_like_example("2026. 10", "2016-03-01"), "2016. 03")
        self.assertEqual(format_like_example("2026. 10. 20", "1990-02-03"), "1990. 02. 03")
        self.assertEqual(format_like_example("2026-10-20", "1990-02-03"), "1990-02-03")
        self.assertIsNone(format_like_example("날짜 선택", "1990-02-03"))


if __name__ == "__main__":
    unittest.main()
