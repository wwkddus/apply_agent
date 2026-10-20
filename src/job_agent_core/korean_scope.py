from __future__ import annotations

import re

from .constants import ALLOWED_RESUME_PATHS, EDUCATION_FIELDS, EDUCATION_LEVELS
from .mapping import FormField, INSTRUCTION_RE, MappingRecord, MappingResult
from .models import PersonalInfo, ResumeProfile


KOREAN_MVP_PATHS = ALLOWED_RESUME_PATHS

KOREAN_MVP_FIELD_LABELS = {
    "person.name.full": "성명",
    "person.name.family": "성",
    "person.name.given": "이름",
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


class KoreanMvpMapper:
    """Deterministic mapper ported from G001_장연_AIAgent.ipynb fallback rules."""

    async def map_fields(self, fields: list[FormField]) -> MappingResult:
        records = [_route(self._map_one(field), field) for field in fields]
        seen: set[str] = set()
        result: list[MappingRecord] = []
        for record in records:
            if record.status == "auto_accepted" and record.resume_path in seen:
                record = record.model_copy(
                    update={"status": "needs_human_review", "reason": "동일한 이력서 항목에 중복 매핑됨"}
                )
            elif record.status == "auto_accepted":
                seen.add(record.resume_path)
            result.append(record)
        return MappingResult(mappings=result)

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
