from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .browser_session import (
    BrowserLoginNotComplete,
    BrowserRedirectApprovalRequired,
    BrowserSessionError,
    BrowserSessionManager,
    BrowserSessionNotFound,
    BrowserStatus,
)
from .browser_worker import BrowserAutomationWorker, BrowserFillReport, BrowserFillResult, BrowserInspectionReport
from .constants import MAX_RESUME_BYTES
from .korean_scope import enforce_korean_mvp_profile, korean_mvp_values
from .mapping import FormField
from .names import confirm_name_parts, name_components_usable, normalized_name
from .planning import set_resume_value
from .resume_llm import LlmUnavailable, merge_llm_values, ollama_settings, proposed_values, request_ollama, should_use_llm
from .resume_parser import ResumeParseError, extract_resume_profile, inspect_resume_bytes
from .security import URLPolicyError, validate_target_url
from .tasks import (
    InvalidTaskTransition,
    SensitiveTaskData,
    SensitiveTaskVault,
    SqliteTaskStore,
    TaskNotFound,
    TaskRecord,
)
from .worker import KoreanPlanningWorker, WorkerPlan


class ApiSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    database_path: Path
    api_token: str = Field(min_length=24)
    browser_profile_root: Path | None = None
    browser_headless: bool = False
    # Local-LLM resume extraction: "off", "auto" (only for sparse rule results) or "always".
    llm_mode: str = "off"
    ollama_transport: object | None = None
    allowed_hosts: frozenset[str] = frozenset({"127.0.0.1", "localhost", "[::1]"})
    allowed_origins: frozenset[str] = frozenset(
        {"http://127.0.0.1", "http://localhost", "https://127.0.0.1", "https://localhost"}
    )


class CreateTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_url: str = Field(min_length=1, max_length=2048)


class ResumePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: dict[str, str]
    name_confirmation_required: bool
    warnings: list[str]
    missing: list[str]
    # path -> "rule" | "llm"; LLM values are shown to the user for confirmation.
    sources: dict[str, str] = Field(default_factory=dict)


class ConfirmNameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    family: str = Field(min_length=1, max_length=60)
    given: str = Field(min_length=1, max_length=80)


class EditResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=80)
    value: str = Field(max_length=500)


class FillFieldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    form_field: str = Field(min_length=1, max_length=80)
    label: str = Field(max_length=300)
    value: str = Field(min_length=1, max_length=500)


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: list[FormField] = Field(max_length=300)


class CancelResponse(BaseModel):
    task_id: str
    phase: str
    sensitive_data_cleared: bool


class ApproveDomainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hostname: str = Field(min_length=1, max_length=253)


def default_settings() -> ApiSettings:
    data_dir = Path(os.environ.get("JOB_AGENT_DATA_DIR", ".job_agent"))
    token = os.environ.get("JOB_AGENT_API_TOKEN")
    if not token:
        raise RuntimeError("로컬 API를 시작하기 전에 JOB_AGENT_API_TOKEN을 설정해야 합니다.")
    return ApiSettings(
        database_path=data_dir / "task_state.sqlite3",
        browser_profile_root=data_dir / "browser_profiles",
        api_token=token,
        llm_mode=os.environ.get("JOB_AGENT_LLM", "auto"),
    )


