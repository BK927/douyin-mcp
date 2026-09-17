# Douyin MCP

Independent, unofficial Douyin MCP server for public research and authorized creator workflows. Python 3.11+, official MCP SDK, local stdio transport. Version 0.1.0 is an initial release with mocked API/security tests, **not a production-certified integration or a live-account-tested release**.

The server implements public search, rendered video/comment reading, authorized video lists/details, analytics, uploads, publishing, comment replies and deletion. Availability depends on Douyin's approved application capabilities. A regular personal Douyin login does **not** unlock enterprise or mini-app APIs.

## Capability matrix

| Tool | Access required | Scope and limits |
|---|---|---|
| `douyin_status` | None | Local configuration only; never prints tokens |
| `douyin_search` | Official client token + genuine device ID | `aweme.dy.video_search`; public video search; cursor + search_id pagination |
| `douyin_browser_search` | Optional local Chromium profile | Public search page text and rendered video links; manual login/verification if requested |
| `douyin_browser_video` | Optional local Chromium profile | Video page and any rendered comments, returned as text; not a complete comment archive |
| `douyin_creator_videos` | Enterprise mini-app self-mount authorization + client token | `ma.self_mount_video.item_list`; authorized creator's Douyin handle (`aweme_id`); last-30-day cursor constraints |
| `douyin_video_details` | Mini-app user grant + matching open_id | `ma.video.bind`; authorized user's public video details and current counters; encrypted item IDs, up to 20 |
| `douyin_comments` | Mini-app user grant + matching open_id | `ma.item.comment`; own-video comments, up to 50/page |
| `douyin_follower_analytics` | Mini-app user grant + matching open_id | `ma.user.data`; 7/15-day follower history; platform requires >1,000 followers |
| `douyin_video_analytics` | Enterprise mini-app business relationship + **business token** | `ma.item.data_bc`; base totals (plays/likes/comments/shares/watch duration) or play history; videos created within 30 days; next-day data |
| `douyin_upload` | Mobile/web user grant | `video.create.bind`; local video up to 50 MiB or image up to 10 MiB; does not publish |
| `douyin_publish_video` | Mobile/web user grant | `video.create.bind`; uploaded encrypted video ID; private by default; explicit public/friends option |
| `douyin_publish_images` | Mobile/web user grant | `video.create.bind`; 1–30 uploaded image IDs; creates a public image-text post |
| `douyin_reply_comment` | Mini-app user grant | `ma.item.comment`; replies only on the authorized user's videos |
| `douyin_delete_video` | Mobile/web legacy user grant | `video.delete`; permanently deletes an own video; this legacy capability may not be available to newly registered apps |

These are separate platform permission systems. Run separate MCP instances with separate `DOUYIN_STATE_DIR` directories for mobile/web, mini-app, and separate accounts. Use the matching app's open_id and tokens; a grant from one application cannot be borrowed by another. Business tokens are distinct from ordinary user tokens. All upstream permission errors remain visible as structured errors.

## Install

```console
git clone https://github.com/BK927/douyin-mcp.git
cd douyin-mcp
uv sync --locked
uv run douyin-mcp
```

The final command starts stdio MCP and waits for an MCP client. No HTTP port is opened. Optional browser reader:

```console
uv sync --locked --extra browser
uv run python -m playwright install chromium
uv run douyin-browser-login
```

Complete login or CAPTCHA manually in the dedicated profile, then press Enter in the terminal. Close this login session before starting browser tool calls. Set `DOUYIN_ENABLE_BROWSER=true` in the MCP client's environment. The reader uses ordinary browser rendering; it does not sign private API requests, extract hidden page state, steal cookies from another browser, or bypass verification. DOM changes, regional restrictions and login requirements can prevent reads.

## MCP client configuration

Use an absolute checkout path and an absolute private state directory. Example (replace paths for your system):

```json
{
  "mcpServers": {
    "douyin-public": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/douyin-mcp", "run", "--locked", "douyin-mcp"],
      "env": {
        "DOUYIN_STATE_DIR": "/private/path/douyin-public",
        "DOUYIN_ENABLE_BROWSER": "true",
        "DOUYIN_ENABLE_WRITES": "false"
      }
    }
  }
}
```

