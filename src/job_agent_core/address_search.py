"""Address-search adapter for read-only 주소 fields filled by the Kakao (Daum) postcode widget.

Most Korean application forms make 우편번호/기본주소 read-only and fill them from the postcode
search window, which also sets hidden fields (building code...) the site needs on submit. So the
agent never writes those inputs with JS; it uses the widget the way the applicant would:

1. click the '우편번호 찾기 / 주소 검색' button next to the address field (never a submit button);
2. type the resume address into the widget's search box (iframe layer or popup window);
3. pick a result only when it is the single match, or the single match with the resume's postcode;
4. read the page's address field back before the 상세주소 is filled.

The widget's two domains are approved by default (user decision, 2026-10-20); only the resume
address is typed into them. Any other domain still needs the user's approval. When the search is not
conclusive the window is left open with the address already searched, so the user picks one result.
Each run keeps a short step trace (no values) so a failure on a real site can be diagnosed.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError, Frame, Page

POSTCODE_HOSTS = ("postcode.map.kakao.com", "postcode.map.daum.net")
SEARCH_BOX = "#region_name"
RESULT_ITEMS = "li.list_post_item"
MAX_CANDIDATES = 5

_FIND_BUTTON_SCRIPT = r"""
(element) => {
  const pattern = /우편\s*번호|주소\s*(검색|찾기)|post\s*code|zip\s*code|address\s*search|^\s*(검색|찾기)\s*$/i;
  const clickable = 'button, a, input[type="button"], [role="button"]';
  const textOf = (node) => (node.innerText || node.value || node.getAttribute('aria-label') || node.getAttribute('title') || '').trim();
  const visible = (node) => { const box = node.getBoundingClientRect(); return box.width > 0 && box.height > 0; };
  let scope = element.parentElement;
  for (let depth = 0; scope && depth < 4; depth += 1, scope = scope.parentElement) {
    const found = [...scope.querySelectorAll(clickable)].filter((node) => visible(node) && pattern.test(textOf(node)));
    if (found.length > 1) return { error: 'ambiguous' };
    if (found.length === 1) {
      const node = found[0];
      const tag = node.tagName.toLowerCase();
      const type = (node.getAttribute('type') || '').toLowerCase();
      // A <button> without a type submits its form; the agent never clicks anything that may submit.
      const submits = (tag === 'button' && (type === 'submit' || (!type && node.closest('form')))) || type === 'submit';
      if (submits) return { error: 'submit' };
      node.setAttribute('data-apply-local-address-search', '1');
      return { text: textOf(node).slice(0, 40) };
    }
  }
  return { error: 'missing' };
}
"""


@dataclass(slots=True)
class AddressSearchResult:
    ok: bool
    reason: str
    zonecode: str = ""
    address: str = ""
    candidates: list[str] = field(default_factory=list)
    # Set when the widget's domain still needs the user's one-time approval.
    approval_host: str | None = None
    # The search window stays open with the resume address searched, for the user to pick a result.
    left_open: bool = False
    # Steps reached, without any value, e.g. ['button:found', 'widget:popup', 'results:3'].
    trace: list[str] = field(default_factory=list)


def _plain(value: str) -> str:
    return re.sub(r"[\s,.\-()·]+", "", value or "").casefold()


def search_query(address: str) -> str:
    """The resume address up to the building/lot number; unit and floor text confuse the search."""
    text = " ".join((address or "").split())
    for pattern in (r"^(.+?(?:로|길)\s*\d+(?:-\d+)?)(?!\d)", r"^(.+?(?:동|리|가)\s+\d+(?:-\d+)?)(?!\d)"):
        match = re.match(pattern, text)
        if match:
            return match.group(1)
    return text


def _key(query: str) -> str:
    """Road/lot name and number, e.g. '세종대로110'; the city part is written differently ('서울특별시' vs '서울')."""
    return _plain(" ".join(query.split()[-2:]))


def is_postcode_host(hostname: str) -> bool:
    host = (hostname or "").casefold()
    return any(host == item or host.endswith("." + item) for item in POSTCODE_HOSTS)


def _host(url: str) -> str:
    return (urlsplit(url or "").hostname or "").casefold()


def _new_pages(page: Page, known_pages: set[Page]) -> list[Page]:
    return [item for item in page.context.pages if item not in known_pages and not item.is_closed()]


async def _widget_frame(page: Page, known_pages: set[Page], timeout: float) -> tuple[Frame | None, Page | None]:
    """The postcode frame inside the page, or in a window opened by the click (directly or nested)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.25)
        for owner in [*_new_pages(page, known_pages), page]:
            # page.frames lists nested frames too, e.g. a site page in the popup embedding the widget.
            for frame in owner.frames:
                if frame.is_detached() or not is_postcode_host(_host(frame.url)):
                    continue
                try:
                    if await frame.locator(SEARCH_BOX).count():
                        return frame, owner if owner is not page else None
                except PlaywrightError:
                    continue
    return None, None


async def _close_new(page: Page, known_pages: set[Page]) -> None:
    """Close every window this click opened; the application page itself is never closed."""
    for item in _new_pages(page, known_pages):
        try:
            await item.close()
        except PlaywrightError:
            pass


