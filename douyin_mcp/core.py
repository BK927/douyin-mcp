from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class DouyinError(Exception):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def output(self) -> dict:
        return {"ok": False, "error": {"code": self.code, "message": self.message, **self.details}}


def enabled(name: str) -> bool:
    return os.getenv(name, "false").lower() in {"1", "true", "yes"}


@dataclass
class Settings:
    client_key: str = ""
    client_secret: str = ""
    access_token: str = ""
    client_token: str = ""
    business_token: str = ""
    open_id: str = ""
    scopes: frozenset[str] = frozenset()
    writes_enabled: bool = False
    browser_enabled: bool = False
    browser_headless: bool = False
    state_dir: Path = field(default_factory=lambda: Path.home() / ".douyin-mcp")
    media_dir: Path | None = None
    device_id: str = ""
    app_type: str = "mobile_web"

    @classmethod
    def from_env(cls) -> Settings:
        state = Path(os.getenv("DOUYIN_STATE_DIR") or str(Path.home() / ".douyin-mcp")).expanduser().resolve()
        result = cls(
            client_key=os.getenv("DOUYIN_CLIENT_KEY", ""),
            client_secret=os.getenv("DOUYIN_CLIENT_SECRET", ""),
            access_token=os.getenv("DOUYIN_ACCESS_TOKEN", ""),
            client_token=os.getenv("DOUYIN_CLIENT_TOKEN", ""),
            business_token=os.getenv("DOUYIN_BUSINESS_TOKEN", ""),
            open_id=os.getenv("DOUYIN_OPEN_ID", ""),
            scopes=frozenset(filter(None, re.split(r"[,\s]+", os.getenv("DOUYIN_SCOPES", "")))),
            writes_enabled=enabled("DOUYIN_ENABLE_WRITES"),
            browser_enabled=enabled("DOUYIN_ENABLE_BROWSER"),
            browser_headless=enabled("DOUYIN_BROWSER_HEADLESS"),
            state_dir=state,
            media_dir=Path(os.environ["DOUYIN_MEDIA_DIR"]).expanduser().resolve()
            if os.getenv("DOUYIN_MEDIA_DIR")
            else None,
            device_id=os.getenv("DOUYIN_DEVICE_ID", ""),
            app_type=os.getenv("DOUYIN_APP_TYPE", "mobile_web"),
        )
        if result.app_type not in {"mobile_web", "mini_app"}:
            raise DouyinError("invalid_app_type", "DOUYIN_APP_TYPE must be mobile_web or mini_app.")
        token_file = state / "credentials.json"
        if token_file.is_file():
            try:
                saved = json.loads(token_file.read_text("utf-8"))
            except (OSError, ValueError):
                raise DouyinError(
                    "credentials_invalid", "The local credentials file cannot be read."
                ) from None
            if not result.access_token:
                result.check_cached_identity(saved)
                result.access_token = saved.get("access_token", "")
                result.open_id = result.open_id or saved.get("open_id", "")
                if not result.scopes:
                    result.scopes = frozenset(filter(None, saved.get("scope", "").split(",")))
        client_file = state / "client-credentials.json"
        if not result.client_token and client_file.is_file():
            try:
                saved_client = json.loads(client_file.read_text("utf-8"))
                result.check_cached_identity(saved_client)
                result.client_token = saved_client.get("access_token", "")
            except (OSError, ValueError):
                raise DouyinError(
                    "credentials_invalid", "The local client credentials file cannot be read."
                ) from None
        return result

    def check_cached_identity(self, saved: dict):
        if not isinstance(saved, dict):
            raise DouyinError("credentials_invalid", "Expected a local credential object.")
        if (
            saved.get("app_type") not in (None, self.app_type)
            or (self.client_key and saved.get("client_key") not in (None, self.client_key))
            or (self.open_id and saved.get("open_id") not in (None, self.open_id))
        ):
            raise DouyinError(
                "credential_profile_mismatch",
                "Cached credentials belong to another app/account. Configure a separate DOUYIN_STATE_DIR.",
            )

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(
            x for x in (self.client_secret, self.access_token, self.client_token, self.business_token) if x
        )

    def require(self, scope: str, *, client: bool = False) -> str:
        if scope not in self.scopes:
            raise DouyinError(
                "scope_required",
                "Configure a capability actually granted to this application/account.",
                required_scope=scope,
            )
        token = self.client_token if client else self.access_token
        if not token or (not client and not self.open_id):
            raise DouyinError(
                "authentication_required",
                "Configure the appropriate token and, for user APIs, open_id.",
                token_type="client_token" if client else "user_access_token",
            )
        return token


