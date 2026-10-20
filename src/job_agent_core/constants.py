from __future__ import annotations

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
SENSITIVE_STATUS_PATHS = {
    "person.military_status",
    "person.disability_status",
    "person.veteran_status",
}

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

DATE_PATHS = {
    "person.birth_date",
    *{
        f"education.{level}.{field}"
        for level in EDUCATION_LEVELS
        for field in ("admission_date", "graduation_date")
    },
}

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

MAX_RESUME_BYTES = 10 * 1024 * 1024
MAX_RESUME_PAGES = 20
AUTO_ACCEPT_CONFIDENCE = 0.8

