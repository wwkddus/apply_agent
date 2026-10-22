"""Score recorded runs against hand-labelled ground truth. Pure functions, no I/O."""
from __future__ import annotations

import math
from statistics import mean


UNMAPPED = "unmapped"
HUMAN_STATUSES = {
    "needs_human",
    "needs_human_review",
    "mapping_review_required",
    "name_confirmation_required",
    "readonly",
    "unsupported",
    # Filled from a local-LLM lookup and read back, but the user still has to confirm it.
    "filled_llm_unconfirmed",
    # Sensitive items (병역/장애/보훈) filled from the resume, confirmed by the user.
    "filled_review",
}
# The page itself disabled the field (e.g. no graduation date while enrolled): not fillable.
DISABLED = "disabled_on_page"
# Ledger rows that tell the user why a field was left: skipped, but not silently.
EXPLAINED = {"not_in_resume", "user_only", "pending_lookup", "missing_resume_value"}


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


LabelKey = tuple[str, str, int, str]


def _label(labels: dict[LabelKey, str], run: dict, field: dict) -> str:
    # Field keys follow each version's detection order, so a key only means the same field within
    # one variant: Before (fewer fields detected) and After are labelled separately.
    return labels.get((run["site"], run["variant"], int(field["page_index"]), field["form_field"]), "")


def _should_fill(expected: str, present: set[str]) -> bool:
    return expected != UNMAPPED and expected in present


def page_fillable(runs: list[dict], labels: dict[LabelKey, str]) -> dict[tuple[str, int], int]:
    """Fields each page should get from the resume, taken from the variant that detected the most.

    Used as the shared denominator, so a field one version never detected counts as not filled
    instead of disappearing from that version's score.
    """
    counts: dict[tuple[str, int], int] = {}
    for run in runs:
        present = set(run["resume"]["present_paths"])
        for page in run["pages"]:
            key = (run["site"], int(page["page_index"]))
            count = sum(_should_fill(_label(labels, run, field), present) for field in page["fields"])
            counts[key] = max(counts.get(key, 0), count)
    return counts


def score_runs(
    runs: list[dict],
    labels: dict[LabelKey, str],
    reference: dict[tuple[str, int], int] | None = None,
) -> dict[str, object]:
    """labels: (site, variant, page_index, form_field) -> expected resume path or 'unmapped'.

    reference: page_fillable() over all variants; when given, page_coverage is filled fields over it.
    """
    rows = []
    unlabeled = 0
    pages = []
    page_keys = set()
    for run in runs:
        present = set(run["resume"]["present_paths"])
        for page in run["pages"]:
            pages.append(page)
            page_keys.add((run["site"], int(page["page_index"])))
            for field in page["fields"]:
                expected = _label(labels, run, field)
                if not expected:
                    unlabeled += 1
                    continue
                rows.append((field, expected, present))

    mapped_correct = filled = unsafe = attempted = fillable = success = 0
    flagged_human = silent_miss = not_attempted = correct_skip = 0
    disabled = lookup_filled = 0
    attempts: list[int] = []
    for field, expected, present in rows:
        predicted = field["predicted_path"] or UNMAPPED
        status = field["fill_status"]
        mapped_correct += predicted == expected
        if field["attempts"]:
            attempted += 1
            attempts.append(int(field["attempts"]))
        if status == "filled_verified":
            filled += 1
            unsafe += predicted != expected
        if predicted == UNMAPPED:
            not_attempted += 1
            correct_skip += expected == UNMAPPED
        human = field["decision_status"] in HUMAN_STATUSES or status in HUMAN_STATUSES
        flagged_human += human
        disabled += status == DISABLED
        lookup_filled += status == "filled_llm_unconfirmed"
        if _should_fill(expected, present) and status not in {"kept_existing", DISABLED}:
            fillable += 1
            ok = status == "filled_verified" and predicted == expected
            success += ok
            silent_miss += not ok and not human and status not in EXPLAINED

    page_total = sum(reference.get(key, 0) for key in page_keys) if reference is not None else None
    inspect_ms = [float(page["inspect_ms"]) for page in pages]
    fill_ms = [float(page["fill_ms"]) for page in pages if page.get("fill_ms") is not None]
    return {
        "runs": len(runs),
        "pages": len(pages),
        "labeled_fields": len(rows),
        "unlabeled_fields": unlabeled,
        "field_mapping_accuracy": _rate(mapped_correct, len(rows)),
        "fillable_fields": fillable,
        "autofill_success_rate": _rate(success, fillable),
        "page_fillable_fields": page_total,
        "page_coverage": _rate(success, page_total) if page_total is not None else None,
        "validation_pass_rate": _rate(filled, attempted),
        "unsafe_fill_count": unsafe,
        "unsafe_fill_rate": _rate(unsafe, filled),
        "human_intervention_rate": _rate(flagged_human, len(rows)),
        "silent_miss_count": silent_miss,
        "disabled_on_page_count": disabled,
        "lookup_filled_count": lookup_filled,
        "skip_precision": _rate(correct_skip, not_attempted),
        "retry_mean": round(mean(attempts), 3) if attempts else None,
        "retry_max": max(attempts) if attempts else None,
        "retried_fields": sum(value > 1 for value in attempts),
        "inspect_ms_mean": round(mean(inspect_ms), 1) if inspect_ms else None,
        "inspect_ms_p95": _p95(inspect_ms),
        "fill_ms_mean": round(mean(fill_ms), 1) if fill_ms else None,
        "fill_ms_p95": _p95(fill_ms),
    }
