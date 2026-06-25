"""结果缓存（F005）。纯内存，键: url，默认 24h TTL。"""
from __future__ import annotations

import time
from typing import Any

from app.config import CACHE_TTL_SECONDS

_cache: dict[str, tuple[float, dict[str, Any]]] = {}

def get_cached(domain: str, url: str) -> dict[str, Any] | None:
    key = url
    item = _cache.get(key)
    if not item:
        return None
    ts, data = item
    if time.time() - ts > CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return data

def set_cached(domain: str, url: str, data: dict[str, Any]) -> None:
    _cache[url] = (time.time(), data)

def clear() -> None:
    _cache.clear()