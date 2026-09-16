"""配置加载与密钥隔离。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from models import Exchange, normalize_asset


def _load_dotenv(path: Path) -> None:
    """轻量读取 .env，已存在的环境变量优先。"""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass(frozen=True, slots=True)
class Settings:
    raw: dict[str, Any]
    root: Path

    @classmethod
    def load(cls, config_path: str | Path = "config.yaml") -> "Settings":
        path = Path(config_path).resolve()
        _load_dotenv(path.parent / ".env")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(raw=raw, root=path.parent)

    @property
    def default_exchange(self) -> Exchange:
        """兼容旧调用方：返回默认交易所列表中的第一个交易所。"""
        return self.default_exchanges[0]

    @property
    def default_exchanges(self) -> tuple[Exchange, ...]:
        """获取公告未指定交易所时的默认执行目标，兼容旧版单值配置。"""
        trading = self.raw.get("trading", {})
        configured = trading.get("default_exchanges")
        if configured is None:
            configured = [trading.get("default_exchange", "OKX")]
        if isinstance(configured, str):
            configured = [configured]
        if not isinstance(configured, list) or not configured:
            raise ValueError("trading.default_exchanges 必须是非空交易所列表")
        try:
            exchanges = tuple(Exchange(str(item).upper()) for item in configured)
        except (TypeError, ValueError) as exc:
            raise ValueError("trading.default_exchanges 包含不支持的交易所") from exc
        if len(set(exchanges)) != len(exchanges):
            raise ValueError("trading.default_exchanges 不允许重复交易所")
        return exchanges

    @property
    def whitelist(self) -> set[str]:
        raw_list = self.raw.get("trading", {}).get("asset_whitelist", ["BTC", "ETH", "SOL"])
        result = set()
        for v in raw_list:
            norm = normalize_asset(str(v))
            if norm:
                result.add(norm)
            result.add(str(v).upper())
        return result

    def quantity_for(self, asset: str) -> Decimal | None:
        value = self.raw["trading"].get("quantity_by_asset", {}).get(asset.upper())
        return Decimal(str(value)) if value is not None else None

    def exchange_config(self, exchange: Exchange) -> dict[str, Any]:
        return self.raw["exchanges"][exchange.value]

    def enabled_exchanges(self) -> list[Exchange]:
        return [e for e in Exchange if self.exchange_config(e).get("enabled", False)]

    @property
    def database_path(self) -> Path:
        return self.root / self.raw.get("database_path", "trading.db")

    @property
    def log_path(self) -> Path:
        return self.root / self.raw.get("log_path", "logs/trading.jsonl")

    @property
    def allow_live_trading(self) -> bool:
        return os.getenv("ALLOW_LIVE_TRADING", "false").lower() == "true"

    @property
    def deepseek_api_key(self) -> str | None:
        return os.getenv("DEEPSEEK_API_KEY") or self.raw.get("deepseek", {}).get("api_key")

