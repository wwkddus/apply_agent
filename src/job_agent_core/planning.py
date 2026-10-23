# 입력 계획 단계. 

# '어느 칸 ↔ 어느 이력서 항목' 연결 결과를 받아, 칸마다 실제로 넣을 값과 상태를 정합니다.
# 브라우저를 건드리지 않는 순수 계산이라, 이미 값이 있거나 확인이 필요한 칸은 여기서 걸러집니다.

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .constants import ALLOWED_RESUME_PATHS, DERIVED_PATHS, NAME_EN_PATHS, NAME_PATHS
from .mapping import LLM_ONLY_SOURCE, FormField, MappingRecord
from .models import EducationRecord, ResumeProfile, clean_field
from .names import confirm_name_parts, infer_latin_name, infer_name_parts, name_components_usable


# 칸 하나에 대한 입력 계획. status로 '입력 예정 / 기존 값 유지 / 사람 확인 필요 / 값 없음' 등을 구분합니다.
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
    part: str | None = None
    # 입력은 하되 사용자 확인으로 남길 이유(예: AI만 연결한 칸). 비어 있으면 검증 후 「입력됨」.
    review: str = ""

    # 로그용 요약. 실제 값은 빼고 '값이 있는지'만 남겨 개인정보가 기록되지 않게 합니다.
    def safe_summary(self) -> dict[str, object]:
        return {
            "form_field": self.form_field,
            "resume_path": self.resume_path,
            "widget_kind": self.widget_kind,
            "status": self.status,
            "value_present": bool(self.value),
            "reason": self.reason,
        }


# 학력 기록으로 '최종 학력' 문구를 계산합니다. 대학원 학위가 안 적혀 있으면 석사/박사를 추측하지 않습니다.
def highest_education(profile: ResumeProfile) -> str:
    """최종 학력 wording from the records; a graduate school without a stated degree stays '대학원'."""
    records = {record.education_level: record for record in profile.education if record.school_name}
    graduate = records.get("graduate_school")
    if graduate is not None:
        degree = graduate.degree.casefold()
        if "박사" in degree or "doctor" in degree or "phd" in degree:
            return "박사"
        if "석사" in degree or "master" in degree:
            return "석사"
        return "대학원"
    university = records.get("university") or records.get("previous_university")
    if university is not None:
        return "전문학사" if "전문" in university.degree or "associate" in university.degree.casefold() else "학사"
    if "high_school" in records:
        return "고등학교"
    return ""


# 편입 전 대학을 어느 대학 블록에 넣을지 알리는 문구(블록 순서로 추정하므로 사용자가 확인).
PREVIOUS_UNIVERSITY_REVIEW = "편입 전 대학을 앞쪽 대학 칸에 입력했습니다. 순서가 맞는지 확인해 주세요."
EXTRA_UNIVERSITY_REASON = "이력서에 이 순서의 대학 학력이 없어 입력하지 않았습니다"
_JUNIOR_COLLEGE_RE = re.compile(r"전문\s*대|전문\s*학사|cllg|junior\s*college|associate", re.I)


def _is_junior_college(record: EducationRecord | None) -> bool:
    return bool(record and _JUNIOR_COLLEGE_RE.search(f"{record.school_name} {record.degree}"))


