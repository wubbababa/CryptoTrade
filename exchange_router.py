"""根据统一交易所枚举路由，业务层不接触具体适配器。"""

from __future__ import annotations

from exchanges import BinanceAdapter, GateAdapter, OKXAdapter
import logging
from decimal import Decimal

from exchanges.base import ExchangeAdapter, ExchangeError, PaperAdapter
from models import Exchange, Instrument
from settings import Settings


logger = logging.getLogger(__name__)


class ExchangeRouter:
    def __init__(self, settings: Settings) -> None:
        self.adapters: dict[Exchange, ExchangeAdapter] = {}
        # 保存初始化失败原因，供默认广播执行和启动报告准确说明跳过原因。
        self.unavailable_reasons: dict[Exchange, str] = {}
        factories = {Exchange.OKX: OKXAdapter, Exchange.BINANCE: BinanceAdapter, Exchange.GATE: GateAdapter}
        for exchange in settings.enabled_exchanges():
            cfg = settings.exchange_config(exchange)
            mode = str(cfg.get("mode", "LOCAL")).upper()
            try:
                if mode == "LIVE" and not settings.allow_live_trading:
                    raise ExchangeError("配置为实盘，但 ALLOW_LIVE_TRADING 未明确设为 true")
                if mode == "LOCAL":
                    self.adapters[exchange] = _local_adapter(exchange, cfg)
                else:
                    self.adapters[exchange] = factories[exchange](cfg)
            except ExchangeError as exc:
                # 单家连接配置失败时单独禁用，不阻断其他交易所监控。
                logger.error("已禁用 %s：%s", exchange.value, exc)
                self.unavailable_reasons[exchange] = str(exc)

    def get(self, exchange: Exchange) -> ExchangeAdapter:
        try:
            return self.adapters[exchange]
        except KeyError as exc:
            raise RuntimeError(f"交易所不可用：{exchange.value}") from exc

    async def close(self) -> None:
        for adapter in self.adapters.values():
            await adapter.close()


def _local_adapter(exchange: Exchange, config: dict) -> PaperAdapter:
    """仅供离线开发测试使用，不代表交易所官方模拟盘。"""
    symbols = {
        Exchange.OKX: lambda a: f"{a}-USDT-SWAP",
        Exchange.BINANCE: lambda a: f"{a}USDT",
        Exchange.GATE: lambda a: f"{a}_USDT",
    }
    raw_assets = config.get("market_stream_assets") or ["BTC", "ETH", "SOL", "XAU", "PAXG", "XAUT"]
    instruments = {
        asset: Instrument(
            f"{asset}/USDT:PERP",
            symbols[exchange](asset),
            Decimal("0.01"),
            Decimal("0.001"),
            Decimal("0.001"),
            Decimal("0"),
        )
        for asset in raw_assets
    }
    return PaperAdapter(instruments, Decimal(str(config.get("paper_equity_usdt", "10000"))))
