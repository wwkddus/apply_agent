import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from job_agent_core.desktop import ApplyLocalWindow, DesktopApiClient


class DesktopWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = ApplyLocalWindow(DesktopApiClient("http://127.0.0.1:1", "x" * 24))

    def tearDown(self):
        self.window.task_id = None
        self.window.close()

    def test_resume_selection_is_kept_across_reset_by_default(self):
        selected = Path("/tmp/example-resume.pdf")
        self.window.resume_path = selected
        self.window.resume_edit.setText(selected.name)
        self.window._reset()
        self.assertEqual(self.window.resume_path, selected)
        self.assertEqual(self.window.resume_edit.text(), selected.name)

    def test_prepared_state_keeps_existing_name_confirmation_behavior(self):
        self.window._prepared(
            {
                "task": {"task_id": "task-1"},
                "preview": {
                    "values": {
                        "person.name.full": "홍길동",
                        "person.name.family": "홍",
                        "person.name.given": "길동",
                    },
                    "name_confirmation_required": True,
                    "warnings": [],
                    "missing": [],
                },
            }
        )
        self.assertFalse(self.window.name_frame.isHidden())
        self.assertFalse(self.window.open_button.isEnabled())
        self.assertEqual(self.window.family_edit.text(), "홍")

    def test_fill_report_exposes_next_only_when_backend_marks_it_safe(self):
        self.window.task_id = "task-1"
        self.window.preview = {"name_confirmation_required": False}
        self.window.browser = {"can_inspect": True}
        self.window.inspection = {"decisions": []}
        self.window.fill_report = {"safe_next_available": True}
        self.window._update_buttons()
        self.assertFalse(self.window.fill_button.isHidden())
        self.assertEqual(self.window.fill_button.text(), "이 페이지 다시 자동 입력")
        self.assertFalse(self.window.next_button.isHidden())

    def test_step_bar_follows_progress(self):
        def active():
            return [label.objectName() for label in self.window.step_labels].index("stepActive")

        self.assertEqual(active(), 0)
        self.window.preview = {"name_confirmation_required": False, "values": {}}
        self.window._update_buttons()
        self.assertEqual(active(), 1)
        self.window.browser = {"can_inspect": True}
        self.window._update_buttons()
        self.assertEqual(active(), 2)
        self.window.fill_report = {"results": [{"status": "needs_human"}], "safe_next_available": False}
        self.window._update_buttons()
        self.assertEqual(active(), 3)
        self.window.fill_report = {"results": [{"status": "filled_verified"}], "safe_next_available": False}
        self.window._update_buttons()
        self.assertEqual(active(), 4)

    def test_items_needing_review_come_first_with_a_detail_button(self):
        self.window._show_results(
            [
                {"form_field": "a", "label": "이름", "resume_path": "person.name.full", "status": "filled_verified", "reason": ""},
                {"form_field": "b", "label": "학교명", "resume_path": "education.university.school_name",
                 "status": "needs_human", "reason": "후보 중에서 골라 주세요", "candidates": ["A대", "B대"]},
            ]
        )
        self.assertEqual(self.window.table_items[0]["form_field"], "b")
        self.assertEqual(self.window.table.cellWidget(0, 3).text(), "상세")
        self.assertIsNone(self.window.table.cellWidget(1, 3))

    def test_lookup_results_are_merged_after_the_rule_results(self):
        self.window.fill_report = {
            "results": [
                {"form_field": "a", "label": "이름", "resume_path": "person.name.full", "status": "filled_verified", "reason": ""}
            ],
            "safe_next_available": False,
        }
        self.window.fill_summary = "입력·검증 1개"
        self.window._looked_up(
            {
                "results": [
                    {"form_field": "c", "label": "회사명", "resume_path": "lookup:company",
                     "status": "filled_llm_unconfirmed", "reason": "", "candidates": ["예시주식회사"]}
                ],
                "lookup_filled_count": 1,
                "notice": "",
            }
        )
        self.assertEqual([item["form_field"] for item in self.window.table_items], ["c", "a"])
        self.assertEqual(self.window.warning_value.text(), "1")
        self.assertIn("추가로 찾아 입력한 1개", self.window.status_label.text())

    def test_review_dialog_offers_resume_value_and_page_candidates(self):
        from job_agent_core.desktop import ReviewDialog

        dialog = ReviewDialog(
            self.window,
            {"label": "학교명", "resume_path": "education.university.school_name", "status": "needs_human",
             "reason": "", "candidates": ["예시대학교 A캠퍼스", "예시대학교 B캠퍼스"]},
            "예시대학교",
        )
        items = [dialog.value_box.itemText(index) for index in range(dialog.value_box.count())]
        self.assertEqual(items, ["예시대학교", "예시대학교 A캠퍼스", "예시대학교 B캠퍼스"])
        dialog.value_box.setCurrentIndex(2)
        self.assertEqual(dialog.value(), "예시대학교 B캠퍼스")


if __name__ == "__main__":
    unittest.main()
