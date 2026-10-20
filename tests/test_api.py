import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient

from job_agent_core.api import ApiSettings, create_app, default_settings


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
        sample = Path(__file__).parents[1] / "resume_kr_table_synthetic.pdf"
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

    def test_non_pdf_upload_is_rejected(self):
        task_id = self.create_task()
        response = self.client.post(
            f"/api/tasks/{task_id}/resume",
            headers=self.headers,
            files={"file": ("resume.txt", b"not a pdf", "text/plain")},
        )
        self.assertEqual(response.status_code, 415)

    def test_cancel_clears_sensitive_memory(self):
        task_id = self.create_task()
        response = self.client.post(f"/api/tasks/{task_id}/cancel", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["sensitive_data_cleared"])


if __name__ == "__main__":
    unittest.main()
