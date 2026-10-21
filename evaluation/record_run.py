"""Record one Before/After run on a real application site without storing resume values.

The recorder drives the same core pipeline as the desktop app (resume parse -> inspect ->
map -> plan -> fill -> readback). The user logs in, solves CAPTCHA/MFA and moves between
pages manually. Final submission is never clicked.

Usage (Before, frozen rule baseline):
    PYTHONPATH="<before>/src" python evaluation/record_run.py --variant before --site site-a --resume <pdf> --url <https://...>

Output (no resume values, no URL):
    evaluation/runs/<variant>/<site>-<timestamp>.json
    evaluation/labels/<site>.csv   (ground-truth template; fill expected_path by hand)
    evaluation/structures/<site>-<timestamp>.json   (only with --dump-structure)

--dump-structure saves the form *structure* of each recorded page (labels, name/id attributes,
widget kinds, option texts, predicted paths) so a new site can be supported without sharing
an account. Field values, resume values and URLs are never written.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import shutil
import sys
import time
import uuid
from datetime import datetime
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit

from job_agent_core.browser_session import BrowserSessionError, BrowserSessionManager
from job_agent_core.browser_worker import BrowserAutomationWorker
from job_agent_core.form_inspector import inspect_page
from job_agent_core.korean_scope import enforce_korean_mvp_profile, korean_mvp_values
from job_agent_core.names import confirm_name_parts
from job_agent_core.resume_parser import ResumeParseError, extract_resume_profile, inspect_resume_file
from job_agent_core.security import URLPolicyError, domain_matches


EVAL_DIR = Path(__file__).resolve().parent
LABEL_COLUMNS = ["site", "page_index", "form_field", "label", "widget_kind", "expected_path", "note"]
MAX_STRUCTURE_OPTIONS = 30


def core_version() -> str:
    try:
        return metadata.version("job-agent-core")
    except metadata.PackageNotFoundError:
        return "unknown"


def load_profile(path: str):
    inspected = inspect_resume_file(path)
    parsed = extract_resume_profile(str(inspected["text"]))
    profile, _ = enforce_korean_mvp_profile(parsed["resume_profile"])
    name = profile.personal.name
    if name.family and name.given and not name.confirmed:
        # Mirrors the desktop app's one-time confirmation; values stay in this terminal only.
        answer = input(f"성/이름 분리 확인: 성='{name.family}', 이름='{name.given}' 맞습니까? [y/N] ").strip()
        if answer.casefold() == "y":
            profile = confirm_name_parts(profile, family=name.family, given=name.given)
    return profile, {
        "pages": inspected["pages"],
        "text_chars": len(str(inspected["text"]).strip()),
        "present_paths": sorted(path for path, value in korean_mvp_values(profile).items() if value),
    }


def update_label_template(site: str, fields: list[dict[str, object]]) -> Path:
    path = EVAL_DIR / "labels" / f"{site}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    known = {(row["page_index"], row["form_field"]) for row in rows}
    for item in fields:
        key = (str(item["page_index"]), str(item["form_field"]))
        if key in known:
            continue
        rows.append(
            {
                "site": site,
                "page_index": key[0],
                "form_field": key[1],
                "label": str(item["label"]),
                "widget_kind": str(item["widget_kind"]),
                "expected_path": "",
                "note": "",
            }
        )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LABEL_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


async def ask(prompt: str) -> str:
    """Wait for terminal input without blocking the event loop.

    Every browser request goes through the session's async route guard; a plain input() would stall
    the loop and leave the page loading forever while the prompt is shown.
    """
    return await asyncio.to_thread(input, prompt)


def field_structure(fields, mappings) -> list[dict[str, object]]:
    """Form structure only: whether a value is present is kept, the value itself never is."""
    paths = {item.form_field: item for item in mappings}
    rows = []
    for field in fields:
        mapping = paths.get(field.key)
        rows.append(
            {
                "form_field": field.key,
                "label": field.label,
                "section": field.section,
                "group_labels": field.group_labels,
                "name": field.name,
                "element_id": field.element_id,
                "type": field.type,
                "widget_kind": field.widget_kind,
                "required": field.required,
                "readonly": field.readonly,
                "placeholder": field.placeholder,
                "maxlength": field.maxlength,
                "date_format": field.date_format,
                "has_existing_value": field.current_value_present,
                "options": [option.text for option in field.options[:MAX_STRUCTURE_OPTIONS]],
                "option_count": len(field.options),
                "predicted_path": mapping.resume_path if mapping else None,
                "mapping_reason": mapping.reason if mapping else None,
            }
        )
    return rows


async def page_structure(worker: BrowserAutomationWorker, task_id: str, profile) -> list[dict[str, object]]:
    session = worker.sessions.get(task_id)
    page = await worker.sessions.application_page(task_id)
    inspection = await inspect_page(page, approved_hosts=session.approved_domains, host_allowed=domain_matches)
    plan = await worker.planner.plan(profile, inspection.fields)
    return field_structure(inspection.fields, plan.mappings)


async def record_page(
    worker: BrowserAutomationWorker, task_id: str, profile, page_index: int, *, dump_structure: bool = False
) -> dict[str, object]:
    started = time.perf_counter()
    report = await worker.inspect(task_id, profile)
    inspect_ms = round((time.perf_counter() - started) * 1000, 1)
    # Captured before filling so the snapshot shows the page as the user left it.
    structure = await page_structure(worker, task_id, profile) if dump_structure else None
    print(f"  검출 {report.field_count}개 / 연결 {sum(d.resume_path is not None for d in report.decisions)}개 / 제출류 버튼 {report.submit_control_count}개")

    fill_ms: float | None = None
    results: dict[str, object] = {}
    if (await ask("  이 페이지를 자동 입력할까요? (최종 제출은 하지 않음) [y/N] ")).strip().casefold() == "y":
        started = time.perf_counter()
        fill_report = await worker.fill(task_id, profile)
        fill_ms = round((time.perf_counter() - started) * 1000, 1)
        results = {item.form_field: item for item in fill_report.results}
        print(f"  입력·확인 {fill_report.filled_count} / 기존값 유지 {fill_report.kept_existing_count} / 사용자 확인 {fill_report.needs_human_count} / 실패 {fill_report.failed_count}")

    fields = []
    for decision in report.decisions:
        result = results.get(decision.form_field)
        fields.append(
            {
                "page_index": page_index,
                "form_field": decision.form_field,
                "label": decision.label,
                "widget_kind": decision.widget_kind,
                "predicted_path": decision.resume_path,
                "decision_status": decision.status,
                "fill_status": result.status if result else None,
                "attempts": result.attempts if result else 0,
            }
        )
    return {
        "page_index": page_index,
        "structure": structure,
        "field_count": report.field_count,
        "submit_control_count": report.submit_control_count,
        "inspect_ms": inspect_ms,
        "fill_ms": fill_ms,
        "fields": fields,
    }


async def run(args: argparse.Namespace) -> int:
    try:
        profile, resume_summary = load_profile(args.resume)
    except ResumeParseError as error:
        print(f"[실패] {error}", file=sys.stderr)
        return 1

    profile_root = Path(args.data_dir) / "eval_browser_profiles"
    sessions = BrowserSessionManager(profile_root, headless=False)
    worker = BrowserAutomationWorker(sessions)
    task_id = str(uuid.uuid4())
    pages: list[dict[str, object]] = []
    try:
        hostname = (urlsplit(args.url).hostname or "").casefold()
        try:
            await worker.open(task_id, args.url, hostname)
        except (BrowserSessionError, URLPolicyError) as error:
            print(f"[실패] {error}", file=sys.stderr)
            return 1
        print("브라우저에서 로그인/인증 후 지원서 페이지로 이동하세요.")
        while True:
            command = (await ask("\n[Enter] 현재 페이지 기록 · [a] 다른 도메인 허용 · [q] 종료 > ")).strip().casefold()
            if command == "q":
                break
            if command == "a":
                status = await worker.status(task_id)
                if status.blocked_hostname:
                    await worker.approve_domain(task_id, status.blocked_hostname)
                    print("  허용했습니다.")
                else:
                    print("  허용 대기 중인 도메인이 없습니다.")
                continue
            try:
                pages.append(
                    await record_page(worker, task_id, profile, len(pages) + 1, dump_structure=args.dump_structure)
                )
            except BrowserSessionError as error:
                print(f"  [건너뜀] {error}")
    finally:
        await worker.close_all()
        shutil.rmtree(profile_root, ignore_errors=True)

    if not pages:
        print("기록된 페이지가 없습니다.")
        return 0
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    structures = [
        {"page_index": page["page_index"], "fields": page.pop("structure")} for page in pages if page.get("structure") is not None
    ]
    for page in pages:
        page.pop("structure", None)
    output = EVAL_DIR / "runs" / args.variant / f"{args.site}-{stamp}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "variant": args.variant,
        "site": args.site,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "core_version": core_version(),
        "resume": resume_summary,
        "pages": pages,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    labels = update_label_template(args.site, [field for page in pages for field in page["fields"]])
    print(f"\n기록 저장: {output.relative_to(EVAL_DIR.parent)}")
    print(f"정답 라벨 템플릿: {labels.relative_to(EVAL_DIR.parent)}")
    if structures:
        structure_path = EVAL_DIR / "structures" / f"{args.site}-{stamp}.json"
        structure_path.parent.mkdir(parents=True, exist_ok=True)
        structure_payload = {"schema": 1, "site": args.site, "core_version": core_version(), "pages": structures}
        structure_path.write_text(json.dumps(structure_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"폼 구조 스냅샷: {structure_path.relative_to(EVAL_DIR.parent)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--variant", required=True, choices=["before", "after"])
    parser.add_argument("--site", required=True, help="실제 URL 대신 쓰는 익명 별칭 (예: site-a)")
    parser.add_argument("--resume", required=True, help="저장소 밖의 이력서 PDF 경로")
    parser.add_argument("--url", required=True, help="HTTPS 시작 주소 (결과 파일에는 저장하지 않음)")
    parser.add_argument("--data-dir", default=str(EVAL_DIR.parent / ".job_agent"))
    parser.add_argument("--dump-structure", action="store_true", help="값 없이 폼 구조만 evaluation/structures/에 저장")
    args = parser.parse_args()
    if not args.site.replace("-", "").replace("_", "").isalnum():
        parser.error("--site에는 영문/숫자/-/_만 사용하세요")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
