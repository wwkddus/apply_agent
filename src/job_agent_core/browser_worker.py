# 실제 입력 실행 담당(Agent의 손). 
# 화면 분석 → 계획 → 칸별 입력 → 다시 읽기 확인을 브라우저에서 수행합니다.
# 원칙: 기존 값은 덮어쓰지 않고, 
#      확인되지 않은 값은 남기지 않으며, 
#      제출·저장 버튼은 절대 누르지 않습니다.

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


# 화면 분석 결과의 칸별 판단(입력 예정/확인 필요 등) — 입력 전 미리보기용.
class BrowserFieldDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    form_field: str
    label: str
    widget_kind: str
    resume_path: str | None = None
    status: str
    reason: str


# 화면 분석 보고서: 칸 수, 칸별 판단, 안전한 '다음' 버튼 여부, 제출 버튼 수.
class BrowserInspectionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    browser: BrowserStatus
    field_count: int
    skipped_field_count: int
    decisions: list[BrowserFieldDecision]
    safe_next_available: bool
    submit_control_count: int
    unapproved_frame_hosts: list[str] = Field(default_factory=list)


# 자동 입력 진행 상황(화면의 진행 막대용). 칸 이름과 상태만 담고 입력한 값은 담지 않습니다.
class FillProgressItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    status: str


class FillProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    running: bool = False
    current: str = ""
    done: int = 0
    total: int = 0
    recent: list[FillProgressItem] = Field(default_factory=list)


# 칸 하나의 입력 결과. 감지된 모든 칸이 반드시 한 줄씩 남아 '조용히 사라지는 칸'이 없게 합니다.
# 주요 상태: 입력·재확인 완료 / 기존 값 유지 / 사람 확인 / 사용자 전용 / AI가 넣음(확인 필요).
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


# 입력 전체 보고서: 결과 목록과 상태별 개수, 사용자 안내 문구.
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


# 입력 준비 결과: 화면 분석 + 입력 계획 + 안전한 '다음' 버튼 + 제출 성격 버튼.
@dataclass(slots=True)
class PreparedPage:
    inspection: PageInspection
    plan: WorkerPlan
    safe_next: list[ControlTarget]
    submit_controls: list[ControlTarget]


# 자동으로 눌러도 되는 '다음' 버튼인지: type=button이고 제출 성격이 전혀 없어야 합니다.
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


# 제출 성격의 버튼인지(절대 누르지 않는 버튼).
def _submit_like(control: ControlTarget) -> bool:
    return is_submit_like_control(
        tag=control.tag,
        attr_type=control.attr_type,
        form_type=control.form_type,
        text=control.text,
    )


# 다시 읽은 값 비교용 정규화(전화는 숫자만, 이메일은 소문자 등).
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


# 화면 값과 계획 값이 같은지(형식 차이는 무시).
def _equivalent(actual: str, expected: str, path: str) -> bool:
    return bool(actual) and _normalized(actual, path) == _normalized(expected, path)


# 공백 제거 + 소문자.
def _plain(value: str) -> str:
    return re.sub(r"\s+", "", value or "").casefold()


# 날짜 비교. 화면이 연-월만 보여 줘도 같은 날짜로 인정합니다.
def _same_date(actual: str, iso_value: str) -> bool:
    """'2020. 03' matches 2020-03-01 when the widget shows only year and month."""
    digits = re.sub(r"\D", "", actual or "")
    expected = re.sub(r"\D", "", iso_value)
    return digits == expected or (len(digits) == 6 and expected.startswith(digits))


# 입력 1회 시도 결과: 오류 메시지, 기대 값, 후보 선택지, 비활성 여부.
@dataclass(slots=True)
class _Outcome:
    error: str | None = None
    expected: str | None = None
    candidates: list[str] = field(default_factory=list)
    disabled: bool = False


# 비활성 칸 안내 문구, 후보 최대 개수, '선택하세요' 같은 자리표시 선택지 패턴.
DISABLED_REASON = "페이지에서 비활성화된 항목입니다. 다른 선택에 따라 입력할 수 없는 상태일 수 있습니다 (예: 재학 중이면 졸업일 없음)."
MAX_OPTION_CANDIDATES = 30
_PLACEHOLDER_OPTION_RE = re.compile(r"(선택|선택하세요|선택해\s*주세요|미선택|choose|select)(\s*\.{0,3})", re.I)


# 결과 한 줄을 만드는 도우미(페이지 선택지를 후보로 함께 담음).
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


# 규칙이 다루지 않은 칸도 빠짐없이 기록: 사용자 전용 / 기존 값 유지 / AI 2차 확인 대기.
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


# LLM이 찾은 값을 기존 입력 규칙(날짜·검색 콤보 등)으로 동작으로 바꿉니다.
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


