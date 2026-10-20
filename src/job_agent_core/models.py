from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .constants import EDUCATION_FIELDS, EDUCATION_LEVELS, FIELD_MAX_LENGTH, PERSONAL_FIELDS

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_MARKUP_RE = re.compile(r"<\s*/?\s*[a-zA-Z!?]|javascript\s*:|\bon[a-z]+\s*=", re.I)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _normalize_birth_date(value: str) -> str:
    match = re.fullmatch(r"(\d{4})[-./ ]?(\d{1,2})[-./ ]?(\d{1,2})\.?", value)
    if not match:
        match = re.fullmatch(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", value)
    if not match:
        raise ValueError("생년월일은 YYYYMMDD 또는 YYYY-MM-DD 형식이어야 합니다")
    year, month, day = map(int, match.groups())
    try:
        parsed = date(year, month, day)
    except ValueError:
        raise ValueError("실제 달력에 존재하는 생년월일이 아닙니다") from None
    if not 1900 <= year <= date.today().year:
        raise ValueError("생년 연도가 허용 범위를 벗어났습니다")
    return parsed.isoformat()


def _normalize_year_month(value: str) -> str:
    match = re.fullmatch(r"(\d{4})[-./ ]?(\d{1,2})\.?", value)
    if not match:
        match = re.fullmatch(r"(\d{4})\s*년\s*(\d{1,2})\s*월", value)
    if not match:
        raise ValueError("날짜는 YYYY-MM 형식이어야 합니다")
    year, month = map(int, match.groups())
    if not (1900 <= year <= date.today().year + 10 and 1 <= month <= 12):
        raise ValueError("유효한 YYYY-MM 날짜가 아닙니다")
    return f"{year:04d}-{month:02d}"


def _normalize_phone(value: str) -> str:
    if not re.fullmatch(r"[0-9+\-(). ]+", value):
        raise ValueError("전화번호에 지원하지 않는 문자가 포함되어 있습니다")
    count = len(re.sub(r"\D", "", value))
    if not 6 <= count <= 15:
        raise ValueError("전화번호에는 숫자가 6~15자리 포함되어야 합니다")
    return value


def _normalize_number(value: str) -> str:
    if not re.fullmatch(r"\d{1,3}(?:\.\d{1,2})?", value) or float(value) > 100:
        raise ValueError("3.85 또는 4.5와 같은 숫자 형식이어야 합니다")
    return value


def clean_field(field: str, value: object) -> str:
    if value is None:
        value = ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("텍스트 형식이어야 합니다")
    text = " ".join(str(value).split())
    if _CONTROL_RE.search(text):
        raise ValueError("제어 문자는 사용할 수 없습니다")
    if _MARKUP_RE.search(text):
        raise ValueError("HTML 또는 JavaScript 마크업은 사용할 수 없습니다")
    limit = FIELD_MAX_LENGTH.get(field, 200)
    if len(text) > limit:
        raise ValueError(f"{limit}자를 초과할 수 없습니다")
    if not text:
        return ""
    if field == "birth_date":
        return _normalize_birth_date(text)
    if field in ("admission_date", "graduation_date"):
        return _normalize_year_month(text)
    if field == "phone":
        return _normalize_phone(text)
    if field in ("email", "email_secondary") and not _EMAIL_RE.fullmatch(text):
        raise ValueError("올바른 이메일 형식이 아닙니다")
    if field == "postal_code":
        compact = re.sub(r"\s", "", text)
        if not re.fullmatch(r"[A-Za-z0-9-]{3,10}", compact):
            raise ValueError("올바른 우편번호 형식이 아닙니다")
        return compact
    if field in ("gpa", "gpa_scale"):
        return _normalize_number(text)
    return text


class PersonName(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full: str = ""
    family: str = ""
    given: str = ""
    middle: str = ""
    order: Literal["family_given", "given_family", "unknown"] = "unknown"
    split_confidence: float = 0.0
    split_method: str = "not_split"
    confirmed: bool = False

    @field_validator("full", "family", "given", "middle", mode="before")
    @classmethod
    def _clean_names(cls, value, info):
        return clean_field(info.field_name, value)

    @field_validator("split_confidence")
    @classmethod
    def _confidence(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("이름 분리 신뢰도는 0과 1 사이여야 합니다")
        return value


class PersonalInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: PersonName = Field(default_factory=PersonName)
    name_en: PersonName = Field(default_factory=PersonName)
    birth_date: str = ""
    phone: str = ""
    email: str = ""
    email_secondary: str = ""
    postal_code: str = ""
    address: str = ""
    address_detail: str = ""
    military_status: str = ""
    disability_status: str = ""
    veteran_status: str = ""

    @field_validator("name", mode="before")
    @classmethod
    def _legacy_name(cls, value):
        if isinstance(value, str):
            return {"full": value}
        return value

    @field_validator(*PERSONAL_FIELDS, mode="before")
    @classmethod
    def _clean(cls, value, info):
        return clean_field(info.field_name, value)


class EducationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    education_level: Literal["university", "graduate_school"]
    school_name: str = ""
    campus_type: str = ""
    degree: str = ""
    major: str = ""
    double_major: str = ""
    admission_type: str = ""
    admission_date: str = ""
    graduation_status: str = ""
    graduation_date: str = ""
    gpa: str = ""
    gpa_scale: str = ""
    region: str = ""

    @field_validator(*EDUCATION_FIELDS, mode="before")
    @classmethod
    def _clean(cls, value, info):
        return clean_field(info.field_name, value)

    @model_validator(mode="after")
    def _gpa_within_scale(self):
        if self.gpa and self.gpa_scale and float(self.gpa) > float(self.gpa_scale):
            raise ValueError("학점이 만점보다 클 수 없습니다")
        return self


class ResumeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    personal: PersonalInfo = Field(default_factory=PersonalInfo)
    education: list[EducationRecord] = Field(default_factory=list)
    skills: str = ""

    @field_validator("skills", mode="before")
    @classmethod
    def _clean_skills(cls, value):
        return clean_field("skills", value)

    @model_validator(mode="after")
    def _one_record_per_level(self):
        levels = [record.education_level for record in self.education]
        if len(levels) != len(set(levels)):
            raise ValueError("학력 단계별 기록은 하나만 저장할 수 있습니다")
        self.education.sort(key=lambda record: EDUCATION_LEVELS.index(record.education_level))
        return self


def flatten_resume_profile(profile: ResumeProfile) -> dict[str, str]:
    result = {
        "name": profile.personal.name.full,
        "name_full": profile.personal.name.full,
        "name_family": profile.personal.name.family,
        "name_given": profile.personal.name.given,
        **{field: getattr(profile.personal, field) for field in PERSONAL_FIELDS},
        "skills": profile.skills,
    }
    for level in EDUCATION_LEVELS:
        record = next((item for item in profile.education if item.education_level == level), None)
        for field in EDUCATION_FIELDS:
            result[f"{level}_{field}"] = getattr(record, field) if record else ""
    result["university"] = result["university_school_name"]
    result["major"] = result["university_major"]
    return result

