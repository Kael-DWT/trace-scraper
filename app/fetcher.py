"""页面抓取器。全局共享 Playwright 浏览器实例。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

from app.config import BROWSER_HEADLESS, NAV_TIMEOUT_MS, WAIT_AFTER_LOAD_MS

_browser_instance = None
_playwright_instance = None


async def _ensure_browser():
    global _browser_instance, _playwright_instance
    if _browser_instance is None or not _browser_instance.is_connected():
        if _playwright_instance is None:
            _playwright_instance = await async_playwright().start()
        _browser_instance = await _playwright_instance.chromium.launch(headless=BROWSER_HEADLESS)
    return _browser_instance


async def _create_context():
    browser = await _ensure_browser()
    return await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
        ),
        viewport={"width": 414, "height": 896},
        locale="zh-CN",
    )


@dataclass
class FetchResult:
    url: str
    html: str
    title: str
    text: str
    status: int
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status in (0, 200, 301, 302) and bool(self.html)

    @property
    def final_url(self) -> str:
        return self.url


async def _fetch(page: Page, url: str) -> FetchResult:
    resp = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    await page.wait_for_timeout(WAIT_AFTER_LOAD_MS)
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except PWTimeout:
        pass
    html = await page.content()
    title = await page.title()
    try:
        text = await page.inner_text("body") if html.strip() else ""
    except Exception:
        text = ""
    status = resp.status if resp else 0
    return FetchResult(url=page.url, html=html, title=title.strip(), text=text.strip(), status=status)


async def fetch(url: str, *, trace_code: Optional[str] = None) -> FetchResult:
    """使用全局浏览器抓取 URL。"""
    ctx = await _create_context()
    page = await ctx.new_page()
    try:
        return await _fetch(page, url)
    except Exception as exc:
        return FetchResult(url=url, html="", title="", text="", status=0, error=str(exc))
    finally:
        await ctx.close()


def fetch_sync(url: str, *, trace_code: Optional[str] = None) -> FetchResult:
    return asyncio.run(fetch(url, trace_code=trace_code))


def fetch_page(url: str, *, trace_code: Optional[str] = None, rule: Optional[dict] = None) -> FetchResult:
    """rule_engine 调用的入口。"""
    result = fetch_sync(url, trace_code=trace_code)
    if result.error or not result.html:
        return result
    if rule and rule.get("search", {}).get("input_selector"):
        try:
            result = asyncio.run(_interactive_fetch(url, trace_code or "", rule))
        except Exception:
            pass
    return result


async def _interactive_fetch(url: str, trace_code: str, rule: dict) -> FetchResult:
    """按规则交互：填表单、点详情、等容器，单次 Playwright 会话完成。"""
    search = rule.get("search", {})
    nav = rule.get("navigation", {})
    need_detail = nav.get("need_detail_page", False)
    detail_sel = nav.get("detail_selector", "")
    inp_sel = search.get("input_selector", "")
    btn_sel = search.get("button_selector", "")

    ctx = await _create_context()
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        await page.wait_for_timeout(WAIT_AFTER_LOAD_MS)
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except PWTimeout:
            pass

        if inp_sel and btn_sel and trace_code:
            try:
                await page.fill(inp_sel, trace_code)
                await page.click(btn_sel)
                await page.wait_for_timeout(3000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except PWTimeout:
                    pass
            except Exception as e:
                pass

        if need_detail and detail_sel:
            try:
                el = await page.query_selector(detail_sel)
                if el:
                    await el.click()
                    await page.wait_for_timeout(3000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except PWTimeout:
                        pass
            except Exception:
                pass

        html = await page.content()
        title = await page.title()
        text = await page.inner_text("body") if html.strip() else ""
        return FetchResult(url=page.url, html=html, title=title.strip(), text=text.strip(), status=200)
    except Exception as exc:
        return FetchResult(url=url, html="", title="", text="", status=0, error=str(exc))
    finally:
        await ctx.close()


def normalize_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return u
    if not u.startswith(("http://", "https://")):
        u = "http://" + u
    return u


def extract_domain(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(normalize_url(url)).netloc.lower()