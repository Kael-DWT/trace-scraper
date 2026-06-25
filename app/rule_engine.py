"""编排引擎：抓取 -> 结构提取 -> LLM兜底 -> 缓存。"""
from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlparse

from app.cache import get_cached, set_cached
from app.fetcher import fetch_page, FetchResult
from app.field_extractor import extract_fields
from app.learning_agent import LearningAgent

logger = logging.getLogger("trace.rule_engine")


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

        # 抓取
        fetched = fetch_page(url)
        if not fetched.ok:
            raise RuntimeError(f"fetch failed: {fetched.error}")

        # 结构化提取
        fields = extract_fields(fetched.html, fetched.final_url or url)
        fields.pop("trace_website", None)
        fields = {k: v for k, v in fields.items() if v}

        # LLM 兜底
        if not fields.get("goods_name") or not fields.get("company_name"):
            logger.info("struct extraction incomplete, trying LLM fallback")
            llm_fields = self.agent.extract_with_llm(fetched.final_url or url, fetched.html)
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