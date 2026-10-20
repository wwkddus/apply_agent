from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .models import ResumeProfile
from .names import normalized_name
from .planning import FillAction


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Literal[
        "name_components_unconfirmed",
        "name_components_inconsistent",
        "planned_value_missing",
        "field_not_found",
        "value_mismatch",
        "browser_invalid",
    ]
    form_field: str | None = None
    message: str


def validate_name_consistency(profile: ResumeProfile, actions: list[FillAction]) -> list[ValidationIssue]:
    planned = {action.resume_path: action for action in actions if action.status == "planned"}
    name = profile.personal.name
    issues: list[ValidationIssue] = []
    component_paths = {"person.name.family", "person.name.given", "person.name.middle"}
    if component_paths & set(planned) and not name.confirmed:
        issues.append(
            ValidationIssue(
                code="name_components_unconfirmed",
                message="분리된 이름 필드를 입력하기 전에 성과 이름을 확인해야 합니다",
            )
        )
        return issues
    if "person.name.family" in planned and "person.name.given" in planned:
        if name.order == "given_family":
            combined = f"{name.given}{name.middle}{name.family}"
        else:
            combined = f"{name.family}{name.middle}{name.given}"
        if normalized_name(combined) != normalized_name(name.full):
            issues.append(
                ValidationIssue(
                    code="name_components_inconsistent",
                    message="확인한 성과 이름을 합친 결과가 전체 이름과 일치하지 않습니다",
                )
            )
    return issues


def validate_readback(
    actions: list[FillAction],
    actual_values: dict[str, str | None],
    browser_validity: dict[str, bool] | None = None,
) -> list[ValidationIssue]:
    browser_validity = browser_validity or {}
    issues: list[ValidationIssue] = []
    for action in actions:
        if action.status != "planned":
            continue
        if action.value is None:
            issues.append(
                ValidationIssue(
                    code="planned_value_missing",
                    form_field=action.form_field,
                    message="입력 예정 항목에 값이 없습니다",
                )
            )
            continue
        if action.form_field not in actual_values or actual_values[action.form_field] is None:
            issues.append(
                ValidationIssue(
                    code="field_not_found",
                    form_field=action.form_field,
                    message="브라우저 재확인 과정에서 해당 필드를 찾지 못했습니다",
                )
            )
            continue
        if str(actual_values[action.form_field]) != action.value:
            issues.append(
                ValidationIssue(
                    code="value_mismatch",
                    form_field=action.form_field,
                    message="브라우저의 실제 값이 입력 예정 값과 다릅니다",
                )
            )
        if browser_validity.get(action.form_field) is False:
            issues.append(
                ValidationIssue(
                    code="browser_invalid",
                    form_field=action.form_field,
                    message="브라우저의 유효성 검사를 통과하지 못했습니다",
                )
            )
    return issues

