# 로컬 API 서버(FastAPI). 
# 데스크톱 UI의 요청을 받아 
# 이력서 분석 → 브라우저 열기 → 화면 분석 → 입력 → 2차 확인을 순서대로 연결합니다.

# 내 컴퓨터(127.0.0.1)에서만, 토큰이 있는 요청만 받습니다. 
# 최종 제출 API는 일부러 없습니다.

from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .app_paths import default_data_dir
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
from .resume_lookup import lookup_answers
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


# 서버 설정: DB 위치, 접근 토큰(24자 이상), 브라우저 프로필 위치, LLM 사용 모드, 허용 호스트·출처.
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


# 작업 생성 요청(지원서 URL).
class CreateTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_url: str = Field(min_length=1, max_length=2048)


# 이력서 분석 결과 미리보기. 값의 출처(rule/llm)를 함께 보내 LLM 값은 사용자가 확인하게 합니다.
class ResumePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: dict[str, str]
    name_confirmation_required: bool
    warnings: list[str]
    missing: list[str]
    # path -> "rule" | "llm"; LLM values are shown to the user for confirmation.
    sources: dict[str, str] = Field(default_factory=dict)


# 사용자가 확인한 성/이름.
class ConfirmNameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    family: str = Field(min_length=1, max_length=60)
    given: str = Field(min_length=1, max_length=80)


# 사용자가 화면에서 고친 이력서 값 하나.
class EditResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=80)
    value: str = Field(max_length=500)


# 칸 하나를 사용자가 고른 값으로 다시 입력하는 요청.
class FillFieldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    form_field: str = Field(min_length=1, max_length=80)
    label: str = Field(max_length=300)
    value: str = Field(min_length=1, max_length=500)
    fingerprint: str = Field(default="", max_length=600)
    # True when the value is one of the page's own option texts that the user picked.
    chosen_from_candidates: bool = False


# AI 2차 확인 요청: 아직 확인이 필요한 결과 행 목록.
class LookupRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fingerprint: str = Field(min_length=1, max_length=600)
    status: str = Field(max_length=40)


class LookupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Rows of the fill report that still need a second check.
    rows: list[LookupRow] = Field(default_factory=list, max_length=300)


# 칸 목록으로 입력 계획만 세우는 요청(브라우저 없이).
class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: list[FormField] = Field(max_length=300)


# 작업 취소 결과. 민감 데이터가 지워졌는지 알려 줍니다.
class CancelResponse(BaseModel):
    task_id: str
    phase: str
    sensitive_data_cleared: bool


# 사용자가 승인한 도메인.
class ApproveDomainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hostname: str = Field(min_length=1, max_length=253)


# 환경변수로 설정을 만듭니다. 토큰이 없으면 서버를 시작하지 않습니다.
def default_settings() -> ApiSettings:
    data_dir = default_data_dir()
    token = os.environ.get("JOB_AGENT_API_TOKEN")
    if not token:
        raise RuntimeError("로컬 API를 시작하기 전에 JOB_AGENT_API_TOKEN을 설정해야 합니다.")
    return ApiSettings(
        database_path=data_dir / "task_state.sqlite3",
        browser_profile_root=data_dir / "browser_profiles",
        api_token=token,
        llm_mode=os.environ.get("JOB_AGENT_LLM", "auto"),
    )


