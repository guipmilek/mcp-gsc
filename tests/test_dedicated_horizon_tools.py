from __future__ import annotations

import asyncio
import inspect
import os
import unittest
from unittest.mock import MagicMock, patch

from googleapiclient.errors import HttpError
from httplib2 import Response

from gsc_safe import GscSafetyError
from gsc_safe import confirmations, google_api
from gsc_safe.tools import (
    gsc_execute_sitemap_delete,
    gsc_execute_sitemap_submit,
    gsc_prepare_sitemap_delete,
    gsc_prepare_sitemap_submit,
)
from horizon_server import (
    _ADDITIVE_WRITE_TOOLS,
    _DESTRUCTIVE_WRITE_TOOLS,
    _PREPARE_TOOLS,
    _tool_annotations,
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
SITE_URL = "sc-domain:example.com"
SITEMAP_URL = "https://www.example.com/mcp-test.xml"


def run(coro):
    return asyncio.run(coro)


def http_404() -> HttpError:
    response = Response({"status": "404", "reason": "Not Found"})
    return HttpError(response, b'{"error":{"message":"not found"}}')


class DedicatedToolTests(unittest.TestCase):
    def setUp(self):
        confirmations._CONSUMED_CONFIRMATIONS.clear()

    def test_public_signatures_do_not_expose_generic_switches(self):
        self.assertEqual(
            list(inspect.signature(gsc_prepare_sitemap_submit).parameters),
            ["site_url", "sitemap_url"],
        )
        self.assertEqual(
            list(inspect.signature(gsc_execute_sitemap_submit).parameters),
            ["site_url", "sitemap_url", "approval_code"],
        )
        self.assertEqual(
            list(inspect.signature(gsc_prepare_sitemap_delete).parameters),
            ["site_url", "sitemap_url"],
        )
        self.assertEqual(
            list(inspect.signature(gsc_execute_sitemap_delete).parameters),
            ["site_url", "sitemap_url", "approval_code"],
        )

    def test_prepare_sitemap_submit_never_calls_mutation(self):
        service = MagicMock()
        service.sitemaps().get().execute.side_effect = http_404()
        with patch.dict(os.environ, BASE_ENV, clear=True), patch.object(
            google_api, "service", return_value=service
        ):
            result = run(gsc_prepare_sitemap_submit(SITE_URL, SITEMAP_URL))
        self.assertEqual(result["mode"], "VALIDATE_ONLY")
        self.assertFalse(result["execution_attempted"])
        self.assertTrue(result["required_approval_code"].startswith("GSC3-"))
        self.assertNotIn(".", result["required_approval_code"])
        service.sitemaps().submit.assert_not_called()

    def test_execute_rejects_missing_approval_code_before_api_read(self):
        service = MagicMock()
        with patch.dict(os.environ, BASE_ENV, clear=True), patch.object(
            google_api, "service", return_value=service
        ):
            with self.assertRaises(GscSafetyError) as ctx:
                run(gsc_execute_sitemap_submit(SITE_URL, SITEMAP_URL, ""))
        self.assertEqual(ctx.exception.code, "CONFIRMATION_REQUIRED")
        service.sitemaps.assert_not_called()

    def test_dedicated_sitemap_submit_executes_once_and_verifies(self):
        service = MagicMock()
        service.sitemaps().get().execute.side_effect = [
            http_404(),
            http_404(),
            http_404(),
            {"path": SITEMAP_URL, "errors": 0, "warnings": 0},
        ]
        submit_request = MagicMock()
        submit_request.execute.return_value = {}
        service.sitemaps().submit.return_value = submit_request
        with patch.dict(os.environ, BASE_ENV, clear=True), patch.object(
            google_api, "service", return_value=service
        ):
            preflight = run(gsc_prepare_sitemap_submit(SITE_URL, SITEMAP_URL))
            result = run(
                gsc_execute_sitemap_submit(
                    SITE_URL,
                    SITEMAP_URL,
                    preflight["required_approval_code"],
                )
            )
        self.assertEqual(result["execution_status"], "SUCCEEDED")
        self.assertTrue(result["confirmation_verified"])
        self.assertTrue(result["approval_code_verified"])
        service.sitemaps().submit.assert_called_once_with(
            siteUrl=SITE_URL,
            feedpath=SITEMAP_URL,
        )
        submit_request.execute.assert_called_once_with()

    def test_dedicated_sitemap_delete_executes_once_and_verifies(self):
        service = MagicMock()
        existing = {"path": SITEMAP_URL, "errors": 0, "warnings": 0}
        service.sitemaps().get().execute.side_effect = [
            existing,
            existing,
            existing,
            http_404(),
        ]
        delete_request = MagicMock()
        delete_request.execute.return_value = {}
        service.sitemaps().delete.return_value = delete_request
        with patch.dict(os.environ, BASE_ENV, clear=True), patch.object(
            google_api, "service", return_value=service
        ):
            preflight = run(gsc_prepare_sitemap_delete(SITE_URL, SITEMAP_URL))
            result = run(
                gsc_execute_sitemap_delete(
                    SITE_URL,
                    SITEMAP_URL,
                    preflight["required_approval_code"],
                )
            )
        self.assertEqual(result["execution_status"], "SUCCEEDED")
        self.assertTrue(result["confirmation_verified"])
        self.assertTrue(result["approval_code_verified"])
        service.sitemaps().delete.assert_called_once_with(
            siteUrl=SITE_URL,
            feedpath=SITEMAP_URL,
        )
        delete_request.execute.assert_called_once_with()


class HorizonManifestTests(unittest.TestCase):
    def test_horizon_exposes_only_dedicated_mutation_tools(self):
        prepare_names = {function.__name__ for function, _ in _PREPARE_TOOLS}
        additive_names = {function.__name__ for function, _ in _ADDITIVE_WRITE_TOOLS}
        destructive_names = {function.__name__ for function, _ in _DESTRUCTIVE_WRITE_TOOLS}
        exposed = prepare_names | additive_names | destructive_names

        self.assertEqual(
            prepare_names,
            {
                "gsc_prepare_site_add",
                "gsc_prepare_site_delete",
                "gsc_prepare_sitemap_submit",
                "gsc_prepare_sitemap_delete",
            },
        )
        self.assertEqual(
            additive_names,
            {"gsc_execute_site_add", "gsc_execute_sitemap_submit"},
        )
        self.assertEqual(
            destructive_names,
            {"gsc_execute_site_delete", "gsc_execute_sitemap_delete"},
        )
        self.assertNotIn("gsc_create_resource", exposed)
        self.assertNotIn("gsc_delete_resource", exposed)
        self.assertNotIn("gsc_batch_operations", exposed)

    def test_annotations_are_explicit_and_action_specific(self):
        prepare = _tool_annotations(
            "Prepare",
            read_only=True,
            destructive=False,
            idempotent=True,
            open_world=False,
        )
        additive = _tool_annotations(
            "Add",
            read_only=False,
            destructive=False,
            idempotent=True,
            open_world=False,
        )
        destructive = _tool_annotations(
            "Delete",
            read_only=False,
            destructive=True,
            idempotent=True,
            open_world=False,
        )

        self.assertTrue(prepare.readOnlyHint)
        self.assertFalse(prepare.destructiveHint)
        self.assertTrue(prepare.idempotentHint)
        self.assertFalse(prepare.openWorldHint)

        self.assertFalse(additive.readOnlyHint)
        self.assertFalse(additive.destructiveHint)
        self.assertTrue(additive.idempotentHint)
        self.assertFalse(additive.openWorldHint)

        self.assertFalse(destructive.readOnlyHint)
        self.assertTrue(destructive.destructiveHint)
        self.assertTrue(destructive.idempotentHint)
        self.assertFalse(destructive.openWorldHint)


if __name__ == "__main__":
    unittest.main()
