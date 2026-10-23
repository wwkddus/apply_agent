"""Compare field-mapping providers on saved form structures (no browser, no resume).

Each provider maps the fields of a structure snapshot (evaluation/structures/*.json) to resume
paths and is scored against a hand-checked label file (evaluation/labels/structures/*.csv).
Only the form structure is sent to a model; resume values are never involved.

Usage:
    .venv/bin/python evaluation/llm_mapping_eval.py evaluation/structures/site-a-20261023-104127.json \
        --provider rules --provider ollama:qwen2.5:7b --provider ollama:qwen3:8b --provider hybrid:ollama:qwen3:8b
    # hybrid:<llm> = rules first; only fields no rule matched go to the model.
    # llmfirst:<llm> = the model sees the whole page; rules only veto non-resume fields (the app's mode).
    #   Scored on what would actually be typed: mappings left for the user count as unmapped.
    # OpenAI (optional): store your key in the macOS Keychain (item "Apply Local OpenAI API key")
    #   or set OPENAI_API_KEY yourself, then add --provider openai:<model> (or openai:<model>@low
    #   for a reasoning effort) and --price openai:<model>=<input $/1M tokens>,<output $/1M tokens>.
    .venv/bin/python evaluation/llm_mapping_eval.py --list-openai-models

Metrics per provider (fields with expected_path = the label file's truth):
    accuracy      predicted == expected over all fields
    fill_recall   fields that should be filled and got the right path
    wrong_fill    predicted a path that is wrong (the dangerous error: a wrong value would be typed)
    missed        should be filled but left unmapped (safe; the user fills it)
Output: evaluation/llm_runs/<snapshot>-<timestamp>.json
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from job_agent_core.korean_scope import KoreanMvpMapper
from job_agent_core.llm_mapping import HybridMapper, LlmFieldMapper, LlmFirstMapper, LlmMappingError, endpoint_from_spec, list_openai_models
from job_agent_core.mapping import FormField, FormOption

EVAL_DIR = Path(__file__).resolve().parent


def load_fields(snapshot: dict) -> list[tuple[int, FormField]]:
    """Snapshot rows back to FormField. Keys are prefixed with the page so they stay unique."""
    fields = []
    for page in snapshot["pages"]:
        for row in page["fields"]:
            fields.append((page["page_index"], FormField(
                key=f"p{page['page_index']}:{row['form_field']}",
                label=row.get("label") or "",
                type=row.get("type") or "text",
                widget_kind=row.get("widget_kind") or "text",
                required=bool(row.get("required")),
                placeholder=row.get("placeholder"),
                section=row.get("section") or "",
                group_labels=row.get("group_labels") or [],
                name=row.get("name"),
                element_id=row.get("element_id"),
                context_names=row.get("context_names") or [],
                current_value_present=bool(row.get("has_existing_value")),
                readonly=bool(row.get("readonly")),
                options=[FormOption(value=text, text=text) for text in row.get("options") or []],
                maxlength=row.get("maxlength"),
                date_format=row.get("date_format"),
            )))
    return fields


def load_labels(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8") as handle:
        return {f"p{row['page_index']}:{row['form_field']}": row["expected_path"].strip() for row in csv.DictReader(handle)}


def score(predicted: dict[str, str], expected: dict[str, str]) -> dict[str, object]:
    keys = [key for key in expected if expected[key]]
    fillable = [key for key in keys if expected[key] != "unmapped"]
    correct = [key for key in keys if predicted.get(key, "unmapped") == expected[key]]
    wrong_fill = [key for key in keys if predicted.get(key, "unmapped") not in {"unmapped", expected[key]}]
    missed = [key for key in fillable if predicted.get(key, "unmapped") == "unmapped"]
    return {
        "fields": len(keys),
        "fillable": len(fillable),
        "accuracy": round(len(correct) / len(keys), 3) if keys else None,
        "fill_recall": round(len([key for key in fillable if key in correct]) / len(fillable), 3) if fillable else None,
        "wrong_fill": len(wrong_fill),
        "missed": len(missed),
        "errors": [
            {"form_field": key, "expected": expected[key], "predicted": predicted.get(key, "unmapped")}
            for key in keys if key not in correct
        ],
    }


def parse_prices(items: list[str]) -> dict[str, tuple[float, float]]:
    prices = {}
    for item in items:
        name, _, pair = item.rpartition("=")
        prompt_price, _, completion_price = pair.partition(",")
        prices[name] = (float(prompt_price), float(completion_price))
    return prices


async def run_provider(spec: str, fields: list[FormField], batch_size: int, timeout: float) -> tuple[dict[str, str], dict[str, object]]:
    if spec == "rules":
        started = time.perf_counter()
        result = await KoreanMvpMapper().map_fields(fields)
        stats = {"seconds": round(time.perf_counter() - started, 3)}
    else:
        mode, _, rest = spec.partition(":")
        wrapper = {"hybrid": HybridMapper, "llmfirst": LlmFirstMapper}.get(mode)
        llm = LlmFieldMapper(endpoint_from_spec(rest if wrapper else spec), batch_size=batch_size, timeout=timeout)
        mapper = wrapper(llm) if wrapper else llm
        result = await mapper.map_fields(fields)
        if getattr(mapper, "notice", ""):
            raise LlmMappingError(mapper.notice)
        usage = llm.usage
        stats = {"seconds": round(usage.seconds, 2), "requests": usage.requests,
                 "prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens}
    # Only what would actually be typed counts as a prediction; a mapping left for the user is not filled.
    typed_only = spec.startswith("llmfirst:")
    filled = {record.form_field: "unmapped" if typed_only and record.status != "auto_accepted" else record.resume_path
              for record in result.mappings}
    stats["review"] = sum(record.status == "needs_human_review" for record in result.mappings)
    stats["ai_only"] = sum(record.source == "llm_only" for record in result.mappings)
    return filled, stats


async def main_async(args: argparse.Namespace) -> int:
    snapshot_path = Path(args.snapshot)
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    labels_path = Path(args.labels) if args.labels else EVAL_DIR / "labels" / "structures" / f"{snapshot_path.stem}.csv"
    expected = load_labels(labels_path)
    # Only labelled fields are mapped and scored (e.g. a page recorded twice is labelled once).
    fields = [field for _, field in load_fields(snapshot) if field.key in expected]
    labels = {field.key: field.label for field in fields}
    prices = parse_prices(args.price)

    report = {"snapshot": snapshot_path.name, "labels": labels_path.name, "batch_size": args.batch_size, "providers": {}}
    for spec in args.provider:
        try:
            predicted, stats = await run_provider(spec, fields, args.batch_size, args.timeout)
        except LlmMappingError as error:
            print(f"{spec}: {error}", file=sys.stderr)
            report["providers"][spec] = {"error": str(error)}
            continue
        result = {**score(predicted, expected), **stats}
        if spec in prices and "prompt_tokens" in stats:
            prompt_price, completion_price = prices[spec]
            result["cost_usd"] = round((stats["prompt_tokens"] * prompt_price + stats["completion_tokens"] * completion_price) / 1e6, 5)
        report["providers"][spec] = result

    print(f"{'provider':24} {'acc':>6} {'recall':>7} {'wrong':>6} {'missed':>7} {'sec':>7} {'tokens':>8} {'cost$':>8}")
    for spec, result in report["providers"].items():
        if "error" in result:
            print(f"{spec:24} error")
            continue
        tokens = result.get("prompt_tokens", 0) + result.get("completion_tokens", 0)
        print(f"{spec:24} {result['accuracy']:>6} {result['fill_recall']:>7} {result['wrong_fill']:>6} {result['missed']:>7} "
              f"{result['seconds']:>7} {tokens or '-':>8} {result.get('cost_usd', '-'):>8}")
    if args.show_errors:
        for spec, result in report["providers"].items():
            for error in result.get("errors", []):
                print(f"  {spec:22} {error['form_field']:12} {labels[error['form_field']][:24]:24} "
                      f"expected {error['expected']:42} got {error['predicted']}")

    out_dir = EVAL_DIR / "llm_runs"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{snapshot_path.stem}-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("saved", out.relative_to(EVAL_DIR.parent))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("snapshot", nargs="?")
    parser.add_argument("--list-openai-models", action="store_true", help="print the model ids your key can use")
    parser.add_argument("--labels", help="label CSV (default: evaluation/labels/structures/<snapshot>.csv)")
    parser.add_argument("--provider", action="append", default=[], help="rules | ollama:<model> | openai:<model> | hybrid:<llm provider> | llmfirst:<llm provider>")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=180.0, help="seconds per model request")
    parser.add_argument("--price", action="append", default=[], help="<provider>=<input $/1M>,<output $/1M>")
    parser.add_argument("--show-errors", action="store_true")
    args = parser.parse_args()
    if args.list_openai_models:
        try:
            print("\n".join(list_openai_models()))
        except LlmMappingError as error:
            print(error, file=sys.stderr)
            return 1
        return 0
    if not args.snapshot:
        parser.error("snapshot is required")
    args.provider = args.provider or ["rules"]
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
