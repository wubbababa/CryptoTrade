"""按交易环境读取 API 凭据，防止模拟盘与实盘 Key 混用。"""

from __future__ import annotations

import os


def credentials(exchange: str, mode: str, *names: str) -> tuple[str, ...]:
    """读取指定交易所、指定环境的一组凭据。"""
    profile = str(mode).upper()
    prefix = f"{exchange.upper()}_{profile}"
    values = tuple(os.getenv(f"{prefix}_{name}", "").strip() for name in names)
    missing = [name for name, value in zip(names, values) if not value]
    if missing:
        keys = ", ".join(f"{prefix}_{name}" for name in missing)
        raise ValueError(f"{exchange.upper()} {profile} 缺少环境变量：{keys}")
    return values
