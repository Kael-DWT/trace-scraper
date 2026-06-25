"""学习 Agent（F003 自动学习 + F006 自修复）。

按照 AGENT_PROMPT.md 的职责划分，本模块只负责「学习网站如何查询」，
即生成可复用的导航规则（搜索输入框、查询按钮、是否需要详情页、结果容器）。
不负责字段提取——字段提取由 field_extractor 统一完成。

同时提供 LLM 字段兜底：当结构化提取拿不到任何字段时，把页面交给 LLM 直接提取。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import OpenAI

from app import config
from app.alias import match_field  # noqa: F401  (re-export for convenience)

logger = logging.getLogger("trace.learner")


def _client() -> OpenAI:
    return OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY)


def _system_prompt() -> str:
    """读取 AGENT_PROMPT.md 作为系统提示；缺失则用内置精简版。"""
    try:
        text = (config.BASE_DIR / "AGENT_PROMPT.md").read_text(encoding="utf-8")
        return text
    except Exception:  # noqa: BLE001
        return (
            "You are an expert website analysis agent. "
            "Given a traceability website URL and HTML, generate reusable navigation rules. "
            "Return JSON only with the schema: "
            '{"domain":"","search":{"input_selector":"","button_selector":"","input_type":"text"},'
            '"navigation":{"need_detail_page":false,"detail_selector":""},'
            '"result":{"container_selector":""},"confidence":0.0}'
        )


def _strip_html(html: str, limit: int = 30000) -> str:
    """精简 HTML：去 script/style/注释/连续空白，截断到 limit。"""
    s = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.S | re.I)
    s = re.sub(r"<style[^>]*>.*?</style>", "", s, flags=re.S | re.I)
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def _call_llm(system: str, user: str) -> str:
    """调用 LLM（兼容 DashScope 通义千问）。"""
    cli = _client()
    resp = cli.chat.completions.create(
        model=config.LLM_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
    )
    return resp.choices[0].message.content or ""


def _parse_rule(raw: str) -> dict[str, Any] | None:
    """从 LLM 输出中解析 JSON 规则。"""
    if not raw:
        return None
    # 去掉可能的 markdown 代码块包裹
    m = re.search(r"\{.*\}", raw, flags=re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    # 基本校验
    if not obj.get("search") and not obj.get("result"):
        return None
    return obj


def _validate_rule(rule: dict[str, Any]) -> bool:
    """按 AGENT_PROMPT 校验规则合法性。"""
    conf = rule.get("confidence", 0.0)
    if conf < config.MIN_CONFIDENCE:
        return False
    search = rule.get("search", {})
    result = rule.get("result", {})
    # 对于「扫码直达」型站点，可能没有搜索框，但必须有结果容器
    has_search = bool(search.get("input_selector"))
    has_result = bool(result.get("container_selector"))
    return has_search or has_result


class LearningAgent:
    """学习 Agent 封装。供 RuleEngine 调用。"""

    def learn(self, url: str, html: str) -> dict[str, Any] | None:
        """学习网站导航规则，返回规则 dict 或 None。"""
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.lower()
        system = _system_prompt()
        stripped = _strip_html(html)
        user = (
            f"Website URL: {url}\n"
            f"Domain: {domain}\n"
            f"HTML (stripped):\n{stripped}\n\n"
            "Analyze this traceability page and generate the navigation rule JSON."
        )
        try:
            raw = _call_llm(system, user)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM call failed: %s", exc)
            
            return None

        rule = _parse_rule(raw)
        result = "success" if (rule and _validate_rule(rule)) else "failed"
        
        if rule and _validate_rule(rule):
            rule.setdefault("domain", domain)
            logger.info("learned rule for %s confidence=%.2f", domain, rule.get("confidence", 0))
            return rule
        logger.info("rule rejected for %s", domain)
        return None

    def extract_with_llm(self, url: str, html: str) -> dict[str, str]:
        """LLM 兜底字段提取：把页面交给 LLM，要求输出标准字段 JSON。"""
        from app.config import STANDARD_FIELDS
        domain = url.split("/")[2] if "/" in url else url
        stripped = _strip_html(html, limit=20000)
        system = (
            "你是溯源信息提取助手。从给定的溯源网页 HTML 中提取产品信息，"
            "映射到以下标准字段（找不到的留空字符串）：\n"
            + ", ".join(STANDARD_FIELDS)
            + "\n只返回 JSON，不要解释。"
        )
        user = f"URL: {url}\nHTML:\n{stripped}"
        try:
            raw = _call_llm(system, user)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM extract failed: %s", exc)
            return {}
        m = re.search(r"\{.*\}", raw, flags=re.S)
        if not m:
            return {}
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
        # 只保留标准字段
        return {k: str(v).strip() for k, v in obj.items()
                if k in STANDARD_FIELDS and v}

    def _log(self, domain: str, agent_input: str, agent_output: str, result: str) -> None:
        try:
            with get_session() as s:
                s.add(TraceLearningLog(
                    domain=domain,
                    agent_input=agent_input[:5000],
                    agent_output=agent_output[:5000],
                    result=result,
                ))
        except Exception:  # noqa: BLE001
            logger.debug("learning log failed", exc_info=True)
    def learn_navigation(self, url: str, html: str) -> dict[str, Any] | None:
        """按 AGENT_PROMPT.md 学习站点的导航规则。
        LLM 只分析页面结构，不提取业务字段。返回规则 dict 或 None。
        """
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.lower()
        stripped = _strip_html(html, limit=30000)
        system = (
            "You are an expert website analysis agent.\n"
            "Your task is to learn how a traceability website works and generate reusable navigation rules.\n"
            "You are NOT responsible for extracting business fields.\n"
            "Your responsibility is only:\n"
            "1. Find query input\n2. Find query button\n"
            "3. Determine whether detail page is required\n"
            "4. Find detail page entry\n5. Identify result container\n"
            "6. Generate navigation rules\n\n"
            "The generated rules must be reusable. Never use temporary IDs.\n"
            "Prefer stable selectors.\n\n"
            "IMPORTANT: Many traceability sites render the product info directly on the "
            "QR code URL page (no search form needed). For those sites, set input_selector "
            "and button_selector to empty strings, need_detail_page to true if there is a "
            "'详情' or detail link, and set detail_selector to the CSS selector for that link.\n"
            "If the page IS the detail page (no further navigation needed), set need_detail_page to false.\n\n"
            "Return ONLY valid JSON, no markdown, no explanations:\n"
            '{"domain":"","search":{"input_selector":"","button_selector":"","input_type":"text"},'
            '"navigation":{"need_detail_page":false,"detail_selector":""},'
            '"result":{"container_selector":""},"confidence":0.0}'
        )
        user = (
            f"Website URL: {url}\n"
            f"Domain: {domain}\n"
            f"HTML:\n{stripped}\n\n"
            "Analyze this traceability page. Return the navigation rule JSON only."
        )
        try:
            raw = _call_llm(system, user)
        except Exception as exc:
            logger.warning("learn_navigation LLM failed: %s", exc)
            return None
        rule = _parse_rule(raw)
        if rule and _validate_rule(rule):
            rule.setdefault("domain", domain)
            logger.info("learned nav rule for %s conf=%.2f", domain, rule.get("confidence", 0))
            return rule
        logger.info("nav rule rejected for %s", domain)
        return None
