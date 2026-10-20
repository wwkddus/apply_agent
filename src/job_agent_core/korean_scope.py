from __future__ import annotations

import re

from .attribute_semantics import UNMAPPED, infer_from_attributes
from .constants import ALLOWED_RESUME_PATHS, EDUCATION_FIELDS, EDUCATION_LEVELS
from .mapping import FormField, INSTRUCTION_RE, MappingRecord, MappingResult
from .models import PersonalInfo, ResumeProfile


KOREAN_MVP_PATHS = ALLOWED_RESUME_PATHS

KOREAN_MVP_FIELD_LABELS = {
    "person.name.full": "성명",
    "person.name.family": "성",
    "person.name.given": "이름",
    "person.name_en.full": "영문 성명",
    "person.name_en.family": "영문 성",
    "person.name_en.given": "영문 이름",
    "person.birth_date": "생년월일",
    "person.phone": "휴대폰번호",
    "person.email": "이메일",
    "person.postal_code": "우편번호",
    "person.address": "주소",
    "person.address_detail": "상세주소",
    "skills": "보유기술",
}


_SITE_EDU_RULES = (
    ("double_major", r"복수\s*전공|부\s*전공|double\s*major|minor"),
    ("admission_type", r"입학\s*구분|admission\s*type"),
    ("graduation_status", r"졸업\s*(구분|여부|상태)|graduation\s*status"),
    ("gpa_scale", r"만점|gpa\s*scale"),
    (
        "school_name",
        r"학교\s*명|대학교\s*명|대학원\s*명|school\s*name|university\s*name|college\s*name|institution",
    ),
    ("degree", r"학위|degree"),
    ("major", r"전공|학과|major|field\s*of\s*study|department"),
    ("admission_date", r"입학|admission|enrol"),
    ("graduation_date", r"졸업|graduat"),
    ("gpa", r"평점|학점|gpa"),
    ("campus_type", r"본교|분교|campus"),
    ("region", r"지역|소재지|region|location"),
    ("school_name", r"학교|school|university|college"),
)

_SITE_PERSONAL_RULES = (
    ("person.birth_date", r"생년월일|출생|birth"),
    ("person.address_detail", r"상세\s*주소|address\s*(line\s*2|detail)"),
    ("person.postal_code", r"우편\s*번호|postal|zip"),
    ("person.email", r"이메일|e-?mail"),
    ("person.address", r"주소|address"),
    ("person.phone", r"휴대|핸드폰|전화|연락처|phone|mobile|\btel\b"),
    ("skills", r"보유\s*기술|기술\s*스택|skills?"),
    ("person.name.full", r"성명|이름|full\s*name|applicant\s*name|^name$"),
)

_GRAD_RE = re.compile(r"대학원|graduate|master|석사|박사|doctor", re.I)
_UNIV_RE = re.compile(r"대학교|대학|학부|undergraduate|bachelor|학사|university|college", re.I)


def _route(record: MappingRecord, field: FormField) -> MappingRecord:
    if record.resume_path == "unmapped":
        status = "unmapped_required" if field.required else "unmapped_optional"
    elif record.confidence >= 0.8:
        status = "auto_accepted"
    else:
        status = "needs_human_review"
    return record.model_copy(update={"status": status})


# Contact details of someone other than the applicant must never receive the applicant's values.
_THIRD_PARTY_RE = re.compile(r"비상|긴급|보호자|추천인|가족|부모|배우자|emergency|guardian|referee|reference|parent|spouse", re.I)
_LABEL_NOISE_RE = re.compile(r"\(?\s*필수\s*\)?|\*|[:：()\[\]]")
_SCRIPT_QUALIFIER_RE = re.compile(r"영문|한자|한글|국문|english|korean|chinese")
_ENGLISH_RE = re.compile(r"영문|english")
_OTHER_SCRIPT_RE = re.compile(r"한자|chinese")
_NAME_ROLES = {
    "성": "family",
    "姓": "family",
    "lastname": "family",
    "familyname": "family",
    "surname": "family",
    "firstname": "given",
    "givenname": "given",
    "名": "given",
    "이름": "name",
    "name": "name",
    "성명": "full",
    "성함": "full",
    "fullname": "full",
    "applicantname": "full",
    "지원자명": "full",
    "지원자이름": "full",
    "지원자성명": "full",
}


def _from_attribute(record: MappingRecord) -> bool:
    return record.reason.startswith("attribute:")


def _bare(value: str | None) -> str:
    return re.sub(r"\s+", "", _LABEL_NOISE_RE.sub("", value or "")).casefold()


def _name_role(field: FormField) -> str | None:
    """Classify a name field from its own label, using the placeholder only to refine it."""
    label = _bare(field.label)
    role = _NAME_ROLES.get(_SCRIPT_QUALIFIER_RE.sub("", label))
    if role is None:
        return None
    if _OTHER_SCRIPT_RE.search(label):
        return "other_script"
    hint = _NAME_ROLES.get(_bare(field.placeholder))
    if role in {"full", "name"} and hint in {"family", "given"}:
        role = hint
    return f"en_{role}" if _ENGLISH_RE.search(label) else role


