# 안전 경계 모듈. 
# 접속할 URL을 검사하고, 다른 도메인 이동과 '제출' 버튼 클릭을 막는 기준을 정합니다.
# 개인정보가 엉뚱한 곳으로 가거나 지원서가 자동 제출되는 일을 막기 위한 장치입니다.

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


# URL 정책 위반 시 사용자에게 보여줄 오류.
class URLPolicyError(ValueError):
    pass


# 검사를 통과한 URL. 화면 표시용(display)은 토큰처럼 보이는 경로를 가린 값입니다.
@dataclass(frozen=True)
class ValidatedURL:
    raw: str
    hostname: str
    display: str


# 토큰처럼 보이는 긴 문자열(로그·화면에 그대로 남기지 않기 위해 가림).
_TOKENISH = re.compile(r"^[A-Za-z0-9_-]{24,}$")
# '지원하기/제출/submit' 같은 최종 제출 버튼 문구. 이 버튼은 절대 누르지 않습니다.
SUBMIT_TEXT_RE = re.compile(r"\bapply\b|submit|complete\s+application|지원\s*하기|최종\s*제출|^제출$", re.I)
# '다음/계속' 같은 페이지 이동 버튼 문구(제출과 구분하기 위함).
NEXT_TEXT_RE = re.compile(r"^(?:다음|계속|저장\s*후\s*다음|next|continue|save\s*(?:and|&)\s*continue)$", re.I)


# URL에서 쿼리·토큰을 지워 로그나 화면에 안전하게 보여줄 수 있는 형태로 만듭니다.
def sanitize_url(url: str) -> str:
    parts = urlsplit(str(url or "").strip())
    segments = ["…" if _TOKENISH.fullmatch(part) else part for part in parts.path.split("/")]
    host = (parts.hostname or "").lower()
    return urlunsplit((parts.scheme.lower(), host, "/".join(segments), "", ""))


# 사설망·루프백 IP 주소는 거부합니다(내부망 접근 방지).
def _reject_non_public_literal(hostname: str) -> None:
    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        return
    if not address.is_global:
        raise URLPolicyError("사설, 루프백, 링크 로컬 IP 주소는 사용할 수 없습니다.")


# 사용자가 입력한 지원서 URL 검사: https만, 계정정보·특수 포트·로컬 주소 금지.
def validate_target_url(url: str) -> ValidatedURL:
    raw = str(url or "").strip()
    parts = urlsplit(raw)
    if parts.scheme.lower() != "https":
        raise URLPolicyError("https:// 주소만 사용할 수 있습니다.")
    if not parts.hostname:
        raise URLPolicyError("URL에 호스트명이 없습니다.")
    if parts.username or parts.password:
        raise URLPolicyError("URL에 아이디나 비밀번호를 포함할 수 없습니다.")
    try:
        port = parts.port
    except ValueError:
        raise URLPolicyError("URL 포트가 올바르지 않습니다.") from None
    if port not in (None, 443):
        raise URLPolicyError("기본 HTTPS 포트만 사용할 수 있습니다.")
    hostname = parts.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        raise URLPolicyError("로컬 호스트명은 사용할 수 없습니다.")
    _reject_non_public_literal(hostname)
    return ValidatedURL(raw=raw, hostname=hostname, display=sanitize_url(raw))


# 호스트가 승인된 도메인(또는 그 하위 도메인)인지 확인.
def domain_matches(hostname: str, approved_domain: str) -> bool:
    hostname = (hostname or "").rstrip(".").lower()
    approved_domain = (approved_domain or "").rstrip(".").lower()
    return bool(hostname and approved_domain) and (
        hostname == approved_domain or hostname.endswith("." + approved_domain)
    )


# 페이지가 다른 도메인으로 이동하면, 사용자가 승인한 도메인일 때만 허용합니다.
def require_approved_redirect(url: str, approved_domains: set[str]) -> ValidatedURL:
    result = validate_target_url(url)
    if not any(domain_matches(result.hostname, domain) for domain in approved_domains):
        raise URLPolicyError("다른 도메인으로 이동하려면 사용자의 명시적 승인이 필요합니다.")
    return result


# 버튼이 '제출' 역할인지 판단(type=submit 또는 제출 문구). True면 자동으로 누르지 않습니다.
def is_submit_like_control(*, tag: str, attr_type: str, form_type: str, text: str) -> bool:
    combined_type = {attr_type.casefold(), form_type.casefold()}
    return "submit" in combined_type or "image" in combined_type or bool(SUBMIT_TEXT_RE.search(text or ""))


# '다음' 버튼이면서 제출 성격이 전혀 없을 때만 안전한 다음 버튼으로 봅니다.
def is_safe_next_control(*, tag: str, attr_type: str, form_type: str, text: str) -> bool:
    """A real-site Next control must not carry submit semantics."""
    return bool(NEXT_TEXT_RE.fullmatch((text or "").strip())) and not is_submit_like_control(
        tag=tag,
        attr_type=attr_type,
        form_type=form_type,
        text=text,
    )

