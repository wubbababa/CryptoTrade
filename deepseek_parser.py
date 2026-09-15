"""调用 DeepSeek 官方 API，将自然语言交易公告解析为严格结构化的交易指令。"""

from __future__ import annotations

import json
import logging
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any

import aiohttp

from models import BreakevenSpec, CommandType, EntrySpec, Exchange, PositionSide, TradeCommand, normalize_asset
from settings import Settings

logger = logging.getLogger(__name__)

# 用于指导 DeepSeek 大模型解析 Telegram 交易信号的系统提示词
SYSTEM_PROMPT = """你是专业的数字货币交易公告解析器，必须且仅输出一个纯 JSON 对象，禁止输出任何 Markdown 格式或额外说明。
JSON 字段必须完整包含以下键：
- command_type: 指令类型，可选 "OPEN_POSITION"、"AMEND_ENTRY"、"CANCEL_ORDER"、"CLOSE_POSITION"、"MOVE_STOP"
- exchange: 交易所名称，若原文未明确指出则必须输出 null（系统将使用本地默认交易所），不能把交易所未指定列为歧义
- base_asset: 标的币种/资产大写英文交易代码（如 "BTC"、"ETH"、"SOL"、"XAU"、"PAXG" 等）。严禁输出中文字符（例如遇到“黄金”必须输出 "XAU"，“大饼”必须输出 "BTC"，“以太/姨太”必须输出 "ETH"，“索拉纳”必须输出 "SOL" 等标准英文交易代码）
- side: 方向，"LONG"（做多/多/买入）或 "SHORT"（做空/空/卖出）
- entry: 进场区间对象，格式为 {"type": "RANGE", "low": "数值字符串", "high": "数值字符串"}。若遇到范围/区间进场价（如 "60000 - 60500"、"60000-60500"），必须取前一个数值为准（以前面的那一个为主，例如 60000），将 low 和 high 均设置为该前一个数值；若为市价或单点进场，则 low 和 high 相同
- take_profits: 止盈目标价字符串列表，如 ["2500", "2600"]
- stop_loss: 止损价格字符串，如 "2300"
- quantity: 始终输出 null（本地资金管理策略会自动计算实际仓位，不可将数量缺失列为歧义）
- confidence: 置信度数值字符串（范围 "0.0" 到 "1.0"），例如 "0.98"
- ambiguities: 真正缺失或存在冲突的歧义说明列表（字符串数组），若无歧义则输出 []
- trade_id: 仅在修改/平仓已有订单且原文带有唯一交易编号时填写字符串，新建开仓指令（OPEN_POSITION）必须输出 null
- breakeven: 动态保本策略配置对象，默认格式 {"trigger_ratio": "0.50", "profit_price_ratio": "0.01"}。
  （注：“浮盈一半做保本”对应 trigger_ratio="0.50"；“保本/浮盈保护”默认移动至进场价浮盈 1%，对应 profit_price_ratio="0.01"，均不可列为歧义）

注意规则：
1. 遇到范围/区间进场价（例如 "60000 - 60500"）时，一律以前面（前一个）的数值为主。
2. base_asset 必须为标准英文交易代码大写（如 BTC、ETH、SOL、XAU、PAXG 等），若原文使用中文名称/别名（如黄金、大饼、以太等），必须转换为对应标准英文代码，绝对禁止输出中文字符。
3. 不要推测或胡乱臆测原文中未提及的关键信息（如方向、进场价、止损等），如关键信息缺失，请在 ambiguities 中记录。
4. 所有价格与数字字段统一输出为字符串形式，避免浮点数精度丢失。
"""


class ParseError(ValueError):
    """解析交易公告失败时抛出的业务异常。"""
    pass


