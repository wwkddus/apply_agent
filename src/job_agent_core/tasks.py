from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import ResumeProfile


TaskPhase = Literal[
    "created",
    "resume_ready",
    "browser_pending",
    "inspecting",
    "planned",
    "awaiting_review",
    "failed",
    "cancelled",
    "expired",
]


class TaskRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    phase: TaskPhase = "created"
    target_hostname: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    available_fields: list[str] = Field(default_factory=list)
    warning_count: int = 0
    last_error_code: str | None = None


class SensitiveTaskData(BaseModel):
    """PII and raw URLs live only in memory and are never serialized by the store."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    raw_url: str
    profile: ResumeProfile | None = None


class TaskNotFound(KeyError):
    pass


class InvalidTaskTransition(ValueError):
    pass


ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "created": {"resume_ready", "cancelled", "failed", "expired"},
    "resume_ready": {"resume_ready", "browser_pending", "planned", "cancelled", "failed", "expired"},
    "browser_pending": {"browser_pending", "inspecting", "planned", "cancelled", "failed", "expired"},
    "inspecting": {"browser_pending", "planned", "cancelled", "failed", "expired"},
    "planned": {"browser_pending", "inspecting", "planned", "awaiting_review", "cancelled", "failed", "expired"},
    "awaiting_review": {"browser_pending", "inspecting", "planned", "cancelled", "failed", "expired"},
    "failed": {"cancelled", "expired"},
    "cancelled": set(),
    "expired": set(),
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class SqliteTaskStore:
    """Durable metadata store. Its schema intentionally has no raw URL or PII columns."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    phase TEXT NOT NULL,
                    target_hostname TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    available_fields TEXT NOT NULL,
                    warning_count INTEGER NOT NULL,
                    last_error_code TEXT
                )
                """
            )

    def create(self, target_hostname: str, *, lifetime: timedelta = timedelta(minutes=30)) -> TaskRecord:
        now = utcnow()
        record = TaskRecord(
            task_id=str(uuid.uuid4()),
            target_hostname=target_hostname,
            created_at=now,
            updated_at=now,
            expires_at=now + lifetime,
        )
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                self._params(record),
            )
        return record

    def get(self, task_id: str) -> TaskRecord:
        with self._lock:
            row = self._connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        record = self._from_row(row)
        if record.expires_at <= utcnow() and record.phase not in {"expired", "cancelled"}:
            record = self.transition(task_id, "expired")
        return record

    def transition(
        self,
        task_id: str,
        phase: TaskPhase,
        *,
        available_fields: list[str] | None = None,
        warning_count: int | None = None,
        last_error_code: str | None = None,
    ) -> TaskRecord:
        current = self._get_without_expiry(task_id)
        if phase != current.phase and phase not in ALLOWED_TRANSITIONS[current.phase]:
            raise InvalidTaskTransition(f"{current.phase} 상태에서 {phase} 상태로 변경할 수 없습니다.")
        updated = current.model_copy(
            update={
                "phase": phase,
                "updated_at": utcnow(),
                "available_fields": current.available_fields if available_fields is None else sorted(set(available_fields)),
                "warning_count": current.warning_count if warning_count is None else warning_count,
                "last_error_code": last_error_code,
            }
        )
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE tasks SET phase=?, target_hostname=?, created_at=?, updated_at=?, expires_at=?,
                    available_fields=?, warning_count=?, last_error_code=? WHERE task_id=?
                """,
                (
                    updated.phase,
                    updated.target_hostname,
                    updated.created_at.isoformat(),
                    updated.updated_at.isoformat(),
                    updated.expires_at.isoformat(),
                    json.dumps(updated.available_fields, ensure_ascii=True),
                    updated.warning_count,
                    updated.last_error_code,
                    updated.task_id,
                ),
            )
        return updated

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _get_without_expiry(self, task_id: str) -> TaskRecord:
        with self._lock:
            row = self._connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        return self._from_row(row)

    @staticmethod
    def _params(record: TaskRecord) -> tuple[object, ...]:
        return (
            record.task_id,
            record.phase,
            record.target_hostname,
            record.created_at.isoformat(),
            record.updated_at.isoformat(),
            record.expires_at.isoformat(),
            json.dumps(record.available_fields, ensure_ascii=True),
            record.warning_count,
            record.last_error_code,
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            phase=row["phase"],
            target_hostname=row["target_hostname"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
            available_fields=json.loads(row["available_fields"]),
            warning_count=row["warning_count"],
            last_error_code=row["last_error_code"],
        )


class SensitiveTaskVault:
    def __init__(self):
        self._items: dict[str, SensitiveTaskData] = {}
        self._lock = threading.RLock()

    def put(self, task_id: str, data: SensitiveTaskData) -> None:
        with self._lock:
            self._items[task_id] = data

    def get(self, task_id: str) -> SensitiveTaskData:
        with self._lock:
            try:
                return self._items[task_id]
            except KeyError:
                raise TaskNotFound(task_id) from None

    def clear(self, task_id: str) -> None:
        with self._lock:
            self._items.pop(task_id, None)

    def clear_all(self) -> None:
        with self._lock:
            self._items.clear()