SENSITIVE_KEYS = {
    "access_token",
    "refresh_token",
    "client_secret",
    "client_token",
    "business_token",
    "biz_token",
    "biz_refresh_token",
    "authorization",
    "cookie",
    "cookies",
    "set-cookie",
    "access-token",
}


def sanitize(value: Any, secrets: tuple[str, ...] = (), *, depth: int = 0) -> Any:
    """Bound every output and remove known secrets, even if an upstream echoes one."""
    if depth > 8:
        return "[depth limit]"
    if isinstance(value, dict):
        return {
            sanitize(str(k), secrets, depth=depth + 1)[:100]: (
                "[REDACTED]" if str(k).lower() in SENSITIVE_KEYS else sanitize(v, secrets, depth=depth + 1)
            )
            for k, v in list(value.items())[:60]
        }
    if isinstance(value, list):
        return [sanitize(v, secrets, depth=depth + 1) for v in value[:50]]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"\b(?:act|clt|rft|bus_act|bus_rft)\.[A-Za-z0-9._~+/-]+", "[REDACTED]", value)
        return value[:8000]
    return value


def bounded_output(value: dict, secrets: tuple[str, ...] = ()) -> dict:
    safe = sanitize(value, secrets)
    if len(json.dumps(safe, ensure_ascii=False)) > 65000:
        return {
            "ok": False,
            "error": {
                "code": "output_too_large",
                "message": "Request a smaller page. Output exceeds 65,000 characters.",
            },
        }
    return safe


class WriteLedger:
    """Reserve BEFORE network IO. Ambiguous failures remain reserved across restarts."""

    def __init__(self, directory: Path):
        self.directory = directory

    @contextmanager
    def _connect(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.directory / "writes.sqlite3", timeout=10)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS writes (key TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL, result TEXT)"
        )
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def reserve(self, key: str, payload: dict) -> dict | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", key):
            raise DouyinError(
                "invalid_idempotency_key",
                "Use an 8–128 character key containing letters, digits, underscore or hyphen.",
            )
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            found = db.execute("SELECT digest,state,result FROM writes WHERE key=?", (key,)).fetchone()
            if found:
                if found[0] != digest:
                    raise DouyinError(
                        "idempotency_conflict", "This key was already used for a different request."
                    )
                if found[1] != "complete":
                    raise DouyinError(
                        "write_outcome_unknown",
                        "This request may already have reached Douyin. Verify the account before manually using a new key.",
                    )
                return json.loads(found[2]) | {"idempotency_replay": True}
            db.execute("INSERT INTO writes VALUES (?, ?, 'pending', NULL)", (key, digest))
        return None

    def complete(self, key: str, result: dict):
        with self._connect() as db:
            db.execute("UPDATE writes SET state='complete',result=? WHERE key=?", (json.dumps(result), key))


def media_file(settings: Settings, relative_path: str, kind: str) -> Path:
    if settings.media_dir is None:
        raise DouyinError(
            "media_directory_required", "Set DOUYIN_MEDIA_DIR to the directory approved for uploads."
        )
    path = (settings.media_dir / relative_path).resolve()
    if not path.is_relative_to(settings.media_dir) or Path(relative_path).is_absolute():
        raise DouyinError(
            "invalid_media_path", "Upload paths must be relative and remain inside DOUYIN_MEDIA_DIR."
        )
    allowed = {"video": {".mp4", ".webm", ".mov"}, "image": {".jpg", ".jpeg", ".png"}}[kind]
    if path.suffix.lower() not in allowed or not path.is_file():
        raise DouyinError(
            "invalid_media_file", "Choose an existing supported media file within DOUYIN_MEDIA_DIR."
        )
    maximum = 50 * 1024 * 1024 if kind == "video" else 10 * 1024 * 1024
    if not 0 < path.stat().st_size <= maximum:
        raise DouyinError(
            "media_size_limit",
            f"This adapter supports nonempty {kind} files up to {maximum // 1024 // 1024} MiB.",
        )
    return path
