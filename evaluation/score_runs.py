"""Summarise Before/After runs against evaluation/labels/*.csv.

Usage:
    python evaluation/score_runs.py            # prints a table and writes evaluation/summary.json

Only the latest run per (variant, site) is scored, so a repeated recording is not counted twice.
page_coverage divides each version's verified fills by the fields the page should get (taken from the
version that detected the most), so fields Before never detected count against it.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metrics import page_fillable, score_runs  # noqa: E402


EVAL_DIR = Path(__file__).resolve().parent


def load_labels() -> dict[tuple[str, str, int, str], str]:
    labels: dict[tuple[str, str, int, str], str] = {}
    for path in sorted((EVAL_DIR / "labels").glob("*.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("variant") and row["expected_path"].strip():
                    key = (row["site"], row["variant"], int(row["page_index"]), row["form_field"])
                    labels[key] = row["expected_path"].strip()
    return labels


def latest_runs(variant: str) -> list[dict]:
    latest: dict[str, dict] = {}
    for path in sorted((EVAL_DIR / "runs" / variant).glob("*.json")):
        run = json.loads(path.read_text(encoding="utf-8"))
        if run["site"] in latest:
            print(f"[참고] {variant}/{run['site']}: 이전 기록 대신 최신 기록 {path.name}만 사용합니다.")
        latest[run["site"]] = run
    return list(latest.values())


def main() -> int:
    labels = load_labels()
    runs = {variant: latest_runs(variant) for variant in ("before", "after")}
    reference = page_fillable([run for items in runs.values() for run in items], labels)
    summary = {variant: score_runs(items, labels, reference) for variant, items in runs.items() if items}
    if not summary:
        print("evaluation/runs/ 아래에 기록이 없습니다.")
        return 1
    keys = list(next(iter(summary.values())))
    print(f"{'metric':<26}" + "".join(f"{variant:>14}" for variant in summary))
    for key in keys:
        print(f"{key:<26}" + "".join(f"{str(summary[variant][key]):>14}" for variant in summary))
    (EVAL_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
