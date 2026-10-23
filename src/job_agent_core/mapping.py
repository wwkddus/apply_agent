# [역할] 칸 연결(매핑) 기본 모듈. 웹 폼의 칸 정보(FormField)와 연결 결과(MappingRecord)를 정의하고,
# 라벨 문구로 '이 칸 = 이력서의 어떤 항목'을 정하는 기본 규칙 매퍼를 제공합니다.

from __future__ import annotations

import re
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .constants import ALLOWED_RESUME_PATHS, AUTO_ACCEPT_CONFIDENCE


# 선택형 칸의 선택지 하나(값 + 화면 문구).
class FormOption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str
    text: str


# 화면에서 찾은 입력 칸 하나. 라벨, 종류, 필수 여부, 기존 값 여부, 선택지 등을 담습니다.
# 실제 값은 담지 않고 '값이 있는지'만 기록합니다.
class FormField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str = ""
    type: str = "text"
    widget_kind: str = "text"
    required: bool = False
    placeholder: str | None = None
    section: str = ""
    group_labels: list[str] = Field(default_factory=list)
    name: str | None = None
    element_id: str | None = None
    # name이 없는 칸(라디오 버튼 묶음 등)을 감싼 가장 가까운 블록 안 다른 칸들의 name. 학력 블록(대학/대학원) 판단용.
    context_names: list[str] = Field(default_factory=list)
    current_value_present: bool = False
    readonly: bool = False
    options: list[FormOption] = Field(default_factory=list)
    pattern: str | None = None
    maxlength: int | None = None
    date_format: str | None = None
    parts: dict[str, str] = Field(default_factory=dict)
    part_options: dict[str, list[FormOption]] = Field(default_factory=dict)
    # Stable identity across re-inspections; positional keys shift when the page adds fields.
    fingerprint: str = ""


