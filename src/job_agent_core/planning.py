from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .constants import NAME_PATHS
from .mapping import FormField, MappingRecord
from .models import ResumeProfile


class FillAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    form_field: str
    resume_path: str
    widget_kind: str
    status: Literal[
        "planned",
        "kept_existing",
        "mapping_review_required",
        "name_confirmation_required",
        "missing_required",
        "missing_optional",
        "readonly",
        "unsupported",
    ]
    value: str | None = None
    reason: str = ""

    def safe_summary(self) -> dict[str, object]:
        return {
            "form_field": self.form_field,
            "resume_path": self.resume_path,
            "widget_kind": self.widget_kind,
            "status": self.status,
            "value_present": bool(self.value),
            "reason": self.reason,
        }


def get_resume_value(profile: ResumeProfile, path: str) -> str:
    if path.startswith("person.name."):
        return str(getattr(profile.personal.name, path.rsplit(".", 1)[1]))
    if path.startswith("person."):
        return str(getattr(profile.personal, path.split(".", 1)[1]))
    if path == "skills":
        return profile.skills
    if path.startswith("education."):
        _, level, field = path.split(".", 2)
        record = next((item for item in profile.education if item.education_level == level), None)
        return str(getattr(record, field)) if record else ""
    return ""


def build_fill_plan(
    profile: ResumeProfile,
    fields: list[FormField],
    mappings: list[MappingRecord],
) -> list[FillAction]:
    """Read only mapped resume paths and produce side-effect-free actions."""
    by_field = {field.key: field for field in fields}
    actions: list[FillAction] = []
    for mapping in mappings:
        if mapping.resume_path == "unmapped":
            continue
        field = by_field[mapping.form_field]
        base = {
            "form_field": field.key,
            "resume_path": mapping.resume_path,
            "widget_kind": field.widget_kind,
        }
        if field.current_value_present:
            actions.append(FillAction(**base, status="kept_existing", reason="페이지에 이미 입력된 값을 유지합니다"))
            continue
        if field.readonly and field.widget_kind != "custom_date_picker":
            actions.append(FillAction(**base, status="readonly", reason="읽기 전용 필드는 수정하지 않습니다"))
            continue
        if field.widget_kind in {"checkbox", "file", "unsupported"}:
            actions.append(FillAction(**base, status="unsupported", reason="사용자가 직접 처리해야 하는 항목입니다"))
            continue
        if mapping.status != "auto_accepted":
            actions.append(FillAction(**base, status="mapping_review_required", reason="필드 연결을 사용자가 확인해야 합니다"))
            continue
        value = get_resume_value(profile, mapping.resume_path)
        if not value:
            status = "missing_required" if field.required else "missing_optional"
            actions.append(FillAction(**base, status=status, reason="이력서에서 값을 찾지 못했습니다"))
            continue
        if mapping.resume_path in NAME_PATHS - {"person.name.full"} and not profile.personal.name.confirmed:
            actions.append(
                FillAction(
                    **base,
                    status="name_confirmation_required",
                    reason="분리한 성과 이름은 사용자의 최초 1회 확인이 필요합니다",
                )
            )
            continue
        actions.append(FillAction(**base, status="planned", value=value, reason="검증된 필드 연결과 이력서 값입니다"))
    return actions

