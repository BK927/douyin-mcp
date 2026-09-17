import json

import pytest

from douyin_mcp.browser import browser_host_allowed, video_url
from douyin_mcp.core import DouyinError, Settings, WriteLedger, bounded_output, media_file


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/video/12345",
        "https://www.douyin.com.evil.test/video/12345",
        "https://www.douyin.com@evil.test/video/12345",
        "https://www.douyin.com:444/video/12345",
        "file:///etc/passwd",
        "https://www.douyin.com/video/12345?redirect=internal",
        "https://v.douyin.com/short",
        "https://www.douyin.com/video/../12345",
    ],
)
def test_public_url_rejects_unsafe_targets(url):
    with pytest.raises((DouyinError, ValueError)):
        video_url(url)


def test_video_ids_canonicalized():
    assert video_url("1234567890") == "https://www.douyin.com/video/1234567890"


@pytest.mark.parametrize(
    "url",
    [
        "http://www.douyin.com",
        "https://localhost",
        "https://127.0.0.1",
        "https://douyin.com.evil.test",
        "https://foo.douyin.com:1234",
        "https://user:pass@www.douyin.com",
        "file:///tmp/file",
    ],
)
def test_browser_network_allowlist(url):
    assert not browser_host_allowed(url)


def test_browser_cdn_allowlist():
    assert browser_host_allowed("https://p11.douyinpic.com/image.png")


def test_known_tokens_and_recursive_fields_redacted():
    value = {
        "ok": True,
        "data": {
            "access_token": "secret",
            "text": "secret / act.unknown-token / clt.unknown-token",
            "nested": [{"cookie": "anything"}],
        },
    }
    output = json.dumps(bounded_output(value, ("secret",)))
    assert "secret" not in output and "unknown-token" not in output and "anything" not in output


def test_output_hard_budget():
    output = bounded_output({"items": ["x" * 8000] * 20})
    assert output["error"]["code"] == "output_too_large"


def test_secret_echoed_as_object_key_is_redacted():
    assert "private-secret" not in json.dumps(
        bounded_output({"private-secret": "value"}, ("private-secret",))
    )


@pytest.mark.parametrize("relative", ["../outside.mp4", "../../outside.mp4", "https://evil.test/video.mp4"])
def test_upload_rejects_escaped_or_remote_paths(tmp_path, relative):
    settings = Settings(media_dir=tmp_path)
    with pytest.raises(DouyinError):
        media_file(settings, relative, "video")


def test_upload_rejects_empty_or_wrong_extension(tmp_path):
    (tmp_path / "empty.mp4").touch()
    (tmp_path / "secret.txt").write_text("private")
    for file in ("empty.mp4", "secret.txt"):
        with pytest.raises(DouyinError):
            media_file(Settings(media_dir=tmp_path), file, "video")


def test_idempotency_key_account_binding(tmp_path):
    ledger = WriteLedger(tmp_path)
    ledger.reserve("same-key1", {"account": "a"})
    with pytest.raises(DouyinError) as error:
        ledger.reserve("same-key1", {"account": "b"})
    assert error.value.code == "idempotency_conflict"
