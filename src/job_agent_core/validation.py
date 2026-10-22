# [역할] 입력 후 검증 단계. 이름 조합이 맞는지, 브라우저에 실제로 들어간 값이 계획과 같은지 확인합니다.
# '넣었다'가 아니라 '다시 읽어서 맞다'를 보장하기 위한 모듈입니다.

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .models import ResumeProfile
from .names import normalized_name
from .planning import FillAction


# 검증에서 발견한 문제 하나(코드 + 사용자에게 보여줄 메시지).
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


# 성/이름을 나눠 넣을 때: 사용자가 확인했는지, 합치면 전체 이름과 같은지 검사합니다.
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


# 입력 후 브라우저 값을 다시 읽어 계획한 값과 비교합니다(값 없음/칸 없음/불일치/브라우저 검증 실패).
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

