import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation"))
from metrics import score_runs  # noqa: E402
from record_run import field_structure  # noqa: E402

from job_agent_core.mapping import FormField, FormOption, MappingRecord  # noqa: E402


def field(key, predicted, decision, fill, attempts=0):
    return {
        "page_index": 1,
        "form_field": key,
        "label": key,
        "widget_kind": "text",
        "predicted_path": predicted,
        "decision_status": decision,
        "fill_status": fill,
        "attempts": attempts,
    }


RUN = {
    "site": "site-a",
    "resume": {"present_paths": ["person.name.full", "person.email", "person.phone"]},
    "pages": [
        {
            "page_index": 1,
            "inspect_ms": 100.0,
            "fill_ms": 300.0,
            "fields": [
                field("f1", "person.name.full", "planned", "filled_verified", 1),
                field("f2", "person.address", "planned", "filled_verified", 2),
                field("f3", None, "skipped", None),
                field("f4", None, "skipped", None),
                field("f5", "person.phone", "needs_human", "needs_human", 1),
                field("f6", "person.email", "planned", None),
            ],
        }
    ],
}

LABELS = {
    ("site-a", 1, "f1"): "person.name.full",
    ("site-a", 1, "f2"): "person.address_detail",
    ("site-a", 1, "f3"): "unmapped",
    ("site-a", 1, "f4"): "person.email",
    ("site-a", 1, "f5"): "person.phone",
}


class EvaluationMetricsTests(unittest.TestCase):
    def test_scores_mapping_fill_and_safety(self):
        result = score_runs([RUN], LABELS)
        self.assertEqual(result["labeled_fields"], 5)
        self.assertEqual(result["unlabeled_fields"], 1)
        self.assertEqual(result["field_mapping_accuracy"], 0.6)
        # f1, f4 and f5 are fillable; f2 expects a path the resume does not have.
        self.assertEqual(result["fillable_fields"], 3)
        self.assertEqual(result["autofill_success_rate"], round(1 / 3, 4))
        self.assertEqual(result["unsafe_fill_count"], 1)
        self.assertEqual(result["unsafe_fill_rate"], 0.5)
        self.assertEqual(result["validation_pass_rate"], round(2 / 3, 4))
        self.assertEqual(result["skip_precision"], 0.5)
        self.assertEqual(result["human_intervention_rate"], 0.2)
        self.assertEqual(result["silent_miss_count"], 1)
        self.assertEqual(result["retry_max"], 2)
        self.assertEqual(result["retried_fields"], 1)

    def test_disabled_fields_are_not_fillable_and_lookup_fills_need_review(self):
        run = {
            "site": "site-b",
            "resume": {"present_paths": ["education.graduate_school.graduation_date"]},
            "pages": [
                {
                    "page_index": 1,
                    "inspect_ms": 1.0,
                    "fill_ms": 1.0,
                    "fields": [
                        field("g1", "education.graduate_school.graduation_date", "planned", "disabled_on_page", 1),
                        field("g2", "lookup:company", "planned", "filled_llm_unconfirmed", 1),
                        field("g3", "education.graduate_school.graduation_date", "planned", "not_in_resume"),
                    ],
                }
            ],
        }
        labels = {
            ("site-b", 1, "g1"): "education.graduate_school.graduation_date",
            ("site-b", 1, "g2"): "unmapped",
            ("site-b", 1, "g3"): "education.graduate_school.graduation_date",
        }
        result = score_runs([run], labels)
        # g3 was not filled but the ledger said why, so it is not a silent miss.
        self.assertEqual((result["fillable_fields"], result["silent_miss_count"]), (1, 0))
        self.assertEqual((result["disabled_on_page_count"], result["lookup_filled_count"]), (1, 1))
        self.assertEqual(result["human_intervention_rate"], round(1 / 3, 4))

    def test_empty_labels_do_not_divide_by_zero(self):
        result = score_runs([RUN], {})
        self.assertIsNone(result["field_mapping_accuracy"])
        self.assertEqual(result["unlabeled_fields"], 6)


class StructureSnapshotTests(unittest.TestCase):
    def test_snapshot_keeps_structure_but_never_values(self):
        fields = [
            FormField(
                key="al-f0-0",
                label="졸업구분",
                name="educationalBackground.universities.0.completionStatus",
                widget_kind="custom_select",
                current_value_present=True,
                options=[FormOption(value="GRADUATED", text="졸업"), FormOption(value="EXPECTED", text="졸업예정")],
            )
        ]
        mappings = [
            MappingRecord(form_field="al-f0-0", resume_path="education.university.graduation_status", confidence=0.95, reason="attribute: status")
        ]
        [row] = field_structure(fields, mappings)
        self.assertEqual(row["options"], ["졸업", "졸업예정"])
        self.assertTrue(row["has_existing_value"])
        self.assertEqual(row["predicted_path"], "education.university.graduation_status")
        self.assertFalse({"value", "current_value", "url"} & set(row))


if __name__ == "__main__":
    unittest.main()
