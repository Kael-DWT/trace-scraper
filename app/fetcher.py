"""页面抓取器：用 Playwright 处理 JS 渲染、跳转与重定向，返回最终 HTML 与 URL。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

from app.config import BROWSER_HEADLESS, NAV_TIMEOUT_MS, WAIT_AFTER_LOAD_MS


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
    # 等待可能的 JS 跳转稳定
    await page.wait_for_timeout(WAIT_AFTER_LOAD_MS)
    # 尝试等待网络空闲，但不阻塞过久
    try:
        await page.wait_for_load_state("networkidle", timeout=6000)
    except PWTimeout:
        pass
    html = await page.content()
    title = await page.title()
    text = await page.inner_text("body") if html.strip() else ""
    status = resp.status if resp else 0
    return FetchResult(
        url=page.url,
        html=html,
        title=title.strip(),
        text=text.strip(),
        status=status,
    )


async def fetch(url: str, *, trace_code: Optional[str] = None) -> FetchResult:
    """抓取单个 URL，返回渲染后的页面内容。"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=BROWSER_HEADLESS)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
            ),
            viewport={"width": 414, "height": 896},
            locale="zh-CN",
        )
        page = await context.new_page()
        try:
            return await _fetch(page, url)
        except Exception as exc:  # noqa: BLE001
            return FetchResult(url=url, html="", title="", text="", status=0, error=str(exc))
        finally:
            await context.close()
            await browser.close()


def fetch_sync(url: str, *, trace_code: Optional[str] = None) -> FetchResult:
    """同步包装：供 FastAPI 同步路由或脚本调用。"""
    return asyncio.run(fetch(url, trace_code=trace_code))
 
 
def fetch_page(url: str, *, trace_code: Optional[str] = None, rule: dict | None = None) -> FetchResult:
     """rule_engine 调用入口。trace_code 已嵌入 URL 的站点直接抓取;
     若 rule 指明需要输入框查询(表单型站点),则在页面内填入 trace_code 并点击查询。"""
     result = fetch_sync(url, trace_code=trace_code)
     if result.error or not result.html:
         return result
     # 表单型站点: rule.search.input_selector / button_selector 存在时做交互
     if rule and rule.get("search", {}).get("input_selector"):
         try:
             result = asyncio.run(_interactive_fetch(url, trace_code or "", rule))
         except Exception:  # noqa: BLE001
             pass
     return result


async def _interactive_fetch(url: str, trace_code: str, rule: dict) -> FetchResult:
     search = rule.get("search", {})
     nav = rule.get("navigation", {})
     result_rule = rule.get("result", {})
     async with async_playwright() as p:
         browser = await p.chromium.launch(headless=BROWSER_HEADLESS)
         context = await browser.new_context(
             user_agent=(
                 "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
             ),
             viewport={"width": 414, "height": 896},
             locale="zh-CN",
         )
         page = await context.new_page()
         try:
             await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
             await page.wait_for_timeout(WAIT_AFTER_LOAD_MS)
             inp_sel = search.get("input_selector", "")
             btn_sel = search.get("button_selector", "")
             if inp_sel:
                 await page.fill(inp_sel, trace_code)
             if btn_sel:
                 await page.click(btn_sel)
                 await page.wait_for_timeout(3000)
                 try:
                     await page.wait_for_load_state("networkidle", timeout=8000)
                 except PWTimeout:
                     pass
             # 若需要详情页
             if nav.get("need_detail_page") and nav.get("detail_selector"):
                 try:
                     await page.click(nav["detail_selector"])
                     await page.wait_for_timeout(3000)
                 except Exception:  # noqa: BLE001
                     pass
             html = await page.content()
             title = await page.title()
             text = await page.inner_text("body") if html.strip() else ""
             return FetchResult(url=page.url, html=html, title=title.strip(), text=text.strip(), status=200)
         except Exception as exc:  # noqa: BLE001
             return FetchResult(url=url, html="", title="", text="", status=0, error=str(exc))
         finally:
             await context.close()
             await browser.close()


def normalize_url(url: str) -> str:
     """补全协议、去首尾空白。"""
     u = (url or "").strip()
     if not u:
         return u
     if not u.startswith(("http://", "https://")):
         u = "http://" + u
     return u


def extract_domain(url: str) -> str:
     """提取 netloc(含端口)，小写化。"""
     from urllib.parse import urlparse
     return urlparse(normalize_url(url)).netloc.lower()
