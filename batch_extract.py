#!/usr/bin/env python3
"""批量提取：按域名去重，抓摘要页->详情页，提取全部标准字段，存入 SQLite。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from html import unescape as html_unescape
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

from app.alias import match_field
from app.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, STANDARD_FIELDS
from app.field_extractor import extract_fields as struct_extract

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "goods_qrcode_info.json"

UA = ("Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")

# ---- LLM 兜底提取 ----
_cli = None
def _get_client():
    global _cli
    if _cli is None and LLM_API_KEY:
        from openai import OpenAI
        _cli = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, timeout=30)
    return _cli

def llm_extract(html: str, url: str) -> dict[str, str]:
    cli = _get_client()
    if not cli:
        return {}
    s = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.S | re.I)
    s = re.sub(r"<style[^>]*>.*?</style>", "", s, flags=re.S | re.I)
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"\s+", " ", s).strip()[:12000]
    system = (
        "你是溯源信息提取助手。从给定的溯源网页 HTML 中提取产品信息，"
        "映射到以下标准字段（找不到的留空字符串）：\n"
        + ", ".join(STANDARD_FIELDS)
        + "\n注意：trace_website 不需要提取。只返回 JSON 对象，不要解释。"
    )
    user = f"URL: {url}\nHTML:\n{s}"
    try:
        resp = cli.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0.1,
        )
        raw = resp.choices[0].message.content or ""
    except Exception as exc:
        print(f"  LLM error: {exc}", flush=True)
        return {}
    m = re.search(r"\{.*\}", raw, flags=re.S)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return {k: str(v).strip() for k, v in obj.items()
            if k in STANDARD_FIELDS and v and k != "trace_website"}


# ---- 无效页面检测 ----
INVALID_MARKERS = [
    "服务到期", "已停止服务", "系统到期",
    "页面不存在", 
    "二维码已失效", "追溯码无效",
    "产品已下架", "访问出错",
]

def is_invalid_page(html: str, text: str) -> bool:
    """检测是否为无效页面（服务到期/404/失效等）。只检测可见文本，不扫描HTML中的JS/CSS。"""
    t = (text or "").strip()
    if len(t) < 20:
        return True
    for marker_text in INVALID_MARKERS:
        if marker_text in t:
            return True
    return False

# ---- 找详情页链接 ----
DETAIL_KEYWORDS = ["description", "detail", "info", "trace", "getmsg", "product"]
# 优先级高的详情页文本（排除"追溯网址"，它通常指向网站首页而非产品详情）
DETAIL_TEXTS_PRIMARY = ["查看产品详情", "产品详情", "品种详情", "查看详情", "详细信息", "查看产品", "进入追溯"]
DETAIL_TEXTS_SECONDARY = ["追溯网址"]

def _context_text(a) -> str:
    """?? <a> ??????????????? span/label ??"""
    parts = []
    # ????
    sib = a.previous_sibling
    while sib:
        t = getattr(sib, "get_text", lambda: str(sib))() if hasattr(sib, "get_text") else str(sib)
        t = t.strip()
        if t:
            parts.append(t)
            break
        sib = getattr(sib, "previous_sibling", None)
    # ??????? <a> ?????
    parent = a.parent
    if parent:
        pt = parent.get_text(separator=" ", strip=True)
        at = (a.get_text() or "").strip()
        if pt and at and pt != at:
            parts.append(pt)
    return " ".join(parts)


def find_detail_url(html: str, base_url: str) -> str | None:
    """从摘要页找出详情页链接。优先匹配文本关键词，其次匹配 href 关键词。"""
    # Pass 0: try to find a trace-website URL from table content
    trace_url = _find_trace_url_in_content(html, base_url)
    if trace_url:
        return trace_url

    soup = BeautifulSoup(html, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        text = (a.get_text() or "").strip()
        href = a["href"].strip()
        ctx = _context_text(a)
        combined = f"{text} {ctx}"
        links.append((a, text, href, combined))

    # Pass 1: PRIMARY 文本匹配（查看产品详情/产品详情/品种详情等）
    for a, text, href, combined in links:
        if any(kw in combined for kw in DETAIL_TEXTS_PRIMARY):
            resolved = _resolve_href(href, base_url)
            if resolved:
                return resolved

    # Pass 2: SECONDARY 文本匹配（追溯网址），排除指向首页/根域名的链接
    for a, text, href, combined in links:
        if any(kw in combined for kw in DETAIL_TEXTS_SECONDARY):
            if not _is_homepage_link(href, base_url):
                resolved = _resolve_href(href, base_url)
                if resolved:
                    return resolved

    # Pass 3: href 路径关键词匹配（description/detail/product 等），排除 trace
    detail_kws = [k for k in DETAIL_KEYWORDS if k != "trace"]
    for a, text, href, combined in links:
        if href.startswith("javascript:"):
            continue
        href_low = href.lower()
        if any(kw in href_low for kw in detail_kws):
            return urljoin(base_url, href)
        # PostBack 里的 URL
        m = re.search(r'["\']([^"\']*(?:description|detail|info)[^"\']*)["\']', href)
        if m:
            return urljoin(base_url, m.group(1))

    # Pass 4: inb123 风格 /s/ /t/ /c/ 路径
    for a, text, href, combined in links:
        if re.search(r'/[stc]/\d+', href) and not href.startswith("javascript:"):
            return urljoin(base_url, href)

    return None


def _resolve_href(href: str, base_url: str) -> str | None:
    """解析 href 为完整 URL。处理 javascript:PostBack 和 HTML 实体。"""
    if not href:
        return None
    if href.startswith("javascript:"):
        m = re.search(r'["\']([^"\']*(?:description|detail|info)[^"\']*)["\']', href)
        if m:
            return urljoin(base_url, html.unescape(m.group(1)))
        return None
    import html as _html_mod; return urljoin(base_url, _html_mod.unescape(href))


def _is_homepage_link(href: str, base_url: str) -> bool:
    """判断链接是否指向首页/根域名（而非产品详情）。"""
    resolved = urljoin(base_url, href)
    parsed = urlparse(resolved)
    path = parsed.path.rstrip("/")
    # 只有域名、无路径，或路径就是 /index 之类的，视为首页
    if not path or path in ("/index", "/index.html", "/home"):
        return True
    return False

async def fetch_page(page, url: str, timeout_ms: int = 12000) -> tuple[str, str, int]:
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(1200)
        try:
            await page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        html = await page.content()
        final_url = page.url
        status = resp.status if resp else 0
        return html, final_url, status
    except Exception:
        return "", url, 0


async def run(limit: int = 0):
    with open(DATA_FILE, encoding="utf-8-sig") as f:
        items = json.load(f)
    seen: set[str] = set()
    urls: list[tuple[str, str, str]] = []
    for it in items:
        u = (it.get("url") or "").strip()
        if not u:
            continue
        d = urlparse(u).netloc.lower()
        if d in seen:
            continue
        seen.add(d)
        urls.append((it.get("id", ""), u, d))

    if limit:
        urls = urls[:limit]
    total = len(urls)
    print(f"Unique domains: {total}", flush=True)

    results: list[dict] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=UA, viewport={"width": 414, "height": 896}, locale="zh-CN",
        )
        page = await context.new_page()

        for i, (item_id, url, domain) in enumerate(urls, 1):
            t0 = time.time()
            rec: dict = {
                "id": item_id, "url": url, "domain": domain,
                "detail_url": "", "status": 0, "fields": {},
                "field_count": 0, "elapsed_ms": 0, "error": "",
                "method": "",
            }
            try:
                html, final_url, status = await fetch_page(page, url)
                rec["status"] = status
                if not html:
                    rec["error"] = "fetch failed"
                    results.append(rec)
                    print(f"[{i}/{total}] FAIL {domain} | fetch failed", flush=True)
                    continue

                # 检测无效页面（服务到期、404、空内容等），不保存
                from bs4 import BeautifulSoup as _BS_tmp
                _soup_tmp = _BS_tmp(html, "lxml")
                _text_tmp = _soup_tmp.get_text(separator=" ", strip=True)
                if is_invalid_page(html, _text_tmp):
                    rec["error"] = "invalid page (expired/error/empty)"
                    rec["status"] = status
                    results.append(rec)
                    print(f"[{i}/{total}] SKIP {domain} | invalid page", flush=True)
                    continue

                detail_url = find_detail_url(html, final_url)
                rec["detail_url"] = detail_url or ""

                extract_html = html
                extract_url = final_url

                # First, try keyword detail buttons/links on the current page (more reliable)
                _detail_keywords = ["查看产品详情", "产品详情", "品种详情", "详细信息", "查看详情", "查看产品", "详细"]
                for _hop in range(3):
                    _navigated = False
                    for _kw in _detail_keywords:
                        try:
                            for _sel in [f'input[type=submit][value*="{_kw}"]', f'button:has-text("{_kw}")', f'a:has-text("{_kw}")']:
                                _btn = await page.query_selector(_sel)
                                if _btn and await _btn.is_visible():
                                    await _btn.click()
                                    await page.wait_for_timeout(3000)
                                    try:
                                        await page.wait_for_load_state("networkidle", timeout=8000)
                                    except:
                                        pass
                                    _navigated = True
                                    extract_html = await page.content()
                                    extract_url = page.url
                                    break
                        except Exception:
                            continue
                        if _navigated:
                            break
                    if not _navigated:
                        break

                # Then try fallback URL discovery (for 追溯网址 links to real detail pages)
                if detail_url:
                    dhtml, dfinal, dstatus = await fetch_page(page, detail_url)
                    if dhtml and len(dhtml) > len(extract_html):
                        extract_html = dhtml
                        extract_url = dfinal
                        rec["status"] = dstatus

                # Multi-hop: continue clicking detail buttons on subsequent pages
                # (e.g. "查看产品详情" ASP.NET postback buttons)
                _detail_keywords = ["查看产品详情", "产品详情", "品种详情", "详细信息", "查看详情", "查看产品", "详细"]
                for _hop in range(3):
                    _navigated = False
                    for _kw in _detail_keywords:
                        try:
                            for _sel in [f'input[type=submit][value*="{_kw}"]', f"button:has-text('{_kw}')", f"a:has-text('{_kw}')"]:
                                _btn = await page.query_selector(_sel)
                                if _btn and await _btn.is_visible():
                                    await _btn.click()
                                    await page.wait_for_timeout(3000)
                                    try:
                                        await page.wait_for_load_state("networkidle", timeout=8000)
                                    except:
                                        pass
                                    _navigated = True
                                    break
                        except Exception:
                            continue
                        if _navigated:
                            break
                    if not _navigated:
                        break
                    # Refresh HTML after navigation
                    extract_html = await page.content()
                    extract_url = page.url

                # In-page reveal: trigger onclick toggles that render hidden data via JS
                # (e.g. <span onclick="showInfo()">\u8ffd\u6eaf\u7f51\u5740</span>)
                try:
                    reveal_els = page.query_selector_all("[onclick]:not(button[type=submit])")
                    for _el in reveal_els:
                        try:
                            _h = (_el.get_attribute("onclick") or "").strip().lower()
                            if _h and "location" not in _h and "href" not in _h and "window.open" not in _h:
                                if _el.is_visible():
                                    _el.click()
                                    await page.wait_for_timeout(600)
                                    try:
                                        await page.wait_for_load_state("networkidle", timeout=3000)
                                    except:
                                        pass
                        except Exception:
                            continue
                    await page.wait_for_timeout(400)
                    dhtml = await page.content()
                    if dhtml and len(dhtml) > len(extract_html):
                        extract_html = dhtml
                except Exception:
                    pass

                # Capture initial fields before clicking tabs (tabs replace content)
                _initial_fields_batch = {k: v for k, v in struct_fields.items()}

                # Click each tab, extract, and merge fields
                _tab_fields_batch = {}
                for _tt in ["产品信息", "企业信息", "详细信息", "品种信息", "生产经营者信息", "生产信息", "追溯信息"]:
                    try:
                        _tel = await page.query_selector(f"text={_tt}")
                        if _tel and await _tel.is_visible():
                            await _tel.click()
                            await page.wait_for_timeout(1500)
                            try: await page.wait_for_load_state("networkidle", timeout=3000)
                            except: pass
                            _th = await page.content()
                            _tf = extract_fields(_th, extract_url)
                            for _tk, _tv in _tf.items():
                                if _tk in STANDARD_FIELDS and _tv and _tk not in _tab_fields_batch:
                                    _tab_fields_batch[_tk] = _tv
                    except Exception:
                        pass

                struct_fields = struct_extract(extract_html, extract_url)
                struct_fields.pop("trace_website", None)
                struct_fields = {k: v for k, v in struct_fields.items()
                                 if k in STANDARD_FIELDS and v}

                # LLM 兜底：只在缺少 goods_name 或 company_name 时调用
                llm_fields: dict[str, str] = {}
                if not struct_fields.get("goods_name") or not struct_fields.get("company_name"):
                    llm_fields = llm_extract(extract_html, extract_url)

                merged = dict(struct_fields)
                for _tk, _tv in _initial_fields_batch.items():
                    if _tv and not merged.get(_tk):
                        merged[_tk] = _tv
                for _tk, _tv in _tab_fields_batch.items():
                    if _tv and not merged.get(_tk):
                        merged[_tk] = _tv
                for k, v in llm_fields.items():
                    if k in STANDARD_FIELDS and v and not merged.get(k):
                        merged[k] = v

                merged["trace_website"] = url

                gn = merged.get("goods_name", "")
                cn = merged.get("company_name", "")
                goods_code = hashlib.md5(f"{url}{gn}{cn}".encode("utf-8")).hexdigest()
                merged["goods_code"] = goods_code

                rec["fields"] = merged
                rec["field_count"] = len([v for v in merged.values() if v])
                rec["method"] = "struct+llm" if llm_fields else "struct"

                if not gn and not cn:
                    rec["error"] = "no key fields"

            except Exception as exc:
                rec["error"] = str(exc)[:200]

            rec["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
            results.append(rec)

            tag = "OK" if rec["field_count"] >= 3 else "WEAK" if rec["field_count"] > 0 else "FAIL"
            gn = rec["fields"].get("goods_name", "")[:20]
            print(f"[{i}/{total}] {tag} {domain} | {rec['field_count']}f | "
                  f"{rec['elapsed_ms']}ms | {gn}", flush=True)

            if i % 10 == 0:
                _save_json(results)

        await context.close()
        await browser.close()

    _save_json(results)
    _print_summary(results)



def _save_json(results: list[dict]) -> None:
    out_path = ROOT / "data" / "extract_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    import json
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"  [saved {len(results)} records to JSON]", flush=True)
def _print_summary(results: list[dict]) -> None:
    ok = [r for r in results if r["field_count"] >= 3]
    weak = [r for r in results if 0 < r["field_count"] < 3]
    fail = [r for r in results if r["field_count"] == 0]
    print(f"\n{'='*60}")
    print(f"Total: {len(results)}  OK(>=3 fields): {len(ok)}  "
          f"Weak: {len(weak)}  Fail: {len(fail)}")
    if ok:
        avg = sum(r["field_count"] for r in ok) / len(ok)
        print(f"Avg fields (OK): {avg:.1f}")
    print(f"\nOK domains:")
    for r in ok:
        gn = r["fields"].get("goods_name", "")[:25]
        cn = r["fields"].get("company_name", "")[:25]
        print(f"  {r['domain']:30s} | {r['field_count']}f | {gn} | {cn}")
    if fail:
        print(f"\nFailed domains:")
        for r in fail:
            print(f"  {r['domain']:30s} | {r['error'][:50]}")


if __name__ == "__main__":
    lim = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 0
    asyncio.run(run(lim))
def _find_trace_url_in_content(html: str, base_url: str) -> str | None:
    """Search page for a trace-website label followed by a URL in an adjacent cell."""
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin
    soup = BeautifulSoup(html, "lxml")
    trace_labels = ["\u8ffd\u6eaf\u7f51\u5740", "\u8ffd\u6eaf\u7f51\u7ad9", "\u8ffd\u6eaf\u5730\u5740", "\u6e90\u6eaf\u7f51\u5740", "\u6e90\u6eaf\u5730\u5740"]
    for label_text in trace_labels:
        for th in soup.find_all(["th", "td"]):
            if label_text not in (th.get_text(strip=True) or ""):
                continue
            td = th.find_next_sibling("td")
            if td:
                a = td.find("a", href=True)
                if a:
                    href = a["href"].strip()
                    resolved = urljoin(base_url, href)
                    if not _is_homepage_link(href, base_url) and not resolved == base_url.rstrip("/"):
                        return resolved
                txt = td.get_text(strip=True)
                if txt.startswith(("http://", "https://")):
                    resolved = urljoin(base_url, txt)
                    if not resolved == base_url.rstrip("/"):
                        return resolved
            parent_tr = th.find_parent("tr")
            if parent_tr:
                cells = parent_tr.find_all(["th", "td"])
                for cell in cells:
                    if cell is th:
                        continue
                    a = cell.find("a", href=True)
                    if a:
                        href = a["href"].strip()
                        resolved = urljoin(base_url, href)
                        if not _is_homepage_link(href, base_url) and not resolved == base_url.rstrip("/"):
                            return resolved
                    txt = cell.get_text(strip=True)
                    if txt.startswith(("http://", "https://")):
                        resolved = urljoin(base_url, txt)
                        if not resolved == base_url.rstrip("/"):
                            return resolved
    return None

