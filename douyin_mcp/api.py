from __future__ import annotations

import hashlib
import json
import mimetypes
from dataclasses import dataclass
from typing import Any

import httpx

from .core import DouyinError, Settings, WriteLedger, bounded_output, media_file

BASE_URL = "https://open.douyin.com"
MAX_RESPONSE_BYTES = 2_000_000


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    scope: str
    app: str = "mini_app"
    client: bool = False
    write: bool = False
    legacy: bool = False
    business: bool = False


ENDPOINTS = {
    "search": Endpoint("GET", "/dy_open_api/v1/search/video/", "aweme.dy.video_search", "any", True),
    "list": Endpoint(
        "POST", "/aweme/v1/video/self/get_user_video_list/", "ma.self_mount_video.item_list", client=True
    ),
    "detail": Endpoint("POST", "/api/apps/v1/video/query/", "ma.video.bind"),
    "comments": Endpoint("GET", "/api/apps/v1/item/list_comment/", "ma.item.comment"),
    "fans": Endpoint("GET", "/api/apps/v1/user/get_fans/", "ma.user.data"),
    "base_metrics": Endpoint("GET", "/api/apps/v1/item_bc/get_base/", "ma.item.data_bc", business=True),
    "play_metrics": Endpoint("GET", "/api/apps/v1/item_bc/get_play/", "ma.item.data_bc", business=True),
    "upload_video": Endpoint(
        "POST", "/api/douyin/v1/video/upload_video/", "video.create.bind", "mobile_web", write=True
    ),
    "upload_image": Endpoint(
        "POST", "/api/douyin/v1/video/upload_image/", "video.create.bind", "mobile_web", write=True
    ),
    "publish_video": Endpoint(
        "POST", "/api/douyin/v1/video/create_video/", "video.create.bind", "mobile_web", write=True
    ),
    "publish_images": Endpoint(
        "POST", "/api/douyin/v1/video/create_image_text/", "video.create.bind", "mobile_web", write=True
    ),
    "reply": Endpoint("POST", "/api/apps/v1/item/reply_comment/", "ma.item.comment", write=True),
    "delete": Endpoint("POST", "/video/delete/", "video.delete", "mobile_web", write=True, legacy=True),
}


def unwrap(payload: dict) -> dict:
    """Check both generations of error envelopes before unwrapping nested data."""
    current = payload
    for _ in range(4):
        for block in (current, current.get("extra", {})):
            if not isinstance(block, dict):
                continue
            for key in ("err_no", "error_code"):
                code = block.get(key, 0)
                if code not in (0, "0", None):
                    text = "Douyin rejected the request. Check the official capability grant and token."
                    kind = "upstream_error"
                    if str(code) in {"28001003", "28001008", "2190008"}:
                        kind = "authentication_required"
                    elif str(code) in {"28001014", "28001018", "28001019", "28001028"}:
                        kind = "permission_denied"
                    elif str(code) == "28001056":
                        kind, text = "capability_retired", "Douyin reports this capability has been retired."
                    elif str(code) == "28003017":
                        kind = "quota_exceeded"
                    raise DouyinError(kind, text, upstream_code=str(code))
        nested = current.get("data")
        if not isinstance(nested, dict):
            break
        current = nested
    return {k: v for k, v in current.items() if k not in {"error_code", "description", "extra"}}


