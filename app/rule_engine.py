"""编排引擎：LLM 学习导航规则 → Playwright 按规则执行 → 结构提取 → 缓存。"""
from __future__ import annotations

import asyncio
import hashlib
import html as _html
import logging
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from app.cache import get_cached, set_cached
from app.config import BROWSER_HEADLESS, NAV_TIMEOUT_MS
from app.field_extractor import extract_fields
from app.learning_agent import LearningAgent
from app.rule_store import get_rule, set_rule

logger = logging.getLogger("trace.rule_engine")

UA = "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"


def _trace_code_from_url(url: str) -> str:
    parsed = urlparse(url)
    for param in ("n", "id", "code", "c", "q", "Guid", "bianhao", "identity", "f", "ds", "215", "96", "7"):
        val = _parse_qs(parsed.query).get(param, [""])[0]
        if val:
            return val
    parts = [p for p in parsed.path.split("/") if p]
    for p in reversed(parts):
        if not p.startswith("index") and not p.startswith("seed"):
            return p
    return ""


def _parse_qs(query: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    if not query:
        return result
    for pair in query.split("&"):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        result.setdefault(k, []).append(v)
    return result


def _get_context_text(a) -> str:
    sib = a.previous_sibling
    parts = []
    while sib:
        t = getattr(sib, "get_text", lambda: str(sib))() if hasattr(sib, "get_text") else str(sib)
        t = t.strip()
        if t:
            parts.append(t)
            break
        sib = getattr(sib, "previous_sibling", None)
    parent = a.parent
    if parent:
        pt = parent.get_text(separator=" ", strip=True)
        at = (a.get_text() or "").strip()
        if pt and at and pt != at:
            parts.append(pt)
        gp = parent.parent
        if gp:
            gpt = gp.get_text(separator=" ", strip=True)
            if gpt and gpt != pt:
                parts.append(gpt)
    return " ".join(parts)


def _is_homepage(url: str) -> bool:
    path = urlparse(url).path.rstrip("/")
    return not path or path in ("/index", "/index.html", "/home", "")


def _find_detail_url_fallback(html: str, base_url: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    detail_texts = ["查看产品详情", "产品详情", "品种详情", "查看详情", "详细信息", "查看产品", "进入追溯", "追溯网址"]
    keywords = ["description", "detail", "info", "trace", "getmsg", "product"]
    for a in soup.find_all("a", href=True):
        text = (a.get_text() or "").strip()
        ctx = _get_context_text(a)
        combined = f"{text} {ctx}"
        if any(kw in combined for kw in detail_texts):
            href = a["href"].strip()
            if href.startswith("javascript:"):
                m = re.search(r'["\']([^"\']*(?:description|detail|info)[^"\']*)["\']', href)
                if m:
                    return urljoin(base_url, _html.unescape(m.group(1)))
            else:
                resolved = urljoin(base_url, _html.unescape(href))
                if not _is_homepage(resolved):
                    return resolved
        for kw in keywords:
            hl = a.get("href", "").lower()
            if kw in hl and "javascript" not in hl:
                resolved = urljoin(base_url, _html.unescape(a["href"]))
                if not _is_homepage(resolved):
                    return resolved
        if "javascript" in a.get("href", ""):
            m = re.search(r'["\']([^"\']*(?:description|detail|info)[^"\']*)["\']', a["href"])
            if m:
                return urljoin(base_url, _html.unescape(m.group(1)))
    return None


def _domain_of(url: str) -> str:
    return urlparse(url).netloc.lower()


class RuleEngine:
    def __init__(self) -> None:
        self.agent = LearningAgent()
        self._browser = None
        self._pw = None

    def init_browser(self):
        if self._browser is None:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=BROWSER_HEADLESS)

    def close_browser(self):
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._pw:
            self._pw.stop()
            self._pw = None

    def query(self, url: str) -> dict[str, Any]:
        domain = _domain_of(url)
        t0 = time.time()
        cached = get_cached(domain, url)
        if cached is not None:
            return {"source": "cache", "fields": cached, "elapsed_ms": round((time.time() - t0) * 1000)}

        rule = get_rule(domain)
        if rule is None:
            if self._browser is None:
                self.init_browser()
            _bx = self._browser
            _cx = _bx.new_context(user_agent=UA, viewport={"width":414,"height":896}, locale="zh-CN")
            _pg = _cx.new_page()
            try:
                _pg.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                _pg.wait_for_timeout(1200)
                try: _pg.wait_for_load_state("networkidle", timeout=5000)
                except: pass
                _html = _pg.content()
                _uf = _pg.url
            except Exception as _e:
                _cx.close()
                raise RuntimeError(f"fetch failed: {_e}")
            _cx.close()
            rule = self.agent.learn_navigation(_uf, _html)
            if rule:
                set_rule(domain, rule)
            else:
                rule = None

        if self._browser is None:
            self.init_browser()

        # Single Playwright session for full navigation
        ctx = self._browser.new_context(user_agent=UA, viewport={"width": 414, "height": 896}, locale="zh-CN")
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            page.wait_for_timeout(1200)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except PWTimeout:
                pass

            if rule and rule.get("search", {}).get("input_selector"):
                tc = _trace_code_from_url(url)
                try:
                    page.fill(rule["search"]["input_selector"], tc)
                    page.click(rule["search"]["button_selector"])
                    page.wait_for_timeout(3000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except PWTimeout:
                        pass
                except Exception as e:
                    logger.warning("search fail: %s", e)

            nav_ok = bool(rule and rule.get("navigation", {}).get("need_detail_page") and rule.get("navigation", {}).get("detail_selector"))
            if nav_ok:
                try:
                    el = page.query_selector(rule["navigation"]["detail_selector"])
                    if el:
                        el.click()
                        page.wait_for_timeout(3000)
                        try:
                            page.wait_for_load_state("networkidle", timeout=8000)
                        except PWTimeout:
                            pass
                except Exception as e:
                    logger.warning("detail click fail: %s", e)
            else:
                html_now = page.content()
                durl = _find_detail_url_fallback(html_now, page.url)
                if durl and durl != page.url:
                    try:
                        page.goto(durl, timeout=NAV_TIMEOUT_MS)
                        page.wait_for_timeout(2000)
                    except Exception as e:
                        logger.warning("fallback nav fail: %s", e)

            final_html = page.content()
            final_url = page.url
        except Exception as exc:
            raise RuntimeError(f"nav failed: {exc}")
        finally:
            ctx.close()

        fields = extract_fields(final_html, final_url)
        fields.pop("trace_website", None)
        fields = {k: v for k, v in fields.items() if v}
        if not fields.get("goods_name") or not fields.get("company_name"):
            lf = self.agent.extract_with_llm(final_url, final_html)
            for k, v in lf.items():
                if v and not fields.get(k):
                    fields[k] = v
        fields["trace_website"] = url
        fields["domain"] = domain
        set_cached(domain, url, fields)
        elapsed = round((time.time() - t0) * 1000)
        logger.info("done %s fields=%d %dms", domain, len(fields), elapsed)
        return {"source": "live", "fields": fields, "elapsed_ms": elapsed}


engine = RuleEngine()