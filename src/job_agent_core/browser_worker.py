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
from .form_inspector import ControlTarget, FieldTarget, PageInspection, inspect_page
from .mapping import FormField
from .models import ResumeProfile
from .security import domain_matches, is_safe_next_control, is_submit_like_control
from .models import clean_field
from .planning import field_name_for_path
from .constants import ALLOWED_RESUME_PATHS
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
    ]
    reason: str
    attempts: int = 0
    widget_kind: str = ""
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
                "widget_kind": field.widget_kind,
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
            field_count=len(prepared.inspection.fields),
            filled_count=filled_count,
            kept_existing_count=kept_count,
            needs_human_count=manual_count,
            failed_count=failed_count,
            safe_next_available=len(refreshed.safe_next) == 1,
            submit_control_count=len(refreshed.submit_controls),
        )

    async def fill_field(
        self,
        task_id: str,
        profile: ResumeProfile,
        form_field: str,
        label: str,
        value: str,
    ) -> BrowserFillResult:
        """Fill one field with a value the user chose in the review dialog, then read it back."""
        prepared = await self._prepare(task_id, profile)
        field = next((item for item in prepared.inspection.fields if item.key == form_field), None)
        if field is None or field.label != label:
            raise BrowserSessionError("페이지가 바뀌어 이 항목을 다시 찾지 못했습니다. '이 페이지 자동 입력'을 다시 실행해 주세요.")
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
                status="needs_human",
                reason=action.reason,
            )
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
                if outcome.error:
                    return BrowserFillResult(
                        **base,
                        status="needs_human",
                        reason=outcome.error,
                        attempts=attempt,
                        candidates=outcome.candidates,
                    )
                if await self._verified(target, candidate, outcome.expected):
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
        if not await first.is_visible() or not await first.is_enabled():
            return _Outcome("보이지 않거나 비활성화된 필드는 자동 입력하지 않습니다.")
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
            matches = [item for item in await locator.all() if (await item.get_attribute("value")) == action.value]
            if len(matches) != 1:
                return _Outcome("일치하는 라디오 선택지를 하나로 확정할 수 없습니다.")
            await matches[0].check()
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
                await first.fill(action.value)
            options = await self._wait_for_options(target)
            texts = [(await option.inner_text()).strip() for option in options]
            index, candidates = pick_option(texts, action.value, action.resume_path)
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
            for radio in await locator.all():
                if await radio.is_checked() and (await radio.get_attribute("value")) == action.value:
                    return True
            return False
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
