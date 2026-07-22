"""Versioned HMAC confirmations, diagnostics, rotation, and replay protection."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import (
    CONFIRMATION_TOKEN_VERSION,
    MINIMUM_SECRET_BYTES,
    GscSafetyError,
    SafetyConfig,
    canonical_json,
    key_id,
    require_confirmation_secret,
    secret_warnings,
)

PROCESS_INSTANCE_ID = secrets.token_hex(8)
_REPLAY_LOCK = threading.Lock()
_CONSUMED_CONFIRMATIONS: set[str] = set()


@dataclass(frozen=True)
class VerifiedConfirmation:
    token_fingerprint: str
    key_id: str
    key_source: str
    issued_by_process_instance_id: str


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def confirmation_fingerprint(confirmation: str) -> str:
    return hashlib.sha256(confirmation.encode("utf-8")).hexdigest()[:16]


def assert_not_replayed(confirmation: str) -> None:
    fingerprint = confirmation_fingerprint(confirmation)
    with _REPLAY_LOCK:
        if fingerprint in _CONSUMED_CONFIRMATIONS:
            raise GscSafetyError(
                "CONFIRMATION_REPLAYED", "Confirmation was already consumed in this process."
            )


def issue_confirmation(
    config: SafetyConfig,
    operation_hash: str,
    preconditions_hash: str,
    site_urls: list[str],
) -> tuple[str, datetime, str]:
    secret = require_confirmation_secret(config)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=config.confirmation_ttl_seconds)
    payload = {
        "v": CONFIRMATION_TOKEN_VERSION,
        "verb": "EXECUTE",
        "hash": operation_hash,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "kid": key_id(secret),
        "iid": PROCESS_INSTANCE_ID,
        "nonce": secrets.token_urlsafe(12),
        "pph": preconditions_hash,
        "sites": sorted(set(site_urls)),
    }
    encoded = b64url_encode(canonical_json(payload))
    signature = b64url_encode(hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest())
    return f"EXECUTE {operation_hash}.{encoded}.{signature}", expires, payload["kid"]


def verify_confirmation(
    config: SafetyConfig,
    confirmation: str,
    expected_hash: str,
    expected_preconditions_hash: str,
) -> VerifiedConfirmation:
    if not confirmation:
        raise GscSafetyError("CONFIRMATION_REQUIRED", "A preflight confirmation is required.")
    try:
        prefix_and_payload, signature = confirmation.rsplit(".", 1)
        prefix, encoded = prefix_and_payload.split(".", 1)
        verb, prefix_hash = prefix.split(" ", 1)
        payload = json.loads(b64url_decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise GscSafetyError("INVALID_CONFIRMATION", "Confirmation token is malformed.") from exc

    if verb != "EXECUTE" or prefix_hash != expected_hash or payload.get("hash") != expected_hash:
        raise GscSafetyError("CONFIRMATION_HASH_MISMATCH", "Confirmation does not match this operation.")
    if payload.get("v") != CONFIRMATION_TOKEN_VERSION or payload.get("verb") != "EXECUTE":
        raise GscSafetyError("INVALID_CONFIRMATION", "Unsupported confirmation token version.")
    if payload.get("pph") != expected_preconditions_hash:
        raise GscSafetyError(
            "PRECONDITION_CHANGED", "Resource state changed after the connector preflight."
        )
    if int(payload.get("exp", 0)) < int(datetime.now(timezone.utc).timestamp()):
        raise GscSafetyError("CONFIRMATION_EXPIRED", "Confirmation token has expired.")

    current = require_confirmation_secret(config)
    candidates: list[tuple[str, bytes]] = [("current", current)]
    previous = config.previous_confirmation_secret
    if previous is not None and not secret_warnings(previous):
        candidates.append(("previous", previous))

    token_key_id = str(payload.get("kid") or "")
    matching = [(source, secret) for source, secret in candidates if key_id(secret) == token_key_id]
    if not matching:
        raise GscSafetyError(
            "CONFIRMATION_KEY_MISMATCH",
            "Confirmation was signed with a different configured key.",
            {"confirmation_key_id": token_key_id, "current_confirmation_key_id": key_id(current)},
        )

    source_used: str | None = None
    for source, secret in matching:
        expected_signature = b64url_encode(
            hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        if hmac.compare_digest(signature, expected_signature):
            source_used = source
            break
    if source_used is None:
        raise GscSafetyError("INVALID_CONFIRMATION", "Confirmation signature is invalid.")

    fingerprint = confirmation_fingerprint(confirmation)
    with _REPLAY_LOCK:
        if fingerprint in _CONSUMED_CONFIRMATIONS:
            raise GscSafetyError(
                "CONFIRMATION_REPLAYED", "Confirmation was already consumed in this process."
            )
        _CONSUMED_CONFIRMATIONS.add(fingerprint)

    return VerifiedConfirmation(
        token_fingerprint=fingerprint,
        key_id=token_key_id,
        key_source=source_used,
        issued_by_process_instance_id=str(payload.get("iid") or ""),
    )


def diagnostics(config: SafetyConfig) -> dict[str, Any]:
    current = config.confirmation_secret
    previous = config.previous_confirmation_secret
    current_warnings = secret_warnings(current)
    previous_warnings = secret_warnings(previous) if previous is not None else []
    self_test = {
        "issued": False,
        "verified": False,
        "replay_registered": False,
        "confirmation_key_id": key_id(current) if current else None,
        "confirmation_key_source": None,
        "issued_by_process_instance_id": PROCESS_INSTANCE_ID,
        "verified_by_process_instance_id": None,
    }
    if current is not None and not current_warnings:
        payload = b64url_encode(canonical_json({"test": True, "iid": PROCESS_INSTANCE_ID}))
        signature = b64url_encode(hmac.new(current, payload.encode("ascii"), hashlib.sha256).digest())
        expected = b64url_encode(hmac.new(current, payload.encode("ascii"), hashlib.sha256).digest())
        self_test.update(
            {
                "issued": True,
                "verified": hmac.compare_digest(signature, expected),
                "confirmation_key_source": "current",
                "verified_by_process_instance_id": PROCESS_INSTANCE_ID,
            }
        )
    return {
        "runtime": "PYTHON_FASTMCP_HORIZON",
        "process_instance_id": PROCESS_INSTANCE_ID,
        "confirmation_secret_configured": current is not None,
        "confirmation_secret_minimum_bytes": MINIMUM_SECRET_BYTES,
        "confirmation_key_id": key_id(current) if current else None,
        "confirmation_secret_format_warnings": current_warnings,
        "previous_confirmation_secret_configured": previous is not None,
        "previous_confirmation_secret_valid": previous is not None and not previous_warnings,
        "previous_confirmation_key_id": key_id(previous) if previous else None,
        "previous_confirmation_secret_format_warnings": previous_warnings,
        "confirmation_token_version": CONFIRMATION_TOKEN_VERSION,
        "self_test": self_test,
        "cross_instance_valid": None,
        "cross_instance_requirement": "MATCHING_CONFIRMATION_KEY_ID",
        "supported_rotation": {
            "previous_secret_environment_variable": "GSC_CONFIRMATION_PREVIOUS_SECRET",
            "previous_key_verification_enabled": previous is not None and not previous_warnings,
        },
        "failure_codes": {
            "different_key": "CONFIRMATION_KEY_MISMATCH",
            "corrupted_signature": "INVALID_CONFIRMATION",
            "expired": "CONFIRMATION_EXPIRED",
            "replayed_in_process": "CONFIRMATION_REPLAYED",
        },
    }