def university_block_paths(
    profile: ResumeProfile, mappings: list[MappingRecord], fields: list[FormField] | None = None,
) -> dict[str, str | None]:
    """Resume path per field of the page's university blocks.

    A page may list a junior-college block before the university block, or several plain blocks.
    The final university goes to a block of its own kind (junior college or not): the first one, or
    the last one when a pre-transfer school also needs a block. The pre-transfer school goes to a
    block of its kind, else the earliest remaining block. Other blocks map to None (not filled).
    """
    university = [m for m in mappings if m.resume_path.startswith("education.university.") and m.instance is not None]
    blocks = list(dict.fromkeys(sorted(m.instance for m in university)))
    if len(blocks) < 2:
        return {}
    by_key = {field.key: field for field in fields or []}
    junior = {
        m.instance for m in university
        if m.form_field in by_key and _JUNIOR_COLLEGE_RE.search(f"{by_key[m.form_field].label} {by_key[m.form_field].name}")
    }
    final_record = next((r for r in profile.education if r.education_level == "university"), None)
    previous = next((r for r in profile.education if r.education_level == "previous_university" and r.school_name), None)

    def of_kind(record: EducationRecord | None) -> list[int]:
        kind = [block for block in blocks if (block in junior) == _is_junior_college(record)]
        return kind or blocks

    final_choices = of_kind(final_record)
    final_block = final_choices[-1] if previous else final_choices[0]
    previous_block = None
    if previous:
        rest = [block for block in of_kind(previous) if block != final_block] or [b for b in blocks if b != final_block]
        previous_block = rest[0]
    paths: dict[str, str | None] = {}
    for mapping in university:
        field = mapping.resume_path.rsplit(".", 1)[1]
        if mapping.instance == previous_block:
            paths[mapping.form_field] = f"education.previous_university.{field}"
        elif mapping.instance == final_block:
            paths[mapping.form_field] = mapping.resume_path
        else:
            paths[mapping.form_field] = None
    return paths


# 'person.email' 같은 경로 문자열로 이력서에서 값을 꺼냅니다.
def get_resume_value(profile: ResumeProfile, path: str) -> str:
    if path == "education.highest_level":
        return highest_education(profile)
    if path.startswith("person.name."):
        return str(getattr(profile.personal.name, path.rsplit(".", 1)[1]))
    if path.startswith("person.name_en."):
        return str(getattr(profile.personal.name_en, path.rsplit(".", 1)[1]))
    if path.startswith("person."):
        return str(getattr(profile.personal, path.split(".", 1)[1]))
    if path == "skills":
        return profile.skills
    if path.startswith("education."):
        _, level, field = path.split(".", 2)
        record = next((item for item in profile.education if item.education_level == level), None)
        return str(getattr(record, field)) if record else ""
    return ""


# 경로의 마지막 부분을 clean_field 규칙 이름으로 사용합니다.
def field_name_for_path(path: str) -> str:
    """The clean_field() rule name for a resume path, e.g. education.university.gpa -> gpa."""
    return "skills" if path == "skills" else path.rsplit(".", 1)[-1]


# 사용자가 화면에서 고친 값을 이력서에 반영한 '새 복사본'을 돌려줍니다(원본은 그대로).
# 이름은 다시 성/이름으로 나누고, 영문 이름·개인정보·학력은 각각의 규칙으로 검증합니다.
def set_resume_value(profile: ResumeProfile, path: str, value: str) -> ResumeProfile:
    """Return a copy with one user-corrected value; raises ValueError for invalid input."""
    if path not in ALLOWED_RESUME_PATHS or path in DERIVED_PATHS:
        raise ValueError("지원하지 않는 이력서 항목입니다")
    clean = clean_field(field_name_for_path(path), value)
    personal = profile.personal
    if path == "person.name.full":
        name = infer_name_parts(clean)
        return profile.model_copy(update={"personal": personal.model_copy(update={"name": name})})
    if path in NAME_PATHS:
        parts = {"family": personal.name.family, "given": personal.name.given, "middle": personal.name.middle}
        parts[path.rsplit(".", 1)[1]] = clean
        return confirm_name_parts(profile, **parts)
    if path == "person.name_en.full":
        name_en = infer_latin_name(clean, personal.name)
        return profile.model_copy(update={"personal": personal.model_copy(update={"name_en": name_en})})
    if path in NAME_EN_PATHS:
        part = path.rsplit(".", 1)[1]
        current = personal.name_en.model_dump()
        current.update({part: clean, "split_method": "user_confirmed", "confirmed": True, "split_confidence": 1.0})
        if current["order"] == "unknown":
            current["order"] = "family_given"
        name_en = type(personal.name_en).model_validate(current)
        return profile.model_copy(update={"personal": personal.model_copy(update={"name_en": name_en})})
    if path.startswith("person."):
        updated = personal.model_validate({**personal.model_dump(), path.split(".", 1)[1]: clean})
        return profile.model_copy(update={"personal": updated})
    if path == "skills":
        return profile.model_copy(update={"skills": clean})
    _, level, field = path.split(".", 2)
    records = [record for record in profile.education if record.education_level != level]
    current = next((record for record in profile.education if record.education_level == level), None)
    data = current.model_dump() if current else {"education_level": level}
    records.append(EducationRecord.model_validate({**data, field: clean}))
    return ResumeProfile.model_validate({**profile.model_dump(), "education": [record.model_dump() for record in records]})


