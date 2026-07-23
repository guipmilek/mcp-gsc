"""Versioned HMAC approval codes, diagnostics, rotation, and replay protection."""

from __future__ import annotations

import hashlib
import hmac
import re
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
_CONFIRMATION_PREFIX = f"GSC{CONFIRMATION_TOKEN_VERSION}"
_SIGNATURE_HEX_LENGTH = 32
_REPLAY_LOCK = threading.Lock()
_CONSUMED_CONFIRMATIONS: set[str] = set()


@dataclass(frozen=True)
class VerifiedConfirmation:
    token_fingerprint: str
    key_id: str
    key_source: str
    issued_by_process_instance_id: str


def confirmation_fingerprint(confirmation: str) -> str:
    return hashlib.sha256(confirmation.encode("utf-8")).hexdigest()[:16]


def assert_not_replayed(confirmation: str) -> None:
    fingerprint = confirmation_fingerprint(confirmation)
    with _REPLAY_LOCK:
        if fingerprint in _CONSUMED_CONFIRMATIONS:
            raise GscSafetyError(
                "CONFIRMATION_REPLAYED", "Approval code was already consumed in this process."
            )


def _message(
    operation_hash: str,
    preconditions_hash: str,
    site_urls: list[str],
    expires_at: int,
    nonce: str,
    confirmation_key_id: str,
) -> bytes:
    return canonical_json(
        {
            "v": CONFIRMATION_TOKEN_VERSION,
            "verb": "EXECUTE",
            "hash": operation_hash,
            "pph": preconditions_hash,
            "sites": sorted(set(site_urls)),
            "exp": expires_at,
            "nonce": nonce,
            "kid": confirmation_key_id,
        }
    )


def _signature(
    secret: bytes,
    operation_hash: str,
    preconditions_hash: str,
    site_urls: list[str],
    expires_at: int,
    nonce: str,
    confirmation_key_id: str,
) -> str:
    digest = hmac.new(
        secret,
        _message(
            operation_hash,
            preconditions_hash,
            site_urls,
            expires_at,
            nonce,
            confirmation_key_id,
        ),
        hashlib.sha256,
    ).hexdigest()
    return digest[:_SIGNATURE_HEX_LENGTH]


def issue_confirmation(
    config: SafetyConfig,
    operation_hash: str,
    preconditions_hash: str,
    site_urls: list[str],
) -> tuple[str, datetime, str]:
    """Issue a compact non-JWT approval code bound to operation and preconditions."""

    secret = require_confirmation_secret(config)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=config.confirmation_ttl_seconds)
    expires_at = int(expires.timestamp())
    confirmation_key_id = key_id(secret)
    nonce = secrets.token_hex(6)
    signature = _signature(
        secret,
        operation_hash,
        preconditions_hash,
        site_urls,
        expires_at,
        nonce,
        confirmation_key_id,
    )
    code = f"{_CONFIRMATION_PREFIX}-{confirmation_key_id}-{expires_at}-{nonce}-{signature}"
    return code, expires, confirmation_key_id


def _parse_confirmation(confirmation: str) -> tuple[str, int, str, str]:
    parts = confirmation.split("-")
    if len(parts) != 5 or parts[0] != _CONFIRMATION_PREFIX:
        raise GscSafetyError("INVALID_CONFIRMATION", "Approval code is malformed.")

    _, confirmation_key_id, expires_raw, nonce, signature = parts
    if not re.fullmatch(r"[0-9a-f]{16}", confirmation_key_id):
        raise GscSafetyError("INVALID_CONFIRMATION", "Approval code key identifier is malformed.")
    if not re.fullmatch(r"[0-9]{10,12}", expires_raw):
        raise GscSafetyError("INVALID_CONFIRMATION", "Approval code expiry is malformed.")
    if not re.fullmatch(r"[0-9a-f]{12}", nonce):
        raise GscSafetyError("INVALID_CONFIRMATION", "Approval code nonce is malformed.")
    if not re.fullmatch(rf"[0-9a-f]{{{_SIGNATURE_HEX_LENGTH}}}", signature):
        raise GscSafetyError("INVALID_CONFIRMATION", "Approval code signature is malformed.")
    return confirmation_key_id, int(expires_raw), nonce, signature


def verify_confirmation(
    config: SafetyConfig,
    confirmation: str,
    expected_hash: str,
    expected_preconditions_hash: str,
    expected_site_urls: list[str],
) -> VerifiedConfirmation:
    if not confirmation:
        raise GscSafetyError("CONFIRMATION_REQUIRED", "A preflight approval code is required.")

    confirmation_key_id, expires_at, nonce, signature = _parse_confirmation(confirmation)
    if expires_at < int(datetime.now(timezone.utc).timestamp()):
        raise GscSafetyError("CONFIRMATION_EXPIRED", "Approval code has expired.")

    current = require_confirmation_secret(config)
    candidates: list[tuple[str, bytes]] = [("current", current)]
    previous = config.previous_confirmation_secret
    if previous is not None and not secret_warnings(previous):
        candidates.append(("previous", previous))

    matching = [
        (source, candidate_secret)
        for source, candidate_secret in candidates
        if key_id(candidate_secret) == confirmation_key_id
    ]
    if not matching:
        raise GscSafetyError(
            "CONFIRMATION_KEY_MISMATCH",
            "Approval code was signed with a different configured key.",
            {
                "confirmation_key_id": confirmation_key_id,
                "current_confirmation_key_id": key_id(current),
            },
        )

    source_used: str | None = None
    for source, candidate_secret in matching:
        expected_signature = _signature(
            candidate_secret,
            expected_hash,
            expected_preconditions_hash,
            expected_site_urls,
            expires_at,
            nonce,
            confirmation_key_id,
        )
        if hmac.compare_digest(signature, expected_signature):
            source_used = source
            break
    if source_used is None:
        raise GscSafetyError(
            "INVALID_CONFIRMATION",
            "Approval code does not match this operation and its current preconditions.",
        )

    fingerprint = confirmation_fingerprint(confirmation)
    with _REPLAY_LOCK:
        if fingerprint in _CONSUMED_CONFIRMATIONS:
            raise GscSafetyError(
                "CONFIRMATION_REPLAYED", "Approval code was already consumed in this process."
            )
        _CONSUMED_CONFIRMATIONS.add(fingerprint)

    return VerifiedConfirmation(
        token_fingerprint=fingerprint,
        key_id=confirmation_key_id,
        key_source=source_used,
        issued_by_process_instance_id="STATELESS_CODE",
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
        test_expiry = int((datetime.now(timezone.utc) + timedelta(minutes=1)).timestamp())
        test_nonce = "0123456789ab"
        test_key_id = key_id(current)
        signature = _signature(
            current,
            "0" * 32,
            "0" * 64,
            ["sc-domain:self-test.invalid"],
            test_expiry,
            test_nonce,
            test_key_id,
        )
        expected = _signature(
            current,
            "0" * 32,
            "0" * 64,
            ["sc-domain:self-test.invalid"],
            test_expiry,
            test_nonce,
            test_key_id,
        )
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
        "confirmation_format": "SHORT_HMAC_APPROVAL_CODE",
        "self_test": self_test,
        "cross_instance_valid": True,
        "cross_instance_requirement": "MATCHING_CONFIRMATION_KEY_ID",
        "replay_protection": "BEST_EFFORT_PROCESS_LOCAL",
        "globally_single_use": False,
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
