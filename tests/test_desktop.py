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
        self.assertTrue(self.window.fill_button.isHidden())
        self.assertFalse(self.window.next_button.isHidden())


if __name__ == "__main__":
    unittest.main()
