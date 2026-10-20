from __future__ import annotations

import re

from .models import PersonName, ResumeProfile

# Common Korean compound family names. Recognising one is useful, but still
# requires confirmation because some three-character names remain ambiguous.
KOREAN_COMPOUND_FAMILY_NAMES = {
    "남궁",
    "독고",
    "동방",
    "사공",
    "서문",
    "선우",
    "제갈",
    "황보",
}


def _compact(value: str) -> str:
    return re.sub(r"[\s·・]", "", value or "")


def infer_name_parts(full_name: str) -> PersonName:
    """Infer components conservatively; uncertain splits are never confirmed."""
    full = " ".join((full_name or "").split())
    compact = _compact(full)
    if not compact:
        return PersonName()

    if re.fullmatch(r"[가-힣]{2,5}", compact):
        compound = next((surname for surname in KOREAN_COMPOUND_FAMILY_NAMES if compact.startswith(surname)), None)
        if compound and len(compact) > len(compound):
            return PersonName(
                full=compact,
                family=compound,
                given=compact[len(compound) :],
                order="family_given",
                split_confidence=0.78,
                split_method="korean_compound_family_rule",
                confirmed=False,
            )
        if len(compact) >= 2:
            return PersonName(
                full=compact,
                family=compact[0],
                given=compact[1:],
                order="family_given",
                split_confidence=0.92 if len(compact) == 3 else 0.82,
                split_method="korean_single_family_rule",
                confirmed=False,
            )

    if "," in full:
        family, given = [part.strip() for part in full.split(",", 1)]
        if family and given:
            return PersonName(
                full=full,
                family=family,
                given=given,
                order="family_given",
                split_confidence=0.9,
                split_method="comma_order_rule",
                confirmed=False,
            )

    parts = full.split()
    if len(parts) >= 2:
        return PersonName(
            full=full,
            family=parts[-1],
            given=parts[0],
            middle=" ".join(parts[1:-1]),
            order="given_family",
            split_confidence=0.55,
            split_method="latin_space_heuristic",
            confirmed=False,
        )
    return PersonName(full=full)


def confirm_name_parts(profile: ResumeProfile, *, family: str, given: str, middle: str = "") -> ResumeProfile:
    """Return a copy with user-confirmed name components."""
    name = PersonName.model_validate(
        {
            **profile.personal.name.model_dump(),
            "family": family.strip(),
            "given": given.strip(),
            "middle": middle.strip(),
            "split_confidence": 1.0,
            "split_method": "user_confirmed",
            "confirmed": True,
        }
    )
    personal = profile.personal.model_copy(update={"name": name})
    return profile.model_copy(update={"personal": personal})


def normalized_name(value: str) -> str:
    return re.sub(r"[\s\-'·・]", "", (value or "")).casefold()