def _position(key: str) -> tuple[str, int | None]:
    prefix, _, number = key.rpartition("-")
    return (prefix, int(number)) if number.isdigit() else (key, None)


def _adjacent(first: FormField, second: FormField, index_gap: int, limit: int) -> bool:
    (prefix_a, number_a), (prefix_b, number_b) = _position(first.key), _position(second.key)
    if number_a is None or number_b is None:
        return abs(index_gap) <= limit
    return prefix_a == prefix_b and abs(number_a - number_b) <= limit


def _phone_head(field: FormField) -> bool:
    if field.widget_kind == "select":
        return any(re.sub(r"\D", "", option.value or option.text) == "010" for option in field.options)
    return field.widget_kind == "text" and field.maxlength == 3


def _phone_tail(field: FormField, record: MappingRecord, head: FormField) -> bool:
    if field.widget_kind != "text" or field.maxlength != 4:
        return False
    if record.resume_path == "person.phone":
        return True
    label = _bare(field.label)
    return record.resume_path == "unmapped" and (label in {"", _bare(head.label)} or re.fullmatch(r"[-~\d]*", label) is not None)


class KoreanMvpMapper:
    """Deterministic mapper ported from G001_장연_AIAgent.ipynb fallback rules."""

    async def map_fields(self, fields: list[FormField]) -> MappingResult:
        records = [self._map_one(field) for field in fields]
        records = self._apply_name_structure(fields, records)
        records = self._apply_phone_segments(fields, records)
        records = self._apply_third_party_guard(fields, records)
        records = [_route(record, field) for record, field in zip(records, fields)]
        whole: set[str] = set()
        parts: set[tuple[str, str]] = set()
        result: list[MappingRecord] = []
        for record in records:
            path, part = record.resume_path, record.part
            # A value used whole conflicts with any segmented use of the same value and vice versa.
            duplicate = path in whole or (part is None and any(item[0] == path for item in parts)) or (path, part) in parts
            if record.status == "auto_accepted" and duplicate:
                record = record.model_copy(
                    update={"status": "needs_human_review", "reason": "동일한 이력서 항목에 중복 매핑됨"}
                )
            elif record.status == "auto_accepted":
                if part is None:
                    whole.add(path)
                else:
                    parts.add((path, part))
            result.append(record)
        return MappingResult(mappings=result)

    def _apply_third_party_guard(self, fields: list[FormField], records: list[MappingRecord]) -> list[MappingRecord]:
        return [
            self._unmapped(field, "structure: 지원자 본인이 아닌 사람(비상연락처·보호자·추천인 등)의 정보")
            if record.resume_path.startswith("person.")
            and not _from_attribute(record)
            and _THIRD_PARTY_RE.search(f"{field.section} {field.label}")
            else record
            for field, record in zip(fields, records)
        ]

    def _apply_name_structure(self, fields: list[FormField], records: list[MappingRecord]) -> list[MappingRecord]:
        """'성' + '이름' next to each other are family/given; a lone '이름' is the full name."""
        roles = [_name_role(field) for field in fields]
        updated: list[MappingRecord] = []
        for index, (field, record, role) in enumerate(zip(fields, records, roles)):
            blocked = record.resume_path == "unmapped" and record.reason != "fallback: no rule matched"
            if role is None or blocked or _from_attribute(record):
                updated.append(record)
                continue
            if role == "other_script":
                updated.append(self._unmapped(field, "structure: 한자 이름은 이력서에 없는 표기라 입력하지 않음"))
                continue
            # English-qualified labels ('영문 성', 'English name') use the romanized name.
            english = role.startswith("en_")
            base, prefix = ("person.name_en", "영문 ") if english else ("person.name", "")
            role = role.removeprefix("en_")
            families = [other for other, item in enumerate(roles) if item == ("en_family" if english else "family")]
            if role == "family":
                updated.append(self._mapped(field, f"{base}.family", 0.95, f"structure: {prefix}성 필드"))
            elif role == "given":
                updated.append(self._mapped(field, f"{base}.given", 0.95, f"structure: {prefix}이름(given) 필드"))
            elif role == "name" and any(_adjacent(fields[other], field, other - index, 2) for other in families):
                updated.append(self._mapped(field, f"{base}.given", 0.95, f"structure: 인접한 {prefix}성 필드와 짝을 이루는 이름"))
            else:
                updated.append(self._mapped(field, f"{base}.full", 0.9, f"structure: 단일 {prefix}이름 필드는 전체 이름"))
        return updated

    def _apply_phone_segments(self, fields: list[FormField], records: list[MappingRecord]) -> list[MappingRecord]:
        """Recognise the common Korean 010 / 1234 / 5678 three-box phone input."""
        updated = list(records)
        index = 0
        while index + 2 < len(fields):
            head, *tail = fields[index : index + 3]
            if (
                records[index].resume_path == "person.phone"
                and _phone_head(head)
                and all(_adjacent(head, item, offset + 1, 2) for offset, item in enumerate(tail))
                and all(_phone_tail(item, records[index + 1 + offset], head) for offset, item in enumerate(tail))
            ):
                for offset in range(3):
                    updated[index + offset] = self._mapped(
                        fields[index + offset],
                        "person.phone",
                        0.92,
                        f"structure: 휴대폰 번호 {offset + 1}/3 칸",
                    ).model_copy(update={"part": f"{offset + 1}/3"})
                index += 3
                continue
            index += 1
        return updated

    def _map_one(self, field: FormField) -> MappingRecord:
        raw = " ".join(
            value
            for value in [field.label, field.placeholder or "", field.name or "", field.element_id or ""]
            if value
        )
        if INSTRUCTION_RE.search(raw):
            return self._unmapped(field, "웹페이지의 지시문처럼 보이는 문구는 신뢰하지 않음")
        if field.widget_kind == "checkbox":
            return self._unmapped(field, "fallback: checkbox (consent/decision) is never mapped to resume data")
        hint = infer_from_attributes(field.name, field.element_id)
        if hint is not None:
            # A structured name/id path outranks a visual label that may belong to a neighbouring column.
            if hint.resume_path == UNMAPPED:
                return self._unmapped(field, hint.reason)
            return self._mapped(field, hint.resume_path, 0.95, hint.reason)

        lowered = raw.casefold()
        context = f"{field.section} {field.label}"
        long_label = len(field.label.split()) > 8
        education = next((suffix for suffix, pattern in _SITE_EDU_RULES if re.search(pattern, lowered, re.I)), None)
        personal = next((path for path, pattern in _SITE_PERSONAL_RULES if re.search(pattern, lowered, re.I)), None)
        school_word_present = re.search(r"학교|school|university|college|대학", lowered, re.I)

        if education and not (personal and education == "school_name" and not school_word_present):
            level = (
                "graduate_school"
                if _GRAD_RE.search(context)
                else "university"
                if _UNIV_RE.search(context)
                else None
            )
            # The notebook defaults an unknown education level to university. Keep that
            # mapping, but let an explicit short education label enter the deterministic
            # baseline instead of making every sectionless real-site field non-actionable.
            confidence = 0.6 if long_label else 0.9
            return self._mapped(
                field,
                f"education.{level or 'university'}.{education}",
                confidence,
                f"fallback: education rule '{education}', level={'from section/label' if level else 'unknown → university (baseline correction)'}",
            )
        if personal:
            return self._mapped(
                field,
                personal,
                0.6 if long_label else 0.9,
                f"fallback: personal rule '{personal.rsplit('.', 1)[-1]}'"
                + (" (long label)" if long_label else ""),
            )
        return self._unmapped(field, "fallback: no rule matched")

    @staticmethod
    def _mapped(field: FormField, path: str, confidence: float, reason: str) -> MappingRecord:
        return MappingRecord(
            form_field=field.key,
            resume_path=path,
            confidence=confidence,
            reason=reason,
            source="notebook_fallback_rule",
        )

    @staticmethod
    def _unmapped(field: FormField, reason: str) -> MappingRecord:
        return MappingRecord(
            form_field=field.key,
            resume_path="unmapped",
            confidence=0,
            reason=reason,
            source="notebook_fallback_rule",
        )


