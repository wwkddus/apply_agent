"""Summarise Before/After runs against evaluation/labels/*.csv.

Usage:
    python evaluation/score_runs.py            # prints a table and writes evaluation/summary.json
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metrics import score_runs  # noqa: E402


EVAL_DIR = Path(__file__).resolve().parent


def load_labels() -> dict[tuple[str, int, str], str]:
    labels: dict[tuple[str, int, str], str] = {}
    for path in sorted((EVAL_DIR / "labels").glob("*.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row["expected_path"].strip():
                    labels[(row["site"], int(row["page_index"]), row["form_field"])] = row["expected_path"].strip()
    return labels


def main() -> int:
    labels = load_labels()
    summary = {}
    for variant in ("before", "after"):
        runs = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((EVAL_DIR / "runs" / variant).glob("*.json"))]
        if runs:
            summary[variant] = score_runs(runs, labels)
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
