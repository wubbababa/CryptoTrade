"""远端→本地挂单状态同步的安全边界测试。

覆盖：远端仍开放保留挂单、远端已终结回填最终状态、零成交交易收敛、
有成交不猜归属、远端孤立订单只告警、小写脏状态归一、dry-run 不写库。
"""

import asyncio
from dataclasses import replace
from decimal import Decimal

import pytest
import yaml

from database import Database
from exchange_router import ExchangeRouter
from models import Exchange, OrderResult
from order_sync import RemoteOrderSync, canonical_status
from settings import Settings
from trading_service import TradingService
from codex_parser import command_from_json


@pytest.fixture
def settings(tmp_path):
    config = tmp_path / "config.yaml"
    source = Settings.load("config.yaml").raw
    for exchange_config in source["exchanges"].values():
        exchange_config["mode"] = "LOCAL"
    config.write_text(yaml.safe_dump(source, allow_unicode=True), encoding="utf-8")
    return Settings.load(config)


def valid_payload():
    return {
        "command_type": "OPEN_POSITION", "exchange": "BINANCE", "base_asset": "ETH",
        "side": "LONG", "entry": {"type": "RANGE", "low": "2480", "high": "2490"},
        "take_profits": ["2519", "2549"], "stop_loss": "2455", "quantity": "0.01",
        "confidence": "0.98", "ambiguities": [],
    }


def _setup(settings, command_id):
    """创建一笔 PENDING_ENTRY 交易与本地开放进场订单。

    LOCAL 模式生成的订单号带 paper- 前缀（同步工具会把它视为从未上链的模拟单），
    这里改写为真实交易所风格的数字订单号，以覆盖远端状态查询路径。
    """
    database = Database(settings.database_path)
    database.initialize()
    router = ExchangeRouter(settings)
    adapter = router.get(Exchange.BINANCE)
    command = command_from_json(valid_payload(), command_id, settings)
    asyncio.run(TradingService(settings, database, router).execute(command))
    rows = asyncio.run(database.fetch_all(
        "SELECT id, client_order_id, exchange_order_id FROM orders WHERE order_type='ENTRY'"))
    row = rows[0]
    paper_id = row["exchange_order_id"]
    real_id = "100001"
    asyncio.run(database.execute("UPDATE orders SET exchange_order_id=? WHERE id=?", (real_id, row["id"])))
    adapter.orders[real_id] = adapter.orders.pop(paper_id)
    return database, router, adapter, row["client_order_id"], real_id


def test_canonical_status_mapping():
    assert canonical_status("live") == "NEW"
    assert canonical_status("open") == "NEW"
    assert canonical_status("partially_filled") == "PARTIALLY_FILLED"
    assert canonical_status("canceled") == "CANCELED"
    assert canonical_status("expired_in_match") == "EXPIRED"
    assert canonical_status("FILLED") == "FILLED"


def test_sync_keeps_order_still_open_remotely(settings):
    """远端仍开放的挂单必须保留，小写脏状态归一为 NEW，交易状态不受影响。"""
    database, router, adapter, client_id, order_id = _setup(settings, "tg-sync-open-1")
    entry = (asyncio.run(database.fetch_all("SELECT * FROM orders")))[0]
    # 远端开放订单仍在（PaperAdapter 默认 NEW），把本地状态改成脏的小写值模拟历史脏数据。
    asyncio.run(database.execute("UPDATE orders SET status='open' WHERE id=?", (entry["id"],)))

    async def scenario():
        report = await RemoteOrderSync(database, router).sync("BINANCE")
        row = (await database.fetch_all("SELECT status FROM orders"))[0]
        state = (await database.fetch_all("SELECT state FROM trade_instances"))[0]
        await router.close()
        return report, row, state

    report, row, state = asyncio.run(scenario())
    assert row["status"] == "NEW"
    assert state["state"] == "PENDING_ENTRY"
    assert all("UNTRACKED" not in warning for warning in report.warnings)


def test_sync_marks_remote_canceled_order_and_converges_trade(settings):
    """远端已撤销且零成交：订单回填 CANCELED，交易收敛 CANCELLED。"""
    database, router, adapter, client_id, order_id = _setup(settings, "tg-sync-cancel-1")
    # 真实交易所的单笔查询响应会携带成交量字段；显式给出 executedQty=0 模拟零成交撤销。
    adapter.orders[order_id] = OrderResult(order_id, client_id, "CANCELED", {"executedQty": "0"})

    async def scenario():
        report = await RemoteOrderSync(database, router).sync("BINANCE")
        row = (await database.fetch_all("SELECT status,filled_quantity FROM orders"))[0]
        state = (await database.fetch_all("SELECT state FROM trade_instances"))[0]
        await router.close()
        return report, row, state

    report, row, state = asyncio.run(scenario())
    assert row["status"] == "CANCELED"
    assert Decimal(row["filled_quantity"]) == 0
    assert state["state"] == "CANCELLED"


