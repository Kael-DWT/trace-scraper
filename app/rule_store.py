"""导航规则持久化存储。JSON 文件落地，服务重启不丢失。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

RULES_FILE = Path(__file__).resolve().parent.parent / "data" / "rules.json"


def _load() -> dict[str, Any]:
    try:
        RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(RULES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(rules: dict[str, Any]) -> None:
    RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RULES_FILE, "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)


def get_rule(domain: str) -> dict | None:
    rules = _load()
    return rules.get(domain)


def set_rule(domain: str, rule: dict) -> None:
    rules = _load()
    rule["updated_at"] = time.time()
    rules[domain] = rule
    _save(rules)


def list_rules() -> dict[str, Any]:
    return _load()