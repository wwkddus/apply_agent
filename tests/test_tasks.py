import tempfile
import unittest
from pathlib import Path

from job_agent_core.tasks import (
    InvalidTaskTransition,
    SensitiveTaskData,
    SensitiveTaskVault,
    SqliteTaskStore,
    TaskNotFound,
)


class TaskStoreTests(unittest.TestCase):
    def test_sqlite_contains_metadata_but_not_raw_url_or_resume_values(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "tasks.sqlite3"
            store = SqliteTaskStore(db_path)
            task = store.create("jobs.example.com")
            store.transition(task.task_id, "resume_ready", available_fields=["person.email"])
            store.close()
            raw = db_path.read_bytes()
        self.assertIn(b"jobs.example.com", raw)
        self.assertNotIn(b"secret-token", raw)
        self.assertNotIn("홍길동".encode(), raw)

    def test_invalid_transition_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SqliteTaskStore(Path(folder) / "tasks.sqlite3")
            task = store.create("jobs.example.com")
            with self.assertRaises(InvalidTaskTransition):
                store.transition(task.task_id, "planned")
            store.close()

    def test_vault_can_clear_sensitive_task_data(self):
        vault = SensitiveTaskVault()
        vault.put("task", SensitiveTaskData(raw_url="https://jobs.example.com/apply?secret-token"))
        self.assertIn("secret-token", vault.get("task").raw_url)
        vault.clear("task")
        with self.assertRaises(TaskNotFound):
            vault.get("task")


if __name__ == "__main__":
    unittest.main()