# 페이지에서 읽은 선택지 문구(자리표시 제외). 확인 창에서 사용자에게 후보로 보여 줍니다.
def _page_options(field: FormField) -> list[str]:
    """Option texts already detected on the page, offered to the user in the review dialog."""
    texts = [" ".join(option.text.split()) for option in field.options]
    texts = [text for text in texts if text and not _PLACEHOLDER_OPTION_RE.fullmatch(text)]
    return list(dict.fromkeys(texts))[:MAX_OPTION_CANDIDATES]


# Never answered by the second check: 상세주소/우편번호 without a confirmed 기본주소 would be filled
# alone, and the model cannot tell which word of a romanized name is the surname.
# AI 2차 확인에서 제외: 주소는 주소 검색으로만, 영문 성/이름은 어순을 알 수 없어 LLM에 맡기지 않습니다.
_NO_LOOKUP_PATHS = {
    "person.address",
    "person.postal_code",
    "person.address_detail",
    "person.name_en.family",
    "person.name_en.given",
}


# 학력 '추가' 버튼 문구 패턴(대학/대학원). 고등학교 블록은 열지 않습니다.
_GRADUATE_WORD_RE = re.compile(r"대학원|post\s*graduate|graduate\s*school", re.I)
# (level, add-button wording, name shown to the user). High school blocks are never opened.
_ADD_SECTION_BUTTONS = (
    ("university", re.compile(r"(대학|university|college|학사).{0,20}(추가|add)|add.{0,20}(university|college)", re.I), "대학 학력"),
    ("graduate_school", re.compile(r"(대학원|post\s*graduate|graduate\s*school).{0,20}(추가|add)|add.{0,20}(post\s*graduate|graduate)", re.I), "대학원 학력"),
)


# 주소 칸이 읽기 전용이면 주소·우편번호·상세주소를 묶어서 주소 검색 흐름으로 처리합니다.
def _address_group(actions: list[FillAction], profile: ResumeProfile) -> dict[str, FillAction]:
    """주소/우편번호/상세주소 actions handled together when the page's 주소 field is read-only."""
    by_path = {action.resume_path: action for action in actions}
    address = by_path.get("person.address")
    if address is None or address.status != "readonly" or not get_resume_value(profile, "person.address"):
        return {}
    return {path: action for path, action in by_path.items()
            if path in {"person.address", "person.postal_code", "person.address_detail"}}


