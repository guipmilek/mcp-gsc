"""Configuration, canonicalization, and fail-closed policy for GSC writes."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

OPERATION_HASH_VERSION = 3
CONFIRMATION_TOKEN_VERSION = 2
MINIMUM_SECRET_BYTES = 32

RESOURCE_REGISTRY: dict[str, dict[str, Any]] = {
    "Site": {
        "actions": ["add", "get", "list", "delete"],
        "create_action": "add",
        "delete_action": "delete",
        "risk_gates": {"add": "site_add", "delete": "site_delete"},
    },
    "Sitemap": {
        "actions": ["submit", "get", "list", "delete"],
        "create_action": "submit",
        "delete_action": "delete",
        "risk_gates": {"submit": "sitemap_submit", "delete": "sitemap_delete"},
    },
}


class GscSafetyError(RuntimeError):
    """Structured error returned consistently by the protected facade."""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass(frozen=True)
class SafetyConfig:
    mutations_enabled: bool
    allow_site_add: bool
    allow_site_delete: bool
    allow_sitemap_submit: bool
    allow_sitemap_delete: bool
    allowed_site_urls: tuple[str, ...]
    allowed_sitemap_prefixes: tuple[str, ...]
    max_operations_per_request: int
    confirmation_ttl_seconds: int
    confirmation_secret: bytes | None
    previous_confirmation_secret: bytes | None


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise GscSafetyError("INVALID_CONFIGURATION", f"{name} must be an integer.") from exc
    if not minimum <= value <= maximum:
        raise GscSafetyError(
            "INVALID_CONFIGURATION", f"{name} must be between {minimum} and {maximum}."
        )
    return value


def split_csv(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, "").split(",") if item.strip())


def secret_bytes(name: str) -> bytes | None:
    value = os.getenv(name)
    return None if value is None or not value.strip() else value.encode("utf-8")


def canonical_site_url(site_url: str) -> str:
    value = str(site_url or "").strip()
    if not value:
        raise GscSafetyError("INVALID_ARGUMENT", "site_url is required.")
    if value.lower().startswith("sc-domain:"):
        domain = value[len("sc-domain:") :].strip().lower().rstrip(".")
        if not domain or "/" in domain or ":" in domain or not re.fullmatch(r"[a-z0-9.-]+", domain):
            raise GscSafetyError("INVALID_ARGUMENT", "Invalid sc-domain property format.")
        return f"sc-domain:{domain}"

    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise GscSafetyError("INVALID_ARGUMENT", "site_url must be HTTP(S) or sc-domain:domain.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise GscSafetyError(
            "INVALID_ARGUMENT",
            "site_url must not contain credentials, query parameters, or fragments.",
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise GscSafetyError("INVALID_ARGUMENT", "site_url contains an invalid port.") from exc
    host = parsed.hostname.lower()
    if port:
        host = f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", "", ""))


def canonical_sitemap_url(sitemap_url: str) -> str:
    value = str(sitemap_url or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise GscSafetyError("INVALID_ARGUMENT", "sitemap_url must be an absolute HTTP(S) URL.")
    if parsed.username or parsed.password or parsed.fragment:
        raise GscSafetyError(
            "INVALID_ARGUMENT", "sitemap_url must not contain credentials or fragments."
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise GscSafetyError("INVALID_ARGUMENT", "sitemap_url contains an invalid port.") from exc
    host = parsed.hostname.lower()
    if port:
        host = f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", parsed.query, ""))


def load_safety_config() -> SafetyConfig:
    """Read environment state on every call; no configuration cache."""

    return SafetyConfig(
        mutations_enabled=env_bool("GSC_ADMIN_MUTATIONS_ENABLED"),
        allow_site_add=env_bool("GSC_ALLOW_SITE_ADD"),
        allow_site_delete=env_bool("GSC_ALLOW_SITE_DELETE"),
        allow_sitemap_submit=env_bool("GSC_ALLOW_SITEMAP_SUBMIT"),
        allow_sitemap_delete=env_bool("GSC_ALLOW_SITEMAP_DELETE"),
        allowed_site_urls=tuple(canonical_site_url(v) for v in split_csv("GSC_ALLOWED_SITE_URLS")),
        allowed_sitemap_prefixes=tuple(
            canonical_sitemap_url(v) for v in split_csv("GSC_ALLOWED_SITEMAP_PREFIXES")
        ),
        max_operations_per_request=env_int("GSC_MAX_OPERATIONS_PER_REQUEST", 10, 1, 10),
        confirmation_ttl_seconds=env_int("GSC_CONFIRMATION_TTL_SECONDS", 900, 60, 3600),
        confirmation_secret=secret_bytes("GSC_CONFIRMATION_SECRET"),
        previous_confirmation_secret=secret_bytes("GSC_CONFIRMATION_PREVIOUS_SECRET"),
    )


def secret_warnings(secret: bytes | None) -> list[str]:
    if secret is None:
        return ["NOT_CONFIGURED"]
    warnings: list[str] = []
    if len(secret) < MINIMUM_SECRET_BYTES:
        warnings.append("TOO_SHORT")
    if secret.strip() != secret:
        warnings.append("LEADING_OR_TRAILING_WHITESPACE")
    if len(set(secret)) < 8:
        warnings.append("LOW_CHARACTER_DIVERSITY")
    return warnings


def require_confirmation_secret(config: SafetyConfig) -> bytes:
    warnings = secret_warnings(config.confirmation_secret)
    if config.confirmation_secret is None or warnings:
        raise GscSafetyError(
            "INVALID_CONFIGURATION",
            "GSC_CONFIRMATION_SECRET must contain at least 32 strong bytes.",
            {"warnings": warnings},
        )
    return config.confirmation_secret


def key_id(secret: bytes) -> str:
    return hashlib.sha256(secret).hexdigest()[:16]


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def assert_allowed_site(config: SafetyConfig, site_url: str) -> None:
    if not config.allowed_site_urls:
        raise GscSafetyError("EMPTY_ALLOWLIST", "GSC_ALLOWED_SITE_URLS is empty.")
    if site_url not in config.allowed_site_urls:
        raise GscSafetyError(
            "SITE_NOT_ALLOWED",
            "The requested Search Console property is outside the allowlist.",
            {"site_url": site_url},
        )


def assert_allowed_sitemap(config: SafetyConfig, sitemap_url: str) -> None:
    if not config.allowed_sitemap_prefixes:
        raise GscSafetyError("EMPTY_ALLOWLIST", "GSC_ALLOWED_SITEMAP_PREFIXES is empty.")
    if not any(sitemap_url.startswith(prefix) for prefix in config.allowed_sitemap_prefixes):
        raise GscSafetyError(
            "SITEMAP_NOT_ALLOWED",
            "The sitemap URL is outside all configured prefixes.",
            {"sitemap_url": sitemap_url},
        )


def assert_mutation_allowed(config: SafetyConfig, action: str, resource: str) -> None:
    if not config.mutations_enabled:
        raise GscSafetyError("MUTATIONS_DISABLED", "GSC mutations are globally disabled.")
    enabled = {
        ("add", "Site"): config.allow_site_add,
        ("delete", "Site"): config.allow_site_delete,
        ("submit", "Sitemap"): config.allow_sitemap_submit,
        ("delete", "Sitemap"): config.allow_sitemap_delete,
    }.get((action, resource), False)
    if not enabled:
        raise GscSafetyError(
            "ACTION_GATE_DISABLED",
            f"The {resource} {action} gate is disabled.",
            {"resource": resource, "action": action},
        )
