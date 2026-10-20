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
        "choose_aria_radio",
    ] | None = None
    value: str | dict[str, str] | None = None
    fallback_value: str | None = None
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


# Equivalent wordings for enumerated education values (resume wording -> page wording).
OPTION_SYNONYMS: dict[str, tuple[tuple[str, ...], ...]] = {
    "graduation_status": (
        ("졸업", "graduated", "graduation", "졸업완료"),
        ("졸업예정", "expected", "expectedgraduation", "졸업예정자"),
        ("재학", "재학중", "enrolled", "attending", "inprogress"),
        ("휴학", "휴학중", "leaveofabsence", "onleave"),
        ("수료", "completed", "completion"),
        ("중퇴", "dropout", "withdrawn", "droppedout"),
    ),
    "degree": (
        ("학사", "bachelor", "bachelors", "bachelor'sdegree", "bachelorsdegree", "4년제", "대학교4년제"),
        ("전문학사", "associate", "associates", "associatedegree", "2년제", "3년제", "전문대"),
        ("석사", "master", "masters", "master'sdegree", "mastersdegree"),
        ("박사", "doctor", "doctorate", "doctoral", "phd", "ph.d."),
    ),
    "admission_type": (
        ("입학", "신입학", "정시", "수시", "regular", "freshman"),
        ("편입", "편입학", "transfer"),
    ),
}


def _strip_note(value: str) -> str:
    """'학사(4년제)' -> '학사' so a parenthetical note does not block an exact match."""
    return re.sub(r"\([^)]*\)|\[[^\]]*\]", "", value or "")


def _equivalents(path: str | None, value: str) -> set[str]:
    target = _norm_option(_strip_note(value))
    field = (path or "").rsplit(".", 1)[-1]
    for group in OPTION_SYNONYMS.get(field, ()):
        if target in {_norm_option(item) for item in group}:
            return {_norm_option(item) for item in group}
    return {target}


def pick_option(texts: list[str], value: str, path: str | None = None) -> tuple[int | None, list[str]]:
    """Pick exactly one option text for a resume value; otherwise return the plausible candidates."""
    usable = [(index, text) for index, text in enumerate(texts) if _norm_option(text)]
    if path and path.endswith((".gpa", ".gpa_scale")):
        try:
            wanted = float(value)
        except ValueError:
            wanted = None
        if wanted is not None:
            numeric = [
                index
                for index, text in usable
                if any(abs(float(number) - wanted) < 1e-9 for number in re.findall(r"\d+(?:\.\d+)?", text))
            ]
            if len(numeric) == 1:
                return numeric[0], []
    wanted_set = _equivalents(path, value)
    enumerated = (path or "").rsplit(".", 1)[-1] in OPTION_SYNONYMS
    exact = [
        index
        for index, text in usable
        if _norm_option(text) in wanted_set or (enumerated and _norm_option(_strip_note(text)) in wanted_set)
    ]
    if len(exact) == 1:
        return exact[0], []
    target = _norm_option(value)
    partial = [
        index
        for index, text in usable
        if len(target) >= 2 and (target in _norm_option(text) or _norm_option(text) in target)
    ]
    if len(partial) == 1 and not exact:
        return partial[0], []
    candidates = [texts[index] for index in (exact or partial)][:5]
    return None, candidates


def match_option(options: list[FormOption], value: str, path: str | None = None) -> FormOption | None:
    real = [option for option in options if option.value.strip()]
    target = _norm_option(value)
    by_value = [option for option in real if _norm_option(option.value) == target]
    if len(by_value) == 1:
        return by_value[0]
    index, _ = pick_option([option.text for option in real], value, path)
    return real[index] if index is not None else None


def _match_number_option(options: list[FormOption], number: int) -> FormOption | None:
    matches = []
    for option in options:
        if not option.value.strip():
            continue
        values = [re.sub(r"\D", "", option.value), re.sub(r"\D", "", option.text)]
        if any(value and int(value) == number for value in values):
            matches.append(option)
    return matches[0] if len(matches) == 1 else None


def phone_segments(value: str) -> list[str] | None:
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("82"):
        digits = "0" + digits[2:]
    if len(digits) == 11:
        return [digits[:3], digits[3:7], digits[7:]]
    if len(digits) == 10:
        return [digits[:3], digits[3:6], digits[6:]]
    return None


