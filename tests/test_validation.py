import unittest

from job_agent_core.mapping import FormField, RuleBasedMapper
from job_agent_core.models import PersonalInfo, ResumeProfile
from job_agent_core.names import confirm_name_parts, infer_name_parts
from job_agent_core.planning import FillAction, build_fill_plan
from job_agent_core.validation import validate_name_consistency, validate_readback


class ValidationTests(unittest.TestCase):
    def test_confirmed_name_components_reconstruct_full_name(self):
        profile = ResumeProfile(personal=PersonalInfo(name=infer_name_parts("홍길동")))
        profile = confirm_name_parts(profile, family="홍", given="길동")
        fields = [
            FormField(key="family", label="성", group_labels=["성", "이름"]),
            FormField(key="given", label="이름", group_labels=["성", "이름"]),
        ]
        mappings = __import__("asyncio").run(RuleBasedMapper().map_fields(fields)).mappings
        actions = build_fill_plan(profile, fields, mappings)
        self.assertEqual(validate_name_consistency(profile, actions), [])

    def test_inconsistent_confirmed_components_are_blocked(self):
        profile = ResumeProfile(personal=PersonalInfo(name={"full": "홍길동"}))
        profile = confirm_name_parts(profile, family="김", given="길동")
        actions = [
            FillAction(form_field="family", resume_path="person.name.family", widget_kind="text", status="planned", value="김"),
            FillAction(form_field="given", resume_path="person.name.given", widget_kind="text", status="planned", value="길동"),
        ]
        issues = validate_name_consistency(profile, actions)
        self.assertEqual([issue.code for issue in issues], ["name_components_inconsistent"])

    def test_browser_readback_detects_mismatch_and_invalidity(self):
        actions = [
            FillAction(
                form_field="full",
                resume_path="person.name.full",
                widget_kind="text",
                status="planned",
                value="홍길동",
            )
        ]
        issues = validate_readback(actions, {"full": "홍길동!"}, {"full": False})
        self.assertEqual({issue.code for issue in issues}, {"value_mismatch", "browser_invalid"})


if __name__ == "__main__":
    unittest.main()

