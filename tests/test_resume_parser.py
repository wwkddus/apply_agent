import tempfile
import unittest
from pathlib import Path

from job_agent_core.resume_parser import ResumeParseError, extract_resume_profile, inspect_resume_file


KOREAN_RESUME = """
인적사항
이름: 홍길동
생년월일: 1999.01.02
휴대전화번호: 010-1234-5678
이메일: hong@example.com
대학교
학교명: 서울대학교
전공명: 컴퓨터공학
입학년월: 2018.03
졸업년월: 2022.02
보유기술: Python, SQL
"""


class ResumeParserTests(unittest.TestCase):
    def test_korean_labeled_resume(self):
        result = extract_resume_profile(KOREAN_RESUME)
        profile = result["resume_profile"]
        self.assertEqual(profile.personal.name.full, "홍길동")
        self.assertEqual(profile.personal.birth_date, "1999-01-02")
        self.assertEqual(profile.personal.email, "hong@example.com")
        self.assertEqual(profile.education[0].school_name, "서울대학교")
        self.assertEqual(profile.education[0].major, "컴퓨터공학")
        self.assertEqual(profile.education[0].admission_date, "2018-03")
        self.assertEqual(profile.skills, "Python, SQL")
        self.assertEqual(result["missing"], [])

    def test_adjacent_table_lines(self):
        result = extract_resume_profile(
            "\n".join(
                [
                    "이름",
                    "김민수",
                    "이메일",
                    "minsu@example.com",
                    "휴대전화",
                    "010 1111 2222",
                    "대학원",
                    "학교명",
                    "예시대학원대학교",
                    "전공",
                    "데이터사이언스",
                ]
            )
        )
        profile = result["resume_profile"]
        self.assertEqual(profile.personal.name.full, "김민수")
        self.assertEqual(profile.education[0].education_level, "graduate_school")
        self.assertEqual(profile.education[0].major, "데이터사이언스")

    def test_email_and_phone_regex_fallback(self):
        result = extract_resume_profile("Hong Gildong\nhong@example.com\n010-1234-5678")
        self.assertEqual(result["profile"]["email"], "hong@example.com")
        self.assertEqual(result["profile"]["phone"], "010-1234-5678")
        self.assertIn("name", result["missing"])

    def test_non_pdf_is_rejected_without_path_in_message(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "secret_resume.txt"
            path.write_text("resume", encoding="utf-8")
            with self.assertRaises(ResumeParseError) as caught:
                inspect_resume_file(path)
        self.assertNotIn("secret_resume", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

