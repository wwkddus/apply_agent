import asyncio
import unittest

from job_agent_core.korean_scope import KoreanMvpMapper
from job_agent_core.mapping import FormField, FormOption
from job_agent_core.models import PersonalInfo, ResumeProfile
from job_agent_core.planning import build_fill_plan
from job_agent_core.widgets import plan_widget_action

DOMAINS = [FormOption(value=text, text=text) for text in ("선택", "직접입력", "naver.com", "gmail.com")]


def mapped(fields):
    return asyncio.run(KoreanMvpMapper().map_fields(fields)).mappings


class EmailSegmentTests(unittest.TestCase):
    FIELDS = [
        FormField(key="al-f0-12", label="이메일 아이디 입력", name="emailId"),
        FormField(key="al-f0-13", label="이메일 도메인 입력", name="emailDomain"),
        FormField(key="al-f0-14", label="이메일 도메인 선택", widget_kind="select", type="select", options=DOMAINS),
    ]

    def test_id_domain_and_domain_select_are_parts_of_one_email(self):
        records = mapped(self.FIELDS)
        self.assertEqual([(r.resume_path, r.part, r.status) for r in records],
                         [("person.email", part, "auto_accepted") for part in ("local", "domain", "domain_select")])

    def test_values_are_split_at_the_at_sign_and_the_select_goes_first(self):
        profile = ResumeProfile(personal=PersonalInfo(email="hong@gmail.com"))
        actions = build_fill_plan(profile, self.FIELDS, mapped(self.FIELDS))
        self.assertEqual([action.part for action in actions], ["local", "domain_select", "domain"])
        by_part = {a.part: plan_widget_action(next(f for f in self.FIELDS if f.key == a.form_field), a.resume_path, a.value, a.part)
                   for a in actions}
        self.assertEqual((by_part["local"].action, by_part["local"].value), ("fill", "hong"))
        self.assertEqual((by_part["domain"].action, by_part["domain"].value), ("fill", "gmail.com"))
        self.assertEqual((by_part["domain_select"].action, by_part["domain_select"].value), ("select_option", "gmail.com"))

    def test_unlisted_domain_chooses_direct_input(self):
        action = plan_widget_action(self.FIELDS[2], "person.email", "hong@example.org", "domain_select")
        self.assertEqual((action.action, action.value), ("select_option", "직접입력"))

    def test_an_email_confirmation_box_is_not_a_domain_box(self):
        fields = [FormField(key="al-f0-1", label="이메일", name="email"),
                  FormField(key="al-f0-2", label="이메일 확인", name="emailConfirm")]
        self.assertTrue(all(record.part is None for record in mapped(fields)))


if __name__ == "__main__":
    unittest.main()
