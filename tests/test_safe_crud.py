from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import MagicMock, patch

from googleapiclient.errors import HttpError
from httplib2 import Response

from gsc_safe import GscSafetyError, google_api
from gsc_safe.tools import (
    gsc_add_site,
    gsc_batch_operations,
    gsc_crud_status,
    gsc_delete_site,
    gsc_delete_sitemap,
    gsc_submit_sitemap,
)

BASE_ENV = {
    "GSC_ALLOWED_SITE_URLS": "sc-domain:example.com",
    "GSC_ALLOWED_SITEMAP_PREFIXES": "https://www.example.com/",
    "GSC_MAX_OPERATIONS_PER_REQUEST": "10",
}
SITE_URL = "sc-domain:example.com"
SITEMAP_URL = "https://www.example.com/mcp-test.xml"


def run(coro):
    return asyncio.run(coro)


def http_404() -> HttpError:
    response = Response({"status": "404", "reason": "Not Found"})
    return HttpError(response, b'{"error":{"message":"not found"}}')


class DirectCrudTests(unittest.TestCase):
    def test_status_reports_direct_contract_without_gate_state(self):
        legacy = {
            **BASE_ENV,
            "GSC_ADMIN_MUTATIONS_ENABLED": "false",
            "GSC_ALLOW_SITE_DELETE": "false",
            "GSC_CONFIRMATION_SECRET": "obsolete-secret",
        }
        with patch.dict(os.environ, legacy, clear=True):
            result = run(gsc_crud_status())
        self.assertEqual(result["contract_version"], "direct-crud-v1")
        self.assertEqual(result["write_mode"], "DIRECT")
        self.assertFalse(result["approval_workflow"])
        self.assertNotIn("gates", result)
        self.assertNotIn("confirmation", str(result).lower())

    def test_site_outside_allowlist_fails_before_google_client(self):
        with (
            patch.dict(os.environ, BASE_ENV, clear=True),
            patch.object(google_api, "service") as service,
        ):
            with self.assertRaises(GscSafetyError) as ctx:
                run(gsc_add_site("sc-domain:not-example.com"))
        self.assertEqual(ctx.exception.code, "SITE_NOT_ALLOWED")
        service.assert_called_once()
        service.return_value.sites.assert_not_called()

    def test_sitemap_prefix_outside_allowlist_fails_before_api_read(self):
        with (
            patch.dict(os.environ, BASE_ENV, clear=True),
            patch.object(google_api, "service") as service,
        ):
            with self.assertRaises(GscSafetyError) as ctx:
                run(
                    gsc_submit_sitemap(
                        SITE_URL, "https://evil.example/sitemap.xml"
                    )
                )
        self.assertEqual(ctx.exception.code, "SITEMAP_NOT_ALLOWED")
        service.return_value.sitemaps.assert_not_called()

    def test_dry_run_reads_precondition_and_never_mutates(self):
        service = MagicMock()
        sites = service.sites.return_value
        sites.get.return_value.execute.side_effect = http_404()
        with (
            patch.dict(os.environ, BASE_ENV, clear=True),
            patch.object(google_api, "service", return_value=service),
        ):
            result = run(gsc_add_site(SITE_URL, dry_run=True))
        self.assertEqual(result["mode"], "DRY_RUN")
        self.assertEqual(result["execution_status"], "NOT_EXECUTED")
        self.assertFalse(result["execution_attempted"])
        self.assertNotIn("approval", str(result).lower())
        self.assertNotIn("confirmation", str(result).lower())
        sites.add.assert_not_called()

    def test_site_add_executes_directly_and_verifies(self):
        service = MagicMock()
        sites = service.sites.return_value
        sites.get.return_value.execute.side_effect = [
            http_404(),
            {"permissionLevel": "siteOwner"},
        ]
        sites.add.return_value.execute.return_value = {}
        with (
            patch.dict(os.environ, BASE_ENV, clear=True),
            patch.object(google_api, "service", return_value=service),
        ):
            result = run(gsc_add_site(SITE_URL))
        self.assertEqual(result["execution_status"], "SUCCEEDED")
        self.assertTrue(result["execution_attempted"])
        sites.add.assert_called_once_with(siteUrl=SITE_URL)
        sites.add.return_value.execute.assert_called_once_with()

    def test_site_add_is_idempotent_when_already_present(self):
        service = MagicMock()
        sites = service.sites.return_value
        sites.get.return_value.execute.return_value = {
            "permissionLevel": "siteOwner"
        }
        with (
            patch.dict(os.environ, BASE_ENV, clear=True),
            patch.object(google_api, "service", return_value=service),
        ):
            result = run(gsc_add_site(SITE_URL))
        self.assertEqual(result["execution_status"], "SUCCEEDED")
        self.assertEqual(result["results"][0]["outcome"], "ALREADY_PRESENT")
        self.assertFalse(result["execution_attempted"])
        sites.add.assert_not_called()

    def test_site_delete_executes_directly_and_verifies(self):
        service = MagicMock()
        sites = service.sites.return_value
        sites.get.return_value.execute.side_effect = [
            {"permissionLevel": "siteOwner"},
            http_404(),
        ]
        sites.delete.return_value.execute.return_value = {}
        with (
            patch.dict(os.environ, BASE_ENV, clear=True),
            patch.object(google_api, "service", return_value=service),
        ):
            result = run(gsc_delete_site(SITE_URL))
        self.assertEqual(result["execution_status"], "SUCCEEDED")
        sites.delete.assert_called_once_with(siteUrl=SITE_URL)
        sites.delete.return_value.execute.assert_called_once_with()

    def test_delete_is_idempotent_when_resource_is_absent(self):
        service = MagicMock()
        sitemaps = service.sitemaps.return_value
        sitemaps.get.return_value.execute.side_effect = http_404()
        with (
            patch.dict(os.environ, BASE_ENV, clear=True),
            patch.object(google_api, "service", return_value=service),
        ):
            result = run(gsc_delete_sitemap(SITE_URL, SITEMAP_URL))
        self.assertEqual(result["execution_status"], "SUCCEEDED")
        self.assertEqual(result["results"][0]["outcome"], "ALREADY_ABSENT")
        self.assertFalse(result["execution_attempted"])
        sitemaps.delete.assert_not_called()

    def test_sitemap_submit_and_delete_are_direct(self):
        submit_service = MagicMock()
        submit_sitemaps = submit_service.sitemaps.return_value
        submit_sitemaps.get.return_value.execute.side_effect = [
            http_404(),
            {"path": SITEMAP_URL, "errors": 0, "warnings": 0},
        ]
        submit_sitemaps.submit.return_value.execute.return_value = {}
        with (
            patch.dict(os.environ, BASE_ENV, clear=True),
            patch.object(google_api, "service", return_value=submit_service),
        ):
            submitted = run(gsc_submit_sitemap(SITE_URL, SITEMAP_URL))
        self.assertEqual(submitted["execution_status"], "SUCCEEDED")
        submit_sitemaps.submit.assert_called_once_with(
            siteUrl=SITE_URL, feedpath=SITEMAP_URL
        )

        delete_service = MagicMock()
        delete_sitemaps = delete_service.sitemaps.return_value
        delete_sitemaps.get.return_value.execute.side_effect = [
            {"path": SITEMAP_URL, "errors": 0, "warnings": 0},
            http_404(),
        ]
        delete_sitemaps.delete.return_value.execute.return_value = {}
        with (
            patch.dict(os.environ, BASE_ENV, clear=True),
            patch.object(google_api, "service", return_value=delete_service),
        ):
            deleted = run(gsc_delete_sitemap(SITE_URL, SITEMAP_URL))
        self.assertEqual(deleted["execution_status"], "SUCCEEDED")
        delete_sitemaps.delete.assert_called_once_with(
            siteUrl=SITE_URL, feedpath=SITEMAP_URL
        )

    def test_batch_stops_after_first_failed_operation(self):
        service = MagicMock()
        sites = service.sites.return_value
        sitemaps = service.sitemaps.return_value
        sites.get.return_value.execute.side_effect = http_404()
        sitemaps.get.return_value.execute.side_effect = http_404()
        sites.add.return_value.execute.side_effect = RuntimeError("network")
        with (
            patch.dict(os.environ, BASE_ENV, clear=True),
            patch.object(google_api, "service", return_value=service),
        ):
            result = run(
                gsc_batch_operations(
                    [
                        {
                            "action": "add",
                            "resource": "Site",
                            "site_url": SITE_URL,
                        },
                        {
                            "action": "submit",
                            "resource": "Sitemap",
                            "site_url": SITE_URL,
                            "data": {"sitemap_url": SITEMAP_URL},
                        },
                    ]
                )
            )
        self.assertEqual(result["execution_status"], "FAILED")
        self.assertEqual(result["operations_attempted"], 1)
        self.assertEqual(result["operations_not_attempted"], 1)


if __name__ == "__main__":
    unittest.main()
