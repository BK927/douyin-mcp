import json

import httpx
import pytest

from douyin_mcp.auth import token_request
from douyin_mcp.core import DouyinError, Settings
from douyin_mcp.server import create_server


async def test_auth_code_form_and_no_secret_output(tmp_path):
    settings = Settings(client_key="app", client_secret="private", state_dir=tmp_path)

    def handler(request):
        assert request.url.path == "/oauth/access_token/"
        assert "application/x-www-form-urlencoded" in request.headers["content-type"]
        assert b"code=user-ticket" in request.content
        return httpx.Response(
            200,
            json={
                "data": {
                    "access_token": "act.private-token",
                    "refresh_token": "rft.private-token",
                    "open_id": "account",
                    "scope": "video.create.bind",
                    "expires_in": 60,
                    "error_code": 0,
                }
            },
        )

    result = await token_request("exchange", settings, "user-ticket", httpx.MockTransport(handler))
    assert "private" not in json.dumps(result)
    saved = json.loads((tmp_path / "credentials.json").read_text())
    assert saved["access_token"] == "act.private-token"
    assert saved["app_type"] == "mobile_web"


async def test_client_token_separate_file_and_json(tmp_path):
    settings = Settings(client_key="app", client_secret="private", state_dir=tmp_path)

    def handler(request):
        assert json.loads(request.content)["grant_type"] == "client_credential"
        return httpx.Response(200, json={"data": {"access_token": "clt.private-token", "error_code": 0}})

    await token_request("client", settings, transport=httpx.MockTransport(handler))
    assert (tmp_path / "client-credentials.json").exists()
    assert not (tmp_path / "credentials.json").exists()


async def test_token_redirect_blocked(tmp_path):
    settings = Settings(client_key="app", client_secret="private", state_dir=tmp_path)
    with pytest.raises(DouyinError):
        await token_request(
            "client",
            settings,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(307, headers={"location": "https://evil.test"})
            ),
        )
    assert not list(tmp_path.iterdir())


async def test_tool_registration_and_safe_defaults(tmp_path):
    server = create_server(Settings(state_dir=tmp_path))
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert len(tools) == 14
    for name in [
        "douyin_upload",
        "douyin_publish_video",
        "douyin_publish_images",
        "douyin_reply_comment",
        "douyin_delete_video",
    ]:
        assert tools[name].inputSchema["properties"]["dry_run"]["default"] is True
        assert tools[name].annotations.destructiveHint
    assert tools["douyin_search"].inputSchema["properties"]["count"]["maximum"] == 50
    assert tools["douyin_search"].annotations.readOnlyHint
    assert tools["douyin_status"].inputSchema["properties"] == {}


async def test_mcp_tool_status_and_preview(tmp_path):
    server = create_server(Settings(state_dir=tmp_path))
    result = await server.call_tool("douyin_status", {})
    text = " ".join(block.text for block in result if hasattr(block, "text"))
    assert '"live_verified": false' in text
    result = await server.call_tool("douyin_publish_video", {"video_id": "@uploaded"})
    text = " ".join(block.text for block in result if hasattr(block, "text"))
    assert '"dry_run": true' in text and '"private_status": 1' in text
    assert not (tmp_path / "writes.sqlite3").exists()


@pytest.mark.parametrize(
    "saved",
    [
        {"app_type": "mini_app", "client_key": "app", "access_token": "private"},
        {"app_type": "mobile_web", "client_key": "other-app", "access_token": "private"},
        {"app_type": "mobile_web", "client_key": "app", "open_id": "other-user", "access_token": "private"},
    ],
)
def test_credentials_cannot_cross_apps_or_accounts(tmp_path, monkeypatch, saved):
    for name in ("DOUYIN_ACCESS_TOKEN", "DOUYIN_CLIENT_TOKEN", "DOUYIN_SCOPES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DOUYIN_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DOUYIN_APP_TYPE", "mobile_web")
    monkeypatch.setenv("DOUYIN_CLIENT_KEY", "app")
    monkeypatch.setenv("DOUYIN_OPEN_ID", "user")
    (tmp_path / "credentials.json").write_text(json.dumps(saved))
    with pytest.raises(DouyinError) as error:
        Settings.from_env()
    assert error.value.code == "credential_profile_mismatch"


async def test_refresh_rejects_conflicting_profile(tmp_path):
    (tmp_path / "credentials.json").write_text(
        json.dumps({"refresh_token": "private", "client_key": "other-app"})
    )
    with pytest.raises(DouyinError) as error:
        await token_request(
            "refresh",
            Settings(client_key="app", state_dir=tmp_path),
            transport=httpx.MockTransport(lambda _: pytest.fail("cross-app refresh")),
        )
    assert error.value.code == "credential_profile_mismatch"