def enforce_korean_mvp_profile(profile: ResumeProfile) -> tuple[ResumeProfile, list[str]]:
    """Validate Korean contact formats while preserving notebook-supported education."""
    warnings: list[str] = []
    personal_data = profile.personal.model_dump()
    postal_code = profile.personal.postal_code
    if postal_code and not re.fullmatch(r"\d{5}", postal_code):
        personal_data["postal_code"] = ""
        warnings.append("postal_code: 한국 우편번호 5자리 형식이 아니어서 자동 입력에서 제외됨")
    phone = profile.personal.phone
    digits = re.sub(r"\D", "", phone)
    if phone and not (re.fullmatch(r"010\d{8}", digits) or re.fullmatch(r"8210\d{8}", digits)):
        personal_data["phone"] = ""
        warnings.append("phone: 한국 휴대폰 번호 형식이 아니어서 자동 입력에서 제외됨")
    personal = PersonalInfo.model_validate(personal_data)
    return profile.model_copy(update={"personal": personal}), warnings


def korean_mvp_values(profile: ResumeProfile) -> dict[str, str]:
    name = profile.personal.name
    values = {
        "person.name.full": name.full,
        "person.name.family": name.family,
        "person.name.given": name.given,
        "person.name_en.full": profile.personal.name_en.full,
        "person.name_en.family": profile.personal.name_en.family,
        "person.name_en.given": profile.personal.name_en.given,
        "person.birth_date": profile.personal.birth_date,
        "person.phone": profile.personal.phone,
        "person.email": profile.personal.email,
        "person.postal_code": profile.personal.postal_code,
        "person.address": profile.personal.address,
        "person.address_detail": profile.personal.address_detail,
        "skills": profile.skills,
    }
    for level in EDUCATION_LEVELS:
        record = next((item for item in profile.education if item.education_level == level), None)
        for field in EDUCATION_FIELDS:
            values[f"education.{level}.{field}"] = getattr(record, field) if record else ""
    return values
