"""字段提取器（F004）。

支持四种结构：
  1. Table     —— <table><tr><th>标签</th><td>值</td></tr>
  2. Label     —— <span>标签</span><strong>值</strong> 或 <label>标签</label><span>值</span>
  3. Description —— <dl><dt>标签</dt><dd>值</dd></dl>
  4. Div       —— <div>标签：值</div> 或通用 key/value 文本

同时兼容结果容器内无显式结构的纯文本区块。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from bs4 import BeautifulSoup, Tag

from app.alias import match_field


@dataclass
class ExtractResult:
    fields: dict[str, str] = field(default_factory=dict)
    method: str = ""


def _clean(text: Optional[str]) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _set_if_known(out: dict[str, str], raw_key: str, raw_val: str) -> None:
    """将原始键值映射到标准字段；未识别的键以原文保留，便于 LLM 兜底。"""
    key = match_field(raw_key)
    val = _clean(raw_val)
    if not val:
        return
    if key and key in out and out[key]:
        return  # 不覆盖已存在值
    if key:
        out[key] = val
    else:
        # 未识别的原始键也保留，便于 LLM 兜底
        rk = _clean(raw_key)
        if rk and rk not in out:
            out[rk] = val


def extract_from_table(container: Tag) -> dict[str, str]:
    out: dict[str, str] = {}
    rows = container.find_all("tr")
    skip = False
    for idx, tr in enumerate(rows):
        if skip:
            skip = False
            continue
        cells = tr.find_all(["th", "td"])
        if len(cells) >= 2:
            has_td = any(c.name == "td" for c in cells)
            if not has_td and idx + 1 < len(rows):
                next_cells = rows[idx + 1].find_all(["td"])
                if len(next_cells) == len(cells):
                    for c_label, c_val in zip(cells, next_cells):
                        _set_if_known(out, c_label.get_text(), c_val.get_text())
                    skip = True
                    continue
            _set_if_known(out, cells[0].get_text(), cells[1].get_text())
        elif len(cells) == 1:
            txt = _clean(cells[0].get_text())
            if "\uff1a" in txt or ":" in txt:
                parts = re.split(r"[\uff1a:]", txt, 1)
                k, v = (parts[0], parts[1]) if len(parts) == 2 else (txt, "")
                _set_if_known(out, k, v)
    return out

def extract_from_dl(container: Tag) -> dict[str, str]:
    out: dict[str, str] = {}
    for dl in container.find_all("dl"):
        dts = dl.find_all("dt")
        dds = dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            _set_if_known(out, dt.get_text(), dd.get_text())
    for dt, dd in zip(container.find_all("dt"), container.find_all("dd")):
        _set_if_known(out, dt.get_text(), dd.get_text())
    return out


def extract_from_label(container: Tag) -> dict[str, str]:
    """<span>标签</span><strong>值</strong> 这类兄弟节点结构。"""
    out: dict[str, str] = {}
    for li in container.find_all("li"):
        spans = li.find_all("span")
        if len(spans) >= 2:
            _set_if_known(out, spans[0].get_text(), spans[1].get_text())
            continue
        sp = li.find("span")
        st = li.find("strong") or li.find("b")
        if sp and st:
            _set_if_known(out, sp.get_text(), st.get_text())
            continue
        txt = _clean(li.get_text())
        if txt and ("\uff1a" in txt or ":" in txt):
            parts = re.split(r"[\uff1a:]", txt, 1); k, v = (parts[0], parts[1]) if len(parts) == 2 else (txt, "")
            _set_if_known(out, k, v)
    for lab in container.find_all(["label"]):
        sib = lab.find_next_sibling()
        if sib:
            _set_if_known(out, lab.get_text(), sib.get_text())
    return out


def extract_from_div(container):
    """逐个 div/p 解析 标签:值 对。支持同一元素内多个 label:value 对。"""
    out = {}
    for el in container.find_all(["div", "p"]):
        txt = _clean(el.get_text())
        if not txt or ("：" not in txt and ":" not in txt):
            continue
        # 按行拆解，每行提一对 label:value
        for line in txt.split():
            if "：" in line:
                k, _, v = line.partition("：")
            elif ":" in line:
                k, _, v = line.partition(":")
            else:
                continue
            k, v = k.strip(), v.strip()
            if len(k) <= 12 and len(v) <= 200 and k and v:
                _set_if_known(out, k, v)
    return out


def extract_fields(html: str, final_url: str = "") -> dict[str, str]:
    """主入口：按优先级尝试多种结构，合并结果。返回标准字段字典。"""
    soup = BeautifulSoup(html, "lxml")
    container: Tag = soup

    out: dict[str, str] = {}

    if container.find("table"):
        out.update(extract_from_table(container))
    if container.find("dl"):
        merged = extract_from_dl(container)
        for k, v in merged.items():
            out.setdefault(k, v)
    li_res = extract_from_label(container)
    if li_res:
        for k, v in li_res.items():
            out.setdefault(k, v)
    div_res = extract_from_div(container)
    if div_res:
        for k, v in div_res.items():
            out.setdefault(k, v)

    # 追加 trace_website 字段
    if final_url and "trace_website" not in out:
        out["trace_website"] = final_url

    return out