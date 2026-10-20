from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Literal

from playwright.async_api import Error as PlaywrightError, Locator
from pydantic import BaseModel, ConfigDict, Field

from .browser_session import (
    BrowserLoginNotComplete,
    BrowserRedirectApprovalRequired,
    BrowserSessionError,
    BrowserSessionManager,
    BrowserStatus,
)
from .address_search import address_matches, search_address, search_query
from .form_inspector import ControlTarget, FieldTarget, PageInspection, inspect_page
from .mapping import FormField, FormOption
from .models import ResumeProfile
from .security import domain_matches, is_safe_next_control, is_submit_like_control
from .models import clean_field
from .planning import FillAction, field_name_for_path, get_resume_value
from .constants import ALLOWED_RESUME_PATHS, SENSITIVE_STATUS_PATHS
from .resume_llm import LlmUnavailable
from .resume_lookup import DATE_WIDGETS, NO_EVIDENCE, LookupFn, LookupItem, user_only_reason
from .widgets import WidgetAction, format_like_example, pick_option, plan_widget_action
from .worker import KoreanPlanningWorker, WorkerPlan


class BrowserFieldDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    form_field: str
    label: str
    widget_kind: str
    resume_path: str | None = None
    status: str
    reason: str


class BrowserInspectionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    browser: BrowserStatus
    field_count: int
    skipped_field_count: int
    decisions: list[BrowserFieldDecision]
    safe_next_available: bool
    submit_control_count: int
    unapproved_frame_hosts: list[str] = Field(default_factory=list)


class BrowserFillResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    form_field: str
    label: str
    resume_path: str | None = None
    status: Literal[
        "filled_verified",
        "kept_existing",
        "needs_human",
        "missing_resume_value",
        "skipped",
        "failed_validation",
        "field_not_found",
        "disabled_on_page",
        "filled_llm_unconfirmed",
        # Filled from the resume and read back, but a sensitive item (병역/장애/보훈) the user confirms.
        "filled_review",
        # Ledger states: every detected field gets a row, nothing disappears silently.
        "user_only",
        "pending_lookup",
        "not_in_resume",
    ]
    reason: str
    attempts: int = 0
    widget_kind: str = ""
    fingerprint: str = ""
    # Option texts read from the page when the agent could not pick one; shown to the user.
    candidates: list[str] = Field(default_factory=list)


class BrowserFillReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    browser: BrowserStatus
    results: list[BrowserFillResult]
    field_count: int = 0
    filled_count: int
    kept_existing_count: int
    needs_human_count: int
    failed_count: int
    disabled_count: int = 0
    lookup_filled_count: int = 0
    # One-line message for the user, e.g. that the local LLM lookup was skipped.
    notice: str = ""
    safe_next_available: bool
    submit_control_count: int


@dataclass(slots=True)
class PreparedPage:
    inspection: PageInspection
    plan: WorkerPlan
    safe_next: list[ControlTarget]
    submit_controls: list[ControlTarget]


def _safe_next(control: ControlTarget) -> bool:
    # Only an explicit non-submit button is automated. Links and implicit form buttons stay manual.
    return (
        control.tag in {"button", "input"}
        and control.attr_type == "button"
        and is_safe_next_control(
            tag=control.tag,
            attr_type=control.attr_type,
            form_type=control.form_type,
            text=control.text,
        )
    )


def _submit_like(control: ControlTarget) -> bool:
    return is_submit_like_control(
        tag=control.tag,
        attr_type=control.attr_type,
        form_type=control.form_type,
        text=control.text,
    )


def _normalized(value: str, path: str) -> str:
    text = str(value or "").strip()
    if path == "person.phone":
        digits = re.sub(r"\D", "", text)
        return "0" + digits[2:] if digits.startswith("82") else digits
    if path in {"person.birth_date"}:
        return re.sub(r"\D", "", text)
    if path == "person.email":
        return text.casefold()
    return re.sub(r"\s+", "", text).casefold()


def _equivalent(actual: str, expected: str, path: str) -> bool:
    return bool(actual) and _normalized(actual, path) == _normalized(expected, path)


def _plain(value: str) -> str:
    return re.sub(r"\s+", "", value or "").casefold()


def _same_date(actual: str, iso_value: str) -> bool:
    """'2020. 03' matches 2020-03-01 when the widget shows only year and month."""
    digits = re.sub(r"\D", "", actual or "")
    expected = re.sub(r"\D", "", iso_value)
    return digits == expected or (len(digits) == 6 and expected.startswith(digits))


@dataclass(slots=True)
class _Outcome:
    error: str | None = None
    expected: str | None = None
    candidates: list[str] = field(default_factory=list)
    disabled: bool = False


DISABLED_REASON = "페이지에서 비활성화된 항목입니다. 다른 선택에 따라 입력할 수 없는 상태일 수 있습니다 (예: 재학 중이면 졸업일 없음)."
MAX_OPTION_CANDIDATES = 30
_PLACEHOLDER_OPTION_RE = re.compile(r"(선택|선택하세요|선택해\s*주세요|미선택|choose|select)(\s*\.{0,3})", re.I)


def _row(
    field: FormField, path: str | None, status: str, reason: str, candidates: list[str] | None = None
) -> BrowserFillResult:
    return BrowserFillResult(
        form_field=field.key,
        label=field.label,
        resume_path=path,
        widget_kind=field.widget_kind,
        fingerprint=field.fingerprint,
        status=status,
        reason=reason,
        candidates=candidates if candidates is not None else _page_options(field),
    )


