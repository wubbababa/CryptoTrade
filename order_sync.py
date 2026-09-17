"""远端→本地挂单状态同步。

当本地数据库与交易所的挂单状态出现偏差（启动对账报告「远程数据和本地数据库挂单状态不一致」）时，
以**交易所为最终事实来源**把本地 `orders` 表收敛到远端状态。本模块与 `trade_cleanup.py` 同属
运维工具，遵循同样的安全边界：

- **只写本地数据库，绝不下单、撤单或平仓**；
- 逐笔核对：本地开放订单仍在远端 → 保留开放并刷新累计成交；本地开放订单已不在远端 →
  用单笔订单查询接口确认最终状态（成交/撤销/过期/拒绝）后回填；
- 进场单终结且**零成交**时，把交易收敛为 `CANCELLED`（`RECEIVED` 收敛为 `REJECTED`）——
  零成交代表未产生任何仓位，收敛是安全的；
- 进场单**已有成交**但远端无对应持仓时**不猜测归属**，仅告警，交由启动对账恢复或
  `trade_cleanup.py` 处置；
- 远端存在但本地无任何记录的开放订单只告警（UNTRACKED），绝不凭空创建。

用法：

```bash
# 先预演，只打印将要发生的变更，不写库
py order_sync.py --dry-run

# 执行同步（只写本地数据库）
py order_sync.py

# 只同步指定交易所
py order_sync.py --exchange OKX
```
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from database import Database
from exchange_router import ExchangeRouter
from models import Exchange, OPEN_ORDER_STATES, TradeState
from settings import Settings
from state_manager import StateManager

logger = logging.getLogger(__name__)

# 终态集合：这些状态不会再变化，回填后可据此收敛交易状态。
TERMINAL_ORDER_STATES = ("FILLED", "CANCELED", "EXPIRED", "REJECTED")

# 各交易所原始状态值到系统标准状态的兜底映射（适配器层已做主要归一，此处防止遗漏）。
_STATUS_ALIASES = {
    "LIVE": "NEW",
    "OPEN": "NEW",
    "SUBMITTING": "NEW",
    "PLACED": "NEW",
    "PARTIAL": "PARTIALLY_FILLED",
    "PARTIAL_FILL": "PARTIALLY_FILLED",
    "PARTIALLY_FILLED": "PARTIALLY_FILLED",
    "FINISHED": "FILLED",
    "CANCELED": "CANCELED",
    "CANCELLED": "CANCELED",
    "MMP_CANCELED": "CANCELED",
    "EXPIRED_IN_MATCH": "EXPIRED",
}


def canonical_status(status: str) -> str:
    """把各交易所（可能小写/私有词汇）的订单状态归一为系统标准大写状态。"""
    value = str(status).upper()
    return _STATUS_ALIASES.get(value, value)


def _decimal(value) -> Decimal | None:
    """安全解析交易所原始数值字段；空值返回 None。"""
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _extract_fill(exchange: str, raw: dict, multiplier: Decimal) -> tuple[Decimal | None, Decimal | None]:
    """从交易所原始订单记录提取「累计成交数量（基础币单位）与成交均价」。"""
    if exchange == "OKX":
        filled = _decimal(raw.get("accFillSz"))
        if filled is not None:
            filled *= multiplier
        average = _decimal(raw.get("avgPx"))
    elif exchange == "BINANCE":
        filled = _decimal(raw.get("executedQty"))
        average = _decimal(raw.get("avgPrice"))
    else:  # GATE：size 为带符号委托量，left 为未成交余量，差值即累计成交（合约数）。
        size, left = _decimal(raw.get("size")), _decimal(raw.get("left"))
        filled = abs(size - left) * multiplier if size is not None and left is not None else None
        average = _decimal(raw.get("fill_price"))
    return filled, average


@dataclass
class OrderSyncOutcome:
    """单笔订单的同步结论。"""

    exchange: str
    client_order_id: str
    trade_id: str | None
    order_type: str
    previous_status: str
    new_status: str | None  # None 表示状态未变化（可能仅刷新成交量）
    note: str = ""


@dataclass
class OrderSyncReport:
    """一次同步运行的汇总，供 CLI 与测试复用。"""

    outcomes: list[OrderSyncOutcome] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked: int = 0  # 核对过的本地开放订单总数（含无需变更的）

    @property
    def changed(self) -> list[OrderSyncOutcome]:
        return [item for item in self.outcomes if item.new_status is not None]

    def summary(self) -> str:
        return (f"共核对 {self.checked} 笔本地开放订单，"
                f"状态更新 {len(self.changed)} 笔，告警 {len(self.warnings)} 条")


class RemoteOrderSync:
    """以交易所为事实来源，把本地开放订单状态收敛到远端。"""

    def __init__(self, database: Database, router: ExchangeRouter, dry_run: bool = False) -> None:
        self.database = database
        self.router = router
        self.states = StateManager(database)
        # dry_run 只判定与打印，不写数据库，便于先审视将要发生的变更。
        self.dry_run = dry_run
        # 运行期产生的告警先暂存，结束时统一并入报告。
        self._warnings: list[str] = []
        self._checked = 0

    async def sync(self, only_exchange: str | None = None) -> OrderSyncReport:
        """逐交易所执行同步；单家失败不影响其余交易所。"""
        self._warnings = []
        self._checked = 0
        report = OrderSyncReport()
        for exchange, adapter in list(self.router.adapters.items()):
            if only_exchange and exchange.value != only_exchange:
                continue
            try:
                report.outcomes.extend(await self._sync_exchange(exchange, adapter))
            except Exception as exc:
                self._warn(f"{exchange.value} 同步失败：{exc}")
                logger.exception("%s 同步失败", exchange.value)
        report.warnings = list(self._warnings)
        report.checked = self._checked
        return report

    async def _sync_exchange(self, exchange, adapter) -> list[OrderSyncOutcome]:
        outcomes: list[OrderSyncOutcome] = []
        # 全部只读拉取；失败时整家跳过并告警，不做任何本地修改。
        await adapter.validate_account()
        remote_orders = await adapter.get_open_orders()
        positions = await adapter.get_positions()
        instruments = await adapter.load_instruments()
        remote_by_client = {order.client_order_id: order for order in remote_orders if order.client_order_id}
        # 持仓快照同样以远端为准：补写缺失、清除已平仓位，避免陈旧快照误导后续核对。
        await self._refresh_position_snapshot(exchange, positions)

        local_rows = await self.database.fetch_all(
            "SELECT o.id, o.client_order_id, o.exchange_order_id, o.order_type, o.status, "
            "o.filled_quantity, o.average_fill_price, t.trade_id, t.instrument_key, t.side, t.state "
            "FROM orders o JOIN trade_instances t ON t.trade_id = o.trade_id "
            "WHERE t.exchange=? AND UPPER(o.status) IN (" + ",".join("?" for _ in OPEN_ORDER_STATES) + ") "
            "ORDER BY o.id",
            (exchange.value, *OPEN_ORDER_STATES),
        )
        self._checked += len(local_rows)
        for row in local_rows:
            outcome = await self._sync_order(exchange, adapter, row, remote_by_client, instruments)
            if outcome is not None:
                outcomes.append(outcome)
        # 远端开放但本地缺失/本地已终结的订单：告警不猜归属，或按远端恢复开放。
        outcomes.extend(await self._sync_remote_only(exchange, remote_by_client, local_rows, instruments))
        return outcomes

    async def _refresh_position_snapshot(self, exchange, positions: list) -> None:
        """把 positions 快照表与远端持仓对齐：补写仍存在的仓位，删除远端已不存在的快照行。"""
        local_rows = await self.database.fetch_all(
            "SELECT instrument_key, side, quantity, average_price FROM positions WHERE exchange=?",
            (exchange.value,),
        )
        remote_keys = {(item.instrument_key, item.side.value) for item in positions if item.quantity > 0}
        if not self.dry_run:
            for item in positions:
                if item.quantity <= 0:
                    continue
                await self.database.execute(
                    "INSERT INTO positions(exchange,instrument_key,side,quantity,average_price) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(exchange,instrument_key,side) DO UPDATE SET quantity=excluded.quantity,"
                    "average_price=excluded.average_price,captured_at=CURRENT_TIMESTAMP",
                    (exchange.value, item.instrument_key, item.side.value,
                     str(item.quantity), str(item.average_price)),
                )
        for row in local_rows:
            if (row["instrument_key"], row["side"]) in remote_keys:
                continue
            before = {"exchange": exchange.value, "instrument_key": row["instrument_key"],
                      "side": row["side"], "quantity": row["quantity"],
                      "average_price": row["average_price"]}
            if not self.dry_run:
                await self.database.execute(
                    "DELETE FROM positions WHERE exchange=? AND instrument_key=? AND side=?",
                    (exchange.value, row["instrument_key"], row["side"]),
                )
            await self._audit("ORDER_SYNC", None, "POSITION_SNAPSHOT_REMOVED", before=before)
            logger.info("%s 持仓快照 %s %s 远端已不存在，已清除（快照数量=%s）",
                        exchange.value, row["instrument_key"], row["side"], row["quantity"])

    async def _sync_order(self, exchange, adapter, row: dict, remote_by_client: dict,
                          instruments: dict) -> OrderSyncOutcome | None:
        client_id, trade_id = row["client_order_id"], row["trade_id"]
        multiplier = self._multiplier(instruments, row["instrument_key"])
        remote = remote_by_client.get(client_id)
        detail = None
        if remote is not None:
            # 远端仍开放：保留开放状态，仅归一状态并刷新累计成交事实。
            new_status = canonical_status(remote.status)
            filled, average = _extract_fill(exchange.value, remote.raw, multiplier)
            note = "远端仍开放，保留挂单"
        else:
            # 远端开放列表中没有：查询单笔订单确认最终状态；订单不存在视为从未受理。
            paper_order = str(row["exchange_order_id"] or "").startswith("paper-")
            detail = None
            if paper_order:
                # LOCAL 模拟盘历史订单从未到达该交易所，直接按远端不存在处理。
                new_status, filled, average = "REJECTED", Decimal("0"), None
                note = "本地模拟盘历史订单（paper-），远端不存在，标记为 REJECTED"
            else:
                try:
                    detail = await adapter.get_order(client_id, row["exchange_order_id"], row["instrument_key"])
                except Exception as exc:
                    self._warn(f"{exchange.value} {client_id} 远端状态查询失败，保持本地状态：{exc}")
                    return None
                if detail is None:
                    # 订单不存在意味着从未成交，零成交是确定事实。
                    new_status, filled, average = "REJECTED", Decimal("0"), None
                    note = "远端不存在该订单（未受理或已清理），标记为 REJECTED"
                else:
                    new_status = canonical_status(detail.status)
                    filled, average = _extract_fill(exchange.value, detail.raw, multiplier)
                    note = f"远端最终状态 {detail.status}"
                    if filled is None:
                        # 单笔接口未返回成交量时信任本地成交事实；本地也没有则保持未知（不当作零）。
                        local_filled = self._dec(row["filled_quantity"])
                        filled = local_filled if local_filled > 0 else None
        previous = str(row["status"]).upper()
        if not self._has_change(row, new_status, filled, average):
            return None  # 完全一致，无需修改
        await self._update_order(row, new_status, filled, average,
                                 remote.exchange_order_id if remote is not None
                                 else (detail.exchange_order_id if detail is not None else None))
        outcome = OrderSyncOutcome(exchange.value, client_id, trade_id, row["order_type"],
                                   row["status"], new_status if new_status != previous else None, note)
        if row["order_type"] == "ENTRY" and new_status in TERMINAL_ORDER_STATES:
            trade_note = await self._converge_trade(row, new_status, filled)
            if trade_note:
                outcome.note += f"；{trade_note}"
        return outcome

    async def _sync_remote_only(self, exchange, remote_by_client: dict,
                                local_rows: list[dict], instruments: dict) -> list[OrderSyncOutcome]:
        """远端开放订单的本地对账：本地缺失只告警；本地已终结则按远端恢复开放。"""
        outcomes: list[OrderSyncOutcome] = []
        open_client_ids = {row["client_order_id"] for row in local_rows}
        for client_id, remote in remote_by_client.items():
            if client_id in open_client_ids:
                continue  # 已在主循环按远端开放处理
            rows = await self.database.fetch_all(
                "SELECT o.id, o.client_order_id, o.exchange_order_id, o.order_type, o.status, "
                "o.filled_quantity, o.average_fill_price, t.trade_id, t.instrument_key "
                "FROM orders o JOIN trade_instances t ON t.trade_id = o.trade_id "
                "WHERE o.client_order_id=?",
                (client_id,),
            )
            if not rows:
                message = (f"{exchange.value} 远端开放订单 {client_id} 本地无记录（UNTRACKED），"
                           "未自动创建；请在交易所侧撤销或人工关联")
                self._warn(message)
                await self._audit("ORDER_SYNC", client_id, "UNTRACKED",
                                  after={"exchange": exchange.value, "remote": remote.raw})
                continue
            row = rows[0]
            multiplier = self._multiplier(instruments, row["instrument_key"])
            filled, average = _extract_fill(exchange.value, remote.raw, multiplier)
            new_status = canonical_status(remote.status)
            if not self._has_change(row, new_status, filled, average):
                continue
            await self._update_order(row, new_status, filled, average, remote.exchange_order_id)
            message = (f"{exchange.value} {client_id} 本地为 {row['status']} 但远端仍开放，"
                       "已按远端恢复开放状态；交易状态需人工复核")
            self._warn(message)
            await self._audit("ORDER_SYNC", client_id, "REOPENED",
                              before={"status": row["status"]}, after={"status": new_status})
            outcomes.append(OrderSyncOutcome(exchange.value, client_id, row["trade_id"], row["order_type"],
                                             row["status"], new_status, message))
        return outcomes

    async def _converge_trade(self, row: dict, new_status: str, filled: Decimal | None) -> str | None:
        """进场单终结后的交易状态收敛；只按已确认事实操作，无法唯一归属时不动交易状态。"""
        trade_id, state = row["trade_id"], row["state"]
        if state not in (TradeState.PENDING_ENTRY.value, TradeState.PARTIAL_FILL.value,
                         TradeState.RECEIVED.value):
            return None
        if filled is None:
            # 成交量未知：无法确认是否零成交，交易状态不自动收敛，交由人工复核。
            note = f"交易 {trade_id} 进场为 {new_status} 但成交量未知，交易状态未自动收敛，请人工复核"
            self._warn(note)
            return note
        if filled > 0:
            # 有成交：仓位可能已被止损/强平/手动平掉，归属无法在 CLI 层唯一确认，交启动对账或人工处理。
            note = (f"进场已有成交 {filled} 但交易仍为 {state}；如远端存在持仓，下次启动对账将自动恢复，"
                    "否则请人工处置（py trade_cleanup.py）")
            self._warn(f"{trade_id} {note}")
            return note
        # 零成交且进场终结：该交易未产生任何仓位，可安全收敛。
        target = TradeState.REJECTED if state == TradeState.RECEIVED.value else TradeState.CANCELLED
        if not self.dry_run:
            try:
                await self.states.transition(trade_id, target)
            except ValueError as exc:
                self._warn(f"{trade_id} 状态收敛失败：{exc}")
                return None
        await self._audit("ORDER_SYNC", trade_id, "TRADE_CONVERGED",
                          before={"state": state}, after={"state": target.value, "order_status": new_status})
        return f"交易 {trade_id} 无成交，将收敛为 {target.value}"

    def _has_change(self, row: dict, status: str, filled: Decimal | None, average: Decimal | None) -> bool:
        """判断远端事实相对本地是否有任何变化，避免无意义的写库与审计。"""
        if status != str(row["status"]).upper():
            return True
        if filled is not None and self._dec(row["filled_quantity"]) != filled:
            return True
        if average is not None and self._dec(row["average_fill_price"]) != average:
            return True
        return False

    async def _update_order(self, row: dict, status: str, filled: Decimal | None,
                            average: Decimal | None, remote_order_id: str | None) -> None:
        """按远端事实回填订单行；本地缺交易所订单号时补记，已有则保持不变。"""
        if self.dry_run:
            logger.info("[dry-run] %s %s: %s -> %s (filled=%s, avg=%s)",
                        row["trade_id"], row["client_order_id"], row["status"], status, filled, average)
            return
        keep_order_id = row["exchange_order_id"] or (remote_order_id or None)
        await self.database.execute(
            "UPDATE orders SET status=?, exchange_order_id=?, "
            "filled_quantity=?, average_fill_price=? WHERE id=?",
            (status, keep_order_id,
             str(filled) if filled is not None else row["filled_quantity"],
             str(average) if average is not None else row["average_fill_price"],
             row["id"]),
        )
        await self._audit("ORDER_SYNC", row["client_order_id"], "SYNCED",
                          before={"status": row["status"], "filled_quantity": row["filled_quantity"]},
                          after={"status": status,
                                 "filled_quantity": str(filled) if filled is not None else row["filled_quantity"]})

    @staticmethod
    def _multiplier(instruments: dict, instrument_key: str) -> Decimal:
        asset = instrument_key.split("/", 1)[0]
        instrument = instruments.get(asset)
        return instrument.contract_multiplier if instrument is not None else Decimal("1")

    @staticmethod
    def _dec(value) -> Decimal:
        parsed = _decimal(value)
        return parsed if parsed is not None else Decimal("0")

    def _warn(self, message: str) -> None:
        logger.warning(message)
        self._warnings.append(message)

    async def _audit(self, category: str, subject_id: str | None, result: str,
                     before=None, after=None) -> None:
        if self.dry_run:
            return
        await self.database.audit(category, subject_id, result, before, after)


async def run_order_sync(database: Database, router: ExchangeRouter,
                         only_exchange: str | None = None, dry_run: bool = False) -> OrderSyncReport:
    """供 CLI 与测试复用的入口。"""
    syncer = RemoteOrderSync(database, router, dry_run=dry_run)
    return await syncer.sync(only_exchange)


def main(argv: list[str] | None = None) -> int:
    """命令行入口：默认同步全部已接通交易所，--dry-run 预演不写库。"""
    parser = argparse.ArgumentParser(description="以交易所为准同步远端挂单状态到本地数据库（只写数据库，不下单）")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--exchange", default=None, choices=[item.value for item in Exchange],
                        help="只同步指定交易所")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要发生的变更，不写入数据库")
    args = parser.parse_args(argv)

    settings = Settings.load(args.config)
    database = Database(settings.database_path)
    database.initialize()
    router = ExchangeRouter(settings)

    async def execute() -> int:
        try:
            if not router.adapters:
                print("没有可用交易所；请检查 API 凭据和 mode 配置。")
                return 1
            report = await run_order_sync(database, router, only_exchange=args.exchange, dry_run=args.dry_run)
            if args.dry_run:
                print("[*] dry-run 模式：以下变更未被写入数据库。")
            for outcome in report.outcomes:
                status = outcome.new_status or "UNCHANGED"
                mark = "CHG" if outcome.new_status else "OK"
                print(f"[{mark}] {outcome.exchange} {outcome.client_order_id} "
                      f"({outcome.trade_id}, {outcome.order_type}): "
                      f"{outcome.previous_status} -> {status}；{outcome.note}")
            for warning in report.warnings:
                print(f"[WARN] {warning}")
            print(report.summary())
            return 0
        finally:
            await router.close()

    return asyncio.run(execute())


if __name__ == "__main__":
    raise SystemExit(main())