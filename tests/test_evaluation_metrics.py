import asyncio
import csv
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation"))
from metrics import page_fillable, score_runs  # noqa: E402
import record_run  # noqa: E402
from record_run import ask, field_structure, update_label_template  # noqa: E402

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
    "variant": "after",
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
    ("site-a", "after", 1, "f1"): "person.name.full",
    ("site-a", "after", 1, "f2"): "person.address_detail",
    ("site-a", "after", 1, "f3"): "unmapped",
    ("site-a", "after", 1, "f4"): "person.email",
    ("site-a", "after", 1, "f5"): "person.phone",
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
            "variant": "after",
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
            ("site-b", "after", 1, "g1"): "education.graduate_school.graduation_date",
            ("site-b", "after", 1, "g2"): "unmapped",
            ("site-b", "after", 1, "g3"): "education.graduate_school.graduation_date",
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


class VariantLabelTests(unittest.TestCase):
    def test_same_key_means_a_different_field_in_each_variant(self):
        # Before detects fewer fields, so its f1 is the email box while After's f1 is a date picker.
        present = {"resume": {"present_paths": ["person.email", "person.birth_date"]}}
        before = {**present, "site": "s", "variant": "before", "pages": [
            {"page_index": 1, "inspect_ms": 1.0, "fields": [field("f1", "person.email", "planned", "filled_verified", 1)]}
        ]}
        after = {**present, "site": "s", "variant": "after", "pages": [
            {"page_index": 1, "inspect_ms": 1.0, "fields": [
                field("f1", "person.birth_date", "planned", "filled_verified", 1),
                field("f2", "person.email", "planned", "filled_verified", 1),
            ]}
        ]}
        labels = {
            ("s", "before", 1, "f1"): "person.email",
            ("s", "after", 1, "f1"): "person.birth_date",
            ("s", "after", 1, "f2"): "person.email",
        }
        reference = page_fillable([before, after], labels)
        self.assertEqual(reference, {("s", 1): 2})
        scored_before = score_runs([before], labels, reference)
        self.assertEqual(scored_before["field_mapping_accuracy"], 1.0)
        # The date field Before never detected still counts against its coverage.
        self.assertEqual((scored_before["page_fillable_fields"], scored_before["page_coverage"]), (2, 0.5))
        self.assertEqual(score_runs([after], labels, reference)["page_coverage"], 1.0)

    def test_label_template_keeps_variants_apart_and_written_answers(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(record_run, "EVAL_DIR", Path(temp)):
            rows = [{"page_index": 1, "form_field": "f1", "label": "이메일", "widget_kind": "text"}]
            path = update_label_template("s", "before", rows)
            update_label_template("s", "after", [{**rows[0], "label": "생년월일"}])
            with path.open(newline="", encoding="utf-8") as handle:
                written = list(csv.DictReader(handle))
            written[0]["expected_path"] = "person.email"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=record_run.LABEL_COLUMNS)
                writer.writeheader()
                writer.writerows(written)
            # Recording Before again keeps the answer for the unchanged field and the After rows.
            update_label_template("s", "before", rows)
            with path.open(newline="", encoding="utf-8") as handle:
                final = {(row["variant"], row["form_field"]): row for row in csv.DictReader(handle)}
        self.assertEqual(final[("before", "f1")]["expected_path"], "person.email")
        self.assertEqual(final[("after", "f1")]["label"], "생년월일")
        self.assertEqual(len(final), 2)


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


class TerminalPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_for_input_does_not_block_the_event_loop(self):
        # Browser requests are answered by async route handlers while the prompt is shown.
        def slow_input(prompt):
            # Answers only once the loop has kept running while it waited.
            deadline = time.monotonic() + 1.0
            while ticks < 5 and time.monotonic() < deadline:
                time.sleep(0.01)
            return "q" if ticks >= 5 else "blocked"

        ticks = 0

        async def tick():
            nonlocal ticks
            for _ in range(5):
                await asyncio.sleep(0.02)
                ticks += 1

        with mock.patch("builtins.input", slow_input):
            answer, _ = await asyncio.gather(ask("> "), tick())
        self.assertEqual(answer, "q")
        self.assertEqual(ticks, 5)


if __name__ == "__main__":
    unittest.main()