def test_sync_remote_order_missing_marks_rejected(settings):
    """远端根本不存在该订单（从未受理）：标记 REJECTED，交易收敛 REJECTED/CANCELLED。"""
    database, router, adapter, client_id, order_id = _setup(settings, "tg-sync-missing-1")
    entry = (asyncio.run(database.fetch_all("SELECT * FROM orders")))[0]
    del adapter.orders[entry["exchange_order_id"]]

    async def scenario():
        report = await RemoteOrderSync(database, router).sync("BINANCE")
        row = (await database.fetch_all("SELECT status FROM orders"))[0]
        state = (await database.fetch_all("SELECT state FROM trade_instances"))[0]
        await router.close()
        return report, row, state

    report, row, state = asyncio.run(scenario())
    assert row["status"] == "REJECTED"
    assert state["state"] == "CANCELLED"


def test_sync_paper_order_is_rejected_without_remote_query(settings):
    """LOCAL 模拟盘历史订单（paper- 订单号）直接按远端不存在处理，不发起远端查询。"""
    database, router, adapter, client_id, order_id = _setup(settings, "tg-sync-paper-1")
    # 恢复 paper- 订单号并让远端（模拟交易所）不再持有该订单，模拟早期 LOCAL 模式遗留记录。
    entry = (asyncio.run(database.fetch_all("SELECT * FROM orders")))[0]
    paper_id = "paper-legacy1"
    asyncio.run(database.execute("UPDATE orders SET exchange_order_id=? WHERE id=?",
                                 (paper_id, entry["id"])))
    del adapter.orders[order_id]

    async def scenario():
        report = await RemoteOrderSync(database, router).sync("BINANCE")
        row = (await database.fetch_all("SELECT status FROM orders"))[0]
        state = (await database.fetch_all("SELECT state FROM trade_instances"))[0]
        await router.close()
        return report, row, state

    report, row, state = asyncio.run(scenario())
    assert row["status"] == "REJECTED"
    assert state["state"] == "CANCELLED"
    assert all("查询失败" not in warning for warning in report.warnings)


def test_sync_filled_entry_without_position_keeps_trade_for_recovery(settings):
    """进场已成交但远端无持仓：只回填订单事实，不猜测交易归属，且必须告警。"""
    database, router, adapter, client_id, order_id = _setup(settings, "tg-sync-filled-1")
    entry = (asyncio.run(database.fetch_all("SELECT * FROM orders")))[0]
    adapter.orders[entry["exchange_order_id"]] = replace(
        adapter.orders[entry["exchange_order_id"]], status="FILLED")
    # 模拟成交回报已更新成交量（与 monitor.process_event 相同的最小事实）。
    asyncio.run(database.execute(
        "UPDATE orders SET filled_quantity=quantity WHERE id=?", (entry["id"],)))

    async def scenario():
        report = await RemoteOrderSync(database, router).sync("BINANCE")
        row = (await database.fetch_all("SELECT status,filled_quantity FROM orders"))[0]
        state = (await database.fetch_all("SELECT state FROM trade_instances"))[0]
        await router.close()
        return report, row, state

    report, row, state = asyncio.run(scenario())
    assert row["status"] == "FILLED"
    assert Decimal(row["filled_quantity"]) == Decimal(entry["quantity"])
    # 交易状态保持 PENDING_ENTRY，等待启动对账恢复或人工处置。
    assert state["state"] == "PENDING_ENTRY"
    assert any("成交" in warning for warning in report.warnings)


def test_sync_untracked_remote_order_only_warns(settings):
    """远端开放但本地无记录的订单只告警，绝不创建本地订单。"""
    database, router, adapter, client_id, order_id = _setup(settings, "tg-sync-untracked-1")
    adapter.orders["paper-external"] = OrderResult(
        "paper-external", "ct-external-1", "NEW", {"symbol": "ETHUSDT"})

    async def scenario():
        report = await RemoteOrderSync(database, router).sync("BINANCE")
        rows = await database.fetch_all("SELECT client_order_id FROM orders")
        await router.close()
        return report, rows

    report, rows = asyncio.run(scenario())
    assert {row["client_order_id"] for row in rows} == {client_id}
    assert any("UNTRACKED" in warning for warning in report.warnings)


def test_sync_local_terminal_remote_open_reopens(settings):
    """本地误记为终结但远端仍开放：以远端为准恢复开放并告警。"""
    database, router, adapter, client_id, order_id = _setup(settings, "tg-sync-reopen-1")
    entry = (asyncio.run(database.fetch_all("SELECT * FROM orders")))[0]
    asyncio.run(database.execute("UPDATE orders SET status='CANCELED' WHERE id=?", (entry["id"],)))

    async def scenario():
        report = await RemoteOrderSync(database, router).sync("BINANCE")
        row = (await database.fetch_all("SELECT status FROM orders"))[0]
        await router.close()
        return report, row

    report, row = asyncio.run(scenario())
    assert row["status"] == "NEW"
    assert any("恢复开放" in warning for warning in report.warnings)


