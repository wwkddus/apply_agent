# [역할] 칸 종류별 입력 방법 결정. '값'을 칸의 종류(텍스트, 선택 목록, 라디오, 날짜, 검색 콤보 등)에 맞는 '동작'으로 바꿉니다.
# 예: 졸업일 2024-02 → 'YYYY.MM' 텍스트면 '2024.02', 연/월 선택 목록이면 각각 선택. 확정할 수 없으면 사람에게 넘깁니다.

from __future__ import annotations

import calendar
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .constants import DATE_PATHS
from .mapping import FormField, FormOption


# 칸 하나에 대한 구체적인 입력 동작(채우기/선택/라디오/날짜 등). 확정 못 하면 status='needs_human'.
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
    # Set only for a value the user picked from page candidates: search with this text, then
    # accept the first option whose text is identical to the pick.
    search_text: str | None = None
    chosen: bool = False
    status: Literal["planned", "needs_human"] = "planned"
    reason: str = ""
    inferred: str | None = None

    # 로그용 요약(값 자체는 빼고 '있음/없음'만).
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


# 선택지 비교용 정규화(공백 제거, 따옴표 통일, 소문자).
def _norm_option(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).replace("’", "'").replace("‘", "'").casefold()


# Equivalent wordings for enumerated education values (resume wording -> page wording).
# 같은 뜻의 다른 표기 묶음. 이력서의 '재학'과 사이트의 'Enrolled', '비대상'과 'Not applicable'을 같은 값으로 봅니다.
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
    "highest_level": (
        ("학사", "대학교", "4년제", "대학교(4년)", "대학교(4년제)", "대학(4년)", "university", "university(bachelor's)", "bachelor", "bachelor's"),
        ("전문학사", "전문대", "전문대학", "2년제", "3년제", "대학(2,3년)", "college", "college(diploma)", "diploma", "associate"),
        ("석사", "대학원(석사)", "석사과정", "postgraduateschool(master's)", "graduateschool(master's)", "master", "master's"),
        ("박사", "대학원(박사)", "박사과정", "postgraduateschool(doctoral)", "graduateschool(doctoral)", "doctor", "doctoral", "phd"),
        ("대학원", "postgraduateschool", "graduateschool"),
        ("고등학교", "highschool"),
    ),
    "military_status": (
        ("비대상", "해당없음", "비해당", "대상아님", "notapplicable", "n/a", "none"),
        ("군필", "만기전역", "전역", "병역필", "필", "fulfilled", "completed", "served", "discharged"),
        ("미필", "미필자", "notserved", "notcompleted", "unfulfilled"),
        ("면제", "병역면제", "exempted", "exemption", "exempt"),
        ("복무중", "재복무중", "현역", "serving", "inservice"),
    ),
    "disability_status": (
        ("비대상", "해당없음", "비해당", "대상아님", "없음", "비장애", "장애없음", "notapplicable", "no", "none", "n/a"),
        ("대상", "해당", "있음", "장애", "applicable", "yes"),
    ),
    "veteran_status": (
        ("비대상", "해당없음", "비해당", "대상아님", "없음", "비보훈", "notapplicable", "no", "none", "n/a"),
        ("대상", "해당", "있음", "보훈대상", "applicable", "yes"),
    ),
    "admission_type": (
        ("입학", "신입학", "정시", "수시", "regular", "freshman"),
        ("편입", "편입학", "transfer"),
    ),
    # 학교 소재지(실측: SK는 영어 선택지 'Seoul'). 도를 시보다 앞에 둡니다: '경기 광주'는 경기도입니다.
    "region": (
        ("경기", "경기도", "gyeonggi", "gyeonggi-do", "gyeonggido", "kyonggi", "kyonggi-do"),
        ("강원", "강원도", "강원특별자치도", "gangwon", "gangwon-do", "gangwondo", "kangwon", "kangwon-do"),
        ("충북", "충청북도", "chungbuk", "chungcheongbuk-do", "chungcheongbukdo", "northchungcheong", "chungcheongnorth"),
        ("충남", "충청남도", "chungnam", "chungcheongnam-do", "chungcheongnamdo", "southchungcheong", "chungcheongsouth"),
        ("전북", "전라북도", "전북특별자치도", "jeonbuk", "jeollabuk-do", "jeollabukdo", "northjeolla", "jeollanorth"),
        ("전남", "전라남도", "jeonnam", "jeollanam-do", "jeollanamdo", "southjeolla", "jeollasouth"),
        ("경북", "경상북도", "gyeongbuk", "gyeongsangbuk-do", "gyeongsangbukdo", "northgyeongsang", "gyeongsangnorth"),
        ("경남", "경상남도", "gyeongnam", "gyeongsangnam-do", "gyeongsangnamdo", "southgyeongsang", "gyeongsangsouth"),
        ("제주", "제주도", "제주특별자치도", "jeju", "jeju-do", "jejudo", "cheju"),
        ("서울", "서울시", "서울특별시", "seoul"),
        ("부산", "부산시", "부산광역시", "busan", "pusan"),
        ("대구", "대구시", "대구광역시", "daegu", "taegu"),
        ("인천", "인천시", "인천광역시", "incheon", "inchon"),
        ("광주", "광주시", "광주광역시", "gwangju", "kwangju"),
        ("대전", "대전시", "대전광역시", "daejeon", "taejon"),
        ("울산", "울산시", "울산광역시", "ulsan"),
        ("세종", "세종시", "세종특별자치시", "sejong"),
        ("해외", "국외", "overseas", "abroad", "foreign"),
    ),
}


# 괄호 설명을 지운 값('학사(4년제)' → '학사').
def _strip_note(value: str) -> str:
    """'학사(4년제)' -> '학사' so a parenthetical note does not block an exact match."""
    return re.sub(r"\([^)]*\)|\[[^\]]*\]", "", value or "")


