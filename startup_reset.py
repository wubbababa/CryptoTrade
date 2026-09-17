"""启动清库与远端持仓重建。

需求：**每次启动都清空本地数据库，并把远端数据同步到本地**。本模块实现该行为，
并尽量守住项目既有的安全边界：

- **只清空本地业务表**（`telegram_messages`、`trade_instances`、`orders`、`positions`、
  `commands`、`exchange_events`）；保留 `runtime_state`（Telegram 轮询断点，避免重启后
  重复消费或漏读公告）与 `audit_logs`（审计追溯，含本次清库记录本身）。
- **只读访问交易所**：仅调用 `validate_account()` 与 `get_positions()`，
  绝不下单、撤单或平仓。
- **先拉取、后清空**：所有可用交易所的远端持仓会在清空前全部读取完成；若没有任何一家
  能读通，则**放弃清空**并告警——在没有事实来源的情况下销毁本地数据只会让状态更糟。
- **只重建 `positions` 快照**：`orders.client_order_id` 是 SHA-256 摘要
  （见 `trading_service._client_order_id`），无法反解出交易编号或原始公告；止盈止损参数
  也只存在于本地 `commands.payload_json`。因此本模块**不伪造** `trade_instances` / `orders`
  记录。清库后自动保本与保护单补建不会再作用于历史交易，这是本需求的已知代价。

用法：由 `Application.run()` 在启动对账前自动调用，无需人工执行。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from database import Database
from exchange_router import ExchangeRouter
from models import Exchange, PositionSnapshot

logger = logging.getLogger(__name__)

# 需要清空的业务表；顺序无关，但保持稳定便于测试与阅读。
# runtime_state 与 audit_logs 明确不在其中（见模块文档）。
BUSINESS_TABLES: tuple[str, ...] = (
    "exchange_events",
    "orders",
    "positions",
    "commands",
    "trade_instances",
    "telegram_messages",
)


@dataclass
class StartupResetReport:
    """一次启动清库的结果，供调用方记录日志与通知。"""

    # 每张业务表实际删除的行数（表名 -> 行数）。
    cleared: dict[str, int] = field(default_factory=dict)
    # 成功读取远端持仓的交易所及其仓位数量。
    synced_positions: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # 非空表示本次**没有**清空数据库，值为原因。
    skipped_reason: str | None = None

    @property
    def cleared_rows(self) -> int:
        return sum(self.cleared.values())

    def summary(self) -> str:
        if self.skipped_reason:
            return f"启动清库已跳过：{self.skipped_reason}"
        detail = "、".join(f"{table}={count}" for table, count in self.cleared.items())
        positions = sum(self.synced_positions.values())
        return (f"启动已清空本地业务表（{detail}），并从远端重建 {positions} 个持仓快照"
                f"（{self.synced_positions or '无'}）")


class StartupResetter:
    """启动时清空本地业务表，并以交易所为事实来源重建持仓快照。"""

    def __init__(self, database: Database, router: ExchangeRouter, notifier=None) -> None:
        self.database = database
        self.router = router
        self.notifier = notifier

    async def reset(self) -> StartupResetReport:
        report = StartupResetReport()
        # 第一步：只读拉取远端持仓。此时尚未清空任何本地数据，失败可安全放弃。
        snapshots: dict[Exchange, list[PositionSnapshot]] = {}
        for exchange, adapter in list(self.router.adapters.items()):
            try:
                await adapter.validate_account()
                snapshots[exchange] = await adapter.get_positions()
            except Exception as exc:
                warning = f"{exchange.value} 启动清库前读取远端持仓失败，该交易所仓位不会写入本地：{exc}"
                report.warnings.append(warning)
                logger.error(warning)
                await self._audit("STARTUP_RESET", exchange.value, f"REMOTE_READ_FAILED: {exc}")
        if not snapshots:
            # 全部交易所不可读：清空后既没有远端快照可恢复，也会丢失本地唯一的交易线索。
            report.skipped_reason = "所有交易所的远端持仓均读取失败，已放弃清空本地数据库"
            report.warnings.append(report.skipped_reason)
            logger.critical(report.skipped_reason)
            await self._audit("STARTUP_RESET", None, "SKIPPED", after={"reason": report.skipped_reason})
            await self._notify("CRITICAL", "STARTUP_RESET_SKIPPED", report.skipped_reason)
            return report

        # 第二步：清空业务表（保留 runtime_state 与 audit_logs）。
        report.cleared = await self._clear_business_tables()
        # 第三步：写入远端持仓快照。positions 已在上一步清空，此处只需新增有效仓位。
        for exchange, positions in snapshots.items():
            active = [item for item in positions if item.quantity > 0]
            for position in active:
                await self.database.execute(
                    "INSERT INTO positions(exchange,instrument_key,side,quantity,average_price) "
                    "VALUES(?,?,?,?,?) "
                    "ON CONFLICT(exchange,instrument_key,side) DO UPDATE SET "
                    "quantity=excluded.quantity,average_price=excluded.average_price,"
                    "captured_at=CURRENT_TIMESTAMP",
                    (exchange.value, position.instrument_key, position.side.value,
                     str(position.quantity), str(position.average_price)),
                )
            report.synced_positions[exchange.value] = len(active)

        summary = report.summary()
        logger.warning(summary)
        await self._audit("STARTUP_RESET", None, "SUCCESS", after={
            "cleared": report.cleared,
            "positions": report.synced_positions,
            "warnings": report.warnings,
        })
        await self._notify("WARNING", "STARTUP_RESET", summary,
                           details={"cleared": report.cleared, "positions": report.synced_positions})
        return report

    async def _clear_business_tables(self) -> dict[str, int]:
        """逐表清空并返回删除行数；使用 DELETE 保留表结构，便于重启后直接复用。"""
        cleared: dict[str, int] = {}
        for table in BUSINESS_TABLES:
            cleared[table] = await self.database.execute(f"DELETE FROM {table}")
        return cleared

    async def _audit(self, category: str, subject_id: str | None, result: str,
                     before=None, after=None) -> None:
        await self.database.audit(category, subject_id, result, before, after)

    async def _notify(self, level: str, event: str, message: str, details: dict | None = None) -> None:
        if self.notifier is not None:
            await self.notifier.emit(level, event, message, details=details)


async def run_startup_reset(database: Database, router: ExchangeRouter,
                            notifier=None) -> StartupResetReport:
    """供启动流程与测试复用的入口。"""
    return await StartupResetter(database, router, notifier).reset()