def _ledger_rows(fields: list[FormField], reported: set[str]) -> list[BrowserFillResult]:
    """A row for every field the rules did not report, with why it was left or what happens next."""
    rows: list[BrowserFillResult] = []
    for field in fields:
        if field.key in reported:
            continue
        reason = user_only_reason(field)
        if reason:
            rows.append(_row(field, None, "user_only", reason))
        elif field.current_value_present:
            rows.append(_row(field, None, "kept_existing", "페이지에 이미 입력된 값을 유지했습니다."))
        else:
            rows.append(_row(field, None, "pending_lookup", "규칙에 없는 항목이라 이력서에서 추가로 확인합니다."))
    return rows


def _lookup_action(field: FormField, value: str) -> WidgetAction:
    """Plan a looked-up value with the same adapters; dates reuse the date planning rules."""
    path = f"lookup:{field.name or field.label}"[:200]
    if field.widget_kind in DATE_WIDGETS:
        try:
            full = len(re.findall(r"\d+", value)) >= 3
            iso = clean_field("birth_date" if full else "admission_date", value)
        except ValueError:
            return WidgetAction(form_field=field.key, resume_path=path, widget_kind=field.widget_kind,
                                status="needs_human", reason="날짜 형식을 확인할 수 없습니다")
        planned = plan_widget_action(field, "person.birth_date" if full else "education.university.admission_date", iso)
    elif field.widget_kind == "custom_combobox":
        planned = plan_widget_action(field, "skills", value)
    else:
        planned = plan_widget_action(field, "user_input", value)
    return planned.model_copy(update={"resume_path": path})


def _page_options(field: FormField) -> list[str]:
    """Option texts already detected on the page, offered to the user in the review dialog."""
    texts = [" ".join(option.text.split()) for option in field.options]
    texts = [text for text in texts if text and not _PLACEHOLDER_OPTION_RE.fullmatch(text)]
    return list(dict.fromkeys(texts))[:MAX_OPTION_CANDIDATES]


# Never answered by the second check: 상세주소/우편번호 without a confirmed 기본주소 would be filled
# alone, and the model cannot tell which word of a romanized name is the surname.
_NO_LOOKUP_PATHS = {
    "person.address",
    "person.postal_code",
    "person.address_detail",
    "person.name_en.family",
    "person.name_en.given",
}


_GRADUATE_WORD_RE = re.compile(r"대학원|post\s*graduate|graduate\s*school", re.I)
# (level, add-button wording, name shown to the user). High school blocks are never opened.
_ADD_SECTION_BUTTONS = (
    ("university", re.compile(r"(대학|university|college|학사).{0,20}(추가|add)|add.{0,20}(university|college)", re.I), "대학 학력"),
    ("graduate_school", re.compile(r"(대학원|post\s*graduate|graduate\s*school).{0,20}(추가|add)|add.{0,20}(post\s*graduate|graduate)", re.I), "대학원 학력"),
)


def _address_group(actions: list[FillAction], profile: ResumeProfile) -> dict[str, FillAction]:
    """주소/우편번호/상세주소 actions handled together when the page's 주소 field is read-only."""
    by_path = {action.resume_path: action for action in actions}
    address = by_path.get("person.address")
    if address is None or address.status != "readonly" or not get_resume_value(profile, "person.address"):
        return {}
    return {path: action for path, action in by_path.items()
            if path in {"person.address", "person.postal_code", "person.address_detail"}}


