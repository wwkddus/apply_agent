# [역할] 패키지 입구. 다른 모듈(데스크톱 앱, API, 테스트)이 자주 쓰는 클래스·함수를 한곳에서 꺼내 쓸 수 있게 모아 둡니다.
"""Testable, provider-neutral core for the job application assistant."""

from .korean_scope import KOREAN_MVP_PATHS, KoreanMvpMapper
from .mapping import FormField, MappingRecord, RuleBasedMapper
from .models import EducationRecord, PersonName, PersonalInfo, ResumeProfile
from .names import confirm_name_parts, infer_name_parts
from .planning import FillAction, build_fill_plan
from .resume_parser import ResumeParseError, extract_resume_profile, inspect_resume_bytes, inspect_resume_file
from .validation import ValidationIssue, validate_name_consistency, validate_readback
from .widgets import WidgetAction, plan_widget_action

__all__ = [
    "EducationRecord",
    "FillAction",
    "FormField",
    "KOREAN_MVP_PATHS",
    "KoreanMvpMapper",
    "MappingRecord",
    "PersonName",
    "PersonalInfo",
    "ResumeParseError",
    "ResumeProfile",
    "RuleBasedMapper",
    "ValidationIssue",
    "WidgetAction",
    "build_fill_plan",
    "confirm_name_parts",
    "extract_resume_profile",
    "infer_name_parts",
    "inspect_resume_file",
    "inspect_resume_bytes",
    "plan_widget_action",
    "validate_name_consistency",
    "validate_readback",
]

