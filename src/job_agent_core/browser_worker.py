from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
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
from .form_inspector import ControlTarget, FieldTarget, PageInspection, inspect_page
from .mapping import FormField
from .models import ResumeProfile
from .security import domain_matches, is_safe_next_control, is_submit_like_control
from .widgets import WidgetAction
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
    ]
    reason: str
    attempts: int = 0


class BrowserFillReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    browser: BrowserStatus
    results: list[BrowserFillResult]
    filled_count: int
    kept_existing_count: int
    needs_human_count: int
    failed_count: int
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
        fields = {item.key: item for item in prepared.inspection.fields}
        targets = prepared.inspection.targets
        widget_actions = {item.form_field: item for item in prepared.plan.widget_actions}
        results: list[BrowserFillResult] = []
        for action in prepared.plan.fill_actions:
            field = fields[action.form_field]
            base = {
                "form_field": action.form_field,
                "label": field.label,
                "resume_path": action.resume_path,
            }
            if action.status == "kept_existing":
                results.append(
                    BrowserFillResult(
                        **base,
                        status="kept_existing",
                        reason="페이지에 이미 입력된 값을 유지했습니다.",
                    )
                )
                continue
            if action.status in {"missing_required", "missing_optional"}:
                results.append(
                    BrowserFillResult(
                        **base,
                        status="missing_resume_value",
                        reason="이력서에서 값을 찾지 못해 입력하지 않았습니다.",
                    )
                )
                continue
            if action.status != "planned":
                results.append(BrowserFillResult(**base, status="needs_human", reason=action.reason))
                continue
            widget = widget_actions.get(action.form_field)
            target = targets.get(action.form_field)
            if target is None:
                results.append(
                    BrowserFillResult(**base, status="field_not_found", reason="DOM에서 필드를 다시 찾지 못했습니다.")
                )
                continue
            if widget is None or widget.status != "planned" or widget.action is None:
                results.append(
                    BrowserFillResult(
                        **base,
                        status="needs_human",
                        reason=widget.reason if widget else "안전한 입력 방법을 결정하지 못했습니다.",
                    )
                )
                continue
            results.append(await self._execute(field, target, widget))

        try:
            refreshed = await self._prepare(task_id, profile)
        except BrowserSessionError:
            refreshed = prepared
        filled_count = sum(item.status == "filled_verified" for item in results)
        kept_count = sum(item.status == "kept_existing" for item in results)
        failed_count = sum(item.status in {"failed_validation", "field_not_found"} for item in results)
        manual_count = len(results) - filled_count - kept_count - failed_count
        return BrowserFillReport(
            browser=await self.sessions.status(task_id),
            results=results,
            filled_count=filled_count,
            kept_existing_count=kept_count,
            needs_human_count=manual_count,
            failed_count=failed_count,
            safe_next_available=len(refreshed.safe_next) == 1,
            submit_control_count=len(refreshed.submit_controls),
        )

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
    ) -> BrowserFillResult:
        base = {
            "form_field": field.key,
            "label": field.label,
            "resume_path": action.resume_path,
        }
        try:
            if await self._has_existing(target):
                return BrowserFillResult(
                    **base,
                    status="kept_existing",
                    reason="실행 직전에 기존 값이 확인되어 유지했습니다.",
                )
        except PlaywrightError:
            return BrowserFillResult(**base, status="field_not_found", reason="DOM에서 필드를 다시 찾지 못했습니다.")

        max_attempts = 2 if action.action in {"fill", "select_option", "check_radio", "select_parts"} else 1
        last_reason = "브라우저 재확인 결과가 입력 예정 값과 일치하지 않습니다."
        attempts = 0
        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            try:
                outcome = await self._execute_once(target, action)
                if outcome:
                    return BrowserFillResult(
                        **base,
                        status="needs_human",
                        reason=outcome,
                        attempts=attempt,
                    )
                if await self._verified(target, action):
                    return BrowserFillResult(
                        **base,
                        status="filled_verified",
                        reason="입력 후 브라우저에서 값을 다시 확인했습니다.",
                        attempts=attempt,
                    )
            except PlaywrightError:
                return BrowserFillResult(
                    **base,
                    status="needs_human",
                    reason="입력 중 페이지 구조가 바뀌어 안전하게 계속할 수 없습니다.",
                    attempts=attempt,
                )
        return BrowserFillResult(
            **base,
            status="failed_validation",
            reason=last_reason,
            attempts=attempts,
        )

    async def _has_existing(self, target: FieldTarget) -> bool:
        if target.widget_kind == "split_date_select":
            for part in target.part_keys:
                if not (await target.frame.locator(target.part_selector(part)).input_value()).strip():
                    return False
            return True
        locator = target.frame.locator(target.selector)
        if target.widget_kind == "radio_group":
            return await locator.locator(":checked").count() > 0
        if await locator.count() != 1:
            raise PlaywrightError("field count changed")
        if target.widget_kind == "custom_select":
            text = (await locator.inner_text()).strip()
            return bool(text and not re.fullmatch(r"선택|선택하세요|미선택|choose|select", text, re.I))
        try:
            return bool((await locator.input_value()).strip())
        except PlaywrightError:
            return bool((await locator.get_attribute("data-value") or "").strip())

    async def _execute_once(self, target: FieldTarget, action: WidgetAction) -> str | None:
        locator = target.frame.locator(target.selector)
        if action.action == "select_parts":
            assert isinstance(action.value, dict)
            for part, value in action.value.items():
                part_locator = target.frame.locator(target.part_selector(part))
                if await part_locator.count() != 1 or not await part_locator.is_enabled():
                    return "날짜 선택 항목의 상태가 바뀌어 사용자가 직접 확인해야 합니다."
                await part_locator.select_option(value=value)
            return None
        if await locator.count() < 1:
            return "DOM에서 필드를 다시 찾지 못했습니다."
        first = locator.first
        if not await first.is_visible() or not await first.is_enabled():
            return "보이지 않거나 비활성화된 필드는 자동 입력하지 않습니다."
        if action.action == "fill":
            assert isinstance(action.value, str)
            await first.fill(action.value)
            return None
        if action.action == "select_option":
            assert isinstance(action.value, str)
            await first.select_option(value=action.value)
            return None
        if action.action == "check_radio":
            assert isinstance(action.value, str)
            matches = [item for item in await locator.all() if (await item.get_attribute("value")) == action.value]
            if len(matches) != 1:
                return "일치하는 라디오 선택지를 하나로 확정할 수 없습니다."
            await matches[0].check()
            return None
        if action.action in {"choose_suggestion", "choose_custom_option"}:
            assert isinstance(action.value, str)
            await first.click()
            if action.action == "choose_suggestion":
                await first.fill(action.value)
            await asyncio.sleep(0.25)
            options = target.frame.locator(
                '[role="option"]:visible, [role="listbox"] button:visible, [role="listbox"] li:visible'
            )
            matches: list[Locator] = []
            for option in await options.all():
                if _normalized(await option.inner_text(), action.resume_path) == _normalized(action.value, action.resume_path):
                    matches.append(option)
            if len(matches) != 1:
                return "일치하는 자동완성 또는 사용자 정의 선택 항목을 하나로 확정할 수 없습니다."
            await matches[0].click()
            return None
        if action.action == "adapter_set_date":
            assert isinstance(action.value, str)
            return await self._set_custom_date(target, first, action.value)
        return "지원하지 않는 입력 동작입니다."

    async def _set_custom_date(self, target: FieldTarget, locator: Locator, iso_value: str) -> str | None:
        year, month, day = iso_value.split("-")
        try:
            await locator.click()
        except PlaywrightError:
            pass
        frame = target.frame
        dialogs = frame.locator('[role="dialog"]:visible')
        if await dialogs.count() == 0:
            sibling = locator.locator("xpath=following-sibling::button[1]")
            if await sibling.count() == 1:
                await sibling.click()
                await asyncio.sleep(0.1)
        if await dialogs.count() != 1:
            return "안전하게 인식할 수 있는 달력 대화상자를 하나로 확정하지 못했습니다."
        dialog = dialogs.first
        year_select = dialog.locator('select[aria-label*="연도"], select[aria-label*="년"]')
        month_select = dialog.locator('select[aria-label*="월"]')
        if await year_select.count() != 1 or await month_select.count() != 1:
            return "달력의 연도와 월 선택 항목을 확실히 찾지 못했습니다."
        if not await self._select_number(year_select, int(year)) or not await self._select_number(month_select, int(month)):
            return "달력에서 이력서 날짜와 일치하는 연도 또는 월을 찾지 못했습니다."
        days = dialog.locator('[role="gridcell"]:visible, button[data-day]:visible')
        matches: list[Locator] = []
        for candidate in await days.all():
            if re.sub(r"\D", "", await candidate.inner_text()) == str(int(day)):
                matches.append(candidate)
        if len(matches) != 1:
            return "달력에서 날짜를 하나로 확정할 수 없습니다."
        await matches[0].click()
        return None

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

    async def _verified(self, target: FieldTarget, action: WidgetAction) -> bool:
        assert action.value is not None
        if action.action == "select_parts":
            assert isinstance(action.value, dict)
            for part, expected in action.value.items():
                actual = await target.frame.locator(target.part_selector(part)).input_value()
                if str(actual) != str(expected):
                    return False
            return True
        locator = target.frame.locator(target.selector)
        if action.action == "check_radio":
            assert isinstance(action.value, str)
            for radio in await locator.all():
                if await radio.is_checked() and (await radio.get_attribute("value")) == action.value:
                    return True
            return False
        first = locator.first
        if action.action == "choose_custom_option":
            actual = await first.inner_text()
        else:
            try:
                actual = await first.input_value()
            except PlaywrightError:
                actual = (await first.get_attribute("data-value")) or await first.inner_text()
        expected = str(action.value)
        if not _equivalent(actual, expected, action.resume_path):
            return False
        try:
            return bool(await first.evaluate("element => typeof element.checkValidity !== 'function' || element.checkValidity()"))
        except PlaywrightError:
            return False
