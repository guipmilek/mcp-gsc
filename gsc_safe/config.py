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
DEPLOY_CONFIG_ENV = "MCP_CONFIG"

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


def _deployment_config() -> Mapping[str, Any]:
    raw = os.getenv(DEPLOY_CONFIG_ENV, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GscSafetyError(
            "INVALID_CONFIGURATION",
            f"{DEPLOY_CONFIG_ENV} must be a valid JSON object.",
        ) from exc
    if not isinstance(value, dict):
        raise GscSafetyError(
            "INVALID_CONFIGURATION",
            f"{DEPLOY_CONFIG_ENV} must be a JSON object.",
        )
    supported = {"sites", "sitemaps", "max_operations"}
    unknown = sorted(set(value) - supported)
    if unknown:
        raise GscSafetyError(
            "INVALID_CONFIGURATION",
            f"{DEPLOY_CONFIG_ENV} contains unsupported keys.",
            {"unsupported_keys": unknown, "supported_keys": sorted(supported)},
        )
    return value


def _config_strings(config: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = config.get(key, [])
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise GscSafetyError(
            "INVALID_CONFIGURATION",
            f"{DEPLOY_CONFIG_ENV}.{key} must be an array of non-empty strings.",
        )
    return tuple(item.strip() for item in value)


def _config_int(
    config: Mapping[str, Any],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise GscSafetyError(
            "INVALID_CONFIGURATION",
            f"{DEPLOY_CONFIG_ENV}.{key} must be an integer.",
        )
    if not minimum <= value <= maximum:
        raise GscSafetyError(
            "INVALID_CONFIGURATION",
            f"{DEPLOY_CONFIG_ENV}.{key} must be between {minimum} and {maximum}.",
        )
    return value


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

    config = _deployment_config()
    return ScopeConfig(
        allowed_site_urls=tuple(
            canonical_site_url(value)
            for value in _config_strings(config, "sites")
        ),
        allowed_sitemap_prefixes=tuple(
            canonical_sitemap_url(value)
            for value in _config_strings(config, "sitemaps")
        ),
        max_operations_per_request=_config_int(
            config, "max_operations", 10, 1, 10
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
    if config.allowed_site_urls and site_url not in config.allowed_site_urls:
        raise GscSafetyError(
            "SITE_NOT_ALLOWED",
            "The requested Search Console property is outside the allowlist.",
            {"site_url": site_url},
        )


def assert_allowed_sitemap(config: ScopeConfig, sitemap_url: str) -> None:
    if config.allowed_sitemap_prefixes and not any(
        sitemap_url.startswith(prefix)
        for prefix in config.allowed_sitemap_prefixes
    ):
        raise GscSafetyError(
            "SITEMAP_NOT_ALLOWED",
            "The sitemap URL is outside all configured prefixes.",
            {"sitemap_url": sitemap_url},
        )