# 값과 같은 뜻인 표기 전체를 구합니다. 병역처럼 긴 문장('육군 만기전역')은 그 안의 가장 긴 표기로 판단.
def _equivalents(path: str | None, value: str) -> set[str]:
    target = _norm_option(_strip_note(value))
    field = (path or "").rsplit(".", 1)[-1]
    groups = OPTION_SYNONYMS.get(field, ())
    for group in groups:
        if target in {_norm_option(item) for item in group}:
            return {_norm_option(item) for item in group}
    if (field.endswith("_status") and field != "graduation_status") or field == "region":
        # '육군 만기전역' -> 군필, '서울특별시 종로구' -> 서울: the longest known wording inside the resume text decides.
        hits = [(len(_norm_option(item)), group) for group in groups for item in group
                if len(_norm_option(item)) >= 2 and _norm_option(item) in target]
        if field == "region":
            # An address starts with its top-level region: '경기 광주시' is Gyeonggi, not Gwangju city.
            hits = [(size, group) for size, group in hits if any(target.startswith(_norm_option(item)) for item in group)]
        if hits:
            return {_norm_option(item) for item in max(hits, key=lambda hit: hit[0])[1]}
    return {target}


# 선택지 고르기. 정확히 하나로 정해질 때만 고르고, 아니면 후보를 돌려줍니다.
# 순서: 사용자가 직접 고른 값 → 학점 숫자 비교 → 같은 뜻 표기 일치 → 부분 일치(하나뿐일 때만).
def pick_option(
    texts: list[str], value: str, path: str | None = None, *, chosen: bool = False
) -> tuple[int | None, list[str]]:
    """Pick exactly one option text for a resume value; otherwise return the plausible candidates.

    chosen=True means the user picked this exact text: identical duplicates (the same major listed
    under two categories) are indistinguishable to the user, so the first one is accepted.
    """
    usable = [(index, text) for index, text in enumerate(texts) if _norm_option(text)]
    if chosen:
        identical = [index for index, text in usable if " ".join(text.split()) == " ".join(value.split())]
        if identical:
            return identical[0], []
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


# 선택지의 value가 정확히 같으면 바로, 아니면 화면 문구로 pick_option.
def match_option(options: list[FormOption], value: str, path: str | None = None) -> FormOption | None:
    real = [option for option in options if option.value.strip()]
    target = _norm_option(value)
    by_value = [option for option in real if _norm_option(option.value) == target]
    if len(by_value) == 1:
        return by_value[0]
    index, _ = pick_option([option.text for option in real], value, path)
    return real[index] if index is not None else None


# 숫자 선택지(연도·월·일, 010 등)에서 같은 숫자를 가진 선택지가 하나일 때만 선택.
def _match_number_option(options: list[FormOption], number: int) -> FormOption | None:
    matches = []
    for option in options:
        if not option.value.strip():
            continue
        values = [re.sub(r"\D", "", option.value), re.sub(r"\D", "", option.text)]
        if any(value and int(value) == number for value in values):
            matches.append(option)
    return matches[0] if len(matches) == 1 else None


# 휴대폰 번호를 [앞, 가운데, 끝] 3조각으로. +82는 0으로 바꿉니다.
def phone_segments(value: str) -> list[str] | None:
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("82"):
        digits = "0" + digits[2:]
    if len(digits) == 11:
        return [digits[:3], digits[3:7], digits[7:]]
    if len(digits) == 10:
        return [digits[:3], digits[3:6], digits[6:]]
    return None


# 칸의 규칙(pattern, placeholder, 최대 길이)을 보고 하이픈 포함/숫자만 형식을 고릅니다.
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


# 텍스트 날짜 칸의 형식 추정: placeholder('YYYY.MM'), 예시('2020.03'), pattern, 최대 길이 순서로 확인.
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


# 예시 모양 그대로 날짜를 다시 씁니다('2026. 10' 같은 특이한 띄어쓰기까지 유지).
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


# ISO 날짜를 목표 형식(YYYY.MM 등)으로 변환. 일(DD)이 필요한데 없으면 None.
def format_date_value(iso_value: str, target_format: str | None) -> str | None:
    match = re.fullmatch(r"(\d{4})-(\d{2})(?:-(\d{2}))?", iso_value or "")
    if not match or not target_format:
        return None
    year, month, day = match.groups()
    if "DD" in target_format and not day:
        return None
    return target_format.replace("YYYY", year).replace("MM", month).replace("DD", day or "")


# '사람이 직접 처리' 동작을 만드는 도우미.
def _human(field: FormField, path: str, reason: str) -> WidgetAction:
    return WidgetAction(
        form_field=field.key,
        resume_path=path,
        widget_kind=field.widget_kind,
        status="needs_human",
        reason=reason,
    )


# 값 → 칸 종류별 동작 변환(브라우저는 건드리지 않음). 흐름:
# ① 파일·체크박스·읽기 전용은 사람에게 → ② 날짜(연월만 있으면 입학은 1일, 졸업은 말일로 보충)
# → ③ 휴대폰 3칸 → ④ 검색 콤보·커스텀 선택 → ⑤ 선택 목록·라디오 → ⑥ 일반 텍스트.
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


# 브라우저가 형식 오류를 낼 때 쓸 대체 값. 전화번호·우편번호 형식만 고치고, 이름 같은 의미 있는 값은 절대 바꾸지 않습니다.
def repair_text_value(field: FormField, resume_path: str, current_value: str) -> str | None:
    """Only repair low-risk formatting; never alter semantic identity fields."""
    pattern = field.pattern

    # 후보가 현재 값과 다르고 칸의 pattern에 맞는지.
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

