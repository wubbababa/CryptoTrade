"""开发者测试模式模块 (--dev)：测试 DeepSeek API 调用与端到端模拟下单。"""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from typing import Any

from database import Database
from deepseek_parser import DeepSeekParser
from exchange_router import ExchangeRouter
from models import Exchange, TradeCommand
from settings import Settings
from trading_service import TradingService

logger = logging.getLogger(__name__)

# 预置的典型交易信号公告样例
DEFAULT_SAMPLE_SIGNAL = (
    "做多 ETH 进场区间 2480 - 2500，止盈 2550, 2600，止损 2420，浮盈一半做保本"
)


async def run_dev_test(config_path: str = "config.yaml", custom_signal: str | None = None) -> None:
    """运行开发测试流程：
    1. 打印配置及 DeepSeek API 连通性测试
    2. 打印大模型原始返回与结构化解析结果
    3. 执行模拟开仓并打印交易报告与数据库记录
    """
    print("\n" + "=" * 60)
    print("       [*] 启动 CryptoTrade 开发者测试模式 (--dev)")
    print("=" * 60)

    # 1. 加载配置
    settings = Settings.load(config_path)
    deepseek_cfg = settings.raw.get("deepseek", {})
    api_key_source = "环境变量 (DEEPSEEK_API_KEY)" if settings.deepseek_api_key else "未配置"
    key_status = "[OK] 已加载 (" + api_key_source + ")" if settings.deepseek_api_key else "[!] 未检测到 Key"
    print(f"\n[1/4] 加载配置：")
    print(f"  - 配置文件: {config_path}")
    print(f"  - 默认交易所: {', '.join(item.value for item in settings.default_exchanges)}")
    print(f"  - DeepSeek 模型: {deepseek_cfg.get('model', 'deepseek-chat')}")
    print(f"  - DeepSeek Base URL: {deepseek_cfg.get('base_url', 'https://api.deepseek.com')}")
    print(f"  - API Key 状态: {key_status}")

    # 2. 初始化数据库与交易所路由
    database = Database(settings.database_path)
    database.initialize()
    router = ExchangeRouter(settings)

    # 开发模式不伪造远程交易所；缺少凭据的目标会由广播执行结果明确标记为跳过。
    for exchange in settings.default_exchanges:
        if exchange not in router.adapters:
            reason = router.unavailable_reasons.get(exchange, "配置未启用或适配器未初始化")
            print(f"  - 提示: {exchange.value} 将跳过：{reason}")

    # 3. 准备测试信号并调用 DeepSeek API
    signal_text = custom_signal.strip() if custom_signal else DEFAULT_SAMPLE_SIGNAL
    command_id = f"dev-test-{int(asyncio.get_event_loop().time() * 1000)}"
    print(f"\n[2/4] 待解析测试公告文本：")
    print(f"  \"{signal_text}\"")

    print(f"\n[3/4] 正在调用 DeepSeek API 进行自然语言公告识别...")
    parser = DeepSeekParser(settings)
    try:
        command, raw_response, extracted_json = await parser.parse_with_debug(signal_text, command_id)
    except Exception as exc:
        print(f"\n[ERROR] DeepSeek API 调用或解析失败: {exc}")
        await router.close()
        return

    print("\n  [+] DeepSeek API 原始返回 JSON 内容：")
    print("  " + "-" * 56)
    print("  " + raw_response.replace("\n", "\n  "))
    print("  " + "-" * 56)

    print("\n  [+] 结构化解析结果 (TradeCommand 实体)：")
    print(f"    - 指令编号: {command.command_id}")
    print(f"    - 指令类型: {command.command_type.value}")
    print(f"    - 交易所:   {command.exchange.value} {'(使用默认值)' if command.exchange_defaulted else ''}")
    print(f"    - 标的币种: {command.base_asset}")
    print(f"    - 交易方向: {command.side.value}")
    print(f"    - 进场区间: {command.entry.low} ~ {command.entry.high} (参考基准价: {command.entry.reference_price})")
    print(f"    - 止盈目标: {[str(tp) for tp in command.take_profits]}")
    print(f"    - 止损价格: {command.stop_loss}")
    print(f"    - 模型置信度: {command.confidence}")
    print(f"    - 歧义说明: {command.ambiguities if command.ambiguities else '无'}")
    print(f"    - 动态保本: 盈利进度达 {command.breakeven.trigger_ratio*100}% 时移动至浮盈 {command.breakeven.profit_price_ratio*100}% 锁定利润")

    # 4. 执行模拟下单与交易用例
    print(f"\n[4/4] 正在执行模拟下单流程...")
    service = TradingService(settings, database, router)
    try:
        report = await service.execute(command)
        print(f"\n  [OK] 交易执行回报: {report}")

        # 查询数据库中落库的最新订单
        orders = await database.fetch_all(
            "SELECT * FROM orders WHERE client_order_id LIKE ? ORDER BY id DESC LIMIT 1",
            (f"%{command.command_id}%",)
        )
        if orders:
            order = orders[0]
            print(f"  [INFO] 本地订单记录: client_order_id={order['client_order_id']}, price={order['price']}, "
                  f"quantity={order['quantity']}, status={order['status']}")
    except Exception as exc:
        print(f"\n  [ERROR] 下单执行失败: {exc}")
    finally:
        await router.close()

    print("\n" + "=" * 60)
    print("       [*] 开发者测试模式运行完毕")
    print("=" * 60 + "\n")
