"""Local-only token exchange utility. Tokens never enter MCP arguments or responses."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import time
from pathlib import Path

import httpx

from .api import BASE_URL, unwrap
from .core import DouyinError, Settings


def save_private(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(data, output)
    os.replace(temp, path)
    os.chmod(path, 0o600)


async def token_request(operation: str, settings: Settings, code: str = "", transport=None) -> dict:
    if not settings.client_key:
        raise DouyinError("client_key_required", "Set DOUYIN_CLIENT_KEY.")
    body = {"client_key": settings.client_key}
    previous = {}
    if operation == "refresh":
        path = settings.state_dir / "credentials.json"
        previous = json.loads(path.read_text("utf-8"))
        settings.check_cached_identity(previous)
        if not previous.get("refresh_token"):
            raise DouyinError("refresh_token_required", "Exchange a user authorization code first.")
        body.update(grant_type="refresh_token", refresh_token=previous["refresh_token"])
        endpoint = "refresh_token"
    else:
        if not settings.client_secret:
            raise DouyinError("client_secret_required", "Set DOUYIN_CLIENT_SECRET.")
        body["client_secret"] = settings.client_secret
        if operation == "client":
            body["grant_type"] = "client_credential"
            endpoint = "client_token"
        else:
            if not code.strip():
                raise DouyinError(
                    "authorization_code_required", "Provide a fresh official authorization code/ticket."
                )
            body.update(grant_type="authorization_code", code=code.strip())
            endpoint = "access_token"
    try:
        async with httpx.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=False, timeout=30
        ) as client:
            response = await client.post(
                f"{BASE_URL}/oauth/{endpoint}/",
                **({"json": body} if operation == "client" else {"data": body}),
            )
            if response.status_code != 200:
                raise DouyinError(
                    "token_http_error",
                    "Token exchange failed; redirects are not followed.",
                    status=response.status_code,
                )
            if len(response.content) > 100_000:
                raise DouyinError("token_response_too_large", "Token response exceeds the size limit.")
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError
            result = unwrap(payload)
    except (httpx.HTTPError, ValueError):
        raise DouyinError(
            "token_exchange_failed",
            "Token exchange failed; check connectivity and official app configuration.",
        ) from None
    if not result.get("access_token"):
        raise DouyinError("token_exchange_failed", "No access token returned; no credentials were saved.")
    result = (
        previous
        | result
        | {"saved_at": int(time.time()), "client_key": settings.client_key, "app_type": settings.app_type}
    )
    path = settings.state_dir / ("client-credentials.json" if operation == "client" else "credentials.json")
    save_private(path, result)
    return {"ok": True, "saved": True, "expires_in": result.get("expires_in"), "restart_mcp": True}


def main():
    parser = argparse.ArgumentParser(
        description="Exchange existing official authorization grants locally; no tokens are printed."
    )
    parser.add_argument("operation", choices=["exchange", "refresh", "client"])
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
        code = (
            getpass.getpass("Official authorization code/ticket (hidden): ")
            if args.operation == "exchange"
            else ""
        )
        result = asyncio.run(token_request(args.operation, settings, code))
        print(json.dumps(result))
    except (DouyinError, OSError, ValueError) as error:
        output = (
            error.output()
            if isinstance(error, DouyinError)
            else {
                "ok": False,
                "error": {
                    "code": "local_credentials_error",
                    "message": "Unable to read or save local credentials.",
                },
            }
        )
        print(json.dumps(output))
        raise SystemExit(1) from None