class DeepSeekParser:
    """基于 DeepSeek API (兼容 OpenAI Chat 接口) 的交易公告结构化解析器。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        raw_cfg = settings.raw.get("deepseek") or {}
        # 优先从环境变量 DEEPSEEK_API_KEY 读取，其次从配置文件读取
        self.api_key = os.getenv("DEEPSEEK_API_KEY") or str(raw_cfg.get("api_key", "")).strip()
        self.base_url = str(raw_cfg.get("base_url", "https://api.deepseek.com")).rstrip("/")
        self.model = str(raw_cfg.get("model", "deepseek-chat"))
        self.timeout_seconds = int(raw_cfg.get("timeout_seconds", 30))
        self.proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or raw_cfg.get("proxy")

    async def parse(self, text: str, command_id: str) -> TradeCommand:
        """调用 DeepSeek API 将文本解析为 TradeCommand 对象。"""
        command, _, _ = await self.parse_with_debug(text, command_id)
        return command

    async def parse_with_debug(self, text: str, command_id: str) -> tuple[TradeCommand, str, dict[str, Any]]:
        """调用 DeepSeek API 并返回 (TradeCommand, 原始响应文本, 提取的字典)，便于开发测试与调试输出。"""
        if not self.api_key:
            raise ParseError("未配置 DEEPSEEK_API_KEY，请在环境变量或配置文件中设置有效的 API 密钥")

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        user_content = f"command_id={command_id}\n待解析公告原文：\n{text}"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }

        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                async with session.post(url, json=payload, headers=headers, proxy=self.proxy) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error("DeepSeek API 请求失败 (状态码 %s): %s", response.status, error_text)
                        raise ParseError(f"DeepSeek API 请求失败，状态码 {response.status}：{error_text[:200]}")

                    result_json = await response.json()
                    choices = result_json.get("choices", [])
                    if not choices:
                        raise ParseError("DeepSeek API 返回结果为空，未包含 choices")

                    raw_content = choices[0].get("message", {}).get("content", "").strip()
                    if not raw_content:
                        raise ParseError("DeepSeek API 返回的内容为空")

                    extracted = _extract_json(raw_content)
                    command = command_from_json(extracted, command_id, self.settings)
                    return command, raw_content, extracted
        except aiohttp.ClientConnectorError as exc:
            raise ParseError(f"连接 DeepSeek API 服务失败：{exc}") from exc
        except TimeoutError as exc:
            raise ParseError(f"DeepSeek API 请求超时（超过 {self.timeout_seconds} 秒）") from exc
        except json.JSONDecodeError as exc:
            raise ParseError(f"解析 DeepSeek API 响应 JSON 失败：{exc}") from exc


def _extract_json(output: str) -> dict[str, Any]:
    """从大模型返回的文本中提取合法的 JSON 对象，兼容 Markdown 代码块及前后杂质文本。"""
    cleaned = output.strip()

    # 1. 尝试直接整段 JSON 解析
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict) and "command_type" in data:
            return data
    except json.JSONDecodeError:
        pass

    # 2. 如果包含 ```json ... ``` 代码块，提取代码块内容
    code_block_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if code_block_match:
        try:
            data = json.loads(code_block_match.group(1).strip())
            if isinstance(data, dict) and "command_type" in data:
                return data
        except json.JSONDecodeError:
            pass

    # 3. 使用 raw_decode 从第一个 '{' 逐字符查找有效 dict
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for index, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[index:])
            if isinstance(value, dict) and "command_type" in value:
                candidates.append(value)
        except json.JSONDecodeError:
            continue

    if candidates:
        return candidates[-1]

    raise ParseError("未能从模型返回内容中解析出符合要求的交易指令 JSON 对象")


def command_from_json(data: dict[str, Any], command_id: str, settings: Settings) -> TradeCommand:
    """将校验提取后的字典转换为领域实体 TradeCommand。"""
    try:
        exchange_defaulted = not data.get("exchange")
        exchange = Exchange(str(data.get("exchange") or settings.default_exchange.value).upper())

        entry_data = data.get("entry")
        entry = None if not entry_data else EntrySpec(
            type=str(entry_data.get("type", "RANGE")).upper(),
            low=Decimal(str(entry_data["low"])),
            high=Decimal(str(entry_data["high"])),
        )

        # 仓位数量始终由本地资金管理模块计算，忽略大模型提取的数量
        quantity = None

        be = data.get("breakeven") or {}
        return TradeCommand(
            command_id=command_id,
            command_type=CommandType(str(data["command_type"]).upper()),
            exchange=exchange,
            base_asset=normalize_asset(str(data["base_asset"])),
            side=PositionSide(str(data["side"]).upper()),
            entry=entry,
            take_profits=tuple(Decimal(str(v)) for v in data.get("take_profits", [])),
            stop_loss=Decimal(str(data["stop_loss"])) if data.get("stop_loss") not in (None, "") else None,
            quantity=quantity,
            confidence=Decimal(str(data.get("confidence", "0"))),
            ambiguities=tuple(str(v) for v in data.get("ambiguities", [])),
            trade_id=data.get("trade_id"),
            exchange_defaulted=exchange_defaulted,
            breakeven=BreakevenSpec(
                trigger_ratio=Decimal(str(be.get("trigger_ratio", "0.50"))),
                profit_price_ratio=Decimal(str(be.get("profit_price_ratio", "0.01"))),
            ),
        )
    except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
        raise ParseError(f"结构化指令字段格式无效：{exc}") from exc
