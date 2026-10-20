from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


class URLPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedURL:
    raw: str
    hostname: str
    display: str


_TOKENISH = re.compile(r"^[A-Za-z0-9_-]{24,}$")
SUBMIT_TEXT_RE = re.compile(r"\bapply\b|submit|complete\s+application|지원\s*하기|최종\s*제출|^제출$", re.I)
NEXT_TEXT_RE = re.compile(r"^(?:다음|계속|저장\s*후\s*다음|next|continue|save\s*(?:and|&)\s*continue)$", re.I)


def sanitize_url(url: str) -> str:
    parts = urlsplit(str(url or "").strip())
    segments = ["…" if _TOKENISH.fullmatch(part) else part for part in parts.path.split("/")]
    host = (parts.hostname or "").lower()
    return urlunsplit((parts.scheme.lower(), host, "/".join(segments), "", ""))


def _reject_non_public_literal(hostname: str) -> None:
    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        return
    if not address.is_global:
        raise URLPolicyError("사설, 루프백, 링크 로컬 IP 주소는 사용할 수 없습니다.")


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


def domain_matches(hostname: str, approved_domain: str) -> bool:
    hostname = (hostname or "").rstrip(".").lower()
    approved_domain = (approved_domain or "").rstrip(".").lower()
    return bool(hostname and approved_domain) and (
        hostname == approved_domain or hostname.endswith("." + approved_domain)
    )


def require_approved_redirect(url: str, approved_domains: set[str]) -> ValidatedURL:
    result = validate_target_url(url)
    if not any(domain_matches(result.hostname, domain) for domain in approved_domains):
        raise URLPolicyError("다른 도메인으로 이동하려면 사용자의 명시적 승인이 필요합니다.")
    return result


def is_submit_like_control(*, tag: str, attr_type: str, form_type: str, text: str) -> bool:
    combined_type = {attr_type.casefold(), form_type.casefold()}
    return "submit" in combined_type or "image" in combined_type or bool(SUBMIT_TEXT_RE.search(text or ""))


def is_safe_next_control(*, tag: str, attr_type: str, form_type: str, text: str) -> bool:
    """A real-site Next control must not carry submit semantics."""
    return bool(NEXT_TEXT_RE.fullmatch((text or "").strip())) and not is_submit_like_control(
        tag=tag,
        attr_type=attr_type,
        form_type=form_type,
        text=text,
    )

