# [역할] 작업(지원서 1건 처리) 상태 관리. 진행 단계만 SQLite에 저장하고,
# 이력서 내용·실제 URL 같은 민감한 데이터는 메모리에만 두어 디스크에 남지 않게 합니다.

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


# 작업이 거치는 단계: 생성 → 이력서 준비 → 브라우저 대기 → 화면 분석 → 계획 완료 → 사용자 확인.
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


# 디스크에 저장되는 작업 정보(상태, 도메인, 시간 등). 개인정보는 넣지 않습니다.
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


# 민감한 작업 데이터(원본 URL, 이력서, 이력서 원문). 메모리에서만 보관합니다.
class SensitiveTaskData(BaseModel):
    """PII and raw URLs live only in memory and are never serialized by the store."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    raw_url: str
    profile: ResumeProfile | None = None
    # Extracted resume text for the local page-driven lookup; memory only, like the profile.
    resume_text: str = ""


# 작업을 찾지 못했을 때 / 허용되지 않는 단계로 바꾸려 할 때의 오류.
class TaskNotFound(KeyError):
    pass


class InvalidTaskTransition(ValueError):
    pass


# 단계별로 이동할 수 있는 다음 단계 목록. 순서가 꼬이는 것을 막습니다.
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


# 현재 시각(UTC). 테스트에서 시간 비교를 일정하게 하기 위함.
def utcnow() -> datetime:
    return datetime.now(UTC)


# 작업 메타데이터 저장소. 테이블에는 일부러 URL·개인정보 칸이 없습니다.
class SqliteTaskStore:
    """Durable metadata store. Its schema intentionally has no raw URL or PII columns."""

    # DB 파일을 열고 tasks 테이블이 없으면 만듭니다. 여러 스레드에서 쓰므로 잠금을 둡니다.
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

    # 새 작업을 만들고 만료 시간(기본 30분)을 정합니다.
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

    # 작업을 읽고, 만료 시간이 지났으면 자동으로 '만료' 상태로 바꿉니다.
    def get(self, task_id: str) -> TaskRecord:
        with self._lock:
            row = self._connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        record = self._from_row(row)
        if record.expires_at <= utcnow() and record.phase not in {"expired", "cancelled"}:
            record = self.transition(task_id, "expired")
        return record

    # 작업 단계를 바꿉니다. 허용 목록에 없는 이동이면 오류를 냅니다.
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

    # 채용 URL 도메인을 나중에 정합니다(이력서를 먼저 올리는 화면 흐름). 바꾼 기록이 남도록 갱신 시각도 바꿉니다.
    def set_target_hostname(self, task_id: str, target_hostname: str) -> TaskRecord:
        current = self._get_without_expiry(task_id)
        updated = current.model_copy(update={"target_hostname": target_hostname, "updated_at": utcnow()})
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE tasks SET target_hostname=?, updated_at=? WHERE task_id=?",
                (updated.target_hostname, updated.updated_at.isoformat(), task_id),
            )
        return updated

    # DB 연결 종료.
    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # 만료 검사 없이 그대로 읽기(transition 내부용).
    def _get_without_expiry(self, task_id: str) -> TaskRecord:
        with self._lock:
            row = self._connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        return self._from_row(row)

    # TaskRecord ↔ DB 행 변환 도우미.
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


# 민감 데이터 메모리 보관함. 작업이 끝나거나 앱이 종료되면 clear로 지웁니다.
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
