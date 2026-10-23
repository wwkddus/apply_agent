# [역할] 칸의 name/id 속성(개발자가 붙인 이름)에서 의미를 읽는 모듈.
# 화면 라벨은 배치에 따라 헷갈리기 쉽지만 'edu_school_nm' 같은 속성은 데이터 구조를 그대로 드러내므로 먼저 확인합니다.
"""Read field meaning from developer-facing name/id attributes.

Many application systems name their inputs after their data model, for example
``educationalBackground.universities.0.schoolName`` or ``edu_school_nm``. That path
is usually more reliable than a visual label in a multi-column layout, so it is read
first. Only generic vocabulary is used; nothing here is tied to one site.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

UNMAPPED = "unmapped"


# 속성에서 얻은 판단 결과: 연결할 이력서 경로와 그 이유.
@dataclass(frozen=True, slots=True)
class AttributeHint:
    resume_path: str
    reason: str


# 'schoolName', 'edu_school_nm'처럼 붙어 있는 이름을 단어 단위로 쪼갭니다.
def attribute_tokens(*values: str | None) -> list[str]:
    tokens: list[str] = []
    for value in values:
        text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value or "")
        tokens.extend(token for token in re.split(r"[^A-Za-z]+", text.casefold()) if token)
    return tokens


# 토큰 중 하나라도 주어진 단어와 같은지 / 주어진 접두어로 시작하는지 확인하는 도우미.
def _has(tokens: list[str], *words: str) -> bool:
    return any(token in words for token in tokens)


def _has_prefix(tokens: list[str], *prefixes: str) -> bool:
    return any(token.startswith(prefixes) for token in tokens)


# Sections whose values are not the applicant's own resume data in scope.
# 경력·가족·자격증·희망 연봉 등은 이력서 범위 밖이므로, 이런 단어가 보이면 연결하지 않습니다.
_OUT_OF_SCOPE = (
    "work", "career", "experience", "experiences", "company", "employment", "employer",
    "family", "emergency", "reference", "referee", "recommend", "recommender", "guardian", "parent",
    "spouse", "award", "awards", "certificate", "certification", "license", "language", "activity",
    "activities", "training", "project", "projects", "cover", "letter", "question", "answers", "custom",
    "survey", "consent", "agree", "agreement", "terms",
    # Job preferences of this application (희망 근무지, 근무 형태, 지원 경로, 희망 연봉...), not resume data.
    "working", "job", "apply", "hope", "wish", "desired", "preferred", "salary", "recruit",
)
_EDUCATION_CONTEXT = ("education", "educational", "edu", "academic", "school", "schools", "univ", "university",
                      "universities", "college", "graduate", "major", "majors", "gpa")


# 속성 단어로 고등학교/대학원/대학 중 어느 학력인지 판단합니다.
def _education_level(tokens: list[str]) -> str | None:
    joined = "".join(tokens)
    # Common Korean HR abbreviations: eduhg* (high school) and edugd* (graduate school) blocks.
    if "highschool" in joined or _has(tokens, "hs") or _has_prefix(tokens, "eduhg"):
        return "high_school"
    if _has_prefix(tokens, "graduate", "master", "doctor", "phd", "edugd") or "gradschool" in joined:
        return "graduate_school"
    if _has_prefix(tokens, "universit", "undergrad", "college", "bachelor", "univ"):
        return "university"
    return None


education_level_from_tokens = _education_level


# 학력 칸이 학교명·전공·학점·입학일 중 무엇인지 판단합니다.
# '예/아니요'나 '전공 계열'처럼 이력서에 없는 항목은 일부러 연결하지 않습니다.
def _education_field(tokens: list[str]) -> tuple[str | None, str]:
    joined = "".join(tokens)
    if _has(tokens, "yn"):
        return UNMAPPED, "예/아니요 선택(편입·복수전공 여부 등)은 사용자가 결정"
    if _has(tokens, "thesis", "dissertation", "capstone"):
        return UNMAPPED, "논문·졸업작품은 이력서 항목이 아님"
    if _has(tokens, "major", "majors"):
        if _has(tokens, "classification", "category", "field", "type", "affiliation", "series", "family"):
            return UNMAPPED, "전공 구분/계열은 이력서 항목이 아님"
        if _has(tokens, "double", "second", "minor", "sub"):
            return "double_major", "double major"
        return "major", "major"
    if _has(tokens, "minor"):
        return "double_major", "minor"
    if "schoolname" in joined or "institution" in joined or (_has(tokens, "school", "univ", "university") and _has(tokens, "name", "nm")):
        return "school_name", "school name"
    if _has(tokens, "gpa", "grade", "credit") or "gpa" in joined:
        if _has(tokens, "scale", "max", "perfect", "total", "full", "standard"):
            return "gpa_scale", "gpa scale"
        return "gpa", "gpa"
    if _has(tokens, "score") and _has(tokens, "scale", "max", "perfect", "total"):
        return "gpa_scale", "gpa scale"
    if _has(tokens, "degree", "degrees"):
        return "degree", "degree"
    if _has(tokens, "completion", "graduation", "graduate") and _has(tokens, "status", "type", "state"):
        return "graduation_status", "graduation status"
    if _has(tokens, "admission", "entrance", "enroll", "enrollment") and _has(tokens, "type", "kind"):
        return "admission_type", "admission type"
    if _has(tokens, "start", "begin", "from", "admission", "entrance", "entered") and not _has(tokens, "type"):
        return "admission_date", "start date"
    if _has(tokens, "end", "to", "until", "graduation", "finish") and not _has(tokens, "status", "type"):
        return "graduation_date", "end date"
    if _has(tokens, "campus", "branch"):
        return "campus_type", "campus"
    if _has(tokens, "region", "location", "area"):
        return "region", "region"
    if _has(tokens, "day", "night") and not _has(tokens, "birth"):
        return UNMAPPED, "주/야간 구분은 이력서 항목이 아님"
    return None, ""


# 개인정보 칸(생년월일, 이메일, 전화, 주소, 영문 이름, 병역 등)인지 판단합니다.
def _personal_field(tokens: list[str]) -> tuple[str | None, str]:
    joined = "".join(tokens)
    if _has(tokens, "country", "nation", "dial") and _has(tokens, "code"):
        return UNMAPPED, "국가 코드는 이력서 항목이 아님"
    if _has(tokens, "military", "army"):
        return "person.military_status", "military service"
    if _has(tokens, "disability") or (_has(tokens, "disabled") and _has(tokens, "yn", "status", "type", "info")):
        return "person.disability_status", "disability"
    if _has(tokens, "veteran", "veterans"):
        return "person.veteran_status", "veteran"
    if "birth" in joined or _has(tokens, "dob", "birthday"):
        return "person.birth_date", "birth date"
    if _has(tokens, "email", "mail"):
        if _has(tokens, "sub", "secondary", "second", "alt", "alternate", "extra", "other", "2"):
            return "person.email_secondary", "secondary email"
        return "person.email", "email"
    if _has(tokens, "phone", "mobile", "cellphone", "cell", "tel", "hp", "contact"):
        return "person.phone", "phone"
    if _has(tokens, "zip", "zipcode", "postal", "postcode", "post"):
        return "person.postal_code", "postal code"
    if _has(tokens, "address", "addr"):
        if _has(tokens, "detail", "dtl", "line", "sub", "extra"):
            return "person.address_detail", "address detail"
        return "person.address", "address"
    if _has(tokens, "english", "eng", "en", "roman", "latin"):
        if _has(tokens, "first", "given"):
            return "person.name_en.given", "english given name"
        if _has(tokens, "last", "family", "surname"):
            return "person.name_en.family", "english family name"
        if _has(tokens, "name", "nm", "fullname"):
            return "person.name_en.full", "english name"
        return UNMAPPED, "영문 표기 항목"
    if _has(tokens, "first", "given"):
        return "person.name.given", "given name"
    if _has(tokens, "last", "family", "surname"):
        return "person.name.family", "family name"
    if _has(tokens, "name", "nm", "fullname"):
        return "person.name.full", "name"
    return None, ""


# 속성만으로 확실할 때만 결과를 돌려주고, 애매하면 None으로 넘겨 라벨 규칙이 판단하게 합니다.
# 흐름: 단어 분리 → 범위 밖 영역 제외 → 학력 칸 판단 → 개인정보 칸 판단.
def infer_from_attributes(name: str | None, element_id: str | None = None) -> AttributeHint | None:
    """Return a decisive hint from a structured attribute path, or None when it says nothing."""
    tokens = attribute_tokens(name)
    if len(tokens) < 2:
        # A single generic token such as 'name' or 'q1' is not structured enough.
        tokens = tokens + attribute_tokens(element_id)
        if len(tokens) < 2 and not (tokens and tokens[0] in {"email", "birthdate", "schoolname", "gpa"}):
            return None
    if _has(tokens, *_OUT_OF_SCOPE):
        return AttributeHint(UNMAPPED, f"attribute: 지원자 이력서 범위가 아닌 영역 ({'.'.join(tokens[:3])})")

    level = _education_level(tokens)
    if level == "high_school":
        return AttributeHint(UNMAPPED, "attribute: 고등학교 학력은 현재 이력서 범위가 아님")
    if level or _has(tokens, *_EDUCATION_CONTEXT):
        field, reason = _education_field(tokens)
        if field is None:
            return None
        if field == UNMAPPED:
            return AttributeHint(UNMAPPED, f"attribute: {reason}")
        if level is None:
            return None  # Let section/label rules decide the education level.
        return AttributeHint(f"education.{level}.{field}", f"attribute: {level} {reason}")

    field, reason = _personal_field(tokens)
    if field is None:
        return None
    return AttributeHint(field, f"attribute: {reason}")
