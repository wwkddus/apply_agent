from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path

from pydantic import ValidationError
from pypdf import PdfReader

from .constants import EDUCATION_FIELDS, EDUCATION_LEVELS, MAX_RESUME_BYTES, MAX_RESUME_PAGES
from .models import EducationRecord, PersonalInfo, PersonName, ResumeProfile, clean_field, flatten_resume_profile
from .names import infer_latin_name, infer_name_parts


class ResumeParseError(Exception):
    pass


WORD_SUFFIXES = {".doc", ".docx", ".hwp", ".hwpx", ".odt", ".rtf"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<![\d+])(?:\+82[ \t.-]?)?0?1[016789][ \t.-]?\d{3,4}[ \t.-]?\d{4}(?!\d)")

SECTION_LABELS = {
    "대학교": "university",
    "대학원": "graduate_school",
    "university": "university",
    "undergraduate": "university",
    "graduate school": "graduate_school",
}

PERSONAL_LABELS = {
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
    "region": "region",
}


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


def _normal_label(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().rstrip(":：")).casefold()


def _split_label_value(line: str) -> tuple[str, str] | None:
    match = re.match(r"^\s*([^:：]{1,40})\s*[:：]\s*(.+?)\s*$", line)
    if match:
        return _normal_label(match.group(1)), match.group(2).strip()
    normalized = line.strip()
    for label in sorted({*PERSONAL_LABELS, *EDUCATION_LABELS}, key=len, reverse=True):
        if normalized.casefold().startswith(label.casefold() + " "):
            return label.casefold(), normalized[len(label) :].strip()
    return None


_LATIN_NAME_LINE_RE = re.compile(r"[A-Za-z][A-Za-z'-]*(?:,?\s+[A-Za-z][A-Za-z'-]*){1,3}")


def _romanized_name_line(lines: list[str], name: PersonName) -> str:
    """An unlabeled Latin-only line whose first or last word is the romanized Hangul surname."""
    if not name.full:
        return ""
    for line in lines[:15]:
        if _LATIN_NAME_LINE_RE.fullmatch(line) and _normal_label(line) not in PERSONAL_LABELS:
            if infer_latin_name(line, name).split_method == "native_surname_match":
                return line
    return ""


def extract_resume_profile(text: str) -> dict[str, object]:
    """Extract a conservative profile without calling an LLM."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    personal: dict[str, str] = {}
    education: dict[str, dict[str, str]] = {level: {} for level in EDUCATION_LEVELS}
    skills = ""
    sources: dict[str, dict[str, object]] = {}
    warnings: list[str] = []
    section: str | None = None

    def record(scope: str, field: str, value: str, method: str, confidence: float) -> None:
        nonlocal skills
        try:
            clean = clean_field("full" if field in {"name", "name_en"} else field, value)
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
            if label in PERSONAL_LABELS or label in EDUCATION_LABELS:
                if index + 1 < len(lines):
                    next_label = _normal_label(lines[index + 1])
                    if next_label not in PERSONAL_LABELS and next_label not in EDUCATION_LABELS and next_label not in SECTION_LABELS:
                        value = lines[index + 1]
                        consumed = 2
        if value and label in PERSONAL_LABELS:
            field = PERSONAL_LABELS[label]
            record("skills" if field == "skills" else "personal", field, value, "label_match", 0.9)
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
    latin = personal.pop("name_en", "") or _romanized_name_line(lines, name)
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

