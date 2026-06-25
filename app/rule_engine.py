"""编排引擎：抓取 -> 找详情页 -> 结构提取 -> LLM兜底 -> 缓存。"""
from __future__ import annotations

import html as _html
import logging
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.cache import get_cached, set_cached
from app.fetcher import fetch_page, FetchResult
from app.field_extractor import extract_fields
from app.learning_agent import LearningAgent

logger = logging.getLogger("trace.rule_engine")

# ---- 详情页链接检测 ----
DETAIL_TEXTS_PRIMARY = ["查看产品详情", "产品详情", "品种详情", "查看详情", "详细信息", "查看产品", "进入追溯"]
DETAIL_TEXTS_SECONDARY = ["追溯网址"]
DETAIL_KEYWORDS = ["description", "detail", "info", "trace", "getmsg", "product"]


def _context_text(a) -> str:
    """收集 <a> 周围的标记文本（前驱兄弟、父级内的 span/label 等）。"""
    parts = []
    sib = a.previous_sibling
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
    return " ".join(parts)


def _is_homepage_link(href: str, base_url: str) -> bool:
    """判断链接是否指向首页。"""
    resolved = urljoin(base_url, href)
    parsed = urlparse(resolved)
    path = parsed.path.rstrip("/")
    if not path or path in ("/index", "/index.html", "/home"):
        return True
    return False


def find_detail_url(html: str, base_url: str) -> str | None:
    """从摘要页 HTML 中找出详情页链接。"""
    soup = BeautifulSoup(html, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        text = (a.get_text() or "").strip()
        href = a["href"].strip()
        ctx = _context_text(a)
        combined = f"{text} {ctx}"
        links.append((a, text, href, combined))

    # Pass 1: 文本匹配 "查看产品详情" / "产品详情"
    for a, text, href, combined in links:
        if any(kw in combined for kw in DETAIL_TEXTS_PRIMARY):
            if href.startswith("javascript:"):
                m = re.search(r'["\']([^"\']*(?:description|detail|info)[^"\']*)["\']', href)
                if m:
                    return urljoin(base_url, _html.unescape(m.group(1)))
            else:
                return urljoin(base_url, _html.unescape(href))

    # Pass 2: "追溯网址" 文本链接（排除首页）
    for a, text, href, combined in links:
        if any(kw in combined for kw in DETAIL_TEXTS_SECONDARY):
            if not _is_homepage_link(href, base_url):
                if href.startswith("javascript:"):
                    m = re.search(r'["\']([^"\']*(?:description|detail|info)[^"\']*)["\']', href)
                    if m:
                        return urljoin(base_url, _html.unescape(m.group(1)))
                else:
                    return urljoin(base_url, _html.unescape(href))

    # Pass 3: href 含 description/detail/product 等关键词
    for a, text, href, combined in links:
        if href.startswith("javascript:"):
            continue
        if any(kw in href.lower() for kw in ["description", "detail", "product"]):
            return urljoin(base_url, _html.unescape(href))

    # Pass 4: 其他 trace/getmsg/info 关键词
    for a, text, href, combined in links:
        if "javascript" not in href:
            if any(kw in href.lower() for kw in ["trace", "getmsg", "info"]):
                resolved = urljoin(base_url, _html.unescape(href))
                if not _is_homepage_link(href, base_url):
                    return resolved

    return None


def _domain_of(url: str) -> str:
    return urlparse(url).netloc.lower()


class RuleEngine:
    def __init__(self) -> None:
        self.agent = LearningAgent()

    def query(self, url: str) -> dict[str, Any]:
        domain = _domain_of(url)
        t0 = time.time()

        # 缓存命中
        cached = get_cached(domain, url)
        if cached is not None:
            logger.info("cache hit domain=%s", domain)
            return {"source": "cache", "fields": cached, "elapsed_ms": round((time.time() - t0) * 1000)}

        # 抓取摘要页
        fetched = fetch_page(url)
        if not fetched.ok:
            raise RuntimeError(f"fetch failed: {fetched.error}")

        # 找详情页链接并抓取
        ext_html = fetched.html
        ext_url = fetched.final_url or url
        detail_url = find_detail_url(ext_html, ext_url)
        if detail_url:
            logger.info("found detail page: %s", detail_url[:60])
            detail_fetched = fetch_page(detail_url)
            if detail_fetched.ok:
                ext_html = detail_fetched.html
                ext_url = detail_fetched.final_url or detail_url

        # 结构化提取
        fields = extract_fields(ext_html, ext_url)
        fields.pop("trace_website", None)
        fields = {k: v for k, v in fields.items() if v}

        # LLM 兜底
        if not fields.get("goods_name") or not fields.get("company_name"):
            logger.info("struct extraction incomplete, trying LLM fallback")
            llm_fields = self.agent.extract_with_llm(ext_url, ext_html)
            for k, v in llm_fields.items():
                if v and not fields.get(k):
                    fields[k] = v

        fields["trace_website"] = url
        fields["domain"] = domain

        # 缓存
        set_cached(domain, url, fields)
        elapsed = round((time.time() - t0) * 1000)
        logger.info("done domain=%s fields=%d elapsed=%dms", domain, len(fields), elapsed)
        return {"source": "live", "fields": fields, "elapsed_ms": elapsed}


engine = RuleEngine()