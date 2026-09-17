"""启动清库与远端持仓重建的安全边界测试。

覆盖：业务表被清空、`runtime_state` 与 `audit_logs` 被保留、远端持仓重建为
`positions` 快照、零数量仓位被忽略、以及全部交易所不可读时**放弃清空**。
"""

import asyncio
from decimal import Decimal

import pytest
import yaml

from database import Database
from exchange_router import ExchangeRouter
from models import Exchange, PositionSide, PositionSnapshot
from settings import Settings
from startup_reset import BUSINESS_TABLES, run_startup_reset


@pytest.fixture
def settings(tmp_path):
    config = tmp_path / "config.yaml"
    source = Settings.load("config.yaml").raw
    for exchange_config in source["exchanges"].values():
        exchange_config["mode"] = "LOCAL"
    config.write_text(yaml.safe_dump(source, allow_unicode=True), encoding="utf-8")
    return Settings.load(config)


def _seed(database: Database) -> None:
    """向所有业务表与保留表写入可辨识的样本数据。"""

    async def scenario() -> None:
        await database.execute(
            "INSERT INTO telegram_messages(chat_id,message_id,raw_text,received_at) VALUES(1,2,'sig','2026-01-01')")
        await database.execute(
            "INSERT INTO trade_instances(trade_id,exchange,instrument_key,side,state) "
            "VALUES('T-1','OKX','BTC/USDT:PERP','LONG','OPEN')")
        await database.execute(
            "INSERT INTO orders(trade_id,exchange_order_id,client_order_id,order_type,price,quantity,status) "
            "VALUES('T-1','1','c1','ENTRY','100','1','NEW')")
        await database.execute(
            "INSERT INTO positions(exchange,instrument_key,side,quantity,average_price) "
            "VALUES('OKX','BTC/USDT:PERP','LONG','1','100')")
        await database.execute(
            "INSERT INTO commands(command_id,trade_id,command_type,payload_json,status) "
            "VALUES('cmd-1','T-1','OPEN_POSITION','{}','EXECUTED')")
        await database.execute("INSERT INTO exchange_events(exchange,event_json) VALUES('OKX','{}')")
        await database.execute("INSERT INTO runtime_state(key,value) VALUES('telegram_offset','42')")
        await database.execute("INSERT INTO audit_logs(category,subject_id,result) VALUES('SEED','x','OK')")

    asyncio.run(scenario())


def _run(settings: Settings, *, seed: bool = False, positions=None, fail: bool = False):
    """执行一次启动清库并回收关键表状态，供断言使用。"""
    database = Database(settings.database_path)
    database.initialize()
    router = ExchangeRouter(settings)
    if seed:
        _seed(database)
    if positions is not None:
        router.get(Exchange.OKX).positions = positions
    if fail:
        # 模拟所有交易所网络/鉴权不可用：远端持仓读取全部抛错。
        async def broken(*_args, **_kwargs):
            raise RuntimeError("远端不可用")

        for adapter in router.adapters.values():
            adapter.get_positions = broken

    report = asyncio.run(run_startup_reset(database, router))

    async def snapshot() -> tuple:
        counts = {
            table: (await database.fetch_all(f"SELECT COUNT(*) AS n FROM {table}"))[0]["n"]
            for table in BUSINESS_TABLES
        }
        runtime = await database.fetch_all("SELECT value FROM runtime_state WHERE key='telegram_offset'")
        audits = await database.fetch_all("SELECT category,result FROM audit_logs ORDER BY id")
        rows = await database.fetch_all("SELECT * FROM positions ORDER BY exchange,instrument_key,side")
        await router.close()
        return counts, runtime, audits, rows

    counts, runtime, audits, rows = asyncio.run(snapshot())
    return report, counts, runtime, audits, rows


def test_reset_clears_business_tables_but_keeps_runtime_state_and_audit_logs(settings):
    """业务表必须清空，而 Telegram 断点与审计日志必须保留。"""
    report, counts, runtime, audits, _ = _run(settings, seed=True)

    assert report.skipped_reason is None
    assert all(count == 0 for count in counts.values()), counts
    assert [row["value"] for row in runtime] == ["42"]
    categories = [(row["category"], row["result"]) for row in audits]
    assert ("SEED", "OK") in categories
    assert ("STARTUP_RESET", "SUCCESS") in categories


def test_reset_rebuilds_position_snapshot_from_remote(settings):
    """远端持仓重建为 positions 快照；零数量仓位不作为快照写入。"""
    remote = [
        PositionSnapshot(Exchange.OKX, "BTC/USDT:PERP", PositionSide.LONG,
                         Decimal("2.5"), Decimal("101.5")),
        PositionSnapshot(Exchange.OKX, "ETH/USDT:PERP", PositionSide.SHORT,
                         Decimal("0"), Decimal("2000")),
    ]
    report, counts, _, _, rows = _run(settings, seed=True, positions=remote)

    assert report.skipped_reason is None
    # 可读通的交易所都会记录（无仓位为 0），只有 OKX 贡献了实际快照。
    assert report.synced_positions["OKX"] == 1
    assert sum(report.synced_positions.values()) == 1
    assert counts["positions"] == 1
    assert rows[0]["exchange"] == "OKX"
    assert rows[0]["instrument_key"] == "BTC/USDT:PERP"
    assert rows[0]["side"] == "LONG"
    assert rows[0]["quantity"] == "2.5"
    assert rows[0]["average_price"] == "101.5"


def test_reset_is_skipped_when_no_exchange_is_readable(settings):
    """所有交易所都读不通时必须放弃清空，避免在没有事实来源时销毁本地数据。"""
    report, counts, runtime, audits, rows = _run(settings, seed=True, fail=True)

    assert report.skipped_reason is not None
    assert report.warnings
    # 本地数据保持原样，未被清空。
    assert counts["telegram_messages"] == 1
    assert counts["trade_instances"] == 1
    assert counts["orders"] == 1
    assert counts["commands"] == 1
    assert rows[0]["quantity"] == "1"
    assert [row["value"] for row in runtime] == ["42"]
    assert ("STARTUP_RESET", "SKIPPED") in [(row["category"], row["result"]) for row in audits]
