import asyncio
import json
import unittest

import httpx

from job_agent_core.models import ResumeProfile
from job_agent_core.resume_llm import (
    LlmUnavailable,
    is_grounded,
    merge_llm_values,
    proposed_values,
    request_ollama,
    should_use_llm,
)

FREE_FORM_TEXT = """홍길동
Hong Gildong | hong@example.com | 010-1234-5678
학력
예시대학교 컴퓨터공학과 학사  2016.03 – 2020.02  졸업  평점 3.77 / 4.5
보유 기술: Python, SQL
"""


def ollama_transport(answer: dict, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is False and body["options"]["temperature"] == 0
        assert "format" in body
        return httpx.Response(status, json={"message": {"content": json.dumps(answer, ensure_ascii=False)}})

    return httpx.MockTransport(handler)


class ResumeLlmTests(unittest.TestCase):
    def run_ollama(self, answer, **kwargs):
        return asyncio.run(
            request_ollama(FREE_FORM_TEXT, url="http://127.0.0.1:11434", model="test", transport=ollama_transport(answer), **kwargs)
        )

    def test_grounded_values_fill_gaps_and_fabrications_are_dropped(self):
        fields = self.run_ollama(
            {
                "name": "홍길동",
                "name_english": "Hong Gildong",
                "email": "hong@example.com",
                "phone": "010-1234-5678",
                "birth_date": "1990-02-03",
                "skills": "Python, SQL",
                "university": {
                    "school_name": "예시대학교",
                    "major": "컴퓨터공학과",
                    "degree": "학사",
                    "admission_date": "2016.03",
                    "graduation_date": "2020.02",
                    "graduation_status": "졸업",
                    "gpa": "3.77",
                    "gpa_scale": "4.5",
                },
                "graduate_school": {"school_name": "가짜대학원"},
            }
        )
        profile, sources, warnings = merge_llm_values(ResumeProfile(), proposed_values(fields), FREE_FORM_TEXT)
        university = profile.education[0]
        self.assertEqual(profile.personal.name.full, "홍길동")
        self.assertEqual(profile.personal.phone, "010-1234-5678")
        self.assertEqual((profile.personal.name_en.family, profile.personal.name_en.order), ("Hong", "family_given"))
        self.assertEqual((university.school_name, university.admission_date, university.graduation_date), ("예시대학교", "2016-03", "2020-02"))
        self.assertEqual((university.gpa, university.gpa_scale), ("3.77", "4.5"))
        # Not written in the resume, so never accepted.
        self.assertEqual(profile.personal.birth_date, "")
        self.assertEqual(len(profile.education), 1)
        self.assertEqual(len(warnings), 2)
        self.assertEqual(sources["education.university.school_name"], "llm")

    def test_rule_values_are_never_overwritten(self):
        profile = ResumeProfile.model_validate({"personal": {"name": "홍길동", "email": "rule@example.com"}})
        merged, sources, _ = merge_llm_values(profile, {"person.email": "hong@example.com"}, FREE_FORM_TEXT)
        self.assertEqual(merged.personal.email, "rule@example.com")
        self.assertEqual(sources, {})

    def test_only_loopback_ollama_is_allowed(self):
        with self.assertRaises(LlmUnavailable):
            asyncio.run(request_ollama("x", url="https://llm.example.com", model="test", transport=ollama_transport({})))

    def test_offline_or_missing_model_is_reported_not_raised_as_crash(self):
        def refused(request):
            raise httpx.ConnectError("refused")

        with self.assertRaises(LlmUnavailable) as offline:
            asyncio.run(request_ollama("x", url="http://127.0.0.1:11434", model="m", transport=httpx.MockTransport(refused)))
        self.assertIn("ollama serve", str(offline.exception))
        with self.assertRaises(LlmUnavailable) as missing:
            asyncio.run(request_ollama("x", url="http://127.0.0.1:11434", model="m", transport=ollama_transport({}, status=404)))
        self.assertIn("ollama pull m", str(missing.exception))

    def test_grounding_rules(self):
        self.assertTrue(is_grounded("2016-03", "입학 2016. 3", "education.university.admission_date"))
        self.assertFalse(is_grounded("2016-04", "입학 2016. 3", "education.university.admission_date"))
        self.assertTrue(is_grounded("4.5", "3.77 / 4.5", "education.university.gpa_scale"))
        self.assertFalse(is_grounded("4.5", "4.53", "education.university.gpa_scale"))

    def test_llm_runs_only_for_sparse_rule_results(self):
        sparse = ResumeProfile.model_validate({"personal": {"name": "홍길동"}})
        rich = ResumeProfile.model_validate(
            {
                "personal": {"name": "홍길동", "email": "a@example.com", "phone": "010-1234-5678", "birth_date": "1990-01-01"},
                "education": [{"education_level": "university", "school_name": "예시대학교"}],
            }
        )
        self.assertTrue(should_use_llm(sparse, "auto"))
        self.assertFalse(should_use_llm(rich, "auto"))
        self.assertFalse(should_use_llm(sparse, "off"))


if __name__ == "__main__":
    unittest.main()
