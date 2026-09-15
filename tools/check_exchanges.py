"""对已配置交易所执行只读连通性与账户检查，不会下单或修改账户。"""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path

# 直接执行 tools 下脚本时，将项目根目录加入模块搜索路径。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exchange_router import ExchangeRouter
from settings import Settings

async def main() -> None:
    settings = Settings.load()
    router = ExchangeRouter(settings)
    if not router.adapters:
        print("没有可用交易所。请先在 .env 填写对应环境的 API 凭据。")
        return
    try:
        for exchange, adapter in router.adapters.items():
            try:
                await adapter.validate_account()
                instruments = await adapter.load_instruments()
                equity = await adapter.get_equity()
                positions = await adapter.get_positions()
                orders = await adapter.get_open_orders()
                mode = settings.exchange_config(exchange).get("mode", "")
                print(f"{exchange.value} [{mode}] 连接成功：产品 {len(instruments)}，权益 {equity} USDT，持仓 {len(positions)}，挂单 {len(orders)}")
            except Exception as exc:
                print(f"{exchange.value} 只读检查失败：{exc}")
    finally:
        await router.close()

if __name__ == "__main__":
    asyncio.run(main())
