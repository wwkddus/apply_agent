from __future__ import annotations

PERSONAL_FIELDS = (
    "birth_date",
    "phone",
    "email",
    "postal_code",
    "address",
    "address_detail",
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

PERSONAL_PATHS = {
    "birth_date": "person.birth_date",
    "phone": "person.phone",
    "email": "person.email",
    "postal_code": "person.postal_code",
    "address": "person.address",
    "address_detail": "person.address_detail",
}

RESUME_PATHS = (
    sorted(NAME_PATHS)
    + list(PERSONAL_PATHS.values())
    + ["skills"]
    + [f"education.{level}.{field}" for level in EDUCATION_LEVELS for field in EDUCATION_FIELDS]
)
ALLOWED_RESUME_PATHS = frozenset(RESUME_PATHS)

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

