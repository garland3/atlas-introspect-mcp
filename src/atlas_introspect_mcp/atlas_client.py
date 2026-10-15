"""Thin HTTP client for the running ATLAS instance.

Everything conversation-, workspace-, and prompt-shaped goes through ATLAS's
own REST API rather than the DuckDB file underneath it. That is deliberate:
DuckDB is single-writer, the ATLAS process holds the write lock for its whole
lifetime, and a second process opening the same file — even read-only — fails
with a lock conflict. The API is also the layer that enforces per-user scoping,
so going through it keeps this server honest about ownership.

Only ``mcp.json`` is touched on disk (see ``mcp_config``); ATLAS has no API for
editing it.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import get_config


class AtlasError(RuntimeError):
    """An ATLAS call failed in a way worth showing the model verbatim."""


def _headers() -> dict[str, str]:
    cfg = get_config()
    headers = {cfg.auth_header: cfg.user_email, "Accept": "application/json"}
    # Outside DEBUG_MODE, ATLAS's proxy-secret gate rejects any request that
    # carries neither a browser session nor this header. Omitted entirely when
    # unset so a debug-mode instance is unaffected.
    if cfg.proxy_secret:
        headers[cfg.proxy_secret_header] = cfg.proxy_secret
    return headers


async def request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: Any | None = None,
) -> Any:
    """Call ATLAS and return the decoded JSON body.

    Raises ``AtlasError`` with an actionable message on transport failure or a
    non-2xx status; ATLAS's own ``detail`` string is preserved when present.
    """
    cfg = get_config()
    url = f"{cfg.base_url}{path}"
    try:
        async with httpx.AsyncClient(timeout=cfg.timeout) as client:
            response = await client.request(
                method, url, params=params, json=json, headers=_headers()
            )
    except httpx.HTTPError as exc:
        raise AtlasError(
            f"Could not reach ATLAS at {cfg.base_url} ({exc.__class__.__name__}: {exc}). "
            "Check `systemctl --user status atlas`."
        ) from exc

    if response.status_code >= 400:
        detail = _detail(response)
        if response.status_code == 404 and "Feature not enabled" in detail:
            raise AtlasError(
                f"{path} is disabled in this ATLAS instance "
                "(see the FEATURE_* flags in atlas-ui-3/.env)."
            )
        raise AtlasError(f"ATLAS returned {response.status_code} for {method} {path}: {detail}")

    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise AtlasError(f"ATLAS returned non-JSON for {method} {path}") from exc


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return str(body)[:500]


async def get(path: str, **params: Any) -> Any:
    clean = {k: v for k, v in params.items() if v is not None}
    return await request("GET", path, params=clean or None)


async def post(path: str, json: Any | None = None) -> Any:
    return await request("POST", path, json=json)


async def put(path: str, json: Any | None = None) -> Any:
    return await request("PUT", path, json=json)


async def delete(path: str) -> Any:
    return await request("DELETE", path)
