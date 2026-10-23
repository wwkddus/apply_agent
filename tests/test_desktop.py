import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from job_agent_core.desktop import ApplyLocalWindow, DesktopApiClient


PREVIEW = {
    "values": {
        "person.name.full": "홍길동",
        "person.name.family": "홍",
        "person.name.given": "길동",
        "person.email": "hong@example.com",
    },
    "name_confirmation_required": False,
    "warnings": [],
    "missing": [],
}


class DesktopWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = ApplyLocalWindow(DesktopApiClient("http://127.0.0.1:1", "x" * 24))

    def tearDown(self):
        self.window.task_id = None
        self.window.close()

    def prepared(self, **preview):
        self.window.resume_path = Path("/tmp/example-resume.pdf")
        self.window._prepared({"task": {"task_id": "task-1"}, "preview": {**PREVIEW, **preview}})

    def visible_page(self):
        return next(name for name, page in self.window.pages.items() if not page.isHidden())

    def test_starts_on_resume_upload_with_remember_on(self):
        self.assertEqual(self.visible_page(), "upload")
        self.assertEqual(self.window.stepper.current, 0)
        self.assertTrue(self.window.keep_resume.isChecked())
        self.assertEqual(self.window.header_status.text(), "1. 이력서를 올려 주세요")

    def test_resume_check_comes_before_the_url(self):
        self.prepared()
        self.assertEqual(self.visible_page(), "resume")
        self.assertTrue(self.window.resume_next_button.isEnabled())
        self.window._resume_confirmed()
        self.assertEqual(self.visible_page(), "url")
        self.assertEqual(self.window.stepper.current, 1)
        # 브라우저를 열기 전에는 「자동 입력 시작」이 꺼져 있습니다.
        self.assertFalse(self.window.fill_button.isEnabled())
        self.window._browser_updated({"state": "open", "can_inspect": True})
        self.assertTrue(self.window.fill_button.isEnabled())
        self.assertTrue(self.window.open_button.isHidden())

    def test_name_confirmation_blocks_next(self):
        self.prepared(name_confirmation_required=True)
        self.assertFalse(self.window.name_frame.isHidden())
        self.assertFalse(self.window.resume_next_button.isEnabled())
        self.assertEqual(self.window.family_edit.text(), "홍")

    def test_header_shows_fill_progress(self):
        self.prepared()
        self.window.browser = {"state": "open"}
        self.window.stage = "filling"
        self.window._progress_updated({"running": True, "current": "이메일", "done": 18, "total": 30, "recent": []})
        self.assertEqual(self.window.header_status.text(), "3. 자동 입력 중")
        self.assertEqual(self.window.header_count.text(), "18 / 30")
        self.assertEqual(self.window.fill_current.text(), "이메일")
        self.assertEqual(self.window.stepper.current, 2)

    def test_done_counts_and_next_only_when_backend_marks_it_safe(self):
        self.prepared()
        self.window.fill_report = {
            "results": [
                {"form_field": "a", "status": "filled_verified"},
                {"form_field": "b", "status": "needs_human"},
                {"form_field": "c", "status": "not_in_resume"},
                {"form_field": "d", "status": "user_only"},
            ],
            "safe_next_available": False,
        }
        self.window._set_stage("done")
        self.assertEqual(self.window.header_status.text(), "5. 완료 · 확인 필요 1")
        self.assertEqual(
            [self.window.filled_value.text(), self.window.review_value.text(),
             self.window.absent_value.text(), self.window.manual_value.text()],
            ["1", "1", "1", "1"],
        )
        self.assertEqual(self.window.review_button.text(), "확인 필요 1개 보기")
        self.assertTrue(self.window.next_button.isHidden())
        self.window.fill_report["safe_next_available"] = True
        self.window._render()
        self.assertFalse(self.window.next_button.isHidden())

    def test_review_lists_items_needing_the_user_with_resume_value_and_page_options(self):
        self.prepared()
        self.window.fill_report = {
            "results": [
                {"form_field": "a", "label": "이메일", "resume_path": "person.email", "status": "filled_verified", "reason": ""},
                {"form_field": "b", "label": "학교명", "resume_path": "education.university.school_name",
                 "status": "needs_human", "reason": "후보 중에서 골라 주세요", "candidates": ["A대", "B대"]},
            ],
            "safe_next_available": False,
        }
        self.window._show_review(all_rows=False)
        self.assertEqual(self.visible_page(), "review")
        self.assertEqual(len(self.window.review_boxes), 1)
        item, box, options = self.window.review_boxes[0]
        self.assertEqual(item["form_field"], "b")
        self.assertEqual([box.itemText(index) for index in range(box.count())], ["A대", "B대"])
        self.assertEqual(options, ["A대", "B대"])
        self.window._show_review(all_rows=True)
        self.assertEqual(self.window.review_title.text(), "입력 결과 2")

    def test_lookup_results_are_merged_after_the_rule_results(self):
        self.prepared()
        self.window.fill_report = {
            "results": [
                {"form_field": "a", "label": "이름", "resume_path": "person.name.full", "status": "filled_verified", "reason": ""}
            ],
            "safe_next_available": False,
        }
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
        self.assertEqual([item["form_field"] for item in self.window._results()], ["a", "c"])
        self.assertEqual(self.window.review_value.text(), "1")
        self.assertIn("추가로 찾아 입력한 1개", self.window.toast.label.text())

    def test_new_application_keeps_the_resume_and_replays_edits_in_memory(self):
        self.prepared()
        self.window.edits = [("edit", "person.email", "new@example.com")]
        started = []
        self.window._prepare = lambda next_stage: started.append((next_stage, list(self.window.edits)))
        self.window.resume_path = Path(__file__)  # 존재하는 파일이면 됩니다.
        self.window.task_id = None
        self.window._new_application()
        self.assertEqual(started, [("url", [("edit", "person.email", "new@example.com")])])
        self.window.keep_resume.setChecked(False)
        self.window._new_application()
        self.assertIsNone(self.window.resume_path)
        self.assertEqual(self.window.edits, [])
        self.assertEqual(self.visible_page(), "upload")

    def test_menu_bar_face_follows_the_stage(self):
        self.assertEqual(self.window.tray_state(), "idle")
        self.prepared()
        self.window._resume_confirmed()
        self.assertEqual(self.window.tray_state(), "idle")
        self.window._browser_updated({"state": "open"})
        self.assertEqual(self.window.tray_state(), "waiting")
        self.window.stage = "filling"
        self.assertEqual(self.window.tray_state(), "applying")
        self.window.fill_report = {"results": [{"form_field": "a", "status": "needs_human"}], "safe_next_available": False}
        self.window.stage = "done"
        self.assertEqual(self.window.tray_state(), "attention")
        self.window.fill_report["results"][0]["status"] = "filled_verified"
        self.assertEqual(self.window.tray_state(), "success")
        self.window.failed_last = True
        self.assertEqual(self.window.tray_state(), "error")

    def test_every_clover_frame_is_packaged_and_square(self):
        from job_agent_core.tray import ICON_DIR, STATES, STYLES, frames, palette, render_icon

        for style in STYLES:
            colors = palette(style)
            for state in STATES:
                for name, role in frames(state, style):
                    self.assertTrue((ICON_DIR / f"{name}.svg").is_file(), name)
                    self.assertIn(role, colors)
        sizes = render_icon("clover", "#4cbb2b").availableSizes()
        self.assertTrue({18, 36, 16, 24}.issubset({size.height() for size in sizes}))
        self.assertTrue(all(size.width() == size.height() for size in sizes))

    def test_clover_states_blink_until_confirmed_and_flash_once_on_success(self):
        from job_agent_core.tray import PLAY_ONCE, frames, palette

        # 확인 필요·조작 대기는 같은 ! 그림이 반복(사용자가 확인할 때까지), 완료는 한 번만 반짝이고 기본색으로 멈춤.
        self.assertEqual(frames("waiting", "green"), frames("attention", "green"))
        self.assertNotIn("attention", PLAY_ONCE)
        # 깜빡임은 ! 를 그대로 두고 색만 바꿉니다(초록 ↔ 금색, 흑백은 진하게 ↔ 흐리게).
        self.assertEqual(set(frames("attention", "green")), {("notice", "gold"), ("notice", "base")})
        self.assertEqual(set(frames("attention", "mono")), {("notice", "base"), ("notice", "dim")})
        self.assertIn(("clover", "gold"), frames("success", "green"))
        self.assertEqual(frames("success", "green")[-1], ("clover", "base"))
        self.assertIn("success", PLAY_ONCE)
        # 흑백은 금색·회색 없이 한 색이고, 반짝임은 흐리게.
        mono = palette("mono", dark_taskbar=True)
        self.assertEqual({color for color, _ in mono.values()}, {"#ffffff"})
        self.assertLess(mono["dim"][1], 1)
        self.assertEqual(frames("error", "green"), [("error", "grey")])

    def test_header_has_native_style_close_and_minimize_buttons(self):
        from job_agent_core.desktop import WindowButton

        kinds = [button.kind for button in self.window.header.findChildren(WindowButton)]
        self.assertIn("close", kinds)
        self.assertIn("minimize", kinds)
        self.window.show()
        self.window.close_button.click()
        self.assertFalse(self.window.isVisible())

    def test_system_notification_only_when_the_window_is_out_of_sight(self):
        from job_agent_core.tray import NOTICES, should_notify

        self.window.show()
        self.assertFalse(should_notify("attention", self.window))
        self.window.hide()
        self.assertTrue(should_notify("attention", self.window))
        self.assertFalse(should_notify("applying", self.window))
        self.assertFalse(should_notify("idle", self.window))
        self.assertEqual(set(NOTICES), {"waiting", "attention", "success", "error"})

    def test_collapse_hides_everything_but_the_header(self):
        self.window._toggle_expanded()
        self.assertTrue(self.window.body.isHidden())
        self.window._toggle_expanded()
        self.assertFalse(self.window.body.isHidden())


if __name__ == "__main__":
    unittest.main()