# 앱 조립(의존성 주입): 저장소·메모리 보관함·계획 작업자·브라우저 작업자를 만들고 API를 등록합니다.
# 테스트에서는 browser_worker에 가짜를 넣어 실제 브라우저 없이 검증합니다.
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

    # 서버 종료 시 브라우저를 닫고, 메모리의 민감 데이터와 DB 연결을 정리합니다.
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

    # 모든 요청 검사: 로컬 호스트만, 허용된 출처(origin)만 받습니다(다른 웹사이트에서의 요청 차단).
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

    # 요청 헤더의 토큰 확인. 타이밍 공격을 막기 위해 compare_digest로 비교합니다.
    def authorize(x_job_agent_token: str | None = Header(default=None)) -> None:
        if not x_job_agent_token or not secrets.compare_digest(x_job_agent_token, settings.api_token):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="로컬 API 토큰이 올바르지 않습니다.")

    # 작업 조회(없으면 404).
    def get_task(task_id: str) -> TaskRecord:
        try:
            return store.get(task_id)
        except TaskNotFound:
            raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.") from None

    # 서버 상태 확인용.
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "scope": "ko-KR-mvp"}

    # 작업 생성: URL 검사 → DB에는 도메인만, 원본 URL은 메모리 보관함에 저장.
    @app.post("/api/tasks", response_model=TaskRecord, dependencies=[Depends(authorize)])
    async def create_task(payload: CreateTaskRequest) -> TaskRecord:
        try:
            target = validate_target_url(payload.target_url)
        except URLPolicyError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        task = store.create(target.hostname)
        vault.put(task.task_id, SensitiveTaskData(raw_url=target.raw))
        return task

    # 작업 상태 조회.
    @app.get("/api/tasks/{task_id}", response_model=TaskRecord, dependencies=[Depends(authorize)])
    async def read_task(task_id: str) -> TaskRecord:
        return get_task(task_id)

    @app.post(
        "/api/tasks/{task_id}/resume",
        response_model=ResumePreview,
        dependencies=[Depends(authorize)],
    )

    # 이력서 업로드·분석. 
    
    # 흐름: PDF 검사 → 규칙 추출 → 한국 형식 검사
    # → (필요 시) 로컬 LLM으로 빈 칸 보충 → 메모리에만 저장 → 미리보기 반환. 

    # 파일은 디스크에 저장하지 않습니다.
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
        vault.put(task_id, secret.model_copy(update={"profile": profile, "resume_text": str(inspected["text"])}))
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
    # 사용자가 확인한 성/이름 반영. 합친 값이 전체 이름과 다르면 거부합니다.
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
    # 사용자가 고친 값을 검증해 반영합니다.
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
    # 브라우저 없이 입력 계획만 계산(테스트·확인용).
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
    # 작업 취소: 브라우저 닫기 → 상태 변경 → 메모리의 민감 데이터 삭제.
    async def cancel_task(task_id: str) -> CancelResponse:
        get_task(task_id)
        await browser_worker.close(task_id)
        try:
            task = store.transition(task_id, "cancelled")
        except InvalidTaskTransition as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        vault.clear(task_id)
        return CancelResponse(task_id=task.task_id, phase=task.phase, sensitive_data_cleared=True)

    # 브라우저 오류를 알맞은 HTTP 상태 코드로 바꿉니다.
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
    # 작업용 브라우저 창 열기(사용자가 이 창에서 로그인).
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
    # 브라우저 상태 조회(막힌 도메인 등).
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
    # 사용자가 승인한 도메인을 허용 목록에 추가.
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
    # 현재 화면 분석과 입력 계획(입력은 하지 않음).
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
    # 원클릭 자동 입력: 분석 → 규칙 입력 → 다시 읽기 확인. 결과는 사용자 확인 대기 상태로 둡니다.
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
        "/api/tasks/{task_id}/browser/lookup",
        response_model=BrowserFillReport,
        dependencies=[Depends(authorize)],
    )
    # AI 2차 확인. 규칙 결과를 먼저 보여 준 뒤 느린 LLM을 따로 실행하려고 fill과 분리했습니다.
    async def lookup_browser(task_id: str, payload: LookupRequest) -> BrowserFillReport:
        """Separate from fill so the rule results reach the user before the slow local LLM runs."""
        get_task(task_id)
        secret = vault.get(task_id)
        if secret.profile is None:
            raise HTTPException(status_code=409, detail="이력서 분석이 아직 완료되지 않았습니다.")
        lookup = None
        if settings.llm_mode != "off" and secret.resume_text:
            url, model = ollama_settings()

            # LLM 조회 함수를 만들어 브라우저 작업자에 주입합니다(LLM 꺼짐이면 None).
            async def lookup(fields):
                return await lookup_answers(
                    secret.resume_text, fields, url=url, model=model, transport=settings.ollama_transport
                )

        try:
            requests = [(row.fingerprint, row.status) for row in payload.rows]
            return await browser_worker.fill_lookup(task_id, secret.profile, lookup, requests)
        except BrowserSessionError as error:
            raise browser_error(error) from None

    @app.post(
        "/api/tasks/{task_id}/browser/fill-field",
        response_model=BrowserFillResult,
        dependencies=[Depends(authorize)],
    )
    # 칸 하나를 사용자가 고른 값으로 입력(후보 중 선택 등).
    async def fill_browser_field(task_id: str, payload: FillFieldRequest) -> BrowserFillResult:
        get_task(task_id)
        secret = vault.get(task_id)
        if secret.profile is None:
            raise HTTPException(status_code=409, detail="이력서 분석이 아직 완료되지 않았습니다.")
        try:
            return await browser_worker.fill_field(
                task_id,
                secret.profile,
                payload.form_field,
                payload.label,
                payload.value,
                fingerprint=payload.fingerprint,
                chosen=payload.chosen_from_candidates,
            )
        except BrowserSessionError as error:
            raise browser_error(error) from None

    @app.post(
        "/api/tasks/{task_id}/browser/next",
        response_model=BrowserStatus,
        dependencies=[Depends(authorize)],
    )
    # '다음' 페이지로 이동. 제출 성격이 없는 안전한 '다음' 버튼만 누릅니다.
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