# 브라우저 입력 작업자. 세션 관리자와 계획 작업자를 주입받습니다(테스트에서 가짜로 교체 가능).
class BrowserAutomationWorker:
    def __init__(
        self,
        sessions: BrowserSessionManager,
        planner: KoreanPlanningWorker | None = None,
    ):
        self.sessions = sessions
        self.planner = planner or KoreanPlanningWorker()
        self.progress: dict[str, FillProgress] = {}

    # 브라우저 열기/상태/도메인 승인/닫기는 세션 관리자에게 그대로 맡깁니다.
    async def open(self, task_id: str, raw_url: str, target_hostname: str) -> BrowserStatus:
        return await self.sessions.open(task_id, raw_url, target_hostname)

    async def status(self, task_id: str) -> BrowserStatus:
        return await self.sessions.status(task_id)

    async def approve_domain(self, task_id: str, hostname: str) -> BrowserStatus:
        return await self.sessions.approve_domain(task_id, hostname)

    async def close(self, task_id: str) -> None:
        self.progress.pop(task_id, None)
        await self.sessions.close(task_id)

    # 진행 중인 자동 입력의 현재 칸·완료 수.
    def fill_progress(self, task_id: str) -> FillProgress:
        return self.progress.get(task_id, FillProgress())

    def _progress_step(self, task_id: str, current: str = "", done: list[BrowserFillResult] | None = None) -> None:
        progress = self.progress.get(task_id)
        if progress is None:
            return
        if current:
            progress.current = current
        for item in done or []:
            progress.done += 1
            progress.recent = [*progress.recent, FillProgressItem(label=item.label or "라벨 없음", status=item.status)][-5:]

    async def close_all(self) -> None:
        await self.sessions.close_all()

    # 입력 준비: 다른 도메인 차단 확인 → 화면 분석 → 로그인 화면이면 중단(로그인은 사용자가)
    # → 입력 계획 → 안전한 '다음'/제출 버튼 분류.
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

    # 화면 분석만 하고 칸별 판단을 보고합니다(입력은 하지 않음).
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

    # 원클릭 자동 입력 전체 흐름:
    # ① 준비 → ② 학력 '추가' 버튼이 필요하면 열고 다시 준비 → ③ 칸별 입력(주소 묶음은 나중에)
    # → ④ 주소 검색 → ⑤ 남은 칸 기록 → ⑥ 다시 분석해서, 비활성이던 칸이 풀렸으면 재시도 → ⑦ 보고서.
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
        self.progress[task_id] = FillProgress(running=True, total=len(prepared.plan.fill_actions))
        try:
            for action in prepared.plan.fill_actions:
                if action.form_field not in deferred:
                    self._progress_step(task_id, current=fields[action.form_field].label or "라벨 없음")
                    results.append(await self._fill_action(action, fields, targets, widget_actions))
                    self._progress_step(task_id, done=results[-1:])
            if address_group:
                self._progress_step(task_id, current="주소")
                address_results = await self._fill_address(task_id, profile, address_group, fields, targets, widget_actions)
                results.extend(address_results)
                self._progress_step(task_id, done=address_results)
        finally:
            if task_id in self.progress:
                self.progress[task_id].running = False

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

    # 이력서에 대학/대학원이 있는데 페이지에 그 칸이 없으면 '추가' 버튼을 누릅니다.
    # 빈 블록만 추가될 뿐 저장되지 않으며, 저장·제출 버튼은 누르지 않습니다.
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

    # 계획 상태에 따라 결과 결정: 기존 값/값 없음/사람 확인이면 기록만, 입력 예정이면 실제 입력(_execute).
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

    # 읽기 전용 주소 처리: 우편번호 창으로 검색·선택 → 주소 칸을 다시 읽어 확인 → 우편번호 확인
    # → 기본주소가 확인된 뒤에만 상세주소 입력. 실패하면 진단(단계만, 값 없음)을 남깁니다.
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

        # 주소 묶음용 결과 한 줄 도우미.
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

    # 칸 값을 다시 읽습니다. 주소 검색 창은 값을 비동기로 넣으므로 잠시 기다리며 확인합니다.
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

    # AI 2차 확인 흐름: 대기 중인 칸을 다시 찾기 → 사용자 전용·기존 값·주소 등 제외 → 선택지 읽기
    # → LLM 조회(검증된 값만) → 입력 후 '확인 필요'로 표시. LLM이 꺼져 있거나 실패해도 안내만 하고 계속합니다.
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

    # 닫혀 있는 커스텀 선택 목록을 한 번 열어 선택지만 읽고, 아무것도 고르지 않고 닫습니다.
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

    # LLM 값 입력. 기존 값은 덮어쓰지 않고, 성공해도 상태를 'AI가 넣음(확인 필요)'으로 바꿉니다.
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

    # 처음엔 비활성이었던 칸(예: 졸업 구분 선택 후 열리는 졸업일)을 다시 시도합니다.
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

    # 사용자가 확인 창에서 고른 값으로 칸 하나 입력. 칸은 fingerprint로 다시 찾고,
    # 라벨이 바뀌었으면 다른 칸일 수 있으므로 중단합니다. 값은 같은 형식 검사를 거칩니다.
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

    # '다음' 페이지 이동. 안전한 '다음' 버튼이 정확히 하나일 때만, 상태를 다시 확인하고 누릅니다.
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

    # 칸 하나 실제 입력 + 확인. 
    # 흐름: 직전에 기존 값 확인(있으면 유지) → 입력 → 다시 읽기 확인.
    # 실패하면 대체 형식으로 1번만 재시도하고, 그래도 안 되면 남은 잘못된 값을 지웁니다.
    # 병역·장애·보훈은 성공해도 '확인 필요'로 남깁니다.
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

    # 확인 실패한 텍스트·선택 값을 지워, 검증 안 된 값이 페이지에 남지 않게 합니다.
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

    # 칸에 이미 값이 있는지(라디오 선택됨, 선택 목록 문구 등 위젯별로 확인). 있으면 덮어쓰지 않습니다.
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

    # 동작 종류별 실제 브라우저 조작 1회: 텍스트 채우기 / 선택 / 라디오(숨겨진 라디오는 label 클릭)
    # / 버튼형 라디오 / 검색·커스텀 목록에서 하나 고르기 / 커스텀 달력. 보이지 않거나 비활성인 칸은 건드리지 않습니다.
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

    # 값으로 라디오 찾기. value가 구분되지 않는 페이지는 순서('#1')로 찾습니다.
    @staticmethod
    async def _radio_matches(locator: Locator, value: str) -> list[Locator]:
        """Radios by value, or by position ('#1') when the page gives them no distinct values."""
        radios = await locator.all()
        if re.fullmatch(r"#\d+", value):
            index = int(value[1:])
            return [radios[index]] if index < len(radios) else []
        return [item for item in radios if (await item.get_attribute("value")) == value]

    # 비동기로 뜨는 선택지 목록을 개수가 안정될 때까지 기다립니다.
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

    # 고르지 못했을 때 입력한 검색어를 지우고 목록을 닫습니다.
    @staticmethod
    async def _dismiss(target: FieldTarget, locator: Locator, *, typed: bool) -> None:
        try:
            if typed:
                await locator.fill("")
            await locator.press("Escape")
        except PlaywrightError:
            pass

    # 커스텀 달력 입력: 달력 안 입력칸이 있으면 예시 형식대로 입력,
    # 없으면 달력 창에서 연·월 선택 후 날짜 칸이 하나일 때만 클릭합니다.
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

    # 숫자가 같은 선택지가 하나일 때만 선택(연도·월).
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

    # 입력 후 다시 읽어 확인: 동작 종류별로 화면 값을 읽어 비교하고, 브라우저 자체 검증(checkValidity)도 통과해야 합니다.
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
