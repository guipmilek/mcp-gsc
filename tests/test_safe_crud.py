from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import MagicMock, patch

from googleapiclient.errors import HttpError

from gsc_safe import GscSafetyError
from gsc_safe import confirmations, google_api
from gsc_safe.tools import (
    gsc_confirmation_diagnostics,
    gsc_create_resource,
    gsc_safety_status,
)

BASE_ENV = {
    "GSC_ADMIN_MUTATIONS_ENABLED": "true",
    "GSC_ALLOW_SITE_ADD": "true",
    "GSC_ALLOW_SITE_DELETE": "true",
    "GSC_ALLOW_SITEMAP_SUBMIT": "true",
    "GSC_ALLOW_SITEMAP_DELETE": "true",
    "GSC_ALLOWED_SITE_URLS": "sc-domain:example.com",
    "GSC_ALLOWED_SITEMAP_PREFIXES": "https://www.example.com/",
    "GSC_MAX_OPERATIONS_PER_REQUEST": "10",
    "GSC_CONFIRMATION_TTL_SECONDS": "900",
    "GSC_CONFIRMATION_SECRET": "0123456789abcdef0123456789abcdef",
}


def run(coro):
    return asyncio.run(coro)


class SafeCrudTests(unittest.TestCase):
    def setUp(self):
        confirmations._CONSUMED_CONFIRMATIONS.clear()

    def test_status_never_returns_secret(self):
        with patch.dict(os.environ, BASE_ENV, clear=True):
            result = run(gsc_safety_status())
        self.assertTrue(result["confirmation_secret_configured"])
        self.assertNotIn(BASE_ENV["GSC_CONFIRMATION_SECRET"], str(result))
        self.assertEqual(result["operation_hash_version"], 3)

    def test_diagnostics_issues_and_verifies_without_replay(self):
        with patch.dict(os.environ, BASE_ENV, clear=True):
            result = run(gsc_confirmation_diagnostics())
        self.assertTrue(result["self_test"]["issued"])
        self.assertTrue(result["self_test"]["verified"])
        self.assertFalse(result["self_test"]["replay_registered"])
        self.assertEqual(confirmations._CONSUMED_CONFIRMATIONS, set())

    def test_site_outside_allowlist_fails_closed(self):
        with patch.dict(os.environ, BASE_ENV, clear=True):
            with self.assertRaises(GscSafetyError) as ctx:
                run(gsc_create_resource("Site", "sc-domain:not-example.com"))
        self.assertEqual(ctx.exception.code, "SITE_NOT_ALLOWED")

    def test_sitemap_prefix_outside_allowlist_fails_closed(self):
        service = MagicMock()
        with patch.dict(os.environ, BASE_ENV, clear=True), patch.object(
            google_api, "service", return_value=service
        ):
            with self.assertRaises(GscSafetyError) as ctx:
                run(
                    gsc_create_resource(
                        "Sitemap",
                        "sc-domain:example.com",
                        {"sitemap_url": "https://evil.example/sitemap.xml"},
                    )
                )
        self.assertEqual(ctx.exception.code, "SITEMAP_NOT_ALLOWED")

    def test_preflight_does_not_call_mutation(self):
        service = MagicMock()
        service.sites().get().execute.side_effect = http_404()
        with patch.dict(os.environ, BASE_ENV, clear=True), patch.object(
            google_api, "service", return_value=service
        ):
            result = run(gsc_create_resource("Site", "sc-domain:example.com"))
        self.assertEqual(result["mode"], "VALIDATE_ONLY")
        self.assertFalse(result["execution_attempted"])
        service.sites().add.assert_not_called()

    def test_create_executes_once_and_verifies(self):
        service = MagicMock()
        service.sites().get().execute.side_effect = [
            http_404(),
            http_404(),
            http_404(),
            {"permissionLevel": "siteOwner"},
        ]
        service.sites().add().execute.return_value = {}
        with patch.dict(os.environ, BASE_ENV, clear=True), patch.object(
            google_api, "service", return_value=service
        ):
            preflight = run(gsc_create_resource("Site", "sc-domain:example.com"))
            result = run(
                gsc_create_resource(
                    "Site",
                    "sc-domain:example.com",
                    validate_only=False,
                    confirmation=preflight["required_confirmation"],
                )
            )
        self.assertEqual(result["execution_status"], "SUCCEEDED")
        self.assertTrue(result["confirmation_verified"])
        self.assertEqual(service.sites().add.call_count, 1)

    def test_replay_is_rejected_before_another_api_read(self):
        service = MagicMock()
        service.sites().get().execute.side_effect = [
            http_404(),
            http_404(),
            http_404(),
            {"permissionLevel": "siteOwner"},
        ]
        service.sites().add().execute.return_value = {}
        with patch.dict(os.environ, BASE_ENV, clear=True), patch.object(
            google_api, "service", return_value=service
        ):
            preflight = run(gsc_create_resource("Site", "sc-domain:example.com"))
            run(
                gsc_create_resource(
                    "Site",
                    "sc-domain:example.com",
                    validate_only=False,
                    confirmation=preflight["required_confirmation"],
                )
            )
            prior_reads = service.sites().get().execute.call_count
            with self.assertRaises(GscSafetyError) as ctx:
                run(
                    gsc_create_resource(
                        "Site",
                        "sc-domain:example.com",
                        validate_only=False,
                        confirmation=preflight["required_confirmation"],
                    )
                )
        self.assertEqual(ctx.exception.code, "CONFIRMATION_REPLAYED")
        self.assertEqual(service.sites().get().execute.call_count, prior_reads)

    def test_key_mismatch_has_specific_error(self):
        service = MagicMock()
        service.sites().get().execute.side_effect = http_404()
        with patch.object(google_api, "service", return_value=service):
            with patch.dict(os.environ, BASE_ENV, clear=True):
                preflight = run(gsc_create_resource("Site", "sc-domain:example.com"))
            changed = dict(BASE_ENV)
            changed["GSC_CONFIRMATION_SECRET"] = "fedcba9876543210fedcba9876543210"
            with patch.dict(os.environ, changed, clear=True):
                with self.assertRaises(GscSafetyError) as ctx:
                    run(
                        gsc_create_resource(
                            "Site",
                            "sc-domain:example.com",
                            validate_only=False,
                            confirmation=preflight["required_confirmation"],
                        )
                    )
        self.assertEqual(ctx.exception.code, "CONFIRMATION_KEY_MISMATCH")

    def test_tampered_signature_is_invalid(self):
        service = MagicMock()
        service.sites().get().execute.side_effect = http_404()
        with patch.dict(os.environ, BASE_ENV, clear=True), patch.object(
            google_api, "service", return_value=service
        ):
            preflight = run(gsc_create_resource("Site", "sc-domain:example.com"))
            token = preflight["required_confirmation"]
            tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
            with self.assertRaises(GscSafetyError) as ctx:
                run(
                    gsc_create_resource(
                        "Site",
                        "sc-domain:example.com",
                        validate_only=False,
                        confirmation=tampered,
                    )
                )
        self.assertEqual(ctx.exception.code, "INVALID_CONFIRMATION")


class Response404:
    status = 404
    reason = "Not Found"


def http_404() -> HttpError:
    return HttpError(Response404(), b'{"error":{"message":"not found"}}')


if __name__ == "__main__":
    unittest.main()
