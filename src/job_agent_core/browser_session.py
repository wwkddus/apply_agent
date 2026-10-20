from __future__ import annotations

import asyncio
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Error as PlaywrightError, Page, Playwright, Route, async_playwright
from pydantic import BaseModel, ConfigDict, Field

from .address_search import POSTCODE_HOSTS
from .security import URLPolicyError, domain_matches, sanitize_url, validate_target_url


class BrowserSessionError(RuntimeError):
    pass


class BrowserSessionNotFound(BrowserSessionError):
    pass


class BrowserRedirectApprovalRequired(BrowserSessionError):
    pass


class BrowserLoginNotComplete(BrowserSessionError):
    pass


class BrowserStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str
    current_hostname: str = ""
    display_url: str = ""
    page_title: str = ""
    blocked_hostname: str | None = None
    discovered_frame_hosts: list[str] = Field(default_factory=list)
    can_inspect: bool = False


@dataclass(slots=True)
class BrowserSession:
    task_id: str
    target_hostname: str
    context: BrowserContext
    page: Page
    profile_dir: Path
    approved_domains: set[str]
    opened_at: float = field(default_factory=time.monotonic)
    blocked_url: str | None = None
    blocked_hostname: str | None = None
    discovered_frame_hosts: set[str] = field(default_factory=set)
    crashed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BrowserSessionManager:
    """Owns visible, task-isolated Chromium profiles on the user's computer."""

    def __init__(
        self,
        profile_root: str | Path,
        *,
        headless: bool = False,
        max_sessions: int = 1,
        max_runtime_seconds: int = 30 * 60,
    ):
        self.profile_root = Path(profile_root)
        self.headless = headless
        self.max_sessions = max_sessions
        self.max_runtime_seconds = max_runtime_seconds
        self._playwright: Playwright | None = None
        self._sessions: dict[str, BrowserSession] = {}
        self._manager_lock = asyncio.Lock()
        self._cleanup_orphaned_profiles()

    def _cleanup_orphaned_profiles(self) -> None:
        if not self.profile_root.exists():
            return
        for child in self.profile_root.iterdir():
            if child.is_dir() and re.fullmatch(r"[0-9a-fA-F-]{36}", child.name):
                shutil.rmtree(child, ignore_errors=True)

    async def _runtime(self) -> Playwright:
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        return self._playwright

    async def open(self, task_id: str, raw_url: str, target_hostname: str) -> BrowserStatus:
        async with self._manager_lock:
            if task_id in self._sessions:
                return await self.status(task_id)
            if len(self._sessions) >= self.max_sessions:
                raise BrowserSessionError("현재 다른 브라우저 작업이 실행 중입니다. 먼저 해당 작업을 종료해 주세요.")
            target = validate_target_url(raw_url)
            if target.hostname != target_hostname:
                raise BrowserSessionError("작업의 대상 도메인과 브라우저 주소가 일치하지 않습니다.")
            profile_dir = self.profile_root / task_id
            profile_dir.mkdir(parents=True, exist_ok=False)
            try:
                profile_dir.chmod(0o700)
            except OSError:
                pass
            runtime = await self._runtime()
            try:
                context = await runtime.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=self.headless,
                    viewport=None if not self.headless else {"width": 1280, "height": 900},
                    args=["--no-default-browser-check", "--disable-background-networking"],
                )
            except PlaywrightError as error:
                shutil.rmtree(profile_dir, ignore_errors=True)
                if "Executable doesn't exist" in str(error):
                    raise BrowserSessionError(
                        "Chromium이 설치되지 않았습니다. '.venv/bin/python -m playwright install chromium'을 실행해 주세요."
                    ) from None
                raise BrowserSessionError("브라우저를 시작하지 못했습니다.") from None
            page = context.pages[0] if context.pages else await context.new_page()
            session = BrowserSession(
                task_id=task_id,
                target_hostname=target_hostname,
                context=context,
                page=page,
                profile_dir=profile_dir,
                # The Kakao/Daum postcode search window is allowed by default (user decision, 2026-10-20).
                approved_domains={target_hostname, *POSTCODE_HOSTS},
            )
            self._sessions[task_id] = session

            async def route_guard(route: Route) -> None:
                request = route.request
                try:
                    is_top_navigation = request.is_navigation_request() and request.frame == request.frame.page.main_frame
                except Exception:
                    is_top_navigation = False
                if not is_top_navigation or request.url in {"about:blank", ""}:
                    await route.continue_()
                    return
                try:
                    candidate = validate_target_url(request.url)
                    approved = any(domain_matches(candidate.hostname, item) for item in session.approved_domains)
                except URLPolicyError:
                    approved = False
                    candidate = None
                if not approved:
                    hostname = (urlsplit(request.url).hostname or "").casefold()
                    try:
                        popup = request.frame.page != session.page
                    except Exception:
                        popup = False
                    if popup and hostname:
                        # A new window (e.g. the address search) is asked about like a frame host;
                        # approving it must not navigate the application page there.
                        session.discovered_frame_hosts.add(hostname)
                        await route.abort("blockedbyclient")
                        return
                    session.blocked_url = request.url
                    session.blocked_hostname = (urlsplit(request.url).hostname or "").casefold() or None
                    await route.abort("blockedbyclient")
                    return
                session.blocked_url = None
                session.blocked_hostname = None
                await route.continue_()

            await context.route("**/*", route_guard)
            page.on("crash", lambda _page: setattr(session, "crashed", True))
            try:
                await page.goto(raw_url, wait_until="domcontentloaded", timeout=30_000)
            except PlaywrightError:
                if session.blocked_hostname is None:
                    self._sessions.pop(task_id, None)
                    try:
                        await context.close()
                    except PlaywrightError:
                        pass
                    shutil.rmtree(profile_dir, ignore_errors=True)
                    raise BrowserSessionError("채용 페이지를 열지 못했습니다.") from None
            return await self.status(task_id)

    def get(self, task_id: str) -> BrowserSession:
        try:
            session = self._sessions[task_id]
        except KeyError:
            raise BrowserSessionNotFound("이 작업의 브라우저 세션을 찾을 수 없습니다.") from None
        if time.monotonic() - session.opened_at > self.max_runtime_seconds:
            raise BrowserSessionError("브라우저 작업 시간이 30분을 초과했습니다. 작업을 종료하고 다시 시작해 주세요.")
        if session.crashed:
            raise BrowserSessionError("브라우저가 비정상 종료되었습니다. 작업을 종료하고 다시 시작해 주세요.")
        return session

    async def application_page(self, task_id: str) -> Page:
        session = self.get(task_id)
        pages = [page for page in session.context.pages if not page.is_closed()]
        if not pages:
            raise BrowserSessionError("열려 있는 브라우저 페이지가 없습니다.")
        if session.page in pages:
            return session.page
        for page in reversed(pages):
            hostname = (urlsplit(page.url).hostname or "").casefold()
            if domain_matches(hostname, session.target_hostname):
                session.page = page
                return page
        session.page = pages[-1]
        return session.page

    async def status(self, task_id: str) -> BrowserStatus:
        session = self.get(task_id)
        page = await self.application_page(task_id)
        url = page.url if page.url not in {"", "about:blank"} else ""
        hostname = (urlsplit(url).hostname or "").casefold()
        try:
            title = (await page.title())[:160]
        except PlaywrightError:
            title = ""
        state = "blocked_redirect" if session.blocked_hostname else "open"
        return BrowserStatus(
            state=state,
            current_hostname=hostname,
            display_url=sanitize_url(url) if url else "",
            page_title=title,
            blocked_hostname=session.blocked_hostname,
            discovered_frame_hosts=sorted(session.discovered_frame_hosts),
            can_inspect=bool(url and not session.blocked_hostname and not session.crashed),
        )

    async def approve_domain(self, task_id: str, hostname: str) -> BrowserStatus:
        session = self.get(task_id)
        normalized = str(hostname or "").rstrip(".").casefold()
        permitted = {session.blocked_hostname, *session.discovered_frame_hosts}
        if not normalized or normalized not in permitted:
            raise BrowserSessionError("브라우저가 실제로 요청한 도메인만 추가로 허용할 수 있습니다.")
        validate_target_url(f"https://{normalized}/")
        session.approved_domains.add(normalized)
        session.discovered_frame_hosts.discard(normalized)
        pending = session.blocked_url if normalized == session.blocked_hostname else None
        session.blocked_url = None
        session.blocked_hostname = None
        if pending:
            page = await self.application_page(task_id)
            try:
                await page.goto(pending, wait_until="domcontentloaded", timeout=30_000)
            except PlaywrightError:
                if session.blocked_hostname is None:
                    raise BrowserSessionError("허용한 페이지로 이동하지 못했습니다.") from None
        return await self.status(task_id)

    async def close(self, task_id: str) -> None:
        async with self._manager_lock:
            session = self._sessions.pop(task_id, None)
            if session is None:
                return
            try:
                await session.context.close()
            except PlaywrightError:
                pass
            shutil.rmtree(session.profile_dir, ignore_errors=True)

    async def close_all(self) -> None:
        for task_id in list(self._sessions):
            await self.close(task_id)
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
