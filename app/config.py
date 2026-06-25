"""应用配置。环境变量优先，缺省值兼容本地开发与 DashScope/通义千问。"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# ---- 缓存 ----
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "86400"))  # 24h

# ---- Playwright ----
BROWSER_HEADLESS = os.getenv("BROWSER_HEADLESS", "1") == "1"
NAV_TIMEOUT_MS = int(os.getenv("NAV_TIMEOUT_MS", "25000"))
WAIT_AFTER_LOAD_MS = int(os.getenv("WAIT_AFTER_LOAD_MS", "1200"))

# ---- LLM（兼容 OpenAI SDK，默认走 DashScope 通义千问） ----
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
LLM_API_KEY = os.getenv("DASHSCOPE_API_KEY", os.getenv("LLM_API_KEY", ""))
LLM_MODEL = os.getenv("LLM_MODEL", "qwen-plus")

# 规则学习相关阈值
MIN_CONFIDENCE = 0.5
RULE_AUTO_REPAIR = os.getenv("RULE_AUTO_REPAIR", "1") == "1"

STANDARD_FIELDS = [
    "goods_name", "company_name", "crop_category", "mobile", "reg_address",
    "sale_area", "unit_code", "batch_number", "brand", "query_count",
    "germination_rate", "purity", "cleanliness", "moisture", "seed_category",
    "origin", "test_date", "warranty_period", "consult_service", "supplier",
    "characteristics", "cultivation_points", "risk_warning", "trace_website",
    "license_number", "planting_season",
]
