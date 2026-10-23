# 이력서 읽기 1단계(규칙 기반). 
# PDF에서 텍스트를 뽑고, '항목: 값' 형태를 찾아 ResumeProfile로 만듭니다.
# LLM 없이 동작하는 기본 경로이며, 애매한 값은 경고로 남겨 사용자가 확인하게 합니다.

from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path

from pydantic import ValidationError
from pypdf import PdfReader

from .constants import EDUCATION_FIELDS, EDUCATION_LEVELS, MAX_RESUME_BYTES, MAX_RESUME_PAGES
from .models import EducationRecord, PersonalInfo, PersonName, ResumeProfile, clean_field, flatten_resume_profile
from .names import infer_latin_name, infer_name_parts


# 이력서를 읽을 수 없을 때 사용자에게 보여줄 오류.
class ResumeParseError(Exception):
    pass


# 지원하지 않는 문서 형식(Word/HWP)과, 이메일·휴대폰 번호를 찾는 패턴.
WORD_SUFFIXES = {".doc", ".docx", ".hwp", ".hwpx", ".odt", ".rtf"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<![\d+])(?:\+82[ \t.-]?)?0?1[016789][ \t.-]?\d{3,4}[ \t.-]?\d{4}(?!\d)")

# 학력 구역 제목. 이 줄 다음에 나오는 학력 항목이 고등학교/편입 전 대학/대학/대학원 중 어디 것인지 정합니다.
SECTION_LABELS = {
    "고등학교": "high_school",
    "고교": "high_school",
    "high school": "high_school",
    "편입 전 대학교": "previous_university",
    "편입 전 대학": "previous_university",
    "편입전 대학교": "previous_university",
    "편입전대학교": "previous_university",
    "대학교(편입 전)": "previous_university",
    "대학교 (편입 전)": "previous_university",
    "이전 대학교": "previous_university",
    "previous university": "previous_university",
    "대학교": "university",
    "대학원": "graduate_school",
    "university": "university",
    "undergraduate": "university",
    "graduate school": "graduate_school",
}

# 이력서 라벨 → 개인정보 항목. 같은 의미의 여러 표기(이름/성명/full name 등)를 한곳에 모았습니다.
PERSONAL_LABELS = {
    # 모판 이력서는 영문 성과 영문 이름을 따로 적습니다(이때 '영문 이름'은 성을 뺀 이름).
    "영문 성": "name_en_family",
    "영문성": "name_en_family",
    "영어 성": "name_en_family",
    "english family name": "name_en_family",
    "english surname": "name_en_family",
    "영문 이름": "name_en",
    "영문이름": "name_en",
    "영문 성명": "name_en",
    "영문성명": "name_en",
    "이름(영문)": "name_en",
    "성명(영문)": "name_en",
    "english name": "name_en",
    "name (english)": "name_en",
    "name in english": "name_en",
    "영어 이름": "name_en",
    "영어이름": "name_en",
    "영어 성명": "name_en",
    "영어성명": "name_en",
    "영문명": "name_en",
    "이름(영어)": "name_en",
    "성명(영어)": "name_en",
    "여권 영문명": "name_en",
    "여권영문명": "name_en",
    "로마자 이름": "name_en",
    "이름": "name",
    "성명": "name",
    "성함": "name",
    "full name": "name",
    "name": "name",
    "생년월일": "birth_date",
    "date of birth": "birth_date",
    "birth date": "birth_date",
    "휴대전화번호": "phone",
    "휴대전화": "phone",
    "휴대폰": "phone",
    "전화번호": "phone",
    "연락처": "phone",
    "phone": "phone",
    "mobile": "phone",
    "병역": "military_status",
    "병역사항": "military_status",
    "병역 사항": "military_status",
    "병역여부": "military_status",
    "병역구분": "military_status",
    "병역 구분": "military_status",
    "군필여부": "military_status",
    "군필 여부": "military_status",
    "military service": "military_status",
    "military": "military_status",
    "장애": "disability_status",
    "장애여부": "disability_status",
    "장애 여부": "disability_status",
    "장애사항": "disability_status",
    "장애 사항": "disability_status",
    "장애정보": "disability_status",
    "장애 정보": "disability_status",
    "장애구분": "disability_status",
    "disability": "disability_status",
    "보훈": "veteran_status",
    "보훈여부": "veteran_status",
    "보훈 여부": "veteran_status",
    "보훈대상": "veteran_status",
    "보훈 대상": "veteran_status",
    "보훈대상여부": "veteran_status",
    "보훈사항": "veteran_status",
    "보훈 사항": "veteran_status",
    "보훈정보": "veteran_status",
    "veteran": "veteran_status",
    "veteran status": "veteran_status",
    "보조 이메일": "email_secondary",
    "보조이메일": "email_secondary",
    "secondary email": "email_secondary",
    "이메일": "email",
    "email": "email",
    "e-mail": "email",
    "우편번호": "postal_code",
    "postal code": "postal_code",
    "zip code": "postal_code",
    "상세주소": "address_detail",
    "address line 2": "address_detail",
    "주소": "address",
    "address": "address",
    "보유기술": "skills",
    "기술스택": "skills",
    "technical skills": "skills",
    "skills": "skills",
}