# 칸 ↔ 이력서 경로 연결 결과. 신뢰도와 이유를 함께 남기고, status로 자동 수락/사람 확인을 구분합니다.
class MappingRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    form_field: str
    resume_path: str
    confidence: float
    reason: str
    source: str = "rule"
    part: str | None = None
    # 페이지에 대학 블록이 여러 개일 때 몇 번째 블록인지(0부터). 편입 전 대학은 입력 계획에서 이 순서로 정합니다.
    instance: int | None = None
    status: Literal[
        "auto_accepted",
        "needs_human_review",
        "unmapped_required",
        "unmapped_optional",
    ] = "needs_human_review"

    # 신뢰도는 0~1만 허용.
    @field_validator("confidence")
    @classmethod
    def _confidence(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("신뢰도는 0과 1 사이여야 합니다")
        return value

    # 허용된 이력서 경로만 받습니다(LLM이 엉뚱한 경로를 만들어도 여기서 차단).
    @field_validator("resume_path")
    @classmethod
    def _resume_path(cls, value: str) -> str:
        if value != "unmapped" and value not in ALLOWED_RESUME_PATHS:
            raise ValueError("지원하지 않는 이력서 필드 경로입니다")
        return value


# 여러 칸의 연결 결과 묶음.
class MappingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mappings: list[MappingRecord]


# AI만 연결하고 규칙은 판단하지 못한 칸의 출처 표시. 입력은 하되 「입력됨 · 확인 필요」로 남깁니다.
LLM_ONLY_SOURCE = "llm_only"


# 매퍼 공통 규격. 규칙 매퍼든 LLM 매퍼든 map_fields만 있으면 바꿔 끼울 수 있습니다(의존성 주입).
class MappingProvider(Protocol):
    """Provider-neutral contract for OpenAI, local or hosted open-weight models."""

    async def map_fields(self, fields: list[FormField]) -> MappingResult: ...


# 웹페이지에 숨은 '이전 지시 무시' 같은 문구(프롬프트 인젝션)를 찾는 패턴.
INSTRUCTION_RE = re.compile(
    r"ignore\s+(all\s+)?previous|system\s+prompt|developer\s+message|submit\s+(now|immediately)|"
    r"이전\s*지시|무시하고|[立即马上].{0,6}[提交提出]",
    re.I,
)


# 라벨·placeholder·name·id를 합친 판단용 문자열.
def _text(field: FormField) -> str:
    return " ".join(
        part
        for part in [field.label, field.placeholder or "", field.name or "", field.element_id or ""]
        if part
    ).strip()


# 공백·밑줄·하이픈을 지우고 소문자로 통일(표기 차이 무시).
def _compact(value: str) -> str:
    return re.sub(r"[\s_\-/]+", "", value or "").casefold()


# 주변 문구로 대학원/대학 구분. 단서가 없으면 대학으로 보되 신뢰도를 낮춰 사람 확인으로 보냅니다.
def _education_level(field: FormField) -> tuple[str, float]:
    context = f"{field.section} {field.label} {field.name or ''} {field.element_id or ''}"
    if re.search(r"대학원|graduate|master|석사|박사", context, re.I):
        return "graduate_school", 0.95
    if re.search(r"대학교|대학|학부|undergraduate|bachelor|university|college", context, re.I):
        return "university", 0.95
    return "university", 0.6


# 신뢰도에 따라 상태 결정: 기준 이상이면 자동 수락, 아니면 사람 확인.
def _route(record: MappingRecord, field: FormField) -> MappingRecord:
    if record.resume_path == "unmapped":
        status = "unmapped_required" if field.required else "unmapped_optional"
    elif record.confidence >= AUTO_ACCEPT_CONFIDENCE:
        status = "auto_accepted"
    else:
        status = "needs_human_review"
    return record.model_copy(update={"status": status})


# 규칙 기반 매퍼(Before 버전의 기준). LLM 없이 항상 같은 결과를 냅니다.
class RuleBasedMapper:
    """Deterministic multilingual mapper used before any optional LLM."""

    # 모든 칸을 연결한 뒤, 같은 이력서 값이 두 칸에 자동 연결되면 두 번째는 사람 확인으로 돌립니다.
    async def map_fields(self, fields: list[FormField]) -> MappingResult:
        records = [_route(self._map_one(field), field) for field in fields]
        seen: dict[str, MappingRecord] = {}
        routed: list[MappingRecord] = []
        for record in records:
            if record.status == "auto_accepted" and record.resume_path in seen:
                record = record.model_copy(
                    update={
                        "status": "needs_human_review",
                        "reason": f"중복 연결입니다. {seen[record.resume_path].form_field} 필드가 이미 같은 이력서 값을 사용합니다",
                    }
                )
            elif record.status == "auto_accepted":
                seen[record.resume_path] = record
            routed.append(record)
        return MappingResult(mappings=routed)

    # 칸 하나 연결. 순서: 지시문 의심 → 무시 / 학력 규칙 / 이름 규칙 / 개인정보 규칙 / 없으면 미연결.
    def _map_one(self, field: FormField) -> MappingRecord:
        raw = _text(field)
        compact = _compact(raw)
        label = _compact(field.label)
        siblings = {_compact(value) for value in field.group_labels}

        if INSTRUCTION_RE.search(raw):
            return MappingRecord(
                form_field=field.key,
                resume_path="unmapped",
                confidence=0.0,
                reason="지시문처럼 보이는 웹페이지 문구는 신뢰하지 않는 데이터로 처리합니다",
            )

        education_rules = (
            ("double_major", r"복수전공|부전공|doublemajor|minor"),
            ("admission_type", r"입학구분|admissiontype"),
            ("graduation_status", r"졸업(?:구분|여부|상태)|graduationstatus"),
            ("gpa_scale", r"만점|gpascale"),
            ("school_name", r"학교명|대학교명|대학원명|schoolname|universityname|institution"),
            ("degree", r"학위|degree"),
            ("major", r"전공|학과|major|fieldofstudy|department"),
            ("admission_date", r"입학년월|admissiondate|enrolmentdate"),
            ("graduation_date", r"졸업년월|graduationdate"),
            ("gpa", r"전체평점|평점|학점|gpa"),
            ("campus_type", r"본교|분교|campus"),
            ("region", r"소재지|지역|region"),
        )
        for suffix, pattern in education_rules:
            if re.search(pattern, compact, re.I):
                level, confidence = _education_level(field)
                return MappingRecord(
                    form_field=field.key,
                    resume_path=f"education.{level}.{suffix}",
                    confidence=confidence,
                    reason=f"라벨과 영역에서 학력 항목 '{suffix}' 의미를 확인했습니다",
                )

        if label in {"성함", "성명", "지원자이름", "fullname", "applicantname", "legalname"}:
            return MappingRecord(
                form_field=field.key,
                resume_path="person.name.full",
                confidence=0.97,
                reason="라벨이 지원자의 전체 이름을 요구합니다",
            )
        if label in {"성", "姓", "lastname", "familyname", "surname"}:
            return MappingRecord(
                form_field=field.key,
                resume_path="person.name.family",
                confidence=0.96,
                reason="라벨이 성을 명확히 요구합니다",
            )
        if label in {"firstname", "givenname", "名"}:
            return MappingRecord(
                form_field=field.key,
                resume_path="person.name.given",
                confidence=0.96,
                reason="라벨이 이름을 명확히 요구합니다",
            )
        if label == "이름":
            split_group = bool(siblings & {"성", "lastname", "familyname", "surname", "姓"})
            return MappingRecord(
                form_field=field.key,
                resume_path="person.name.given" if split_group else "person.name.full",
                confidence=0.94 if split_group else 0.9,
                reason="같은 그룹에 성 필드가 있을 때만 '이름'을 이름 부분으로 판단합니다",
            )

        personal_rules = (
            ("person.birth_date", r"생년월일|출생|dateofbirth|birthdate"),
            ("person.address_detail", r"상세주소|addressline2|addressdetail"),
            ("person.postal_code", r"우편번호|postalcode|zipcode"),
            ("person.address", r"주소|address"),
            ("person.email", r"이메일|email"),
            ("person.phone", r"휴대|핸드폰|전화|연락처|phone|mobile|tel"),
            ("skills", r"보유기술|기술스택|technicalskills|skills"),
        )
        for path, pattern in personal_rules:
            if re.search(pattern, compact, re.I):
                return MappingRecord(
                    form_field=field.key,
                    resume_path=path,
                    confidence=0.92,
                    reason=f"라벨의 의미가 {path} 항목과 일치합니다",
                )

        return MappingRecord(
            form_field=field.key,
            resume_path="unmapped",
            confidence=0.0,
            reason="일치하는 확정적 의미 규칙을 찾지 못했습니다",
        )


# 외부 모델에 보낼 수 있는 안전한 요약. 이력서 값·URL·선택자는 넣지 않고 길이도 자릅니다.
def build_mapping_payload(fields: list[FormField]) -> dict[str, object]:
    """Safe payload for any model backend: no resume values, URL or selectors."""
    return {
        "allowed_resume_paths": sorted(ALLOWED_RESUME_PATHS) + ["unmapped"],
        "form_fields": [
            {
                "form_field": field.key,
                "label": field.label[:200],
                "section": field.section[:120],
                "group_labels": [label[:80] for label in field.group_labels[:10]],
                "type": field.type,
                "widget_kind": field.widget_kind,
                "placeholder": (field.placeholder or "")[:100],
                "required": field.required,
                "options": [option.text[:60] for option in field.options[:20]],
            }
            for field in fields
        ],
    }