# 칸마다 입력 여부를 결정. 판단 순서(안전한 쪽 우선):
# 기존 값 있음 → 유지 / 읽기 전용 → 건드리지 않음 / 체크박스·파일 → 사용자 처리 /
# 연결이 불확실 → 사람 확인 / 이름 분리 미확인 → 1회 확인 / 이력서에 값 없음 → 누락 / 그 외 → 입력 예정.
def build_fill_plan(
    profile: ResumeProfile,
    fields: list[FormField],
    mappings: list[MappingRecord],
) -> list[FillAction]:
    """Read only mapped resume paths and produce side-effect-free actions."""
    by_field = {field.key: field for field in fields}
    blocks = university_block_paths(profile, mappings, fields)
    actions: list[FillAction] = []
    for mapping in mappings:
        if mapping.resume_path == "unmapped":
            continue
        field = by_field[mapping.form_field]
        path = blocks.get(mapping.form_field, mapping.resume_path)
        review = mapping.reason if mapping.source == LLM_ONLY_SOURCE else ""
        if path and path.startswith("education.previous_university."):
            review = PREVIOUS_UNIVERSITY_REVIEW
        base = {
            "form_field": field.key,
            "resume_path": path or mapping.resume_path,
            "widget_kind": field.widget_kind,
            "part": mapping.part,
            "review": review,
        }
        if path is None:
            actions.append(FillAction(**base, status="missing_optional", reason=EXTRA_UNIVERSITY_REASON))
            continue
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
        component_name = (
            profile.personal.name
            if mapping.resume_path in NAME_PATHS - {"person.name.full"}
            else profile.personal.name_en
            if mapping.resume_path in NAME_EN_PATHS - {"person.name_en.full"}
            else None
        )
        # A full name whose order is unknown is asked about once instead of being reported missing.
        if component_name is not None and component_name.full and not name_components_usable(component_name):
            actions.append(
                FillAction(
                    **base,
                    status="name_confirmation_required",
                    reason="분리한 성과 이름은 사용자의 최초 1회 확인이 필요합니다",
                )
            )
            continue
        value = get_resume_value(profile, base["resume_path"])
        if not value:
            status = "missing_required" if field.required else "missing_optional"
            actions.append(FillAction(**base, status=status, reason="이력서에서 값을 찾지 못했습니다"))
            continue
        actions.append(FillAction(**base, status="planned", value=value, reason="검증된 필드 연결과 이력서 값입니다"))
    return _domain_select_first(actions)


# 이메일 도메인 선택(예: 「직접입력」)을 도메인 글자 칸보다 먼저 합니다. 나중에 고르면 사이트가 입력한 도메인을 지우기도 합니다.
def _domain_select_first(actions: list[FillAction]) -> list[FillAction]:
    selects = [action for action in actions if action.part == "domain_select"]
    if not selects:
        return actions
    ordered: list[FillAction] = []
    for action in actions:
        if action.part == "domain_select":
            continue
        if action.part == "domain":
            ordered.extend(item for item in selects if item.resume_path == action.resume_path and item not in ordered)
        ordered.append(action)
    ordered.extend(item for item in selects if item not in ordered)
    return ordered