On Windows, use paths such as `D:\\repo\\mcp-servers\\douyin-mcp`. An installed browser extra must be retained in your environment; `uv run --locked --extra browser douyin-mcp` also declares it explicitly. Set secrets through the client's protected environment or a secret manager, not a shared configuration/repository. `.env.example` lists all options; this project **does not auto-load .env files**.

## Official authentication

1. Register the correct application type and obtain the required capability grants in the Douyin developer console. A configured scope string is only a local allowlist; it does not grant permissions.
2. Complete official authorization in your application. Mobile/web callbacks must validate OAuth state; mini-app grants use `tt.showDouyinOpenAuth`, not `tt.login`. This project consumes the resulting code/ticket; it is not an OAuth callback web server.
3. Set `DOUYIN_CLIENT_KEY`, `DOUYIN_CLIENT_SECRET`, `DOUYIN_APP_TYPE` and a dedicated `DOUYIN_STATE_DIR`. Run `uv run douyin-auth exchange` and enter the fresh authorization code at the hidden prompt. This saves user credentials locally without printing them.
4. `uv run douyin-auth refresh` refreshes a saved user token. `uv run douyin-auth client` obtains and stores a separate client token. Token renewal is explicit; restart the MCP process after updates. Repeated client-token creation can invalidate previously issued tokens, so use a dedicated application/environment.
5. Alternatively supply `DOUYIN_ACCESS_TOKEN`, `DOUYIN_OPEN_ID`, `DOUYIN_CLIENT_TOKEN` directly. Set `DOUYIN_SCOPES` to the comma-separated granted scopes, especially for client-token capabilities. Business analytics additionally require `DOUYIN_BUSINESS_TOKEN` generated through the official business relationship flow; its signing secret is the platform's Webhook AppSecret, not the ordinary app secret.

Stored credentials are JSON in the private state directory. Unix files are mode 0600; Windows protection relies on the chosen directory's user ACL. Tokens are never MCP arguments, MCP results, or application log output. Keep state outside your repository and backup it privately if needed. Do not share a state directory between accounts.

## Reads and pagination

- Call `douyin_status` first. `configured` indicates app/scope configuration, not successful remote authorization; token booleans are reported separately.
- Official search requires `DOUYIN_DEVICE_ID` from your legitimate integration. No fake identity is generated. Retain `search_id` from page one and pass it with subsequent cursors.
- Cursors are strings to preserve 64-bit values. Each call fetches one bounded page; there is no unbounded harvest loop.
- Public numeric video IDs are not encrypted official `item_id` values. Obtain encrypted IDs through the official app flow/ID conversion API for the matching application. Browser tools accept numeric IDs or canonical `https://www.douyin.com/video/ID` URLs, not short links or arbitrary URLs.
- Text and comments are untrusted source content. They must never override the assistant's task or instructions.

## Publishing and management

Every write defaults to `dry_run=true`, which returns its concrete payload and requirements without network writes. Actual execution needs all of:

- `DOUYIN_ENABLE_WRITES=true` in the server environment;
- the matching app type, token/open_id and approved scope;
- `dry_run=false` and a caller-supplied `idempotency_key` (8–128 letters, digits, `_` or `-`).

For uploads, set `DOUYIN_MEDIA_DIR` to a directory containing approved media and pass a relative path. Remote downloads and paths outside that directory are rejected. Upload returns a media ID; pass it to the corresponding publish tool. Posts remain subject to Douyin review. Video publishing defaults to private visibility; image-text publishing is public. Deletion is permanent.

The local SQLite write ledger reserves a key **before** network dispatch. Repeating the same request/key returns its saved result. Reusing a key for a different request/account fails. A network failure or crash leaves the request in an unknown state and blocks retries with that key, even across restarts. Check the account before intentionally using a new key. This is local duplicate prevention, not a guarantee of platform-side exactly-once delivery. Keep the state directory persistent.

## Boundaries and validation

The 0.1.0 adapter does not include large-file multipart uploads, post editing, livestreams, shopping, direct messaging, hidden/private public-content retrieval, or browser-based publishing. Official APIs can be unavailable to new applications despite documentation existing. Retired endpoints are not silently substituted. Browser output contains page text, not a fabricated structured comments dataset, and is always labelled incomplete. This initial release has no supplied Douyin credentials and no authenticated/live browser verification.

