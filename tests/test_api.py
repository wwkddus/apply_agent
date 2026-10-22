import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient

from job_agent_core.api import ApiSettings, create_app, default_settings
from job_agent_core.browser_session import BrowserStatus
from job_agent_core.browser_worker import BrowserFillReport
from job_agent_core.resume_llm import LlmUnavailable


class FakeLookupWorker:
    """Runs the injected lookup once and records whether it was refused."""

    def __init__(self):
        self.errors: list[str] = []

    async def fill_lookup(self, task_id, profile, lookup, requests):
        try:
            await lookup([])
        except LlmUnavailable as error:
            self.errors.append(str(error))
        return BrowserFillReport(
            browser=BrowserStatus(state="open"), results=[], filled_count=0, kept_existing_count=0,
            needs_human_count=0, failed_count=0, safe_next_available=False, submit_control_count=0,
        )

    async def close_all(self):
        pass


TOKEN = "test-token-with-at-least-24-characters"


class ApiTests(unittest.TestCase):
    def test_default_server_fails_closed_without_a_local_token(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(RuntimeError):
            default_settings()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        settings = ApiSettings(
            database_path=Path(self.temp.name) / "tasks.sqlite3",
            api_token=TOKEN,
            allowed_hosts=frozenset({"testserver"}),
        )
        self.client_context = TestClient(create_app(settings))
        self.client = self.client_context.__enter__()
        self.headers = {"X-Job-Agent-Token": TOKEN}

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def create_task(self) -> str:
        response = self.client.post(
            "/api/tasks",
            headers=self.headers,
            json={"target_url": "https://jobs.example.com/apply?secret=hidden"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["task_id"]

    def test_mutating_api_requires_local_token(self):
        response = self.client.post("/api/tasks", json={"target_url": "https://jobs.example.com/apply"})
        self.assertEqual(response.status_code, 401)

    def test_task_rejects_unsafe_url(self):
        response = self.client.post(
            "/api/tasks", headers=self.headers, json={"target_url": "http://localhost/apply"}
        )
        self.assertEqual(response.status_code, 422)

    def test_pdf_upload_and_notebook_plan_include_education(self):
        task_id = self.create_task()
        sample = Path(__file__).parents[1] / "sample_resume.pdf"
        with sample.open("rb") as handle:
            upload = self.client.post(
                f"/api/tasks/{task_id}/resume",
                headers=self.headers,
                files={"file": ("resume.pdf", handle, "application/pdf")},
            )
        self.assertEqual(upload.status_code, 200, upload.text)
        body = upload.json()
        self.assertIn("education.university.school_name", body["values"])

        planned = self.client.post(
            f"/api/tasks/{task_id}/plan",
            headers=self.headers,
            json={
                "fields": [
                    {"key": "name", "label": "이름", "widget_kind": "text"},
                    {"key": "email", "label": "이메일", "widget_kind": "text"},
                    {"key": "school", "label": "학교명", "widget_kind": "text"},
                ]
            },
        )
        self.assertEqual(planned.status_code, 200, planned.text)
        paths = {item["resume_path"] for item in planned.json()["mappings"]}
        self.assertIn("education.university.school_name", paths)
        self.assertEqual(planned.json()["skipped_field_count"], 0)

    def test_local_llm_unavailable_falls_back_to_rules_with_warning(self):
        import httpx

        def refused(request):
            raise httpx.ConnectError("refused")

        settings = ApiSettings(
            database_path=Path(self.temp.name) / "llm.sqlite3",
            api_token=TOKEN,
            allowed_hosts=frozenset({"testserver"}),
            llm_mode="always",
            ollama_transport=httpx.MockTransport(refused),
        )
        with TestClient(create_app(settings)) as client:
            task_id = client.post(
                "/api/tasks", headers=self.headers, json={"target_url": "https://jobs.example.com/apply"}
            ).json()["task_id"]
            sample = Path(__file__).parents[1] / "sample_resume.pdf"
            with sample.open("rb") as handle:
                upload = client.post(
                    f"/api/tasks/{task_id}/resume",
                    headers=self.headers,
                    files={"file": ("resume.pdf", handle, "application/pdf")},
                )
        self.assertEqual(upload.status_code, 200, upload.text)
        body = upload.json()
        self.assertTrue(any("ollama serve" in warning for warning in body["warnings"]))
        self.assertEqual(body["sources"]["education.university.school_name"], "rule")

    def test_user_can_correct_a_resume_value_with_validation(self):
        task_id = self.create_task()
        sample = Path(__file__).parents[1] / "sample_resume.pdf"
        with sample.open("rb") as handle:
            self.client.post(
                f"/api/tasks/{task_id}/resume",
                headers=self.headers,
                files={"file": ("resume.pdf", handle, "application/pdf")},
            )
        edited = self.client.patch(
            f"/api/tasks/{task_id}/resume",
            headers=self.headers,
            json={"path": "education.graduate_school.admission_date", "value": "2021.03"},
        )
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(edited.json()["values"]["education.graduate_school.admission_date"], "2021-03")
        invalid = self.client.patch(
            f"/api/tasks/{task_id}/resume",
            headers=self.headers,
            json={"path": "person.email", "value": "not-an-email"},
        )
        self.assertEqual(invalid.status_code, 422)
        unknown = self.client.patch(
            f"/api/tasks/{task_id}/resume",
            headers=self.headers,
            json={"path": "person.password", "value": "x"},
        )
        self.assertEqual(unknown.status_code, 422)

    def test_non_pdf_upload_is_rejected(self):
        task_id = self.create_task()
        response = self.client.post(
            f"/api/tasks/{task_id}/resume",
            headers=self.headers,
            files={"file": ("resume.txt", b"not a pdf", "text/plain")},
        )
        self.assertEqual(response.status_code, 415)

    def test_target_url_can_be_set_after_the_resume(self):
        created = self.client.post("/api/tasks", headers=self.headers, json={})
        self.assertEqual(created.status_code, 200, created.text)
        task_id = created.json()["task_id"]
        self.assertEqual(created.json()["target_hostname"], "")
        sample = Path(__file__).parents[1] / "sample_resume.pdf"
        with sample.open("rb") as handle:
            upload = self.client.post(
                f"/api/tasks/{task_id}/resume",
                headers=self.headers,
                files={"file": ("resume.pdf", handle, "application/pdf")},
            )
        self.assertEqual(upload.status_code, 200, upload.text)
        opened = self.client.post(f"/api/tasks/{task_id}/browser/open", headers=self.headers)
        self.assertEqual(opened.status_code, 409)
        unsafe = self.client.put(
            f"/api/tasks/{task_id}/target", headers=self.headers, json={"target_url": "http://localhost/apply"}
        )
        self.assertEqual(unsafe.status_code, 422)
        target = self.client.put(
            f"/api/tasks/{task_id}/target", headers=self.headers, json={"target_url": "https://jobs.example.com/apply"}
        )
        self.assertEqual(target.status_code, 200, target.text)
        self.assertEqual(target.json()["target_hostname"], "jobs.example.com")
        self.assertNotIn("apply", target.text)  # 원본 URL은 응답·DB에 남지 않고 도메인만 남습니다.

    def test_target_url_is_locked_after_the_task_moves_on(self):
        task_id = self.create_task()
        self.client.post(f"/api/tasks/{task_id}/cancel", headers=self.headers)
        response = self.client.put(
            f"/api/tasks/{task_id}/target", headers=self.headers, json={"target_url": "https://other.example.com/apply"}
        )
        self.assertEqual(response.status_code, 409)

    def test_skipped_second_check_never_fills_a_late_answer(self):
        worker = FakeLookupWorker()
        settings = ApiSettings(
            database_path=Path(self.temp.name) / "lookup.sqlite3",
            api_token=TOKEN,
            allowed_hosts=frozenset({"testserver"}),
            llm_mode="auto",
        )
        app = create_app(settings, browser_worker=worker)
        with TestClient(app) as client:
            task_id = client.post(
                "/api/tasks", headers=self.headers, json={"target_url": "https://jobs.example.com/apply"}
            ).json()["task_id"]
            sample = Path(__file__).parents[1] / "sample_resume.pdf"
            with sample.open("rb") as handle, patch("job_agent_core.api.request_ollama", side_effect=LlmUnavailable("off")):
                client.post(
                    f"/api/tasks/{task_id}/resume",
                    headers=self.headers,
                    files={"file": ("resume.pdf", handle, "application/pdf")},
                )

            async def answered(*_args, **_kwargs):
                return {}, {}

            async def answered_after_skip(*_args, **_kwargs):
                # 사용자가 기다리는 동안 「건너뛰기」를 누른 경우.
                app.state.lookup_generation[task_id] += 1
                return {}, {}

            with patch("job_agent_core.api.lookup_answers", side_effect=answered):
                response = client.post(f"/api/tasks/{task_id}/browser/lookup", headers=self.headers, json={"rows": []})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(worker.errors, [])
            with patch("job_agent_core.api.lookup_answers", side_effect=answered_after_skip):
                client.post(f"/api/tasks/{task_id}/browser/lookup", headers=self.headers, json={"rows": []})
            self.assertEqual(len(worker.errors), 1)
            self.assertIn("건너뛰었습니다", worker.errors[0])
            skipped = client.post(f"/api/tasks/{task_id}/browser/lookup/skip", headers=self.headers)
            self.assertEqual(skipped.json(), {"skipped": True})

    def test_cancel_clears_sensitive_memory(self):
        task_id = self.create_task()
        response = self.client.post(f"/api/tasks/{task_id}/cancel", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["sensitive_data_cleared"])


if __name__ == "__main__":
    unittest.main()