class DouyinAPI:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport
        self.ledger = WriteLedger(settings.state_dir)

    def authorize(self, name: str) -> str:
        endpoint = ENDPOINTS[name]
        if endpoint.app not in {"any", self.settings.app_type}:
            raise DouyinError(
                "application_type_required",
                "Run a separately configured MCP instance for this application type and its tokens.",
                required_app_type=endpoint.app,
            )
        if endpoint.business:
            if endpoint.scope not in self.settings.scopes:
                raise DouyinError(
                    "scope_required",
                    "This business relationship capability must be granted.",
                    required_scope=endpoint.scope,
                )
            if not self.settings.business_token or not self.settings.open_id:
                raise DouyinError(
                    "authentication_required",
                    "Configure DOUYIN_BUSINESS_TOKEN and its corresponding open_id; an ordinary user token cannot substitute.",
                    token_type="business_token",
                )
            return self.settings.business_token
        return self.settings.require(endpoint.scope, client=endpoint.client)

    async def request(
        self, name: str, *, params: dict | None = None, body: dict | None = None, files: dict | None = None
    ) -> dict:
        endpoint = ENDPOINTS[name]
        token = self.authorize(name)
        params = dict(params or {})
        if not endpoint.client:
            params["open_id"] = self.settings.open_id
        headers = {"access-token": token, "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                follow_redirects=False,
                trust_env=False,
                timeout=httpx.Timeout(60, connect=15),
            ) as client:
                async with client.stream(
                    endpoint.method,
                    BASE_URL + endpoint.path,
                    params=params,
                    json=body,
                    files=files,
                    headers=headers,
                ) as response:
                    if 300 <= response.status_code < 400:
                        raise DouyinError(
                            "redirect_blocked", "Official API redirects are blocked to protect credentials."
                        )
                    if response.status_code >= 400:
                        raise DouyinError(
                            "http_error",
                            "The official API returned an HTTP error.",
                            status=response.status_code,
                        )
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise DouyinError(
                                "response_too_large",
                                "Request a smaller page; upstream response exceeds 2 MB.",
                            )
                        chunks.append(chunk)
            payload = json.loads(b"".join(chunks))
            if not isinstance(payload, dict):
                raise ValueError
        except httpx.HTTPError:
            raise DouyinError(
                "network_error", "Douyin could not be reached. Writes are never automatically retried."
            ) from None
        except (ValueError, UnicodeError):
            raise DouyinError("invalid_response", "Douyin returned an invalid JSON object.") from None
        return unwrap(payload)

    async def page(
        self,
        name: str,
        *,
        count: int,
        params: dict | None = None,
        body: dict | None = None,
        key: str = "list",
    ) -> dict:
        data = await self.request(name, params=params, body=body)
        items = data.get(key)
        if not isinstance(items, list):
            raise DouyinError("invalid_response", "Expected a result list in the upstream response.")
        # Do not discard pagination silently if an upstream ignores the requested count.
        if len(items) > count:
            raise DouyinError(
                "upstream_page_limit",
                "Douyin returned more results than requested. No items discarded; retry with a supported page size.",
            )
        return {
            "ok": True,
            "source": "official_api",
            "items": items,
            "cursor": str(data.get("cursor", data.get("max_cursor", "0"))),
            "has_more": bool(data.get("has_more", False)),
            "search_id": data.get("search_id"),
            "count": len(items),
        }

    async def write(
        self, name: str, body: dict, *, dry_run: bool, key: str, upload: tuple[str, str] | None = None
    ) -> dict:
        endpoint = ENDPOINTS[name]
        if not endpoint.write:
            raise DouyinError("invalid_operation", "This operation is not a write.")
        payload: dict[str, Any] = {
            "operation": name,
            "body": body,
            "account": self.settings.open_id,
            "app": self.settings.client_key,
        }
        path = None
        if upload:
            relative_path, kind = upload
            path = media_file(self.settings, relative_path, kind)
            with path.open("rb") as media_stream:
                digest = hashlib.file_digest(media_stream, "sha256").hexdigest()
            payload["media"] = {"name": path.name, "size": path.stat().st_size, "sha256": digest}
        preview = {
            "ok": True,
            "dry_run": True,
            "operation": name,
            "body": body,
            "required_scope": endpoint.scope,
            "required_app_type": endpoint.app,
            "legacy_capability": endpoint.legacy,
        }
        if path:
            preview["media"] = payload["media"]
        if dry_run:
            return preview
        if not self.settings.writes_enabled:
            raise DouyinError("writes_disabled", "Set DOUYIN_ENABLE_WRITES=true before executing writes.")
        self.authorize(name)
        cached = self.ledger.reserve(key, payload)
        if cached:
            return cached
        if path and upload:
            with path.open("rb") as stream:
                result = await self.request(
                    name,
                    files={
                        upload[1]: (
                            path.name,
                            stream,
                            mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                        )
                    },
                )
        else:
            result = await self.request(name, body=body)
        output = bounded_output(
            {
                "ok": True,
                "source": "official_api",
                "operation": name,
                "data": result,
                "legacy_capability": endpoint.legacy,
            },
            self.settings.secrets,
        )
        self.ledger.complete(key, output)
        return output
