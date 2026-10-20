from __future__ import annotations

import calendar
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .constants import DATE_PATHS
from .mapping import FormField, FormOption


class WidgetAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    form_field: str
    resume_path: str
    widget_kind: str
    action: Literal[
        "fill",
        "select_option",
        "check_radio",
        "choose_suggestion",
        "choose_custom_option",
        "select_parts",
        "adapter_set_date",
    ] | None = None
    value: str | dict[str, str] | None = None
    status: Literal["planned", "needs_human"] = "planned"
    reason: str = ""
    inferred: str | None = None

    def safe_summary(self) -> dict[str, object]:
        return {
            "form_field": self.form_field,
            "resume_path": self.resume_path,
            "widget_kind": self.widget_kind,
            "action": self.action,
            "status": self.status,
            "value_present": self.value not in (None, "", {}),
            "reason": self.reason,
            "inferred": self.inferred,
        }


def _norm_option(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def match_option(options: list[FormOption], value: str) -> FormOption | None:
    real = [option for option in options if option.value.strip()]
    target = _norm_option(value)
    exact = [option for option in real if target in {_norm_option(option.value), _norm_option(option.text)}]
    if len(exact) == 1:
        return exact[0]
    partial = [
        option
        for option in real
        if len(target) >= 2 and (target in _norm_option(option.text) or _norm_option(option.text) in target)
    ]
    return partial[0] if len(partial) == 1 else None


def _match_number_option(options: list[FormOption], number: int) -> FormOption | None:
    matches = []
    for option in options:
        if not option.value.strip():
            continue
        values = [re.sub(r"\D", "", option.value), re.sub(r"\D", "", option.text)]
        if any(value and int(value) == number for value in values):
            matches.append(option)
    return matches[0] if len(matches) == 1 else None


def detect_text_date_format(*, placeholder: str | None, pattern: str | None, maxlength: int | None) -> str | None:
    compact = re.sub(r"\s", "", placeholder or "").upper()
    match = re.search(r"YYYY([^A-Z0-9]?)MM([^A-Z0-9]?)DD", compact)
    if match:
        return f"YYYY{match.group(1)}MM{match.group(2)}DD"
    match = re.search(r"YYYY([^A-Z0-9]?)MM", compact)
    if match:
        return f"YYYY{match.group(1)}MM"
    normalized_pattern = (pattern or "").replace("[0-9]", r"\d")
    known = {
        r"\d{8}": "YYYYMMDD",
        r"\d{4}-\d{2}-\d{2}": "YYYY-MM-DD",
        r"\d{4}\.\d{2}\.\d{2}": "YYYY.MM.DD",
        r"\d{4}/\d{2}/\d{2}": "YYYY/MM/DD",
        r"\d{6}": "YYYYMM",
        r"\d{4}-\d{2}": "YYYY-MM",
        r"\d{4}\.\d{2}": "YYYY.MM",
    }
    if normalized_pattern in known:
        return known[normalized_pattern]
    if maxlength == 8:
        return "YYYYMMDD"
    if maxlength == 6:
        return "YYYYMM"
    return None


def format_date_value(iso_value: str, target_format: str | None) -> str | None:
    match = re.fullmatch(r"(\d{4})-(\d{2})(?:-(\d{2}))?", iso_value or "")
    if not match or not target_format:
        return None
    year, month, day = match.groups()
    if "DD" in target_format and not day:
        return None
    return target_format.replace("YYYY", year).replace("MM", month).replace("DD", day or "")


def _human(field: FormField, path: str, reason: str) -> WidgetAction:
    return WidgetAction(
        form_field=field.key,
        resume_path=path,
        widget_kind=field.widget_kind,
        status="needs_human",
        reason=reason,
    )


def plan_widget_action(field: FormField, resume_path: str, value: str) -> WidgetAction:
    """Convert a validated resume value into a side-effect-free widget action."""
    kind = field.widget_kind
    base = {"form_field": field.key, "resume_path": resume_path, "widget_kind": kind}
    if kind in {"unsupported", "file"}:
        return _human(field, resume_path, "지원하지 않는 입력 방식입니다")
    if kind == "checkbox":
        return _human(field, resume_path, "체크박스는 사용자가 직접 선택해야 합니다")
    if field.readonly and kind != "custom_date_picker":
        return _human(field, resume_path, "읽기 전용 필드는 수정하지 않습니다")

    if resume_path in DATE_PATHS:
        iso_value = value
        inferred = None
        if re.fullmatch(r"\d{4}-\d{2}", iso_value or ""):
            year, month = map(int, iso_value.split("-"))
            day = calendar.monthrange(year, month)[1] if resume_path.endswith("graduation_date") else 1
            iso_value = f"{year:04d}-{month:02d}-{day:02d}"
            inferred = "month_to_last_day" if resume_path.endswith("graduation_date") else "month_to_first_day"
        full_date = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso_value or ""))
        if kind == "native_date":
            return WidgetAction(**base, action="fill", value=iso_value, inferred=inferred) if full_date else _human(
                field, resume_path, "연월일 전체가 필요합니다"
            )
        if kind == "native_month":
            return WidgetAction(**base, action="fill", value=iso_value[:7], inferred=inferred)
        if kind == "text":
            target_format = field.date_format or detect_text_date_format(
                placeholder=field.placeholder, pattern=field.pattern, maxlength=field.maxlength
            )
            formatted = format_date_value(iso_value, target_format)
            return WidgetAction(**base, action="fill", value=formatted, inferred=inferred) if formatted else _human(
                field, resume_path, "날짜 입력 형식을 확실히 판단할 수 없습니다"
            )
        if kind == "split_date_select":
            year, month, *day = iso_value.split("-")
            wanted = {"year": int(year), "month": int(month)}
            if day:
                wanted["day"] = int(day[0])
            if "day" in field.parts and "day" not in wanted:
                return _human(field, resume_path, "일 선택 항목이 있지만 이력서에는 연월만 있습니다")
            chosen: dict[str, str] = {}
            for part in field.parts:
                option = _match_number_option(field.part_options.get(part, []), wanted[part])
                if option is None:
                    return _human(field, resume_path, f"{part} 항목에 일치하는 선택지를 하나로 확정할 수 없습니다")
                chosen[part] = option.value
            return WidgetAction(**base, action="select_parts", value=chosen, inferred=inferred)
        if kind == "custom_date_picker":
            return WidgetAction(**base, action="adapter_set_date", value=iso_value, inferred=inferred) if full_date else _human(
                field, resume_path, "사용자 정의 달력에는 연월일 전체가 필요합니다"
            )
        return _human(field, resume_path, f"'{kind}' 입력 방식에서는 날짜 값을 지원하지 않습니다")

    if kind == "custom_combobox":
        return WidgetAction(**base, action="choose_suggestion", value=value)
    if kind == "custom_select":
        return WidgetAction(**base, action="choose_custom_option", value=value)
    if kind in {"select", "radio_group"}:
        option = match_option(field.options, value)
        if option is None:
            return _human(field, resume_path, "일치하는 선택지를 하나로 확정할 수 없습니다")
        action = "select_option" if kind == "select" else "check_radio"
        return WidgetAction(**base, action=action, value=option.value)
    if kind in {"text", "textarea"}:
        return WidgetAction(**base, action="fill", value=value)
    return _human(field, resume_path, f"'{kind}' 입력 방식은 자동 처리하지 않습니다")


def repair_text_value(field: FormField, resume_path: str, current_value: str) -> str | None:
    """Only repair low-risk formatting; never alter semantic identity fields."""
    pattern = field.pattern

    def acceptable(candidate: str) -> bool:
        if not candidate or candidate == current_value:
            return False
        try:
            return not pattern or re.fullmatch(pattern, candidate) is not None
        except re.error:
            return True

    if resume_path == "person.phone":
        digits = re.sub(r"\D", "", current_value)
        if digits.startswith("82"):
            digits = "0" + digits[2:]
        if len(digits) == 11:
            candidates = (digits, f"{digits[:3]}-{digits[3:7]}-{digits[7:]}", f"{digits[:3]} {digits[3:7]} {digits[7:]}")
            return next((candidate for candidate in candidates if acceptable(candidate)), None)
    if resume_path == "person.postal_code":
        candidate = re.sub(r"\s", "", current_value)
        return candidate if acceptable(candidate) else None
    return None

