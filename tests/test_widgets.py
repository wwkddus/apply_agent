import unittest

from job_agent_core.mapping import FormField, FormOption
from job_agent_core.widgets import (
    detect_text_date_format,
    plan_widget_action,
    repair_text_value,
)


class WidgetPlanningTests(unittest.TestCase):
    def test_select_and_radio_require_a_unique_option(self):
        options = [
            FormOption(value="graduated", text="졸업"),
            FormOption(value="expected", text="졸업예정"),
        ]
        select = plan_widget_action(
            FormField(key="status", widget_kind="select", options=options),
            "education.university.graduation_status",
            "졸업",
        )
        radio = plan_widget_action(
            FormField(key="status-radio", widget_kind="radio_group", options=options),
            "education.university.graduation_status",
            "졸업예정",
        )
        self.assertEqual((select.action, select.value), ("select_option", "graduated"))
        self.assertEqual((radio.action, radio.value), ("check_radio", "expected"))

    def test_ambiguous_option_is_handed_to_user(self):
        action = plan_widget_action(
            FormField(
                key="degree",
                widget_kind="select",
                options=[
                    FormOption(value="bachelor-a", text="학사 과정"),
                    FormOption(value="bachelor-b", text="학사 졸업"),
                ],
            ),
            "education.university.degree",
            "학사",
        )
        self.assertEqual(action.status, "needs_human")
        self.assertIsNone(action.value)

    def test_month_dates_are_inferred_only_for_known_date_paths(self):
        admission = plan_widget_action(
            FormField(key="admission", widget_kind="native_date"),
            "education.university.admission_date",
            "2021-03",
        )
        graduation = plan_widget_action(
            FormField(key="graduation", widget_kind="native_date"),
            "education.university.graduation_date",
            "2025-02",
        )
        self.assertEqual(admission.value, "2021-03-01")
        self.assertEqual(admission.inferred, "month_to_first_day")
        self.assertEqual(graduation.value, "2025-02-28")
        self.assertEqual(graduation.inferred, "month_to_last_day")

    def test_text_date_needs_an_explicitly_detectable_format(self):
        known = plan_widget_action(
            FormField(key="birth", widget_kind="text", placeholder="YYYY.MM.DD"),
            "person.birth_date",
            "1990-02-03",
        )
        unknown = plan_widget_action(
            FormField(key="birth2", widget_kind="text", placeholder="생년월일"),
            "person.birth_date",
            "1990-02-03",
        )
        self.assertEqual(known.value, "1990.02.03")
        self.assertEqual(unknown.status, "needs_human")

    def test_split_date_select_uses_option_values(self):
        action = plan_widget_action(
            FormField(
                key="birth",
                widget_kind="split_date_select",
                parts={"year": "year", "month": "month", "day": "day"},
                part_options={
                    "year": [FormOption(value="1990", text="1990년")],
                    "month": [FormOption(value="02", text="2월")],
                    "day": [FormOption(value="d03", text="3일")],
                },
            ),
            "person.birth_date",
            "1990-02-03",
        )
        self.assertEqual(action.action, "select_parts")
        self.assertEqual(action.value, {"year": "1990", "month": "02", "day": "d03"})

    def test_checkbox_and_readonly_controls_are_not_automated(self):
        checkbox = plan_widget_action(
            FormField(key="consent", widget_kind="checkbox"), "skills", "yes"
        )
        readonly = plan_widget_action(
            FormField(key="name", widget_kind="text", readonly=True),
            "person.name.full",
            "홍길동",
        )
        self.assertEqual(checkbox.status, "needs_human")
        self.assertEqual(readonly.status, "needs_human")

    def test_safe_summary_never_contains_the_value(self):
        action = plan_widget_action(
            FormField(key="name", widget_kind="text"),
            "person.name.full",
            "홍길동",
        )
        summary = action.safe_summary()
        self.assertNotIn("홍길동", repr(summary))
        self.assertTrue(summary["value_present"])

    def test_phone_repair_is_limited_to_formatting(self):
        field = FormField(key="phone", pattern=r"\d{3}-\d{4}-\d{4}")
        self.assertEqual(
            repair_text_value(field, "person.phone", "01012345678"),
            "010-1234-5678",
        )
        self.assertIsNone(repair_text_value(field, "person.name.full", "홍길동"))

    def test_date_format_detection(self):
        self.assertEqual(
            detect_text_date_format(placeholder="", pattern=r"[0-9]{8}", maxlength=None),
            "YYYYMMDD",
        )


if __name__ == "__main__":
    unittest.main()
