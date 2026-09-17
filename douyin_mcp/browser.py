from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import quote, urlsplit

from .core import DouyinError, Settings

BROWSER_HOSTS = (
    "douyin.com",
    "douyinpic.com",
    "douyincdn.com",
    "douyinvod.com",
    "bytedance.com",
    "byteimg.com",
    "bytedance.net",
    "bdstatic.com",
    "bytescm.com",
    "bytednsdoc.com",
    "pstatp.com",
    "snssdk.com",
    "zijieapi.com",
)


def video_url(value: str) -> str:
    if re.fullmatch(r"[0-9]{5,24}", value):
        return f"https://www.douyin.com/video/{value}"
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.douyin.com"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise DouyinError(
            "invalid_video_url",
            "Use a numeric video ID or https://www.douyin.com/video/ID. Short links are intentionally unsupported.",
        )
    if not re.fullmatch(r"/video/[0-9]{5,24}/?", parsed.path) or parsed.query or parsed.fragment:
        raise DouyinError("invalid_video_url", "Use the canonical video URL without query or fragment.")
    return "https://www.douyin.com" + parsed.path.rstrip("/")


def browser_host_allowed(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        return (
            parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
            and any(host == suffix or host.endswith("." + suffix) for suffix in BROWSER_HOSTS)
        )
    except ValueError:
        return False


async def public_host(host: str) -> bool:
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(row[4][0]).is_global for row in addresses)
    except (OSError, ValueError):
        return False


class BrowserReader:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.lock = asyncio.Lock()

    async def read(
        self, *, query: str | None = None, video: str | None = None, limit: int = 10, scrolls: int = 0
    ) -> dict:
        if not self.settings.browser_enabled:
            raise DouyinError(
                "browser_disabled",
                "Install the browser extra, log in with douyin-browser-login, and set DOUYIN_ENABLE_BROWSER=true.",
            )
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise DouyinError(
                "browser_dependency_missing",
                "Install .[browser] and run python -m playwright install chromium.",
            ) from None
        url = (
            "https://www.douyin.com/search/" + quote(query or "", safe="")
            if query is not None
            else video_url(video or "")
        )
        async with self.lock:
            try:
                async with async_playwright() as playwright:
                    context = await playwright.chromium.launch_persistent_context(
                        str(self.settings.state_dir / "browser-profile"),
                        headless=self.settings.browser_headless,
                        accept_downloads=False,
                        service_workers="block",
                    )
                    try:
                        dns: dict[str, bool] = {}

                        async def route_request(route):
                            request_url = route.request.url
                            if not browser_host_allowed(request_url):
                                await route.abort()
                                return
                            host = urlsplit(request_url).hostname or ""
                            if host not in dns:
                                dns[host] = await public_host(host)
                            if not dns[host] or route.request.resource_type in {"media", "font"}:
                                await route.abort()
                            else:
                                await route.continue_()

                        await context.route("**/*", route_request)
                        # Playwright's HTTP route does not cover WebSocket upgrades.
                        await context.route_web_socket("**/*", lambda socket: socket.close())
                        page = await context.new_page()
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        if urlsplit(page.url).hostname != "www.douyin.com":
                            raise DouyinError(
                                "redirect_blocked", "The browser left the expected public Douyin host."
                            )
                        await page.wait_for_timeout(1500)
                        # Normal UI scrolling only; never request undocumented network endpoints.
                        for _ in range(scrolls):
                            await page.mouse.wheel(0, 650)
                            await page.wait_for_timeout(700)
                        text = (await page.locator("body").inner_text(timeout=10000))[:24000]
                        if any(mark in text for mark in ("完成验证", "拖动滑块", "安全验证", "请先登录")):
                            raise DouyinError(
                                "browser_interaction_required",
                                "Douyin requires login or verification. Use douyin-browser-login manually, then retry. No bypass is attempted.",
                            )
                        links = await page.locator('a[href*="/video/"]').evaluate_all(
                            "els => els.filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden').slice(0, 100).map(e => ({url:e.href,text:e.innerText.slice(0,1500)}))"
                        )
                        items, seen = [], set()
                        for link in links:
                            try:
                                canonical = video_url(link["url"])
                            except (DouyinError, ValueError):
                                continue
                            if canonical not in seen:
                                items.append({"url": canonical, "text": link["text"]})
                                seen.add(canonical)
                            if len(items) >= limit:
                                break
                        # Comments remain labelled as page text: avoid guessing DOM selectors/identities.
                        return {
                            "ok": True,
                            "source": "rendered_browser",
                            "url": url,
                            "title": await page.title(),
                            "videos": items,
                            "visible_page_text": text,
                            "complete": False,
                            "limitation": "Only rendered page content, including any rendered comments. No complete history, hidden data, or structured comment count is implied.",
                            "untrusted_content": True,
                        }
                    finally:
                        await context.close()
            except DouyinError:
                raise
            except Exception:
                # Browser exceptions may include URLs/session information; never forward them.
                raise DouyinError(
                    "browser_unavailable",
                    "Browser read failed. Check browser installation, profile lock, login, and network; live DOM compatibility is not guaranteed.",
                ) from None


async def login():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise SystemExit("Install .[browser] and run python -m playwright install chromium.") from None
    settings = Settings.from_env()
    settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(settings.state_dir / "browser-profile"), headless=False, accept_downloads=False
        )
        try:
            page = await context.new_page()
            await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
            await asyncio.to_thread(
                input,
                "Complete login/verification in the dedicated browser, then press Enter here to close it: ",
            )
        finally:
            await context.close()


def login_main():
    asyncio.run(login())
