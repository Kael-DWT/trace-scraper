"""FastAPI 入口。提供查询接口（F001）。"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.config import BASE_DIR, STANDARD_FIELDS
from app.rule_engine import RuleEngine
from app.fetcher import normalize_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("trace")

app = FastAPI(title="Trace Source Universal Extractor", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
engine = RuleEngine()


class QueryRequest(BaseModel):
    url: str


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok"}


@app.post("/api/query")
def query(req: QueryRequest) -> dict[str, Any]:
    """F001 查询接口。输入 url，返回标准字段。"""
    url = normalize_url(req.url)
    logger.info("query url=%s", url[:80])
    try:
        raw = engine.query(url)
        source = raw.get("source", "live")
        fields = raw.get("fields", {})
        elapsed = raw.get("elapsed_ms", 0)

        # 构建完整的 27 字段输出
        out = _build_output(url, fields)

        ok = bool(out.get("goods_name") or out.get("company_name"))
        return {"success": ok, "data": {"source": source, "fields": out, "elapsed_ms": elapsed}}
    except Exception as exc:
        logger.exception("query failed")
        return {"success": False, "data": {"source": "error", "fields": _build_output(url, {}), "elapsed_ms": 0}}


def _build_output(url: str, fields: dict[str, str]) -> dict[str, Any]:
    """确保全部 27 个字段都有值，缺失的用空字符串。"""
    gn = fields.get("goods_name", "")
    cn = fields.get("company_name", "")
    out: dict[str, Any] = {f: fields.get(f, "") for f in STANDARD_FIELDS}
    # 计算 goods_code
    raw = f"{url}{gn}{cn}"
    out["goods_code"] = hashlib.md5(raw.encode("utf-8")).hexdigest()
    out["trace_website"] = url
    out["query_count"] = int(re.sub(r"\D", "", str(out.get("query_count", ""))) or 0)
    return out


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)