def create_app(
    settings: ApiSettings | None = None,
    browser_worker: BrowserAutomationWorker | None = None,
) -> FastAPI:
    settings = settings or default_settings()
    store = SqliteTaskStore(settings.database_path)
    vault = SensitiveTaskVault()
    worker = KoreanPlanningWorker()
    profile_root = settings.browser_profile_root or settings.database_path.parent / "browser_profiles"
    browser_worker = browser_worker or BrowserAutomationWorker(
        BrowserSessionManager(profile_root, headless=settings.browser_headless),
        worker,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            await browser_worker.close_all()
            vault.clear_all()
            store.close()

    app = FastAPI(
        title="Korean Job Application Agent",
        version="0.3.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.vault = vault
    app.state.worker = worker
    app.state.browser_worker = browser_worker

    @app.middleware("http")
    async def local_request_boundary(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":", 1)[0].casefold()
        if host not in settings.allowed_hosts:
            return JSONResponse(status_code=400, content={"detail": "허용되지 않은 호스트입니다."})
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            normalized = f"{parsed.scheme}://{parsed.hostname}"
            if normalized not in settings.allowed_origins:
                return JSONResponse(status_code=403, content={"detail": "허용되지 않은 요청 출처입니다."})
        return await call_next(request)

    def authorize(x_job_agent_token: str | None = Header(default=None)) -> None:
        if not x_job_agent_token or not secrets.compare_digest(x_job_agent_token, settings.api_token):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="로컬 API 토큰이 올바르지 않습니다.")

    def get_task(task_id: str) -> TaskRecord:
        try:
            return store.get(task_id)
        except TaskNotFound:
            raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.") from None

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "scope": "ko-KR-mvp"}

    @app.post("/api/tasks", response_model=TaskRecord, dependencies=[Depends(authorize)])
    async def create_task(payload: CreateTaskRequest) -> TaskRecord:
        try:
            target = validate_target_url(payload.target_url)
        except URLPolicyError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        task = store.create(target.hostname)
        vault.put(task.task_id, SensitiveTaskData(raw_url=target.raw))
        return task

    @app.get("/api/tasks/{task_id}", response_model=TaskRecord, dependencies=[Depends(authorize)])
    async def read_task(task_id: str) -> TaskRecord:
        return get_task(task_id)

    @app.post(
        "/api/tasks/{task_id}/resume",
        response_model=ResumePreview,
        dependencies=[Depends(authorize)],
    )
    async def upload_resume(task_id: str, file: UploadFile = File(...)) -> ResumePreview:
        get_task(task_id)
        suffix = Path(file.filename or "").suffix.casefold()
        if suffix != ".pdf":
            raise HTTPException(status_code=415, detail="PDF 이력서만 업로드할 수 있습니다.")
        data = await file.read(MAX_RESUME_BYTES + 1)
        await file.close()
        if len(data) > MAX_RESUME_BYTES:
            raise HTTPException(status_code=413, detail="이력서는 10MB를 초과할 수 없습니다.")
        try:
            inspected = inspect_resume_bytes(data)
            parsed = extract_resume_profile(str(inspected["text"]))
            profile, scope_warnings = enforce_korean_mvp_profile(parsed["resume_profile"])
        except ResumeParseError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        sources = {path: "rule" for path, value in korean_mvp_values(profile).items() if value}
        if should_use_llm(profile, settings.llm_mode):
            url, model = ollama_settings()
            try:
                fields = await request_ollama(
                    str(inspected["text"]), url=url, model=model, transport=settings.ollama_transport
                )
                profile, llm_sources, llm_warnings = merge_llm_values(profile, proposed_values(fields), str(inspected["text"]))
                profile, more_scope_warnings = enforce_korean_mvp_profile(profile)
                sources.update({path: source for path, source in llm_sources.items() if korean_mvp_values(profile).get(path)})
                scope_warnings = [*scope_warnings, *llm_warnings, *more_scope_warnings]
            except LlmUnavailable as error:
                scope_warnings = [*scope_warnings, str(error)]
        secret = vault.get(task_id)
        vault.put(task_id, secret.model_copy(update={"profile": profile}))
        values = korean_mvp_values(profile)
        warnings = [*parsed["warnings"], *scope_warnings]
        available = [path for path, value in values.items() if value]
        try:
            store.transition(
                task_id,
                "resume_ready",
                available_fields=available,
                warning_count=len(warnings),
            )
        except InvalidTaskTransition as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        return ResumePreview(
            values=values,
            name_confirmation_required=bool(
                profile.personal.name.family
                and profile.personal.name.given
                and not name_components_usable(profile.personal.name)
            ),
            warnings=warnings,
            missing=[path for path, value in values.items() if not value],
            sources=sources,
        )

    @app.post(
        "/api/tasks/{task_id}/confirm-name",
        response_model=ResumePreview,
        dependencies=[Depends(authorize)],
    )
    async def confirm_name(task_id: str, payload: ConfirmNameRequest) -> ResumePreview:
        get_task(task_id)
        secret = vault.get(task_id)
        if secret.profile is None:
            raise HTTPException(status_code=409, detail="이력서 분석이 아직 완료되지 않았습니다.")
        combined = payload.family + payload.given
        if normalized_name(combined) != normalized_name(secret.profile.personal.name.full):
            raise HTTPException(status_code=422, detail="성과 이름을 합친 값이 전체 이름과 일치하지 않습니다.")
        try:
            profile = confirm_name_parts(secret.profile, family=payload.family, given=payload.given)
        except ValueError:
            raise HTTPException(status_code=422, detail="성과 이름 형식이 올바르지 않습니다.") from None
        vault.put(task_id, secret.model_copy(update={"profile": profile}))
        values = korean_mvp_values(profile)
        return ResumePreview(
            values=values,
            name_confirmation_required=False,
            warnings=[],
            missing=[path for path, value in values.items() if not value],
        )

    @app.patch(
        "/api/tasks/{task_id}/resume",
        response_model=ResumePreview,
        dependencies=[Depends(authorize)],
    )
    async def edit_resume(task_id: str, payload: EditResumeRequest) -> ResumePreview:
        get_task(task_id)
        secret = vault.get(task_id)
        if secret.profile is None:
            raise HTTPException(status_code=409, detail="이력서 분석이 아직 완료되지 않았습니다.")
        try:
            profile = set_resume_value(secret.profile, payload.path, payload.value)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=f"값을 저장할 수 없습니다: {error}") from None
        vault.put(task_id, secret.model_copy(update={"profile": profile}))
        values = korean_mvp_values(profile)
        return ResumePreview(
            values=values,
            name_confirmation_required=bool(
                profile.personal.name.family
                and profile.personal.name.given
                and not name_components_usable(profile.personal.name)
            ),
            warnings=[],
            missing=[path for path, value in values.items() if not value],
        )

    @app.post(
        "/api/tasks/{task_id}/plan",
        response_model=WorkerPlan,
        dependencies=[Depends(authorize)],
    )
    async def plan_task(task_id: str, payload: PlanRequest) -> WorkerPlan:
        get_task(task_id)
        secret = vault.get(task_id)
        if secret.profile is None:
            raise HTTPException(status_code=409, detail="이력서 분석이 아직 완료되지 않았습니다.")
        plan = await worker.plan(secret.profile, payload.fields)
        try:
            store.transition(task_id, "planned")
        except InvalidTaskTransition as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        return plan

    @app.post(
        "/api/tasks/{task_id}/cancel",
        response_model=CancelResponse,
        dependencies=[Depends(authorize)],
    )
    async def cancel_task(task_id: str) -> CancelResponse:
        get_task(task_id)
        await browser_worker.close(task_id)
        try:
            task = store.transition(task_id, "cancelled")
        except InvalidTaskTransition as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        vault.clear(task_id)
        return CancelResponse(task_id=task.task_id, phase=task.phase, sensitive_data_cleared=True)

    def browser_error(error: BrowserSessionError) -> HTTPException:
        if isinstance(error, BrowserSessionNotFound):
            return HTTPException(status_code=404, detail=str(error))
        if isinstance(error, (BrowserRedirectApprovalRequired, BrowserLoginNotComplete)):
            return HTTPException(status_code=409, detail=str(error))
        return HTTPException(status_code=422, detail=str(error))

    @app.post(
        "/api/tasks/{task_id}/browser/open",
        response_model=BrowserStatus,
        dependencies=[Depends(authorize)],
    )
    async def open_browser(task_id: str) -> BrowserStatus:
        task = get_task(task_id)
        secret = vault.get(task_id)
        if secret.profile is None:
            raise HTTPException(status_code=409, detail="이력서 분석이 아직 완료되지 않았습니다.")
        try:
            result = await browser_worker.open(task_id, secret.raw_url, task.target_hostname)
            store.transition(task_id, "browser_pending")
            return result
        except BrowserSessionError as error:
            raise browser_error(error) from None

    @app.get(
        "/api/tasks/{task_id}/browser/status",
        response_model=BrowserStatus,
        dependencies=[Depends(authorize)],
    )
    async def browser_status(task_id: str) -> BrowserStatus:
        get_task(task_id)
        try:
            return await browser_worker.status(task_id)
        except BrowserSessionError as error:
            raise browser_error(error) from None

    @app.post(
        "/api/tasks/{task_id}/browser/approve-domain",
        response_model=BrowserStatus,
        dependencies=[Depends(authorize)],
    )
    async def approve_browser_domain(task_id: str, payload: ApproveDomainRequest) -> BrowserStatus:
        get_task(task_id)
        try:
            return await browser_worker.approve_domain(task_id, payload.hostname)
        except BrowserSessionError as error:
            raise browser_error(error) from None

    @app.post(
        "/api/tasks/{task_id}/browser/inspect",
        response_model=BrowserInspectionReport,
        dependencies=[Depends(authorize)],
    )
    async def inspect_browser(task_id: str) -> BrowserInspectionReport:
        get_task(task_id)
        secret = vault.get(task_id)
        if secret.profile is None:
            raise HTTPException(status_code=409, detail="이력서 분석이 아직 완료되지 않았습니다.")
        try:
            report = await browser_worker.inspect(task_id, secret.profile)
            store.transition(task_id, "planned")
            return report
        except BrowserSessionError as error:
            raise browser_error(error) from None

    @app.post(
        "/api/tasks/{task_id}/browser/fill",
        response_model=BrowserFillReport,
        dependencies=[Depends(authorize)],
    )
    async def fill_browser(task_id: str) -> BrowserFillReport:
        get_task(task_id)
        secret = vault.get(task_id)
        if secret.profile is None:
            raise HTTPException(status_code=409, detail="이력서 분석이 아직 완료되지 않았습니다.")
        try:
            report = await browser_worker.fill(task_id, secret.profile)
            # One-click fill inspects internally, so pass through 'planned' first.
            store.transition(task_id, "planned")
            store.transition(task_id, "awaiting_review")
            return report
        except BrowserSessionError as error:
            raise browser_error(error) from None

    @app.post(
        "/api/tasks/{task_id}/browser/fill-field",
        response_model=BrowserFillResult,
        dependencies=[Depends(authorize)],
    )
    async def fill_browser_field(task_id: str, payload: FillFieldRequest) -> BrowserFillResult:
        get_task(task_id)
        secret = vault.get(task_id)
        if secret.profile is None:
            raise HTTPException(status_code=409, detail="이력서 분석이 아직 완료되지 않았습니다.")
        try:
            return await browser_worker.fill_field(
                task_id, secret.profile, payload.form_field, payload.label, payload.value
            )
        except BrowserSessionError as error:
            raise browser_error(error) from None

    @app.post(
        "/api/tasks/{task_id}/browser/next",
        response_model=BrowserStatus,
        dependencies=[Depends(authorize)],
    )
    async def advance_browser(task_id: str) -> BrowserStatus:
        get_task(task_id)
        secret = vault.get(task_id)
        if secret.profile is None:
            raise HTTPException(status_code=409, detail="이력서 분석이 아직 완료되지 않았습니다.")
        try:
            result = await browser_worker.advance(task_id, secret.profile)
            store.transition(task_id, "browser_pending")
            return result
        except BrowserSessionError as error:
            raise browser_error(error) from None

    return app


