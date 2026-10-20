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