def test_sync_dry_run_does_not_write(settings):
    """dry-run 只判定不写库：订单与交易状态保持原样。"""
    database, router, adapter, client_id, order_id = _setup(settings, "tg-sync-dry-1")
    entry = (asyncio.run(database.fetch_all("SELECT * FROM orders")))[0]
    adapter.orders[entry["exchange_order_id"]] = replace(
        adapter.orders[entry["exchange_order_id"]], status="CANCELED")

    async def scenario():
        report = await RemoteOrderSync(database, router, dry_run=True).sync("BINANCE")
        row = (await database.fetch_all("SELECT status FROM orders"))[0]
        state = (await database.fetch_all("SELECT state FROM trade_instances"))[0]
        audits = await database.fetch_all("SELECT result FROM audit_logs WHERE category='ORDER_SYNC'")
        await router.close()
        return report, row, state, audits

    report, row, state, audits = asyncio.run(scenario())
    assert row["status"] == "NEW"
    assert state["state"] == "PENDING_ENTRY"
    assert audits == []
    # 预演仍要给出结论，便于上线前审视。
    assert any(outcome.new_status == "CANCELED" for outcome in report.outcomes)


def test_partial_fill_updates_fill_facts(settings):
    """远端部分成交：状态归一为 PARTIALLY_FILLED 并回填成交量，交易进入 PARTIAL_FILL 之外的状态不动。"""
    database, router, adapter, client_id, order_id = _setup(settings, "tg-sync-partial-1")
    entry = (asyncio.run(database.fetch_all("SELECT * FROM orders")))[0]
    adapter.orders[entry["exchange_order_id"]] = replace(
        adapter.orders[entry["exchange_order_id"]], status="PARTIALLY_FILLED")
    # PaperAdapter 的 raw 不含成交量字段；直接补最小事实以覆盖 Gate/OKX 的提取路径。
    adapter.orders[entry["exchange_order_id"]] = OrderResult(
        entry["exchange_order_id"], client_id, "PARTIALLY_FILLED",
        {"executedQty": str(Decimal(entry["quantity"]) / 2)})

    async def scenario():
        report = await RemoteOrderSync(database, router).sync("BINANCE")
        row = (await database.fetch_all("SELECT status,filled_quantity FROM orders"))[0]
        state = (await database.fetch_all("SELECT state FROM trade_instances"))[0]
        await router.close()
        return report, row, state

    report, row, state = asyncio.run(scenario())
    assert row["status"] == "PARTIALLY_FILLED"
    assert Decimal(row["filled_quantity"]) == Decimal(entry["quantity"]) / 2
    # 部分成交属于开放状态，交易状态不由同步工具变更。
    assert state["state"] == "PENDING_ENTRY"


def test_sync_aligns_position_snapshot_with_remote(settings):
    """持仓快照以远端为准：仍存在的仓位刷新数量，远端已平仓的陈旧快照行被清除。"""
    from models import PositionSide, PositionSnapshot

    database, router, adapter, client_id, order_id = _setup(settings, "tg-sync-pos-1")
    # 本地存在一条陈旧快照（BTC 仓位早已平掉），远端仅剩 ETH 仓位。
    asyncio.run(database.execute(
        "INSERT INTO positions(exchange,instrument_key,side,quantity,average_price) VALUES(?,?,?,?,?)",
        ("BINANCE", "BTC/USDT:PERP", "LONG", "1.0", "60000")))
    adapter.positions = [PositionSnapshot(
        Exchange.BINANCE, "ETH/USDT:PERP", PositionSide.LONG, Decimal("2"), Decimal("2480"))]

    async def scenario():
        report = await RemoteOrderSync(database, router).sync("BINANCE")
        rows = await database.fetch_all("SELECT instrument_key,quantity FROM positions ORDER BY instrument_key")
        await router.close()
        return report, rows

    report, rows = asyncio.run(scenario())
    assert {(row["instrument_key"], row["quantity"]) for row in rows} == {("ETH/USDT:PERP", "2")}


def test_sync_position_snapshot_dry_run_keeps_stale_rows(settings):
    """dry-run 不改动持仓快照表。"""
    from models import PositionSide, PositionSnapshot

    database, router, adapter, client_id, order_id = _setup(settings, "tg-sync-pos-dry-1")
    asyncio.run(database.execute(
        "INSERT INTO positions(exchange,instrument_key,side,quantity,average_price) VALUES(?,?,?,?,?)",
        ("BINANCE", "BTC/USDT:PERP", "LONG", "1.0", "60000")))
    adapter.positions = []

    async def scenario():
        report = await RemoteOrderSync(database, router, dry_run=True).sync("BINANCE")
        rows = await database.fetch_all("SELECT instrument_key FROM positions")
        audits = await database.fetch_all("SELECT result FROM audit_logs WHERE result='POSITION_SNAPSHOT_REMOVED'")
        await router.close()
        return report, rows, audits

    report, rows, audits = asyncio.run(scenario())
    # 预演不删除、不写审计。
    assert [row["instrument_key"] for row in rows] == ["BTC/USDT:PERP"]
    assert audits == []
