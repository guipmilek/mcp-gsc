"""Scope, canonicalization, and validation for direct GSC CRUD."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

CRUD_CONTRACT_VERSION = "direct-crud-v1"
OPERATION_HASH_VERSION = 4

RESOURCE_REGISTRY: dict[str, dict[str, Any]] = {
    "Site": {
        "actions": ["add", "get", "list", "delete"],
        "create_action": "add",
        "delete_action": "delete",
        "update_supported": False,
    },
    "Sitemap": {
        "actions": ["submit", "get", "list", "delete"],
        "create_action": "submit",
        "delete_action": "delete",
        "update_supported": False,
    },
}


class GscSafetyError(RuntimeError):
    """Structured connector error returned by the Horizon facade."""

    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
        *,
        retryable: bool = False,
        execution_may_have_completed: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.retryable = retryable
        self.execution_may_have_completed = execution_may_have_completed

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
            "execution_may_have_completed": self.execution_may_have_completed,
        }


@dataclass(frozen=True)
class ScopeConfig:
    allowed_site_urls: tuple[str, ...]
    allowed_sitemap_prefixes: tuple[str, ...]
    max_operations_per_request: int


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise GscSafetyError(
            "INVALID_CONFIGURATION", f"{name} must be an integer."
        ) from exc
    if not minimum <= value <= maximum:
        raise GscSafetyError(
            "INVALID_CONFIGURATION",
            f"{name} must be between {minimum} and {maximum}.",
        )
    return value


def split_csv(name: str) -> tuple[str, ...]:
    return tuple(
        item.strip() for item in os.getenv(name, "").split(",") if item.strip()
    )


def canonical_site_url(site_url: str) -> str:
    value = str(site_url or "").strip()
    if not value:
        raise GscSafetyError("INVALID_ARGUMENT", "site_url is required.")
    if value.lower().startswith("sc-domain:"):
        domain = value[len("sc-domain:") :].strip().lower().rstrip(".")
        if (
            not domain
            or "/" in domain
            or ":" in domain
            or not re.fullmatch(r"[a-z0-9.-]+", domain)
        ):
            raise GscSafetyError(
                "INVALID_ARGUMENT", "Invalid sc-domain property format."
            )
        return f"sc-domain:{domain}"

    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise GscSafetyError(
            "INVALID_ARGUMENT",
            "site_url must be HTTP(S) or sc-domain:domain.",
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise GscSafetyError(
            "INVALID_ARGUMENT",
            "site_url must not contain credentials, query parameters, or fragments.",
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise GscSafetyError(
            "INVALID_ARGUMENT", "site_url contains an invalid port."
        ) from exc
    host = parsed.hostname.lower()
    if port:
        host = f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", "", ""))


def canonical_sitemap_url(sitemap_url: str) -> str:
    value = str(sitemap_url or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise GscSafetyError(
            "INVALID_ARGUMENT",
            "sitemap_url must be an absolute HTTP(S) URL.",
        )
    if parsed.username or parsed.password or parsed.fragment:
        raise GscSafetyError(
            "INVALID_ARGUMENT",
            "sitemap_url must not contain credentials or fragments.",
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise GscSafetyError(
            "INVALID_ARGUMENT", "sitemap_url contains an invalid port."
        ) from exc
    host = parsed.hostname.lower()
    if port:
        host = f"{host}:{port}"
    return urlunsplit(
        (parsed.scheme.lower(), host, parsed.path or "/", parsed.query, "")
    )


def load_scope_config() -> ScopeConfig:
    """Load scope on every request so Horizon environment updates apply."""

    return ScopeConfig(
        allowed_site_urls=tuple(
            canonical_site_url(value)
            for value in split_csv("GSC_ALLOWED_SITE_URLS")
        ),
        allowed_sitemap_prefixes=tuple(
            canonical_sitemap_url(value)
            for value in split_csv("GSC_ALLOWED_SITEMAP_PREFIXES")
        ),
        max_operations_per_request=env_int(
            "GSC_MAX_OPERATIONS_PER_REQUEST", 10, 1, 10
        ),
    )


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return sha256(canonical_json(value)).hexdigest()


def assert_allowed_site(config: ScopeConfig, site_url: str) -> None:
    if not config.allowed_site_urls:
        raise GscSafetyError(
            "EMPTY_ALLOWLIST", "GSC_ALLOWED_SITE_URLS is empty."
        )
    if site_url not in config.allowed_site_urls:
        raise GscSafetyError(
            "SITE_NOT_ALLOWED",
            "The requested Search Console property is outside the allowlist.",
            {"site_url": site_url},
        )


def assert_allowed_sitemap(config: ScopeConfig, sitemap_url: str) -> None:
    if not config.allowed_sitemap_prefixes:
        raise GscSafetyError(
            "EMPTY_ALLOWLIST", "GSC_ALLOWED_SITEMAP_PREFIXES is empty."
        )
    if not any(
        sitemap_url.startswith(prefix)
        for prefix in config.allowed_sitemap_prefixes
    ):
        raise GscSafetyError(
            "SITEMAP_NOT_ALLOWED",
            "The sitemap URL is outside all configured prefixes.",
            {"sitemap_url": sitemap_url},
        )