class BrowserAutomationWorker:
    def __init__(
        self,
        sessions: BrowserSessionManager,
        planner: KoreanPlanningWorker | None = None,
    ):
        self.sessions = sessions
        self.planner = planner or KoreanPlanningWorker()

    async def open(self, task_id: str, raw_url: str, target_hostname: str) -> BrowserStatus:
        return await self.sessions.open(task_id, raw_url, target_hostname)

    async def status(self, task_id: str) -> BrowserStatus:
        return await self.sessions.status(task_id)

    async def approve_domain(self, task_id: str, hostname: str) -> BrowserStatus:
        return await self.sessions.approve_domain(task_id, hostname)

    async def close(self, task_id: str) -> None:
        await self.sessions.close(task_id)

    async def close_all(self) -> None:
        await self.sessions.close_all()

    async def _prepare(self, task_id: str, profile: ResumeProfile) -> PreparedPage:
        session = self.sessions.get(task_id)
        if session.blocked_hostname:
            raise BrowserRedirectApprovalRequired(
                "다른 도메인으로 이동하려면 Apply Local 화면에서 먼저 허용해야 합니다."
            )
        page = await self.sessions.application_page(task_id)
        inspection = await inspect_page(
            page,
            approved_hosts=session.approved_domains,
            host_allowed=domain_matches,
        )
        session.discovered_frame_hosts.update(inspection.unapproved_frame_hosts)
        if inspection.login_controls_detected:
            raise BrowserLoginNotComplete(
                "로그인 또는 본인 인증 화면이 감지되었습니다. 브라우저에서 직접 완료한 뒤 다시 확인해 주세요."
            )
        plan = await self.planner.plan(profile, inspection.fields)
        return PreparedPage(
            inspection=inspection,
            plan=plan,
            safe_next=[control for control in inspection.controls if _safe_next(control)],
            submit_controls=[control for control in inspection.controls if _submit_like(control)],
        )

    async def inspect(self, task_id: str, profile: ResumeProfile) -> BrowserInspectionReport:
        prepared = await self._prepare(task_id, profile)
        fields = {item.key: item for item in prepared.inspection.fields}
        mappings = {item.form_field: item for item in prepared.plan.mappings}
        actions = {item.form_field: item for item in prepared.plan.fill_actions}
        widget_actions = {item.form_field: item for item in prepared.plan.widget_actions}
        decisions: list[BrowserFieldDecision] = []
        for key, field in fields.items():
            mapping = mappings.get(key)
            action = actions.get(key)
            widget = widget_actions.get(key)
            if mapping is None:
                decisions.append(
                    BrowserFieldDecision(
                        form_field=key,
                        label=field.label,
                        widget_kind=field.widget_kind,
                        status="skipped",
                        reason="notebook fallback 규칙에서 이력서 항목과 연결하지 못했습니다.",
                    )
                )
                continue
            status = action.status if action else mapping.status
            reason = action.reason if action else mapping.reason
            if widget and widget.status == "needs_human":
                status = "needs_human"
                reason = widget.reason
            decisions.append(
                BrowserFieldDecision(
                    form_field=key,
                    label=field.label,
                    widget_kind=field.widget_kind,
                    resume_path=mapping.resume_path,
                    status=status,
                    reason=reason,
                )
            )
        return BrowserInspectionReport(
            browser=await self.sessions.status(task_id),
            field_count=len(prepared.inspection.fields),
            skipped_field_count=prepared.plan.skipped_field_count,
            decisions=decisions,
            safe_next_available=len(prepared.safe_next) == 1,
            submit_control_count=len(prepared.submit_controls),
            unapproved_frame_hosts=sorted(prepared.inspection.unapproved_frame_hosts),
        )

    async def fill(self, task_id: str, profile: ResumeProfile) -> BrowserFillReport:
        prepared = await self._prepare(task_id, profile)
        opened = await self._expand_sections(prepared, profile)
        if opened:
            prepared = await self._prepare(task_id, profile)
        fields = {item.key: item for item in prepared.inspection.fields}
        targets = prepared.inspection.targets
        widget_actions = {item.form_field: item for item in prepared.plan.widget_actions}
        results: list[BrowserFillResult] = []
        address_group = _address_group(prepared.plan.fill_actions, profile)
        deferred = {action.form_field for action in address_group.values()}
        for action in prepared.plan.fill_actions:
            if action.form_field not in deferred:
                results.append(await self._fill_action(action, fields, targets, widget_actions))
        if address_group:
            results.extend(await self._fill_address(task_id, profile, address_group, fields, targets, widget_actions))

        results.extend(_ledger_rows(prepared.inspection.fields, {item.form_field for item in results}))
        try:
            refreshed = await self._prepare(task_id, profile)
        except BrowserSessionError:
            refreshed = prepared
        # A field disabled in the first pass may be enabled by a later choice (e.g. graduation status).
        if refreshed is not prepared and any(item.status == "disabled_on_page" for item in results):
            results = await self._retry_disabled(results, refreshed)
        filled_count = sum(item.status == "filled_verified" for item in results)
        kept_count = sum(item.status == "kept_existing" for item in results)
        failed_count = sum(item.status in {"failed_validation", "field_not_found"} for item in results)
        disabled_count = sum(item.status == "disabled_on_page" for item in results)
        manual_count = sum(item.status in {"needs_human", "missing_resume_value", "skipped", "filled_review"} for item in results)
        return BrowserFillReport(
            browser=await self.sessions.status(task_id),
            results=results,
            field_count=len(prepared.inspection.fields),
            filled_count=filled_count,
            kept_existing_count=kept_count,
            needs_human_count=manual_count,
            failed_count=failed_count,
            disabled_count=disabled_count,
            notice=(
                f"{', '.join(opened)} 입력란을 '추가' 버튼으로 열어 채웠습니다. 필요 없으면 해당 블록의 '삭제'를 누르세요."
                if opened
                else ""
            ),
            safe_next_available=len(refreshed.safe_next) == 1,
            submit_control_count=len(refreshed.submit_controls),
        )

    async def _expand_sections(self, prepared: PreparedPage, profile: ResumeProfile) -> list[str]:
        """Click '대학/대학원 학력 추가' when the resume has that level and the page has no field for it yet.

        Only explicit type=button add buttons are clicked; they add an empty block on the page and save
        nothing. Save and submit buttons are never clicked.
        """
        mapped = {mapping.resume_path for mapping in prepared.plan.mappings}
        opened: list[str] = []
        for level, pattern, name in _ADD_SECTION_BUTTONS:
            if not get_resume_value(profile, f"education.{level}.school_name"):
                continue
            if f"education.{level}.school_name" in mapped:
                continue
            buttons = [
                control for control in prepared.inspection.controls
                if control.tag in {"button", "input"} and control.attr_type == "button"
                and not _submit_like(control) and pattern.search(control.text)
                and not (level == "university" and _GRADUATE_WORD_RE.search(control.text))
            ]
            if not buttons:
                continue
            locator = buttons[0].frame.locator(buttons[0].selector)
            try:
                if await locator.count() != 1 or not await locator.is_visible():
                    continue
                await locator.click(timeout=5_000)
            except PlaywrightError:
                continue
            await asyncio.sleep(0.5)
            opened.append(name)
        return opened

    async def _fill_action(
        self,
        action: FillAction,
        fields: dict[str, FormField],
        targets: dict[str, FieldTarget],
        widget_actions: dict[str, WidgetAction],
    ) -> BrowserFillResult:
        field = fields[action.form_field]
        base = {
            "form_field": action.form_field,
            "label": field.label,
            "resume_path": action.resume_path,
            "widget_kind": field.widget_kind,
            "fingerprint": field.fingerprint,
            "candidates": _page_options(field),
        }
        if action.status == "kept_existing":
            return BrowserFillResult(**base, status="kept_existing", reason="페이지에 이미 입력된 값을 유지했습니다.")
        if action.status in {"missing_required", "missing_optional"}:
            return BrowserFillResult(
                **base, status="missing_resume_value", reason="이력서에서 값을 찾지 못해 입력하지 않았습니다."
            )
        if action.status != "planned":
            return BrowserFillResult(**base, status="needs_human", reason=action.reason)
        widget = widget_actions.get(action.form_field)
        target = targets.get(action.form_field)
        if target is None:
            return BrowserFillResult(**base, status="field_not_found", reason="DOM에서 필드를 다시 찾지 못했습니다.")
        if widget is None or widget.status != "planned" or widget.action is None:
            return BrowserFillResult(
                **base,
                status="needs_human",
                reason=widget.reason if widget else "안전한 입력 방법을 결정하지 못했습니다.",
            )
        return await self._execute(field, target, widget)

    async def _fill_address(
        self,
        task_id: str,
        profile: ResumeProfile,
        group: dict[str, FillAction],
        fields: dict[str, FormField],
        targets: dict[str, FieldTarget],
        widget_actions: dict[str, WidgetAction],
    ) -> list[BrowserFillResult]:
        """Read-only 주소 via the postcode search window; 상세주소 only after the address is confirmed."""
        address_action = group["person.address"]
        postal_action = group.get("person.postal_code")
        detail_action = group.get("person.address_detail")

        def row(action: FillAction, status: str, reason: str, candidates: list[str] | None = None) -> BrowserFillResult:
            return _row(fields[action.form_field], action.resume_path, status, reason, candidates)

        session = self.sessions.get(task_id)
        target = targets.get(address_action.form_field)
        address = get_resume_value(profile, "person.address")
        outcome = None
        verified = False
        if target is not None:
            page = await self.sessions.application_page(task_id)
            outcome = await search_address(
                page,
                target.frame,
                target.selector,
                address=address,
                postal_code=profile.personal.postal_code,
                host_approved=lambda host: any(domain_matches(host, item) for item in session.approved_domains),
            )
            if outcome.approval_host:
                session.discovered_frame_hosts.add(outcome.approval_host)
            if outcome.ok:
                actual = await self._read_back(targets[address_action.form_field])
                verified = address_matches(actual, outcome.address, search_query(address))
        results: list[BrowserFillResult] = []
        if verified:
            assert outcome is not None
            results.append(row(address_action, "filled_verified", "주소 검색 창에서 이력서 주소와 일치하는 결과 하나를 선택하고 페이지 값을 다시 확인했습니다."))
            if postal_action is not None:
                postal_target = targets.get(postal_action.form_field)
                actual = await self._read_back(postal_target) if postal_target else ""
                if re.sub(r"\D", "", actual) == outcome.zonecode:
                    results.append(row(postal_action, "filled_verified", "주소 검색으로 입력된 우편번호를 다시 확인했습니다."))
                else:
                    results.append(row(postal_action, "needs_human", "주소 검색 후 우편번호 칸에서 선택한 결과의 우편번호가 확인되지 않았습니다."))
            if detail_action is not None:
                results.append(await self._fill_action(detail_action, fields, targets, widget_actions))
            return results
        if outcome is None:
            reason, candidates = "DOM에서 주소 칸을 다시 찾지 못했습니다.", []
        elif outcome.ok:
            outcome.trace.append("readback:mismatch")
            reason, candidates = "주소를 선택했지만 페이지의 주소 칸에서 확인되지 않았습니다. 직접 확인해 주세요.", []
        else:
            reason, candidates = outcome.reason, outcome.candidates
        if outcome is not None and outcome.trace:
            # Steps only, never values: shows on a real site where the search stopped.
            reason += f" (진단: {' > '.join(outcome.trace)})"
        results.append(row(address_action, "needs_human", reason, candidates))
        if postal_action is not None:
            results.append(row(postal_action, "needs_human", "주소 검색 창에서 주소를 고르면 함께 입력되는 항목입니다."))
        if detail_action is not None:
            results.append(
                row(detail_action, "needs_human", "기본주소가 아직 입력되지 않아 상세주소도 입력하지 않았습니다. 주소를 선택한 뒤 '다시 자동 입력'을 누르면 채웁니다.")
            )
        return results

    @staticmethod
    async def _read_back(target: FieldTarget, timeout: float = 3.0) -> str:
        """The field's value once the page has written it (the widget fills it asynchronously)."""
        deadline = asyncio.get_running_loop().time() + timeout
        value = ""
        while asyncio.get_running_loop().time() < deadline:
            try:
                value = (await target.frame.locator(target.selector).first.input_value()).strip()
            except PlaywrightError:
                value = ""
            if value:
                break
            await asyncio.sleep(0.2)
        return value

    async def fill_lookup(
        self,
        task_id: str,
        profile: ResumeProfile,
        lookup: LookupFn | None,
        requests: list[tuple[str, str]],
    ) -> BrowserFillReport:
        """Second check of the rows fill() could not finish; requests are (fingerprint, previous status).

        Rows that were only waiting for this check always get a final state. Rows the rules already
        reported (missing value, ambiguous option) are returned only when the lookup changes them.
        """
        prepared = await self._prepare(task_id, profile)
        by_fingerprint = {item.fingerprint: item for item in prepared.inspection.fields}
        mappings = {item.form_field: item for item in prepared.plan.mappings}
        pending: set[str] = set()
        rule_paths: dict[str, str] = {}
        items: list[LookupItem] = []
        results: list[BrowserFillResult] = []
        for fingerprint, previous in requests:
            field = by_fingerprint.get(fingerprint)
            if field is None:
                continue
            if previous == "pending_lookup":
                pending.add(field.key)
            reason = user_only_reason(field)
            if reason:
                if previous == "pending_lookup":
                    results.append(_row(field, None, "user_only", reason))
                continue
            if field.current_value_present:
                if previous == "pending_lookup":
                    results.append(_row(field, None, "kept_existing", "페이지에 이미 입력된 값을 유지했습니다."))
                continue
            mapping = mappings.get(field.key)
            path = mapping.resume_path if mapping and mapping.resume_path in ALLOWED_RESUME_PATHS else ""
            if path in _NO_LOOKUP_PATHS:
                # Address parts follow the address search; English name parts need a known order.
                continue
            if field.widget_kind == "custom_select" and not field.options:
                field = await self._read_options(field, prepared.inspection.targets.get(field.key))
            if path:
                rule_paths[field.key] = path
            items.append(LookupItem(field=field, known_value=get_resume_value(profile, path) if path else ""))

        notice = ""
        accepted: dict[str, str] = {}
        rejected: dict[str, str] = {}
        if items and lookup is not None:
            try:
                accepted, rejected = await lookup(items)
            except LlmUnavailable as error:
                notice = f"이력서 추가 확인을 건너뛰었습니다: {error}"
        elif items:
            notice = "로컬 LLM이 꺼져 있어 이력서 추가 확인을 하지 않았습니다."
        for item in items:
            field = item.field
            value = accepted.get(field.key)
            if value:
                target = prepared.inspection.targets.get(field.key)
                results.append(await self._fill_looked_up(field, target, value, rule_paths.get(field.key)))
            elif field.key in pending:
                results.append(
                    _row(field, None, "not_in_resume", rejected.get(field.key) or notice or NO_EVIDENCE, _page_options(field))
                )

        failed = sum(item.status in {"failed_validation", "field_not_found"} for item in results)
        return BrowserFillReport(
            browser=await self.sessions.status(task_id),
            results=results,
            field_count=len(prepared.inspection.fields),
            filled_count=0,
            kept_existing_count=sum(item.status == "kept_existing" for item in results),
            needs_human_count=sum(item.status in {"needs_human", "filled_llm_unconfirmed"} for item in results),
            failed_count=failed,
            disabled_count=sum(item.status == "disabled_on_page" for item in results),
            lookup_filled_count=sum(item.status == "filled_llm_unconfirmed" for item in results),
            notice=notice,
            safe_next_available=len(prepared.safe_next) == 1,
            submit_control_count=len(prepared.submit_controls),
        )

    async def _read_options(self, field: FormField, target: FieldTarget | None) -> FormField:
        """Open a closed listbox once to learn its options, then close it without choosing."""
        if target is None:
            return field
        locator = target.frame.locator(target.selector).first
        try:
            if not await locator.is_visible() or not await locator.is_enabled():
                return field
            await locator.click()
            texts = [" ".join((await option.inner_text()).split()) for option in await self._wait_for_options(target, 1.5)]
            await self._dismiss(target, locator, typed=False)
        except PlaywrightError:
            return field
        options = [FormOption(value=text, text=text) for text in dict.fromkeys(texts) if text]
        return field.model_copy(update={"options": options[:MAX_OPTION_CANDIDATES]})

    async def _fill_looked_up(
        self, field: FormField, target: FieldTarget | None, value: str, rule_path: str | None
    ) -> BrowserFillResult:
        candidates = [value, *[text for text in _page_options(field) if text != value]]
        action = _lookup_action(field, value)
        if rule_path:
            # A rule-mapped field keeps its resume path; only the option was converted.
            action = action.model_copy(update={"resume_path": rule_path})
        if target is None:
            return _row(field, action.resume_path, "field_not_found", "DOM에서 필드를 다시 찾지 못했습니다.", candidates)
        if action.status != "planned" or action.action is None:
            return _row(
                field, action.resume_path, "needs_human",
                f"이력서에서 찾은 값 '{value}'을(를) 입력할 방법을 확정하지 못했습니다.", candidates,
            )
        result = await self._execute(field, target, action, overwrite=False)
        if result.status in {"filled_verified", "filled_review"}:
            result = result.model_copy(
                update={
                    "status": "filled_llm_unconfirmed",
                    "reason": "로컬 LLM이 이력서 원문에서 찾아 입력했습니다. 값이 맞는지 확인해 주세요.",
                }
            )
        return result.model_copy(update={"candidates": candidates})

    async def _retry_disabled(self, results: list[BrowserFillResult], refreshed: PreparedPage) -> list[BrowserFillResult]:
        by_fingerprint = {item.fingerprint: item for item in refreshed.inspection.fields}
        widget_actions = {item.form_field: item for item in refreshed.plan.widget_actions}
        updated: list[BrowserFillResult] = []
        for result in results:
            field = by_fingerprint.get(result.fingerprint) if result.status == "disabled_on_page" else None
            widget = widget_actions.get(field.key) if field else None
            target = refreshed.inspection.targets.get(field.key) if field else None
            if field is None or widget is None or target is None or widget.status != "planned" or widget.action is None:
                updated.append(result)
                continue
            retried = await self._execute(field, target, widget)
            updated.append(retried.model_copy(update={"attempts": result.attempts + retried.attempts}))
        return updated

    async def fill_field(
        self,
        task_id: str,
        profile: ResumeProfile,
        form_field: str,
        label: str,
        value: str,
        *,
        fingerprint: str = "",
        chosen: bool = False,
    ) -> BrowserFillResult:
        """Fill one field with a value the user chose in the review dialog, then read it back."""
        prepared = await self._prepare(task_id, profile)
        # Positional keys shift when the page adds fields; the fingerprint must name exactly one field.
        if fingerprint:
            matches = [item for item in prepared.inspection.fields if item.fingerprint == fingerprint]
        else:
            matches = [item for item in prepared.inspection.fields if item.key == form_field]
        field = matches[0] if len(matches) == 1 else None
        if field is None or field.label != label:
            raise BrowserSessionError("페이지가 바뀌어 이 항목을 다시 찾지 못했습니다. '이 페이지 자동 입력'을 다시 실행해 주세요.")
        form_field = field.key
        if field.widget_kind in {"checkbox", "file", "unsupported"} or (field.readonly and field.widget_kind != "custom_date_picker"):
            raise BrowserSessionError("이 항목은 브라우저에서 직접 처리해야 합니다.")
        mapping = next((item for item in prepared.plan.mappings if item.form_field == form_field), None)
        path = mapping.resume_path if mapping and mapping.resume_path != "unmapped" else "user_input"
        try:
            clean = clean_field(field_name_for_path(path) if path in ALLOWED_RESUME_PATHS else "skills", value)
        except ValueError as error:
            raise BrowserSessionError(f"입력값 형식이 올바르지 않습니다: {error}") from None
        if not clean:
            raise BrowserSessionError("입력할 값이 비어 있습니다.")
        action = plan_widget_action(field, path, clean, mapping.part if mapping else None)
        if action.status != "planned":
            return BrowserFillResult(
                form_field=form_field,
                label=field.label,
                resume_path=path,
                widget_kind=field.widget_kind,
                fingerprint=field.fingerprint,
                status="needs_human",
                reason=action.reason,
                candidates=_page_options(field),
            )
        if chosen:
            # Search with the text that produced the candidates, then click the identical option.
            original = get_resume_value(profile, path) if path in ALLOWED_RESUME_PATHS else ""
            action = action.model_copy(update={"chosen": True, "search_text": original or clean})
        target = prepared.inspection.targets.get(form_field)
        if target is None:
            raise BrowserSessionError("DOM에서 필드를 다시 찾지 못했습니다.")
        return await self._execute(field, target, action, overwrite=True)

    async def advance(self, task_id: str, profile: ResumeProfile) -> BrowserStatus:
        prepared = await self._prepare(task_id, profile)
        if len(prepared.safe_next) != 1:
            raise BrowserSessionError("안전한 '다음' 버튼을 하나로 확정할 수 없습니다. 브라우저에서 직접 이동해 주세요.")
        control = prepared.safe_next[0]
        if not _safe_next(control):
            raise BrowserSessionError("이 버튼은 최종 제출과 안전하게 구분되지 않아 자동으로 누르지 않습니다.")
        locator = control.frame.locator(control.selector)
        if await locator.count() != 1 or not await locator.is_visible() or not await locator.is_enabled():
            raise BrowserSessionError("'다음' 버튼의 상태가 바뀌었습니다. 브라우저에서 직접 확인해 주세요.")
        await locator.click(timeout=5_000)
        page = await self.sessions.application_page(task_id)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except PlaywrightError:
            pass
        await asyncio.sleep(0.35)
        return await self.sessions.status(task_id)

    async def _execute(
        self,
        field: FormField,
        target: FieldTarget,
        action: WidgetAction,
        *,
        overwrite: bool = False,
    ) -> BrowserFillResult:
        base = {
            "form_field": field.key,
            "label": field.label,
            "resume_path": action.resume_path,
            "widget_kind": field.widget_kind,
            "fingerprint": field.fingerprint,
        }
        try:
            if not overwrite and await self._has_existing(target):
                return BrowserFillResult(
                    **base,
                    status="kept_existing",
                    reason="실행 직전에 기존 값이 확인되어 유지했습니다.",
                )
        except PlaywrightError:
            return BrowserFillResult(**base, status="field_not_found", reason="DOM에서 필드를 다시 찾지 못했습니다.")

        # Retry at most once: with the alternative format when one exists, otherwise the same value.
        candidates = [action]
        if action.action in {"fill", "select_option", "check_radio", "select_parts", "choose_aria_radio"}:
            retry_value = action.fallback_value if action.action == "fill" and action.fallback_value else action.value
            candidates.append(action.model_copy(update={"value": retry_value, "fallback_value": None}))
        for attempt, candidate in enumerate(candidates, start=1):
            try:
                outcome = await self._execute_once(target, candidate)
                if outcome.disabled:
                    return BrowserFillResult(**base, status="disabled_on_page", reason=DISABLED_REASON, attempts=attempt)
                if outcome.error:
                    return BrowserFillResult(
                        **base,
                        status="needs_human",
                        reason=outcome.error,
                        attempts=attempt,
                        candidates=outcome.candidates,
                    )
                if await self._verified(target, candidate, outcome.expected):
                    if action.resume_path in SENSITIVE_STATUS_PATHS:
                        return BrowserFillResult(
                            **base,
                            status="filled_review",
                            reason="이력서 값대로 입력하고 다시 확인했습니다. 병역·장애·보훈은 민감 정보라 직접 확인해 주세요.",
                            attempts=attempt,
                        )
                    return BrowserFillResult(
                        **base,
                        status="filled_verified",
                        reason="입력 후 브라우저에서 값을 다시 확인했습니다."
                        + (" (대체 형식으로 재시도해 통과)" if candidate.value != action.value else ""),
                        attempts=attempt,
                    )
            except PlaywrightError:
                return BrowserFillResult(
                    **base,
                    status="needs_human",
                    reason="입력 중 페이지 구조가 바뀌어 안전하게 계속할 수 없습니다.",
                    attempts=attempt,
                )
        cleared = await self._clear(target, action)
        return BrowserFillResult(
            **base,
            status="failed_validation",
            reason="브라우저 재확인 결과가 입력 예정 값과 일치하지 않아 건너뛰었습니다."
            + (" 잘못 남은 값은 지웠습니다." if cleared else " 남은 값을 직접 확인해 주세요."),
            attempts=len(candidates),
        )

    async def _clear(self, target: FieldTarget, action: WidgetAction) -> bool:
        """Undo a failed text/select input so an unverified value is never left behind."""
        try:
            locator = target.frame.locator(target.selector).first
            if action.action in {"fill", "choose_suggestion"}:
                await locator.fill("")
                return not (await locator.input_value()).strip()
            if action.action == "select_option":
                await locator.select_option(value="")
                return not (await locator.input_value()).strip()
        except PlaywrightError:
            pass
        return False

    async def _has_existing(self, target: FieldTarget) -> bool:
        if target.widget_kind == "split_date_select":
            for part in target.part_keys:
                if not (await target.frame.locator(target.part_selector(part)).input_value()).strip():
                    return False
            return True
        locator = target.frame.locator(target.selector)
        if target.widget_kind == "radio_group":
            return await locator.locator(":checked").count() > 0
        if target.widget_kind == "aria_radio_group":
            return await locator.locator('[role="radio"][aria-checked="true"]').count() > 0
        if await locator.count() != 1:
            raise PlaywrightError("field count changed")
        if await locator.get_attribute("data-placeholder-shown") is not None or await locator.get_attribute("data-placeholder") == "true":
            return False
        if target.widget_kind == "custom_select" or (
            target.widget_kind == "custom_date_picker" and await locator.evaluate("element => element.tagName") == "BUTTON"
        ):
            text = (await locator.inner_text()).strip()
            return bool(text and not re.fullmatch(r"선택|선택하세요|미선택|choose|select|날짜 선택", text, re.I))
        try:
            return bool((await locator.input_value()).strip())
        except PlaywrightError:
            return bool((await locator.get_attribute("data-value") or "").strip())

    async def _execute_once(self, target: FieldTarget, action: WidgetAction) -> _Outcome:
        locator = target.frame.locator(target.selector)
        if action.action == "select_parts":
            assert isinstance(action.value, dict)
            for part, value in action.value.items():
                part_locator = target.frame.locator(target.part_selector(part))
                if await part_locator.count() != 1 or not await part_locator.is_enabled():
                    return _Outcome("날짜 선택 항목의 상태가 바뀌어 사용자가 직접 확인해야 합니다.")
                await part_locator.select_option(value=value)
            return _Outcome()
        if await locator.count() < 1:
            return _Outcome("DOM에서 필드를 다시 찾지 못했습니다.")
        first = locator.first
        if target.widget_kind == "radio_group":
            if not await first.is_enabled():
                return _Outcome("보이지 않거나 비활성화된 필드는 자동 입력하지 않습니다.", disabled=True)
        elif not await first.is_visible() or not await first.is_enabled():
            return _Outcome("보이지 않거나 비활성화된 필드는 자동 입력하지 않습니다.", disabled=True)
        assert action.value is not None
        if action.action == "fill":
            assert isinstance(action.value, str)
            await first.fill(action.value)
            if target.widget_kind == "custom_combobox":
                await first.press("Escape")
            return _Outcome(expected=action.value)
        if action.action == "select_option":
            assert isinstance(action.value, str)
            await first.select_option(value=action.value)
            return _Outcome(expected=action.value)
        if action.action == "check_radio":
            assert isinstance(action.value, str)
            matches = await self._radio_matches(locator, action.value)
            if len(matches) != 1:
                return _Outcome("일치하는 라디오 선택지를 하나로 확정할 수 없습니다.")
            radio = matches[0]
            if await radio.is_visible():
                await radio.check()
            else:
                # A custom-styled radio is hidden behind its label; click the label like the user would.
                label = radio.locator("xpath=ancestor::label[1]")
                if await label.count() != 1 or not await label.is_visible():
                    return _Outcome("선택지 버튼을 화면에서 찾지 못했습니다. 직접 선택해 주세요.")
                await label.click()
            return _Outcome(expected=action.value)
        if action.action == "choose_aria_radio":
            assert isinstance(action.value, str)
            radios = first.locator('[role="radio"]')
            matches = [item for item in await radios.all() if _plain(await item.inner_text()) == _plain(action.value)]
            if len(matches) != 1:
                return _Outcome("일치하는 버튼형 선택지를 하나로 확정할 수 없습니다.")
            await matches[0].click()
            return _Outcome(expected=action.value)
        if action.action in {"choose_suggestion", "choose_custom_option"}:
            assert isinstance(action.value, str)
            await first.click()
            if action.action == "choose_suggestion":
                await first.fill(action.search_text or action.value)
            options = await self._wait_for_options(target)
            texts = [" ".join((await option.inner_text()).split()) for option in options]
            index, candidates = pick_option(texts, action.value, action.resume_path, chosen=action.chosen)
            if index is None:
                await self._dismiss(target, first, typed=action.action == "choose_suggestion")
                if not texts:
                    return _Outcome("선택 목록이 나타나지 않았습니다. 직접 선택해 주세요.")
                return _Outcome(
                    "이력서 값과 일치하는 선택지를 하나로 확정할 수 없습니다. 후보 중에서 골라 주세요.",
                    candidates=candidates or texts[:5],
                )
            await options[index].click()
            return _Outcome(expected=texts[index])
        if action.action == "adapter_set_date":
            assert isinstance(action.value, str)
            return await self._set_custom_date(target, first, action.value)
        return _Outcome("지원하지 않는 입력 동작입니다.")

    @staticmethod
    async def _radio_matches(locator: Locator, value: str) -> list[Locator]:
        """Radios by value, or by position ('#1') when the page gives them no distinct values."""
        radios = await locator.all()
        if re.fullmatch(r"#\d+", value):
            index = int(value[1:])
            return [radios[index]] if index < len(radios) else []
        return [item for item in radios if (await item.get_attribute("value")) == value]

    @staticmethod
    async def _wait_for_options(target: FieldTarget, timeout: float = 3.0) -> list[Locator]:
        """Wait for suggestions that may load asynchronously; stop once the list is stable."""
        selector = '[role="option"]:visible, [role="listbox"] button:visible, [role="listbox"] li:visible'
        previous = -1
        options: list[Locator] = []
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.15)
            options = await target.frame.locator(selector).all()
            if options and len(options) == previous:
                break
            previous = len(options)
        return options

    @staticmethod
    async def _dismiss(target: FieldTarget, locator: Locator, *, typed: bool) -> None:
        try:
            if typed:
                await locator.fill("")
            await locator.press("Escape")
        except PlaywrightError:
            pass

    async def _set_custom_date(self, target: FieldTarget, locator: Locator, iso_value: str) -> _Outcome:
        year, month, day = iso_value.split("-")
        try:
            await locator.click()
        except PlaywrightError:
            pass
        frame = target.frame
        await asyncio.sleep(0.2)
        # Many component libraries put a typed input inside the open calendar popover.
        popover_inputs = frame.locator(
            '[role="dialog"]:visible input:visible, [role="application"]:visible input:visible, '
            '[data-part="content"][data-state="open"] input:visible'
        )
        if await popover_inputs.count() == 1:
            box = popover_inputs.first
            formatted = format_like_example(await box.get_attribute("placeholder"), iso_value)
            if formatted is None:
                await box.press("Escape")
                return _Outcome("달력 입력칸의 날짜 형식을 확실히 판단할 수 없습니다.")
            await box.fill(formatted)
            await box.press("Enter")
            await asyncio.sleep(0.2)
            if await popover_inputs.count():
                try:
                    await popover_inputs.first.press("Escape")
                except PlaywrightError:
                    pass
            return _Outcome(expected=iso_value)
        dialogs = frame.locator('[role="dialog"]:visible')
        if await dialogs.count() == 0:
            sibling = locator.locator("xpath=following-sibling::button[1]")
            if await sibling.count() == 1:
                await sibling.click()
                await asyncio.sleep(0.1)
        if await dialogs.count() != 1:
            return _Outcome("안전하게 인식할 수 있는 달력 대화상자를 하나로 확정하지 못했습니다.")
        dialog = dialogs.first
        year_select = dialog.locator('select[aria-label*="연도"], select[aria-label*="년"]')
        month_select = dialog.locator('select[aria-label*="월"]')
        if await year_select.count() != 1 or await month_select.count() != 1:
            return _Outcome("달력의 연도와 월 선택 항목을 확실히 찾지 못했습니다.")
        if not await self._select_number(year_select, int(year)) or not await self._select_number(month_select, int(month)):
            return _Outcome("달력에서 이력서 날짜와 일치하는 연도 또는 월을 찾지 못했습니다.")
        days = dialog.locator('[role="gridcell"]:visible, button[data-day]:visible')
        matches: list[Locator] = []
        for candidate in await days.all():
            if re.sub(r"\D", "", await candidate.inner_text()) == str(int(day)):
                matches.append(candidate)
        if len(matches) != 1:
            return _Outcome("달력에서 날짜를 하나로 확정할 수 없습니다.")
        await matches[0].click()
        return _Outcome(expected=iso_value)

    @staticmethod
    async def _select_number(locator: Locator, number: int) -> bool:
        options = await locator.locator("option").all()
        matches = []
        for option in options:
            value = re.sub(r"\D", "", (await option.get_attribute("value")) or "")
            text = re.sub(r"\D", "", await option.inner_text())
            if any(item and int(item) == number for item in (value, text)):
                matches.append(option)
        if len(matches) != 1:
            return False
        await locator.select_option(value=(await matches[0].get_attribute("value")) or "")
        return True

    async def _verified(self, target: FieldTarget, action: WidgetAction, expected: str | None) -> bool:
        assert action.value is not None
        if action.action == "select_parts":
            assert isinstance(action.value, dict)
            for part, wanted in action.value.items():
                actual = await target.frame.locator(target.part_selector(part)).input_value()
                if str(actual) != str(wanted):
                    return False
            return True
        locator = target.frame.locator(target.selector)
        if action.action == "check_radio":
            assert isinstance(action.value, str)
            matches = await self._radio_matches(locator, action.value)
            return len(matches) == 1 and await matches[0].is_checked()
        first = locator.first
        if action.action == "choose_aria_radio":
            for radio in await first.locator('[role="radio"][aria-checked="true"]').all():
                if _plain(await radio.inner_text()) == _plain(str(expected)):
                    return True
            return False
        if action.action == "choose_custom_option" or await first.evaluate("element => element.tagName") == "BUTTON":
            actual = await first.inner_text()
        else:
            try:
                actual = await first.input_value()
            except PlaywrightError:
                actual = (await first.get_attribute("data-value")) or await first.inner_text()
        if action.action == "adapter_set_date":
            if not _same_date(actual, str(action.value)):
                return False
        elif action.action in {"choose_custom_option", "choose_suggestion"}:
            if not actual.strip() or _plain(actual) != _plain(str(expected)):
                return False
        elif not _equivalent(actual, str(expected), action.resume_path):
            return False
        try:
            return bool(await first.evaluate("element => typeof element.checkValidity !== 'function' || element.checkValidity()"))
        except PlaywrightError:
            return False