# 이력서 라벨 → 학력 항목.
EDUCATION_LABELS = {
    "학교명": "school_name",
    "학교": "school_name",
    "university": "school_name",
    "school": "school_name",
    "본교/분교 구분": "campus_type",
    "학위": "degree",
    "degree": "degree",
    "전공명": "major",
    "전공": "major",
    "major": "major",
    "field of study": "major",
    "department": "major",
    "복수전공명": "double_major",
    "복수전공": "double_major",
    "double major": "double_major",
    "입학구분": "admission_type",
    "입학년월": "admission_date",
    "졸업구분": "graduation_status",
    "졸업년월": "graduation_date",
    "전체평점": "gpa",
    "평점": "gpa",
    "gpa": "gpa",
    "만점기준": "gpa_scale",
    "만점": "gpa_scale",
    "gpa scale": "gpa_scale",
    "지역": "region",
    "소재지": "region",
    "region": "region",
    "location": "region",
}
# 고등학교 구역에서만 쓰는 라벨: '계열'(문과/이과, 인문계/자연계)은 고등학교의 전공 칸으로 봅니다.
HIGH_SCHOOL_LABELS = {"계열": "major", "고교 계열": "major", "track": "major"}


# PDF 내용 검사와 텍스트 추출. 파일은 저장하지 않습니다.
# 크기·암호·페이지 수를 확인하고, 텍스트가 없으면(스캔본) OCR이 필요하다고 알려 줍니다.
def inspect_resume_bytes(data: bytes) -> dict[str, object]:
    """Inspect PDF bytes without persisting the resume or its filename."""
    size = len(data)
    if not size:
        raise ResumeParseError("이력서 파일이 비어 있습니다.")
    if size > MAX_RESUME_BYTES:
        raise ResumeParseError("이력서 파일은 10MB를 초과할 수 없습니다.")
    try:
        reader = PdfReader(BytesIO(data))
    except Exception as error:
        raise ResumeParseError(f"PDF를 열 수 없습니다. ({type(error).__name__})") from None
    if reader.is_encrypted:
        try:
            unlocked = reader.decrypt("")
        except Exception:
            unlocked = 0
        if not unlocked:
            raise ResumeParseError("암호화된 PDF를 읽을 수 없습니다.")
    try:
        pages = len(reader.pages)
        if not pages:
            raise ResumeParseError("PDF에 페이지가 없습니다.")
        if pages > MAX_RESUME_PAGES:
            raise ResumeParseError("PDF는 20페이지를 초과할 수 없습니다.")
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except ResumeParseError:
        raise
    except Exception as error:
        raise ResumeParseError(f"PDF를 읽을 수 없습니다. ({type(error).__name__})") from None
    if not text.strip():
        raise ResumeParseError("추출 가능한 텍스트가 없습니다. 스캔 PDF에는 OCR이 필요합니다.")
    return {"text": text, "pages": pages, "size_bytes": size}