def format_phone_value(field: FormField, value: str) -> str:
    """Pick hyphenated or digits-only from the field's own constraints; keep the resume form otherwise."""
    segments = phone_segments(value)
    if segments is None:
        return value
    hyphen, digits = "-".join(segments), "".join(segments)
    if field.pattern:
        try:
            fitting = [item for item in (hyphen, digits) if re.fullmatch(field.pattern, item)]
        except re.error:
            fitting = []
        if len(fitting) == 1:
            return fitting[0]
    placeholder = field.placeholder or ""
    if re.search(r"숫자만|-\s*(없이|제외)|without|digits only", placeholder, re.I):
        return digits
    if re.fullmatch(r"\d{3}-\d{3,4}-\d{4}", placeholder.strip()):
        return hyphen
    if re.fullmatch(r"\d{10,11}", placeholder.strip()):
        return digits
    if field.maxlength is not None and field.maxlength < len(hyphen):
        return digits
    return value


def detect_text_date_format(*, placeholder: str | None, pattern: str | None, maxlength: int | None) -> str | None:
    compact = re.sub(r"\s", "", placeholder or "").upper()
    match = re.search(r"YYYY([^A-Z0-9]?)MM([^A-Z0-9]?)DD", compact)
    if match:
        return f"YYYY{match.group(1)}MM{match.group(2)}DD"
    match = re.search(r"YYYY([^A-Z0-9]?)MM", compact)
    if match:
        return f"YYYY{match.group(1)}MM"
    # An example date such as '2020.03' or '1990-01-01' shows the expected shape.
    example = re.sub(r"^(?:예\)|예:|EX\)|EX:|E\.G\.)", "", compact)
    match = re.fullmatch(r"(?:19|20)\d{2}([-./]?)(0[1-9]|1[0-2])(?:([-./]?)(0[1-9]|[12]\d|3[01]))?", example)
    if match:
        separator, _, day_separator, day = match.groups()
        if day is None:
            return f"YYYY{separator}MM"
        if separator == (day_separator or ""):
            return f"YYYY{separator}MM{separator}DD"
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


def format_like_example(example: str | None, iso_value: str) -> str | None:
    """Rewrite an ISO date in the shape of an example such as '2026. 10' or '2026-10-20'."""
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", iso_value or "")
    parts = re.split(r"(\d+)", (example or "").strip())
    numbers = parts[1::2]
    if not match or len(numbers) not in {2, 3} or len(numbers[0]) != 4:
        return None
    values = list(match.groups())[: len(numbers)]
    parts[1::2] = values
    return "".join(parts)


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


def plan_widget_action(field: FormField, resume_path: str, value: str, part: str | None = None) -> WidgetAction:
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

    if resume_path == "person.phone" and part:
        segments = phone_segments(value)
        index = int(part.split("/", 1)[0]) - 1
        if segments is None or index >= len(segments):
            return _human(field, resume_path, "휴대폰 번호를 칸별로 나눌 수 없습니다")
        segment = segments[index]
        if kind == "select":
            option = _match_number_option(field.options, int(segment)) if index == 0 else None
            if option is None or re.sub(r"\D", "", option.value or option.text) != segment:
                return _human(field, resume_path, "휴대폰 앞자리 선택지를 확정할 수 없습니다")
            return WidgetAction(**base, action="select_option", value=option.value)
        if kind != "text" or (field.maxlength is not None and len(segment) > field.maxlength):
            return _human(field, resume_path, "휴대폰 번호 칸의 길이가 맞지 않습니다")
        return WidgetAction(**base, action="fill", value=segment)

    if kind == "custom_combobox":
        # Search boxes for schools/majors/skills must commit a listed option; contact fields such as
        # an e-mail box with domain suggestions accept the typed text as-is.
        if resume_path.startswith("education.") or resume_path == "skills":
            return WidgetAction(**base, action="choose_suggestion", value=value)
        return WidgetAction(**base, action="fill", value=value)
    if kind == "custom_select":
        return WidgetAction(**base, action="choose_custom_option", value=value)
    if kind in {"select", "radio_group", "aria_radio_group"}:
        option = match_option(field.options, value, resume_path)
        if option is None:
            return _human(field, resume_path, "일치하는 선택지를 하나로 확정할 수 없습니다")
        if kind == "aria_radio_group":
            return WidgetAction(**base, action="choose_aria_radio", value=option.text)
        action = "select_option" if kind == "select" else "check_radio"
        return WidgetAction(**base, action=action, value=option.value)
    if kind in {"text", "textarea"}:
        if resume_path == "person.phone":
            value = format_phone_value(field, value)
        fallback = repair_text_value(field, resume_path, value) if kind == "text" else None
        return WidgetAction(**base, action="fill", value=value, fallback_value=fallback)
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