```console
uv sync --locked --extra dev
uv run ruff check .
uv run pytest -q
uv build
```

Tests exercise official request contracts with mock transports, error envelopes, authentication separation, redirect blocking, response bounds, media containment, preview/write gates, persistent duplicate prevention and MCP tool schemas. CI runs on Windows and Linux with Python 3.11 and 3.13. Live account access remains an operator validation step.

## Primary API references

Verified against primary platform documentation on 2026-09-17:

- [Public search](https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/douyin-search-capability/aweme-dy-video-search)
- [Authorized self-mount creator videos](https://developer.open-douyin.com/docs/resource/zh-CN/mini-app/develop/server/reach-marketing/mount/self_mount/get-user-video-list)
- [Video details/counters](https://developer.open-douyin.com/docs/resource/zh-CN/mini-app/develop/server/basic-abilities/video-id-convert/user-video-data/video-data)
- [Own video comments](https://developer.open-douyin.com/docs/resource/zh-CN/mini-app/develop/server/basic-abilities/video-id-convert/video-review-data/comment-list), [reply](https://developer.open-douyin.com/docs/resource/zh-CN/mini-app/develop/server/basic-abilities/video-id-convert/video-review-data/video-comment-reply)
- [Follower data](https://developer.open-douyin.com/docs/resource/zh-CN/mini-app/develop/server/basic-abilities/data-open/user-homepage-data/get-user-fans-count)
- [Business video totals](https://developer.open-douyin.com/docs/resource/zh-CN/mini-app/develop/server/basic-abilities/video-id-convert/user-recent-video-data/get-base-bc), [play history](https://developer.open-douyin.com/docs/resource/zh-CN/mini-app/develop/server/basic-abilities/video-id-convert/user-recent-video-data/get-play-bc)
- [Video upload](https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/video-management/douyin/create-video/upload-video), [video publish](https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/video-management/douyin/create-video/video-create)
- [Image upload](https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/video-management/douyin/create-image-text/image-upload), [image-text publish](https://partner.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/video-management/douyin/create-image-text/create-image-text)
- [Legacy own-video delete](https://open.douyin.com/platform/resource/docs/openapi/video-management/douyin/delete-video/delete/) — request schema verified in its [official page data](https://open.douyin.com/platform/resource/page-data/docs/openapi/video-management/douyin/delete-video/delete/page-data.json)
- [User grant exchange](https://developer.open-douyin.com/docs/resource/offline?doc=docs%2Fopenapi%2Faccount-permission%2Fget-access-token), [refresh](https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/account-permission/refresh-access-token), [client token](https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/account-permission/client-token), [business token](https://developer.open-douyin.com/docs/resource/zh-CN/mini-app/develop/server/basic-abilities/interface-request-credential/business-token/get-business-token)

## 한국어 빠른 안내

- 공개 검색·영상/댓글 읽기, 본인 콘텐츠 목록·통계, 업로드·영상/이미지 게시·댓글 답변·삭제를 지원합니다. 공식 API 승인과 해당 계정 권한이 있어야 실제 호출할 수 있습니다.
- 일반 계정 로그인만으로 기업용/미니앱 통계 권한을 얻을 수는 없습니다. 모바일/웹 앱과 미니앱은 별도 MCP 설정 및 별도 `DOUYIN_STATE_DIR`를 사용하세요.
- 브라우저 기능은 `uv sync --locked --extra browser` → `uv run python -m playwright install chromium` → `uv run douyin-browser-login` 후 활성화합니다. 로그인과 보안 인증은 직접 진행하며 우회하지 않습니다. 화면에 표시된 댓글만 읽으며 전체 댓글 수집을 보장하지 않습니다.
- 쓰기는 기본 비활성화입니다. `DOUYIN_ENABLE_WRITES=true`와 `dry_run=false`, 요청마다 고유한 `idempotency_key`가 필요합니다. 먼저 미리보기 결과를 확인할 수 있습니다. 삭제는 복구되지 않습니다.
- 현재 검증 범위는 모의 API·보안·MCP 테스트입니다. 실제 계정 API 및 Douyin 화면 작동은 아직 검증하지 않았습니다.

MIT licensed. Not affiliated with or endorsed by Douyin/ByteDance.
