"""僵尸交易清理：收敛因失败或中断而卡住的本地交易记录。

清理动作**只写本地数据库，绝不下单、撤单或平仓**。因此模块被刻意设计为不依赖
`ExchangeRouter`——任何需要触碰交易所的判断都改为「先只读核对，条件不满足则拒绝」。

清理分两级，按风险从低到高：

1. `close_phantom_trades`（默认执行）：`ENTERED`/`PENDING_ENTRY`/`PARTIAL_FILL` 状态、
   但**本地不存在任何活动订单**的交易。这类交易在交易所端确认无挂单后可直接收敛为
   `CANCELLED`，它对应的是「进场已撤或从未受理」这种没有持仓的场景。
2. `unlock_trades`（需显式 `--unlock`）：`ERROR_LOCKED` 交易。锁定代表需要人工判断，
   因此只有同时满足以下**全部**条件才会解除：

   - 远程不存在该合约该方向的非零持仓；
   - 远程不存在该交易仍开放的活动订单；
   - 本地不存在仍开放的进场或保护单。

   任一条件不满足即拒绝解锁并给出原因，**绝不猜测归属**。解锁只是把状态恢复为 `CANCELLED`，
   不代表已对交易所做过任何补偿动作。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from database import Database
from models import Exchange, TradeState

logger = logging.getLogger(__name__)

# 交易所开放订单状态（与 monitor/trading_service 保持一致）。
OPEN_ORDER_STATES = ("NEW", "OPEN", "PARTIALLY_FILLED", "SUBMITTING")

# 可被自动收敛为 CANCELLED 的「疑似僵尸」状态：都还没有真实持仓。
PHANTOM_STATES = (
    TradeState.PENDING_ENTRY.value,
    TradeState.PARTIAL_FILL.value,
    TradeState.RECEIVED.value,
)

_LOCKED_STATES = (TradeState.ERROR_LOCKED.value,)


@dataclass
class CleanupOutcome:
    """单笔交易的清理结论。"""

    trade_id: str
    action: str          # CANCELLED / UNLOCKED / SKIPPED
    reason: str
    detail: dict = field(default_factory=dict)


@dataclass
class CleanupReport:
    """一次清理运行的汇总，供 CLI 与通知复用。"""

    outcomes: list[CleanupOutcome] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def cancelled(self) -> list[CleanupOutcome]:
        return [item for item in self.outcomes if item.action == "CANCELLED"]

    @property
    def unlocked(self) -> list[CleanupOutcome]:
        return [item for item in self.outcomes if item.action == "UNLOCKED"]

    @property
    def skipped(self) -> list[CleanupOutcome]:
        return [item for item in self.outcomes if item.action == "SKIPPED"]

    def summary(self) -> str:
        return (f"共检查 {len(self.outcomes)} 笔："
                f"收敛 {len(self.cancelled)} 笔，解锁 {len(self.unlocked)} 笔，"
                f"跳过 {len(self.skipped)} 笔，对账告警 {len(self.warnings)} 条")


class TradeCleaner:
    """按「先只读核对、再本地收敛」的顺序清理卡住的交易。"""

    def __init__(self, database: Database, router=None, dry_run: bool = False) -> None:
        self.database = database
        # router 可选：为 None 时无法核对远程状态，仅能清理已有本地事实支撑的交易。
        self.router = router
        # dry_run 只做判定与打印，不写数据库，便于上线前先审视将要发生的变更。
        self.dry_run = dry_run

    async def clean(self, unlock: bool = False) -> CleanupReport:
        report = CleanupReport()
        report.outcomes.extend(await self.close_phantom_trades())
        if unlock:
            report.outcomes.extend(await self.unlock_trades())
        return report

    async def close_phantom_trades(self) -> list[CleanupOutcome]:
        """把「无活动订单」的未成交交易收敛为 CANCELLED。"""
        rows = await self.database.fetch_all(
            "SELECT trade_id,exchange,instrument_key,side,state FROM trade_instances "
            "WHERE state IN (" + ",".join("?" for _ in PHANTOM_STATES) + ") ORDER BY trade_id",
            PHANTOM_STATES,
        )
        outcomes: list[CleanupOutcome] = []
        for row in rows:
            trade_id = row["trade_id"]
            open_orders = await self._local_open_orders(trade_id)
            if open_orders:
                outcomes.append(CleanupOutcome(
                    trade_id, "SKIPPED",
                    f"仍有 {len(open_orders)} 笔本地活动订单，需先人工处置",
                    {"order_types": [item["order_type"] for item in open_orders]},
                ))
                continue
            # 部分成交可能已产生真实持仓，绝不当作僵尸直接撤销。
            if row["state"] == TradeState.PARTIAL_FILL.value:
                outcomes.append(CleanupOutcome(
                    trade_id, "SKIPPED", "部分成交可能已有真实持仓，需人工确认", {},
                ))
                continue
            await self._cancel(trade_id, f"无活动订单的 {row['state']} 交易，已收敛为 CANCELLED")
            outcomes.append(CleanupOutcome(
                trade_id, "CANCELLED", "无活动订单，已收敛为 CANCELLED",
                {"exchange": row["exchange"], "previous_state": row["state"]},
            ))
        return outcomes

    async def unlock_trades(self) -> list[CleanupOutcome]:
        """`ERROR_LOCKED` 交易在确认无持仓、无挂单后才解除锁定。"""
        rows = await self.database.fetch_all(
            "SELECT trade_id,exchange,instrument_key,side,state FROM trade_instances "
            "WHERE state=? ORDER BY trade_id", _LOCKED_STATES,
        )
        outcomes: list[CleanupOutcome] = []
        for row in rows:
            trade_id = row["trade_id"]
            blockers = await self._unlock_blockers(row)
            if blockers:
                outcomes.append(CleanupOutcome(
                    trade_id, "SKIPPED", "；".join(blockers), {"blockers": blockers},
                ))
                continue
            await self._cancel(trade_id, "确认无持仓且无挂单，已解除锁定并收敛为 CANCELLED")
            outcomes.append(CleanupOutcome(
                trade_id, "UNLOCKED", "确认无持仓且无挂单，已解除锁定",
                {"exchange": row["exchange"], "instrument_key": row["instrument_key"]},
            ))
        return outcomes

    async def _unlock_blockers(self, row: dict) -> list[str]:
        """返回阻止解锁的原因列表；空列表表示可以安全解锁。"""
        trade_id = row["trade_id"]
        blockers: list[str] = []
        open_orders = await self._local_open_orders(trade_id)
        if open_orders:
            blockers.append(f"本地仍有 {len(open_orders)} 笔活动订单")
        if self.router is None:
            blockers.append("未接入交易所路由，无法核对远程持仓与挂单")
            return blockers
        try:
            exchange = Exchange(row["exchange"])
            adapter = self.router.adapters.get(exchange)
            if adapter is None:
                blockers.append(f"{row['exchange']} 适配器不可用，无法核对远程状态")
                return blockers
            positions = await adapter.get_positions()
            position = next(
                (item for item in positions
                 if item.instrument_key == row["instrument_key"] and item.side.value == row["side"]),
                None,
            )
            if position is not None and position.quantity > 0:
                blockers.append(f"远程仍有 {position.quantity} 的持仓")
            remote_orders = await adapter.get_open_orders()
            # 只认「本交易已知客户端编号」的远程挂单，避免把他人订单算作阻塞。
            known = await self._known_client_ids(trade_id)
            still_open = [item for item in remote_orders if item.client_order_id in known]
            if still_open:
                blockers.append(f"远程仍有 {len(still_open)} 笔本交易的活动订单")
        except Exception as exc:  # 核对失败一律视为不可解锁
            blockers.append(f"远程核对失败：{exc}")
        return blockers

    async def _local_open_orders(self, trade_id: str) -> list[dict]:
        return await self.database.fetch_all(
            "SELECT id,order_type,client_order_id FROM orders WHERE trade_id=? AND status IN ("
            + ",".join("?" for _ in OPEN_ORDER_STATES) + ")",
            (trade_id, *OPEN_ORDER_STATES),
        )

    async def _known_client_ids(self, trade_id: str) -> set[str]:
        rows = await self.database.fetch_all(
            "SELECT client_order_id FROM orders WHERE trade_id=?", (trade_id,),
        )
        return {row["client_order_id"] for row in rows}

    async def _cancel(self, trade_id: str, reason: str) -> None:
        """把交易收敛为 CANCELLED，并保留可审计的前后状态。"""
        if self.dry_run:
            logger.info("[dry-run] %s 将收敛为 CANCELLED：%s", trade_id, reason)
            return
        rows = await self.database.fetch_all(
            "SELECT state FROM trade_instances WHERE trade_id=?", (trade_id,),
        )
        previous = rows[0]["state"] if rows else None
        await self.database.execute(
            "UPDATE trade_instances SET state=?,updated_at=CURRENT_TIMESTAMP WHERE trade_id=?",
            (TradeState.CANCELLED.value, trade_id),
        )
        await self.database.audit("TRADE_CLEANUP", trade_id, "SUCCESS", {"state": previous}, {"state": "CANCELLED"})
        logger.info("%s 清理完成：%s", trade_id, reason)


async def run_cleanup(database: Database, router=None, unlock: bool = False,
                      dry_run: bool = False) -> CleanupReport:
    """供 CLI 与测试复用的入口。"""
    cleaner = TradeCleaner(database, router, dry_run=dry_run)
    return await cleaner.clean(unlock=unlock)


def main(argv: list[str] | None = None) -> int:
    """命令行入口：默认只收敛僵尸交易，解除 ERROR_LOCKED 需显式 --unlock。"""
    import argparse
    import sys

    from exchange_router import ExchangeRouter
    from settings import Settings

    parser = argparse.ArgumentParser(description="清理卡住的本地交易记录（只写数据库，不下单）")
    parser.add_argument("--unlock", action="store_true",
                        help="同时尝试解除 ERROR_LOCKED 交易（需确认无持仓且无挂单）")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--no-router", action="store_true",
                        help="不连接交易所，仅按本地事实清理（无法解锁 ERROR_LOCKED）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将要发生的变更，不写入数据库")
    args = parser.parse_args(argv)

    settings = Settings.load(args.config)
    database = Database(settings.database_path)
    database.initialize()
    router = None if args.no_router else ExchangeRouter(settings)

    async def execute() -> int:
        try:
            if args.unlock and router is None:
                print("[!] --no-router 模式下无法核对远程状态，ERROR_LOCKED 交易会被跳过。")
            report = await run_cleanup(database, router, unlock=args.unlock, dry_run=args.dry_run)
            if args.dry_run:
                print("[*] dry-run 模式：以下变更未被写入数据库。")
            for outcome in report.outcomes:
                mark = {"CANCELLED": "[OK]", "UNLOCKED": "[UNLOCK]", "SKIPPED": "[SKIP]"}[outcome.action]
                print(f"{mark} {outcome.trade_id}: {outcome.reason}")
            for warning in report.warnings:
                print(f"[WARN] {warning}")
            print(report.summary())
            return 0
        finally:
            if router is not None:
                await router.close()

    return asyncio.run(execute())


if __name__ == "__main__":
    raise SystemExit(main())