# 파일 경로로 PDF 읽기. Word/HWP·확장자·크기를 먼저 확인한 뒤 inspect_resume_bytes로 넘깁니다.
def inspect_resume_file(path: str | Path) -> dict[str, object]:
    file_path = Path(path).expanduser()
    if file_path.suffix.lower() in WORD_SUFFIXES:
        raise ResumeParseError("Word/HWP 파일은 지원하지 않습니다. 텍스트형 PDF로 내보내 주세요.")
    if not file_path.exists() or not file_path.is_file():
        raise ResumeParseError("이력서 파일을 찾을 수 없습니다.")
    if file_path.suffix.lower() != ".pdf":
        raise ResumeParseError("이력서는 .pdf 파일이어야 합니다.")
    size = file_path.stat().st_size
    if size > MAX_RESUME_BYTES:
        raise ResumeParseError("이력서 파일은 10MB를 초과할 수 없습니다.")
    try:
        data = file_path.read_bytes()
    except OSError as error:
        raise ResumeParseError(f"PDF를 읽을 수 없습니다. ({type(error).__name__})") from None
    return inspect_resume_bytes(data)


# 라벨 비교용 정규화(공백 정리, 끝의 콜론 제거, 소문자).
def _normal_label(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().rstrip(":：")).casefold()


# 한 줄을 (라벨, 값)으로 나눕니다. '이메일: a@b.com'과 '이메일 a@b.com' 두 형태를 모두 처리.
def _split_label_value(line: str) -> tuple[str, str] | None:
    match = re.match(r"^\s*([^:：]{1,40})\s*[:：]\s*(.+?)\s*$", line)
    if match:
        return _normal_label(match.group(1)), match.group(2).strip()
    normalized = line.strip()
    for label in sorted({*PERSONAL_LABELS, *EDUCATION_LABELS, *HIGH_SCHOOL_LABELS}, key=len, reverse=True):
        if normalized.casefold().startswith(label.casefold() + " "):
            value = normalized[len(label) :].strip()
            # '병역 · 보훈 · 장애' 같은 구역 제목은 값이 아닙니다.
            if re.match(r"[·・/&,+|]", value):
                return None
            return label.casefold(), value
    return None


# 영문으로만 된 2~4단어 줄(라벨 없이 적힌 영문 이름 후보).
_LATIN_NAME_LINE_RE = re.compile(r"[A-Za-z][A-Za-z'-]*(?:,?\s+[A-Za-z][A-Za-z'-]*){1,3}")


# 라벨 없는 영문 이름 찾기: 이력서 앞부분에서 한글 성과 맞는 영문 줄만 영문 이름으로 봅니다.
def _romanized_name_line(lines: list[str], name: PersonName) -> str:
    """An unlabeled Latin-only line whose first or last word is the romanized Hangul surname."""
    if not name.full:
        return ""
    for line in lines[:15]:
        if _LATIN_NAME_LINE_RE.fullmatch(line) and _normal_label(line) not in PERSONAL_LABELS:
            if infer_latin_name(line, name).split_method == "native_surname_match":
                return line
    return ""


