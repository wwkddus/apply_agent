# [역할] 프로젝트 전체가 공유하는 상수 모음. 이력서 항목 이름(경로), 글자 수 제한, 파일 크기 제한 등을 한곳에서 관리합니다.

from __future__ import annotations

# 이력서에서 읽는 개인정보 항목 목록.
PERSONAL_FIELDS = (
    "birth_date",
    "phone",
    "email",
    "email_secondary",
    "postal_code",
    "address",
    "address_detail",
    # Sensitive status items, filled as written in the resume and always left for the user to confirm.
    "military_status",
    "disability_status",
    "veteran_status",
)

# 지원하는 학력 단계(대학, 대학원)와 각 학력에서 읽는 항목들.
EDUCATION_LEVELS = ("university", "graduate_school")
EDUCATION_FIELDS = (
    "school_name",
    "campus_type",
    "degree",
    "major",
    "double_major",
    "admission_type",
    "admission_date",
    "graduation_status",
    "graduation_date",
    "gpa",
    "gpa_scale",
    "region",
)

# 이름 관련 경로. 전체 이름과 성/이름/중간 이름을 따로 다룹니다.
NAME_PATHS = {
    "person.name.full",
    "person.name.family",
    "person.name.given",
    "person.name.middle",
}

# Romanized (English) name, kept separately from the native-script name.
NAME_EN_PATHS = {
    "person.name_en.full",
    "person.name_en.family",
    "person.name_en.given",
}

# 항목 이름 → 'person.xxx' 경로. 매핑 결과는 항상 이 경로 형태로 표현합니다.
PERSONAL_PATHS = {
    "birth_date": "person.birth_date",
    "phone": "person.phone",
    "email": "person.email",
    "email_secondary": "person.email_secondary",
    "postal_code": "person.postal_code",
    "address": "person.address",
    "address_detail": "person.address_detail",
    "military_status": "person.military_status",
    "disability_status": "person.disability_status",
    "veteran_status": "person.veteran_status",
}
# 병역·장애·보훈처럼 민감한 항목. 입력하더라도 항상 사람 확인으로 남깁니다.
SENSITIVE_STATUS_PATHS = {
    "person.military_status",
    "person.disability_status",
    "person.veteran_status",
}

# 허용되는 모든 이력서 경로. LLM이나 규칙이 이 목록 밖의 경로를 만들면 버립니다.
RESUME_PATHS = (
    sorted(NAME_PATHS)
    + sorted(NAME_EN_PATHS)
    + list(PERSONAL_PATHS.values())
    + ["skills"]
    + [f"education.{level}.{field}" for level in EDUCATION_LEVELS for field in EDUCATION_FIELDS]
)
# Computed from the profile, never edited directly (e.g. 최종 학력 from the education records).
DERIVED_PATHS = {"education.highest_level"}
ALLOWED_RESUME_PATHS = frozenset(RESUME_PATHS) | DERIVED_PATHS

# 날짜 형식으로 다뤄야 하는 경로(생년월일, 입학·졸업일).
DATE_PATHS = {
    "person.birth_date",
    *{
        f"education.{level}.{field}"
        for level in EDUCATION_LEVELS
        for field in ("admission_date", "graduation_date")
    },
}

# 항목별 최대 글자 수. 비정상적으로 긴 값이 들어가는 것을 막습니다.
FIELD_MAX_LENGTH = {
    "full": 120,
    "family": 60,
    "given": 80,
    "middle": 80,
    "birth_date": 10,
    "phone": 30,
    "email": 120,
    "postal_code": 10,
    "address": 200,
    "address_detail": 120,
    "military_status": 40,
    "disability_status": 40,
    "veteran_status": 40,
    "skills": 500,
    "school_name": 120,
    "campus_type": 20,
    "degree": 40,
    "major": 120,
    "double_major": 120,
    "admission_type": 40,
    "admission_date": 7,
    "graduation_status": 40,
    "graduation_date": 7,
    "gpa": 6,
    "gpa_scale": 6,
    "region": 40,
}

# 이력서 파일 크기·페이지 제한과, 자동 수락할 최소 신뢰도.
MAX_RESUME_BYTES = 10 * 1024 * 1024
MAX_RESUME_PAGES = 20
AUTO_ACCEPT_CONFIDENCE = 0.8

