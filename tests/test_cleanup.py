"""僵尸交易清理模块（trade_cleanup）单元测试。

这些用例把「远程状态」抽象成可控的桩，覆盖三条安全边界：
仍有序的交易不得被误清理、部分成交不得当僵尸、无法核对远程时不得解锁。
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from database import Database
from models import Exchange, PositionSide, PositionSnapshot
from settings import Settings
from trade_cleanup import TradeCleaner, run_cleanup


@pytest.fixture
def settings(tmp_path):
    """复用主配置，但把所有交易所切到 LOCAL，避免测试触碰真实网络。"""
    import yaml

    config = tmp_path / "config.yaml"
    source = Settings.load("config.yaml").raw
    for exchange_config in source["exchanges"].values():
        exchange_config["mode"] = "LOCAL"
    config.write_text(yaml.safe_dump(source, allow_unicode=True), encoding="utf-8")
    return Settings.load(config)


class StubAdapter:
    """只提供清理所需的只读能力，任何写操作都不应被调用。"""

    def __init__(self, positions=None, orders=None) -> None:
        self._positions = positions or []
        self._orders = orders or []

    async def get_positions(self):
        return list(self._positions)

    async def get_open_orders(self):
        return list(self._orders)

    async def cancel_order(self, order_id):  # pragma: no cover - 清理绝不应撤单
        raise AssertionError("清理模块不允许撤单")

    async def close_position(self, request):  # pragma: no cover - 清理绝不应平仓
        raise AssertionError("清理模块不允许平仓")


class StubRouter:
    def __init__(self, adapters: dict | None = None) -> None:
        self.adapters = adapters or {}


async def _seed_trade(database, trade_id, state, exchange="OKX", order_status="NEW",
                      instrument_key="ETH/USDT:PERP", side="LONG"):
    """写入一笔交易及其进场订单，用于构造清理场景。"""
    await database.execute(
        "INSERT INTO trade_instances(trade_id,exchange,instrument_key,side,state) VALUES(?,?,?,?,?)",
        (trade_id, exchange, instrument_key, side, state),
    )
    await database.execute(
        "INSERT INTO orders(trade_id,exchange_order_id,client_order_id,order_type,price,quantity,status) "
        "VALUES(?,?,?,?,?,?,?)",
        (trade_id, "1", f"ct-{trade_id}", "ENTRY", "2395", "1", order_status),
    )


def test_cancels_pending_entry_without_open_orders(settings):
    """无活动订单的未成交僵尸交易应被收敛为 CANCELLED 并留下审计。"""
    async def scenario():
        database = Database(settings.database_path)
        database.initialize()
        await _seed_trade(database, "OKX-ETH-USDT-PERP-LONG-20990101-001",
                          "PENDING_ENTRY", order_status="CANCELED")
        report = await TradeCleaner(database, router=None).close_phantom_trades()
        trade = (await database.fetch_all("SELECT state FROM trade_instances"))[0]
        audits = await database.fetch_all(
            "SELECT category,result,before_json,after_json FROM audit_logs WHERE category='TRADE_CLEANUP'"
        )
        return report, trade, audits

    report, trade, audits = asyncio.run(scenario())
    assert [item.action for item in report] == ["CANCELLED"]
    assert trade["state"] == "CANCELLED"
    # 审计必须记录前后状态，便于回溯。
    assert audits == [{
        "category": "TRADE_CLEANUP", "result": "SUCCESS",
        "before_json": '{"state": "PENDING_ENTRY"}', "after_json": '{"state": "CANCELLED"}',
    }]


def test_leaves_trade_with_open_orders_untouched(settings):
    """仍有活动挂单的交易必须跳过，避免把等待成交的挂单误判为僵尸。"""
    async def scenario():
        database = Database(settings.database_path)
        database.initialize()
        await _seed_trade(database, "OKX-ETH-USDT-PERP-LONG-20990101-002",
                          "PENDING_ENTRY", order_status="NEW")
        report = await TradeCleaner(database, router=None).close_phantom_trades()
        trade = (await database.fetch_all("SELECT state FROM trade_instances"))[0]
        return report, trade

    report, trade = asyncio.run(scenario())
    assert [item.action for item in report] == ["SKIPPED"]
    assert "活动订单" in report[0].reason
    assert trade["state"] == "PENDING_ENTRY"


def test_partial_fill_is_never_treated_as_phantom(settings):
    """部分成交可能已有真实持仓，即使本地无活动订单也必须交给人工确认。"""
    async def scenario():
        database = Database(settings.database_path)
        database.initialize()
        await _seed_trade(database, "OKX-ETH-USDT-PERP-LONG-20990101-003",
                          "PARTIAL_FILL", order_status="CANCELED")
        report = await TradeCleaner(database, router=None).close_phantom_trades()
        trade = (await database.fetch_all("SELECT state FROM trade_instances"))[0]
        return report, trade

    report, trade = asyncio.run(scenario())
    assert [item.action for item in report] == ["SKIPPED"]
    assert "部分成交" in report[0].reason
    assert trade["state"] == "PARTIAL_FILL"


def test_unlock_requires_router(settings):
    """未接入交易所路由时无法核对远程状态，ERROR_LOCKED 必须跳过。"""
    async def scenario():
        database = Database(settings.database_path)
        database.initialize()
        await _seed_trade(database, "BINANCE-ETH-USDT-PERP-LONG-20990101-001",
                          "ERROR_LOCKED", exchange="BINANCE", order_status="CANCELED")
        # 默认清理不应触碰 ERROR_LOCKED。
        phantom = await TradeCleaner(database, router=None).close_phantom_trades()
        report = await TradeCleaner(database, router=None).unlock_trades()
        trade = (await database.fetch_all("SELECT state FROM trade_instances"))[0]
        audits = await database.fetch_all("SELECT category FROM audit_logs WHERE category='TRADE_CLEANUP'")
        return phantom, report, trade, audits

    phantom, report, trade, audits = asyncio.run(scenario())
    assert phantom == []
    assert [item.action for item in report] == ["SKIPPED"]
    assert "无法核对远程" in report[0].reason
    assert trade["state"] == "ERROR_LOCKED"
    assert audits == []


def test_unlock_succeeds_when_no_position_and_no_open_orders(settings):
    """确认无持仓且无挂单时，锁定交易可安全收敛为 CANCELLED。"""
    async def scenario():
        database = Database(settings.database_path)
        database.initialize()
        await _seed_trade(database, "BINANCE-ETH-USDT-PERP-LONG-20990101-002",
                          "ERROR_LOCKED", exchange="BINANCE", order_status="CANCELED")
        router = StubRouter({Exchange.BINANCE: StubAdapter(positions=[], orders=[])})
        report = await run_cleanup(database, router, unlock=True)
        trade = (await database.fetch_all("SELECT state FROM trade_instances"))[0]
        return report, trade

    report, trade = asyncio.run(scenario())
    assert [item.action for item in report.outcomes] == ["UNLOCKED"]
    assert trade["state"] == "CANCELLED"
    assert "收敛 0 笔" in report.summary()


def test_unlock_blocked_by_remote_position(settings):
    """远程仍有持仓时绝不能解锁，避免把有仓位的交易当成僵尸。"""
    async def scenario():
        database = Database(settings.database_path)
        database.initialize()
        await _seed_trade(database, "BINANCE-ETH-USDT-PERP-LONG-20990101-003",
                          "ERROR_LOCKED", exchange="BINANCE", order_status="CANCELED")
        positions = [PositionSnapshot(Exchange.BINANCE, "ETH/USDT:PERP",
                                      PositionSide.LONG, Decimal("1"), Decimal("2480"))]
        router = StubRouter({Exchange.BINANCE: StubAdapter(positions=positions, orders=[])})
        report = await run_cleanup(database, router, unlock=True)
        trade = (await database.fetch_all("SELECT state FROM trade_instances"))[0]
        return report, trade

    report, trade = asyncio.run(scenario())
    assert [item.action for item in report.outcomes] == ["SKIPPED"]
    assert "远程仍有" in report.outcomes[0].reason
    assert trade["state"] == "ERROR_LOCKED"


def test_unlock_blocked_by_remote_order_of_same_trade(settings):
    """远程仍挂着本交易的订单时必须跳过，且不得误认他人订单为阻塞。"""
    class RemoteOrder:
        def __init__(self, client_order_id: str) -> None:
            self.client_order_id = client_order_id

    async def scenario():
        database = Database(settings.database_path)
        database.initialize()
        trade_id = "BINANCE-ETH-USDT-PERP-LONG-20990101-004"
        await _seed_trade(database, trade_id, "ERROR_LOCKED",
                          exchange="BINANCE", order_status="CANCELED")
        # 他人订单不应阻塞解锁。
        others = StubRouter({Exchange.BINANCE: StubAdapter(orders=[RemoteOrder("ct-unrelated")])})
        unrelated = await run_cleanup(database, others, unlock=True)
        # 复位为上锁状态，再验证「本交易订单仍在远程」必须阻塞。
        await database.execute(
            "UPDATE trade_instances SET state='ERROR_LOCKED' WHERE trade_id=?", (trade_id,)
        )
        mine = StubRouter({Exchange.BINANCE: StubAdapter(orders=[RemoteOrder(f"ct-{trade_id}")])})
        blocked = await run_cleanup(database, mine, unlock=True)
        trade = (await database.fetch_all("SELECT state FROM trade_instances"))[0]
        return unrelated, blocked, trade

    unrelated, blocked, trade = asyncio.run(scenario())
    assert [item.action for item in unrelated.outcomes] == ["UNLOCKED"]
    assert [item.action for item in blocked.outcomes] == ["SKIPPED"]
    assert "远程仍有" in blocked.outcomes[0].reason
    assert trade["state"] == "ERROR_LOCKED"


def test_dry_run_does_not_modify_database(settings):
    """dry-run 只做判定，不得写入任何状态或审计。"""
    async def scenario():
        database = Database(settings.database_path)
        database.initialize()
        await _seed_trade(database, "OKX-ETH-USDT-PERP-LONG-20990101-004",
                          "PENDING_ENTRY", order_status="CANCELED")
        report = await TradeCleaner(database, router=None, dry_run=True).close_phantom_trades()
        trade = (await database.fetch_all("SELECT state FROM trade_instances"))[0]
        audits = await database.fetch_all("SELECT category FROM audit_logs WHERE category='TRADE_CLEANUP'")
        return report, trade, audits

    report, trade, audits = asyncio.run(scenario())
    assert [item.action for item in report] == ["CANCELLED"]
    assert trade["state"] == "PENDING_ENTRY"
    assert audits == []


def test_cleanup_never_calls_exchange_write_operations(settings):
    """清理流程只允许只读调用；桩适配器会在撤单/平仓时直接失败。"""
    class RemoteOrder:
        def __init__(self, client_order_id: str) -> None:
            self.client_order_id = client_order_id

    async def scenario():
        database = Database(settings.database_path)
        database.initialize()
        await _seed_trade(database, "OKX-ETH-USDT-PERP-LONG-20990101-005",
                          "ERROR_LOCKED", order_status="CANCELED")
        adapter = StubAdapter(orders=[RemoteOrder("ct-unrelated")])
        router = StubRouter({Exchange.OKX: adapter})
        report = await run_cleanup(database, router, unlock=True)
        return report

    report = asyncio.run(scenario())
    assert [item.action for item in report.outcomes] == ["UNLOCKED"]