# 규칙 기반 이력서 추출(LLM 없음). 흐름:
# ① 줄 단위로 구역 제목/라벨: 값 인식(값이 다음 줄에 있는 경우 포함) → ② 이메일·전화가 없으면 정규식으로 보충
# → ③ 이름을 성/이름으로 분리, 영문 이름 판단 → ④ ResumeProfile 생성, 필수 항목 누락은 경고.
def extract_resume_profile(text: str) -> dict[str, object]:
    """Extract a conservative profile without calling an LLM."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    personal: dict[str, str] = {}
    education: dict[str, dict[str, str]] = {level: {} for level in EDUCATION_LEVELS}
    skills = ""
    sources: dict[str, dict[str, object]] = {}
    warnings: list[str] = []
    section: str | None = None

    # 값 하나 저장: 형식 검사를 통과해야 하고, 같은 항목에 다른 값이 또 나오면 첫 값만 두고 신뢰도를 낮춥니다.
    def record(scope: str, field: str, value: str, method: str, confidence: float) -> None:
        nonlocal skills
        try:
            clean = clean_field("full" if field in {"name", "name_en"} else "family" if field == "name_en_family" else field, value)
        except ValueError:
            warnings.append(f"{field}: 형식 검사를 통과하지 못해 자동 입력에서 제외했습니다")
            return
        if not clean:
            return
        path = field if scope == "personal" else "skills" if scope == "skills" else f"{scope}_{field}"
        target = personal if scope == "personal" else education[scope] if scope in education else None
        current = skills if scope == "skills" else target.get(field, "") if target is not None else ""
        if current and current != clean:
            warnings.append(f"{path}: 여러 값이 발견되어 첫 번째 값만 유지했습니다")
            sources[path]["confidence"] = min(float(sources[path]["confidence"]), 0.5)
            return
        if scope == "skills":
            skills = clean
        elif target is not None:
            target[field] = clean
        sources[path] = {"method": method, "confidence": confidence}

    index = 0
    while index < len(lines):
        line = lines[index]
        marker = SECTION_LABELS.get(_normal_label(line))
        if marker:
            section = marker
            index += 1
            continue

        parsed = _split_label_value(line)
        if parsed:
            label, value = parsed
            consumed = 1
        else:
            label = _normal_label(line)
            value = ""
            consumed = 1
            if label in PERSONAL_LABELS or label in EDUCATION_LABELS or (section == "high_school" and label in HIGH_SCHOOL_LABELS):
                if index + 1 < len(lines):
                    next_label = _normal_label(lines[index + 1])
                    if next_label not in PERSONAL_LABELS and next_label not in EDUCATION_LABELS and next_label not in SECTION_LABELS:
                        value = lines[index + 1]
                        consumed = 2
        if value and label in PERSONAL_LABELS:
            field = PERSONAL_LABELS[label]
            record("skills" if field == "skills" else "personal", field, value, "label_match", 0.9)
        elif value and section == "high_school" and label in HIGH_SCHOOL_LABELS:
            record("high_school", HIGH_SCHOOL_LABELS[label], value, "label_match", 0.9)
        elif value and label in EDUCATION_LABELS:
            level = section if section in EDUCATION_LEVELS else "university"
            confidence = 0.9 if section in EDUCATION_LEVELS else 0.6
            record(level, EDUCATION_LABELS[label], value, "label_match", confidence)
            if section not in EDUCATION_LEVELS:
                warnings.append(
                    f"{EDUCATION_LABELS[label]}: 학력 구분을 확인할 수 없어 대학교로 임시 분류했습니다"
                )
        index += consumed

    if "email" not in personal:
        matches = list(dict.fromkeys(EMAIL_RE.findall(text)))
        if matches:
            record("personal", "email", matches[0], "regex", 0.95 if len(matches) == 1 else 0.6)
    if "phone" not in personal:
        match = PHONE_RE.search(text)
        if match:
            record("personal", "phone", match.group(0), "regex", 0.85)

    name = infer_name_parts(personal.pop("name", ""))
    latin_family = personal.pop("name_en_family", "")
    latin = personal.pop("name_en", "") or _romanized_name_line(lines, name)
    if latin_family and latin and " " not in latin:
        # '영문 성 Hong' + '영문 이름 Gildong': 순서가 적혀 있으므로 추측하지 않습니다.
        name_en = PersonName(full=f"{latin_family} {latin}", family=latin_family, given=latin, order="family_given",
                             split_confidence=1.0, split_method="labeled_parts", confirmed=True)
    else:
        name_en = infer_latin_name(latin, name) if latin else None
    records = []
    for level in EDUCATION_LEVELS:
        values = education[level]
        if values:
            try:
                records.append(EducationRecord(education_level=level, **values))
            except ValidationError:
                raise ResumeParseError(f"{level} 학력 정보의 형식을 확인해 주세요") from None
    personal_info = PersonalInfo(name=name, **personal)
    if name_en is not None:
        personal_info = personal_info.model_copy(update={"name_en": name_en})
    profile = ResumeProfile(personal=personal_info, education=records, skills=skills)
    flat = flatten_resume_profile(profile)
    required = ("name", "email", "phone")
    missing = [field for field in required if not flat[field]]
    warnings.extend(f"{field}: 이력서에서 찾지 못해 비워 두었습니다" for field in missing)
    return {
        "resume_profile": profile,
        "profile": flat,
        "sources": sources,
        "warnings": warnings,
        "missing": missing,
    }