async def _results(frame: Frame, timeout: float = 6.0) -> list[dict[str, str]]:
    deadline = asyncio.get_running_loop().time() + timeout
    previous = -1
    rows: list[dict[str, str]] = []
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.25)
        try:
            rows = await frame.locator(f"{RESULT_ITEMS}:visible").evaluate_all(
                """items => items.map(item => ({
                    zonecode: item.dataset.zonecode || '',
                    addr: item.dataset.addr || '',
                    text: (item.innerText || '').replace(/\\s+/g, ' ').trim(),
                }))"""
            )
        except PlaywrightError:
            rows = []
        if rows and len(rows) == previous:
            break
        previous = len(rows)
    return rows


def choose_result(rows: list[dict[str, str]], query: str, postal_code: str) -> tuple[int | None, list[str]]:
    """Index of the single matching result, else None with candidates for the user."""
    key = _key(query)
    matches = [index for index, row in enumerate(rows) if key and key in _plain(row.get("text", ""))]
    if len(matches) > 1 and postal_code:
        matches = [index for index in matches if rows[index].get("zonecode") == postal_code]
    if len(matches) == 1:
        return matches[0], []
    candidates = [f"({row.get('zonecode', '')}) {row.get('addr', '')}".strip() for row in rows if row.get("addr")]
    return None, candidates[:MAX_CANDIDATES]


async def search_address(
    page: Page,
    address_frame: Frame,
    address_selector: str,
    *,
    address: str,
    postal_code: str,
    host_approved: Callable[[str], bool],
) -> AddressSearchResult:
    """Run the postcode widget for the resume address; the caller reads the page fields back."""
    trace: list[str] = []

    def result(ok: bool, reason: str, **extra) -> AddressSearchResult:
        return AddressSearchResult(ok, reason, trace=trace, **extra)

    query = search_query(address)
    if not query:
        return result(False, "이력서에 주소가 없어 주소 검색을 하지 않았습니다.")
    try:
        found = await address_frame.locator(address_selector).first.evaluate(_FIND_BUTTON_SCRIPT)
    except PlaywrightError:
        found = {"error": "missing"}
    trace.append(f"button:{found.get('error') or 'found'}")
    if found.get("error") == "submit":
        return result(False, "주소 검색 버튼이 제출 버튼으로 되어 있어 누르지 않았습니다. 직접 검색해 주세요.")
    if found.get("error"):
        return result(False, "읽기 전용 주소 칸이며 옆의 주소 검색 버튼을 하나로 확정하지 못했습니다. 직접 검색해 주세요.")

    known_pages = set(page.context.pages)
    try:
        await address_frame.locator('[data-apply-local-address-search="1"]').first.click(timeout=5_000)
    except PlaywrightError:
        trace.append("click:failed")
        return result(False, "주소 검색 버튼을 누르지 못했습니다. 직접 검색해 주세요.")
    frame, popup = await _widget_frame(page, known_pages, timeout=12.0)
    if frame is None:
        opened = _new_pages(page, known_pages)
        hosts = sorted({_host(item.url) or "blank" for item in opened})
        trace.append(f"widget:timeout(new_windows={len(opened)},hosts={'/'.join(hosts)[:80]})")
        await _close_new(page, known_pages)
        return result(False, "주소 검색 창을 찾지 못했습니다. 브라우저에서 직접 검색해 주세요.")
    trace.append("widget:popup" if popup else "widget:frame")
    host = _host(frame.url)
    if not host_approved(host):
        trace.append("widget:unapproved")
        await _close_new(page, known_pages)
        return result(
            False,
            f"주소 검색 창({host})을 쓰려면 Apply Local에서 이 도메인을 한 번 허용한 뒤 다시 자동 입력해 주세요.",
            approval_host=host,
        )
    try:
        if popup is not None:
            await popup.bring_to_front()
        box = frame.locator(SEARCH_BOX).first
        await box.fill(query)
        await box.press("Enter")
        rows = await _results(frame)
        trace.append(f"results:{len(rows)}")
        index, candidates = choose_result(rows, query, postal_code)
        if index is None:
            # Leave the searched window open: the user only has to click the right result.
            return result(
                False,
                ("주소 검색 결과가 없습니다." if not rows else "주소 검색 결과가 여러 개라 하나로 확정하지 못했습니다.")
                + " 열려 있는 주소 검색 창에서 직접 선택한 뒤 '다시 자동 입력'을 누르면 상세주소를 채웁니다.",
                candidates=candidates,
                left_open=True,
            )
        item = frame.locator(f"{RESULT_ITEMS}:visible").nth(index)
        await item.locator("button.link_post, a.link_post").first.click(timeout=5_000)
        trace.append("picked")
    except PlaywrightError:
        trace.append("widget:changed")
        await _close_new(page, known_pages)
        return result(False, "주소 검색 창의 구조가 바뀌어 안전하게 계속할 수 없습니다. 직접 검색해 주세요.")
    row = rows[index]
    return result(True, "주소 검색 창에서 이력서 주소와 일치하는 결과 하나를 선택했습니다.",
                  zonecode=row.get("zonecode", ""), address=row.get("addr", ""))


def address_matches(actual: str, chosen: str, query: str) -> bool:
    """The page's address field shows the selected road/lot address."""
    value = _plain(actual)
    return bool(value) and (_plain(chosen) in value or bool(_key(query)) and _key(query) in value)
