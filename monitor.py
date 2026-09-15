"""启动对账和交易所事件监控。"""

from __future__ import annotations

import asyncio
import json
import logging

from database import Database
from exchange_router import ExchangeRouter

logger = logging.getLogger(__name__)


class Monitor:
    def __init__(self, router: ExchangeRouter, database: Database) -> None:
        self.router = router
        self.database = database

    async def reconcile(self) -> list[str]:
        """以交易所为最终事实来源，刷新仓位快照并报告未关联状态。"""
        warnings: list[str] = []
        # 使用副本遍历，连接或鉴权失败时可安全移除对应适配器。
        for exchange, adapter in list(self.router.adapters.items()):
            try:
                await adapter.validate_account()
                positions = await adapter.get_positions()
                orders = await adapter.get_open_orders()
                for position in positions:
                    await self.database.execute(
                        "INSERT INTO positions(exchange,instrument_key,side,quantity,average_price) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(exchange,instrument_key,side) DO UPDATE SET quantity=excluded.quantity,"
                        "average_price=excluded.average_price,captured_at=CURRENT_TIMESTAMP",
                        (exchange.value, position.instrument_key, position.side.value,
                         str(position.quantity), str(position.average_price)),
                    )
                local = await self.database.fetch_all(
                    "SELECT client_order_id FROM orders WHERE status IN ('NEW','PARTIALLY_FILLED') AND trade_id IN "
                    "(SELECT trade_id FROM trade_instances WHERE exchange=?)", (exchange.value,),
                )
                local_ids = {row["client_order_id"] for row in local}
                remote_ids = {order.client_order_id for order in orders}
                if local_ids != remote_ids:
                    warnings.append(f"{exchange.value} 远程数据和本地数据库挂单状态不一致，已禁止自动推断修改")
            except Exception as exc:
                warnings.append(f"{exchange.value} 对账失败并已禁用：{exc}")
                await adapter.close()
                self.router.adapters.pop(exchange, None)
        return warnings

    async def run_adapter(self, exchange, adapter) -> None:
        try:
            async for event in adapter.stream_market_and_account_events():
                await self.database.execute(
                    "INSERT INTO exchange_events(exchange,event_json) VALUES(?,?)",
                    (exchange.value, json.dumps(event, ensure_ascii=False, default=str)),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("%s 事件流断开，实时策略已暂停：%s", exchange.value, exc)

    def tasks(self) -> list[asyncio.Task]:
        return [asyncio.create_task(self.run_adapter(exchange, adapter), name=f"monitor-{exchange.value}")
                for exchange, adapter in self.router.adapters.items()]
