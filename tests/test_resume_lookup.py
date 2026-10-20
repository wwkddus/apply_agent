import asyncio
import json
import unittest

import httpx

from job_agent_core.mapping import FormField, FormOption, MappingRecord
from job_agent_core.resume_llm import LlmUnavailable
from job_agent_core.resume_lookup import LookupAnswers, LookupItem, lookup_answers, lookup_candidates, validate_answers

RESUME = """홍길동
경력
예시주식회사 데이터팀 2021.03 - 2023.02
자격증: 정보처리기사 (2020.11)
병역: 육군 만기전역
"""


def unmapped(key):
    return MappingRecord(form_field=key, resume_path="unmapped", confidence=0, reason="fallback: no rule matched")


FIELDS = [
    FormField(key="al-f0-0", label="회사명", section="경력사항", name="workHistory.workExperiences.0.companyName"),
    FormField(key="al-f0-1", label="자격증명", section="자격증"),
    FormField(
        key="al-f0-2",
        label="병역구분",
        widget_kind="select",
        options=[FormOption(value="", text="선택"), FormOption(value="D", text="군필"), FormOption(value="N", text="미필")],
    ),
    FormField(key="al-f0-3", label="입사일", widget_kind="custom_date_picker", name="workHistory.workExperiences.0.startDate"),
    FormField(key="al-f0-4", label="자기소개", widget_kind="textarea"),
    FormField(key="al-f0-5", label="지원 동기를 적어주세요"),
    FormField(key="al-f0-6", label="비상연락처"),
    FormField(key="al-f0-7", label="개인정보 동의", widget_kind="checkbox"),
    FormField(key="al-f0-8", label="부서", current_value_present=True),
    FormField(key="al-f0-9", label="이름"),
]
MAPPINGS = [unmapped(field.key) for field in FIELDS[:-1]] + [
    MappingRecord(form_field="al-f0-9", resume_path="person.name.full", confidence=0.9, reason="label")
]


class LookupCandidateTests(unittest.TestCase):
    def test_only_unmapped_answerable_fields_are_sent(self):
        keys = [field.key for field in lookup_candidates(FIELDS, MAPPINGS)]
        # Open questions, other people's contacts, consent, existing values and rule-mapped fields stay out.
        self.assertEqual(keys, ["al-f0-0", "al-f0-1", "al-f0-2", "al-f0-3"])


class LookupValidationTests(unittest.TestCase):
    def validate(self, answers):
        items = [LookupItem(field) for field in FIELDS[:4]]
        return validate_answers(LookupAnswers.model_validate({"answers": answers}), items, RESUME)

    def test_grounded_answers_are_kept(self):
        accepted, rejected = self.validate(
            [
                {"id": "al-f0-0", "value": "예시주식회사", "evidence": "예시주식회사  데이터팀 2021.03-2023.02"},
                {"id": "al-f0-1", "value": "정보처리기사", "evidence": "자격증: 정보처리기사 (2020.11)"},
                {"id": "al-f0-2", "value": "군필", "evidence": "병역: 육군 만기전역"},
                {"id": "al-f0-3", "value": "2021.03", "evidence": "예시주식회사 데이터팀 2021.03 - 2023.02"},
            ]
        )
        # Evidence that differs from the PDF text only in spacing still counts.
        self.assertEqual(
            accepted, {"al-f0-0": "예시주식회사", "al-f0-1": "정보처리기사", "al-f0-2": "군필", "al-f0-3": "2021.03"}
        )
        self.assertEqual(rejected, {})

    def test_fabricated_or_unbacked_answers_are_dropped(self):
        accepted, rejected = self.validate(
            [
                # Evidence that is not in the resume.
                {"id": "al-f0-1", "value": "SQLD", "evidence": "자격증: SQLD"},
                # Value not contained in its evidence.
                {"id": "al-f0-0", "value": "다른회사", "evidence": "예시주식회사 데이터팀 2021.03 - 2023.02"},
                # Not one of the field's options.
                {"id": "al-f0-2", "value": "면제", "evidence": "병역: 육군 만기전역"},
                # Date that the evidence does not contain.
                {"id": "al-f0-3", "value": "2019.01", "evidence": "예시주식회사 데이터팀 2021.03 - 2023.02"},
                # A field that was never asked.
                {"id": "al-f0-4", "value": "열정", "evidence": "홍길동"},
            ]
        )
        self.assertEqual(accepted, {})
        self.assertIn("원문과 달라", rejected["al-f0-1"])
        self.assertIn("근거 문장에 없어", rejected["al-f0-0"])
        self.assertIn("선택지와 일치하지", rejected["al-f0-2"])
        self.assertIn("근거 문장에 없어", rejected["al-f0-3"])


class LookupRequestTests(unittest.TestCase):
    def test_offline_ollama_raises_so_the_caller_can_skip(self):
        def refuse(request):
            raise httpx.ConnectError("refused", request=request)

        with self.assertRaises(LlmUnavailable):
            asyncio.run(
                lookup_answers(RESUME, [LookupItem(FIELDS[0])], url="http://127.0.0.1:11434", model="m", transport=httpx.MockTransport(refuse))
            )

    def test_prompt_contains_only_the_requested_fields(self):
        seen = {}

        def answer(request):
            seen["prompt"] = json.loads(request.content)["messages"][0]["content"]
            content = {"answers": [{"id": "al-f0-0", "value": "예시주식회사", "evidence": "예시주식회사 데이터팀 2021.03 - 2023.02"}]}
            return httpx.Response(200, json={"message": {"content": json.dumps(content, ensure_ascii=False)}})

        accepted = asyncio.run(
            lookup_answers(RESUME, [LookupItem(FIELDS[0])], url="http://127.0.0.1:11434", model="m", transport=httpx.MockTransport(answer))
        )
        self.assertEqual(accepted, ({"al-f0-0": "예시주식회사"}, {}))
        self.assertIn("workHistory.workExperiences.0.companyName", seen["prompt"])
        self.assertNotIn("자기소개", seen["prompt"].split("이력서 텍스트")[0])

    def test_remote_ollama_is_refused(self):
        with self.assertRaises(LlmUnavailable):
            asyncio.run(lookup_answers(RESUME, [LookupItem(FIELDS[0])], url="http://192.168.0.5:11434", model="m"))


if __name__ == "__main__":
    unittest.main()
