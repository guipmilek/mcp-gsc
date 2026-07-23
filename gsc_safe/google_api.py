"""Minimal Google Search Console API adapter used by direct CRUD."""

from __future__ import annotations

from typing import Any, Mapping

from googleapiclient.errors import HttpError

from gsc_server import get_gsc_service

from .config import GscSafetyError


def service() -> Any:
    return get_gsc_service()


def http_status(exc: BaseException) -> int | None:
    return getattr(getattr(exc, "resp", None), "status", None)


def read_site(
    client: Any, site_url: str, *, allow_missing: bool
) -> dict[str, Any] | None:
    try:
        response = client.sites().get(siteUrl=site_url).execute()
        return {
            "site_url": site_url,
            "permission_level": response.get("permissionLevel"),
        }
    except HttpError as exc:
        if allow_missing and http_status(exc) == 404:
            return None
        raise


def list_sites(client: Any) -> list[dict[str, Any]]:
    response = client.sites().list().execute()
    return [
        {
            "site_url": item.get("siteUrl"),
            "permission_level": item.get("permissionLevel"),
        }
        for item in response.get("siteEntry", [])
    ]


def read_sitemap(
    client: Any, site_url: str, sitemap_url: str, *, allow_missing: bool
) -> dict[str, Any] | None:
    try:
        response = (
            client.sitemaps()
            .get(siteUrl=site_url, feedpath=sitemap_url)
            .execute()
        )
        return {
            "site_url": site_url,
            "sitemap_url": response.get("path", sitemap_url),
            "last_submitted": response.get("lastSubmitted"),
            "last_downloaded": response.get("lastDownloaded"),
            "is_pending": response.get("isPending"),
            "errors": int(response.get("errors", 0)),
            "warnings": int(response.get("warnings", 0)),
        }
    except HttpError as exc:
        if allow_missing and http_status(exc) == 404:
            return None
        raise


def list_sitemaps(client: Any, site_url: str) -> list[dict[str, Any]]:
    response = client.sitemaps().list(siteUrl=site_url).execute()
    return [
        {
            "site_url": site_url,
            "sitemap_url": item.get("path"),
            "last_submitted": item.get("lastSubmitted"),
            "last_downloaded": item.get("lastDownloaded"),
            "is_pending": item.get("isPending"),
            "errors": int(item.get("errors", 0)),
            "warnings": int(item.get("warnings", 0)),
        }
        for item in response.get("sitemap", [])
    ]


def precondition_state(
    client: Any, operation: Mapping[str, Any]
) -> dict[str, Any] | None:
    if operation["resource"] == "Site":
        return read_site(client, operation["site_url"], allow_missing=True)

    return read_sitemap(
        client,
        operation["site_url"],
        operation["resource_name"],
        allow_missing=True,
    )


def execute(client: Any, operation: Mapping[str, Any]) -> Any:
    resource, action = operation["resource"], operation["action"]
    if resource == "Site" and action == "add":
        return client.sites().add(siteUrl=operation["site_url"]).execute()
    if resource == "Site" and action == "delete":
        return client.sites().delete(siteUrl=operation["site_url"]).execute()
    if resource == "Sitemap" and action == "submit":
        return (
            client.sitemaps()
            .submit(
                siteUrl=operation["site_url"],
                feedpath=operation["resource_name"],
            )
            .execute()
        )
    if resource == "Sitemap" and action == "delete":
        return (
            client.sitemaps()
            .delete(
                siteUrl=operation["site_url"],
                feedpath=operation["resource_name"],
            )
            .execute()
        )
    raise GscSafetyError(
        "UNSUPPORTED_ACTION", "Unsupported operation reached execution."
    )


def verify(client: Any, operation: Mapping[str, Any]) -> tuple[str, Any]:
    if operation["resource"] == "Site":
        observation = read_site(
            client, operation["site_url"], allow_missing=True
        )
    else:
        observation = read_sitemap(
            client,
            operation["site_url"],
            operation["resource_name"],
            allow_missing=True,
        )
    expected_present = operation["action"] in {"add", "submit"}
    return (
        (
            "VERIFIED"
            if (observation is not None) == expected_present
            else "FAILED"
        ),
        observation,
    )
