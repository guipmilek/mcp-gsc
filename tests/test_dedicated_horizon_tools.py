from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import horizon_server
from gsc_safe.tools import (
    gsc_add_site,
    gsc_batch_operations,
    gsc_delete_site,
    gsc_delete_sitemap,
    gsc_submit_sitemap,
)
from horizon_server import (
    _ADDITIVE_WRITE_TOOLS,
    _DESTRUCTIVE_WRITE_TOOLS,
    _tool_annotations,
    _with_structured_errors,
)


class DirectToolContractTests(unittest.TestCase):
    def test_shared_credential_envelope_materializes_adc(self):
        credentials = {"type": "service_account", "project_id": "test"}
        encoded = base64.b64encode(
            json.dumps({"google_credentials": credentials}).encode()
        ).decode()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "adc.json"
            with (
                patch.dict(os.environ, {"MCP_CREDENTIALS": encoded}, clear=True),
                patch.object(horizon_server, "_ADC_PATH", target),
            ):
                configured = horizon_server._configure_deployment_credentials()
                self.assertEqual(target, configured)
                self.assertEqual(credentials, json.loads(target.read_text()))

    def test_raw_credentials_materialize_adc(self):
        credentials = {"type": "service_account", "project_id": "test"}
        encoded = base64.b64encode(json.dumps(credentials).encode()).decode()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "adc.json"
            with (
                patch.dict(os.environ, {"MCP_CREDENTIALS": encoded}, clear=True),
                patch.object(horizon_server, "_ADC_PATH", target),
            ):
                configured = horizon_server._configure_deployment_credentials()
                self.assertEqual(target, configured)
                self.assertEqual(credentials, json.loads(target.read_text()))

    def test_public_signatures_have_only_direct_inputs(self):
        self.assertEqual(
            list(inspect.signature(gsc_add_site).parameters),
            ["site_url", "dry_run"],
        )
        self.assertEqual(
            list(inspect.signature(gsc_delete_site).parameters),
            ["site_url", "dry_run"],
        )
        self.assertEqual(
            list(inspect.signature(gsc_submit_sitemap).parameters),
            ["site_url", "sitemap_url", "dry_run"],
        )
        self.assertEqual(
            list(inspect.signature(gsc_delete_sitemap).parameters),
            ["site_url", "sitemap_url", "dry_run"],
        )
        self.assertEqual(
            list(inspect.signature(gsc_batch_operations).parameters),
            ["operations", "dry_run"],
        )

    def test_horizon_exposes_direct_tools_without_prepare_execute_pairs(self):
        additive = {function.__name__ for function, _ in _ADDITIVE_WRITE_TOOLS}
        destructive = {function.__name__ for function, _ in _DESTRUCTIVE_WRITE_TOOLS}
        exposed = additive | destructive
        self.assertEqual(additive, {"gsc_add_site", "gsc_submit_sitemap"})
        self.assertEqual(
            destructive,
            {
                "gsc_delete_site",
                "gsc_delete_sitemap",
                "gsc_batch_operations",
            },
        )
        self.assertFalse(any("prepare" in name for name in exposed))
        self.assertFalse(any("execute" in name for name in exposed))
        self.assertFalse(any("confirmation" in name for name in exposed))

    def test_annotations_are_explicit_and_truthful(self):
        additive = _tool_annotations(
            "Add",
            read_only=False,
            destructive=False,
            idempotent=True,
            open_world=True,
        )
        destructive = _tool_annotations(
            "Delete",
            read_only=False,
            destructive=True,
            idempotent=True,
            open_world=True,
        )
        self.assertFalse(additive.readOnlyHint)
        self.assertFalse(additive.destructiveHint)
        self.assertTrue(additive.idempotentHint)
        self.assertTrue(additive.openWorldHint)
        self.assertFalse(destructive.readOnlyHint)
        self.assertTrue(destructive.destructiveHint)
        self.assertTrue(destructive.idempotentHint)
        self.assertTrue(destructive.openWorldHint)

    def test_horizon_decodes_legacy_json_objects(self):
        async def legacy_json():
            return json.dumps({"count": 1, "properties": [{"site_url": "x"}]})

        result = asyncio.run(_with_structured_errors(legacy_json)())

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["properties"][0]["site_url"], "x")

    def test_horizon_preserves_plain_text_and_json_scalars(self):
        async def plain_text():
            return "No Search Console properties found."

        async def json_scalar():
            return json.dumps("Error")

        self.assertEqual(
            asyncio.run(_with_structured_errors(plain_text)()),
            "No Search Console properties found.",
        )
        self.assertEqual(asyncio.run(_with_structured_errors(json_scalar)()), '"Error"')


if __name__ == "__main__":
    unittest.main()
