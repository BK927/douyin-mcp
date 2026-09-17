import json
from dataclasses import replace

import httpx
import pytest

from douyin_mcp.api import ENDPOINTS, DouyinAPI, unwrap
from douyin_mcp.core import DouyinError, Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        access_token="act.testing-secret",
        client_token="clt.testing-secret",
        business_token="bus_act.testing-secret",
        client_key="test-app",
        open_id="account-one",
        scopes=frozenset(ep.scope for ep in ENDPOINTS.values()),
        writes_enabled=True,
        state_dir=tmp_path,
        app_type="mini_app",
    )


async def test_search_uses_client_header_correct_envelope_and_cursor(settings):
    def handler(request):
        assert request.url.host == "open.douyin.com"
        assert request.url.path == "/dy_open_api/v1/search/video/"
        assert request.headers["access-token"] == settings.client_token
        assert "access_token" not in request.url.params
        assert request.url.params["cursor"] == "9007199254740993"
        assert request.url.params["search_id"] == "page-session"
        return httpx.Response(
            200,
            json={
                "err_no": 0,
                "data": {
                    "data": {
                        "video_list": [{"item_id": "1"}],
                        "cursor": 9007199254740994,
                        "has_more": True,
                        "search_id": "page-session",
                    }
                },
            },
        )

    api = DouyinAPI(settings, httpx.MockTransport(handler))
    result = await api.page(
        "search",
        count=10,
        params={"cursor": "9007199254740993", "search_id": "page-session"},
        key="video_list",
    )
    assert result["cursor"] == "9007199254740994"
    assert result["has_more"] and result["count"] == 1


@pytest.mark.parametrize(
    "response",
    [
        {"err_no": 28001018},
        {"data": {"extra": {"error_code": "28001018"}, "data": {}}},
        {"data": {"error_code": 28001018}},
    ],
)
def test_all_error_envelopes_detected(response):
    with pytest.raises(DouyinError) as error:
        unwrap(response)
    assert error.value.code == "permission_denied"


async def test_redirect_never_forwards_credentials(settings):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secrets"})

    with pytest.raises(DouyinError, match="redirect"):
        await DouyinAPI(settings, httpx.MockTransport(handler)).request("comments")
    assert len(calls) == 1


async def test_response_size_bounded(settings):
    api = DouyinAPI(settings, httpx.MockTransport(lambda _: httpx.Response(200, content=b" " * 2_000_001)))
    with pytest.raises(DouyinError) as error:
        await api.request("comments")
    assert error.value.code == "response_too_large"


@pytest.mark.parametrize(
    "data,expected", [({}, "invalid_response"), ({"list": [{}, {}]}, "upstream_page_limit")]
)
async def test_missing_or_oversized_page_not_silently_truncated(settings, data, expected):
    api = DouyinAPI(settings, httpx.MockTransport(lambda _: httpx.Response(200, json={"data": data})))
    with pytest.raises(DouyinError) as error:
        await api.page("comments", count=1)
    assert error.value.code == expected


async def test_app_and_scope_checks_before_network(settings):
    def forbidden(_):
        pytest.fail("No network request allowed")

    with pytest.raises(DouyinError) as error:
        await DouyinAPI(settings, httpx.MockTransport(forbidden)).request("publish_video")
    assert error.value.code == "application_type_required"
    with pytest.raises(DouyinError) as error:
        await DouyinAPI(replace(settings, scopes=frozenset()), httpx.MockTransport(forbidden)).request(
            "comments"
        )
    assert error.value.code == "scope_required"


async def test_business_token_is_not_user_token(settings):
    def handler(request):
        assert request.headers["access-token"] == settings.business_token
        assert request.url.params["open_id"] == settings.open_id
        assert request.url.path == "/api/apps/v1/item_bc/get_base/"
        return httpx.Response(200, json={"data": {"data": {"result": {"total_play": 8}}}, "err_no": 0})

    data = await DouyinAPI(settings, httpx.MockTransport(handler)).request(
        "base_metrics", params={"item_id": "@item+="}
    )
    assert data["result"]["total_play"] == 8
    with pytest.raises(DouyinError):
        DouyinAPI(replace(settings, business_token="")).authorize("base_metrics")


async def test_preview_has_zero_network_and_no_ledger(settings):
    api = DouyinAPI(settings, httpx.MockTransport(lambda _: pytest.fail("preview wrote")))
    result = await api.write("publish_video", {"video_id": "@id"}, dry_run=True, key="")
    assert result["dry_run"] is True
    assert not (settings.state_dir / "writes.sqlite3").exists()


async def test_writes_disabled(settings):
    api = DouyinAPI(replace(settings, writes_enabled=False))
    with pytest.raises(DouyinError) as error:
        await api.write("reply", {}, dry_run=False, key="unique-key")
    assert error.value.code == "writes_disabled"


async def test_delete_payload_and_persistent_replay(settings):
    settings = replace(settings, app_type="mobile_web")
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.url.path == "/video/delete/"
        assert request.url.params["open_id"] == "account-one"
        assert json.loads(request.content) == {"item_id": "@encrypted+id="}
        return httpx.Response(200, json={"extra": {"error_code": 0}})

    api = DouyinAPI(settings, httpx.MockTransport(handler))
    first = await api.write("delete", {"item_id": "@encrypted+id="}, dry_run=False, key="delete-unique")
    second = await DouyinAPI(settings, httpx.MockTransport(handler)).write(
        "delete", {"item_id": "@encrypted+id="}, dry_run=False, key="delete-unique"
    )
    assert first["ok"] and second["idempotency_replay"]
    assert len(calls) == 1
    with pytest.raises(DouyinError) as error:
        await api.write("delete", {"item_id": "another"}, dry_run=False, key="delete-unique")
    assert error.value.code == "idempotency_conflict"


async def test_ambiguous_write_is_not_retried(settings):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("private token in exception", request=request)

    api = DouyinAPI(settings, httpx.MockTransport(handler))
    with pytest.raises(DouyinError) as error:
        await api.write("reply", {"content": "hello"}, dry_run=False, key="reply-unique")
    assert "private token" not in str(error.value)
    with pytest.raises(DouyinError) as error:
        await api.write("reply", {"content": "hello"}, dry_run=False, key="reply-unique")
    assert error.value.code == "write_outcome_unknown"
    assert len(calls) == 1


async def test_upload_sends_local_bytes_only(settings, tmp_path):
    (tmp_path / "hello.mp4").write_bytes(b"local-video-content")
    settings = replace(settings, app_type="mobile_web", media_dir=tmp_path)

    def handler(request):
        assert request.url.path == "/api/douyin/v1/video/upload_video/"
        assert "multipart/form-data" in request.headers["content-type"]
        assert b"local-video-content" in request.content
        return httpx.Response(200, json={"data": {"video": {"video_id": "@uploaded"}, "error_code": 0}})

    result = await DouyinAPI(settings, httpx.MockTransport(handler)).write(
        "upload_video", {}, dry_run=False, key="upload-unique", upload=("hello.mp4", "video")
    )
    assert result["data"]["video"]["video_id"] == "@uploaded"
