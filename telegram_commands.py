"""Telegram 文本命令层。

只读查询与帮助，不包含任何下单/平仓动作；交易动作仍然只能由
来源频道的自然语言公告经解析与校验后触发。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from database import Database
from exchange_router import ExchangeRouter

logger = logging.getLogger(__name__)

# Telegram 单条消息上限 4096 字符，留出余量避免发送失败。
MAX_REPLY_CHARS = 3500

# 这些状态代表交易已经结束，不再计入“活动交易”。
TERMINAL_STATES = ("CLOSED", "CANCELLED", "REJECTED")

# 交易所开放订单状态（与 monitor/trading_service 保持一致）。
OPEN_ORDER_STATES = ("NEW", "OPEN", "PARTIALLY_FILLED")

# 读取账户权益的等待上限，避免 /status 因网络问题长时间卡住。
EQUITY_TIMEOUT_SECONDS = 5


class UnknownCommand(ValueError):
    """命令不在支持列表内。"""


def parse_command(text: str, bot_username: str | None = None) -> tuple[str, tuple[str, ...]] | None:
    """解析 ``/status@bot arg`` 形式的文本命令。

    返回 ``(命令名, 参数元组)``；不是命令、或 @ 指向其他机器人时返回 ``None``。
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    head, _, rest = stripped.partition(" ")
    name = head[1:]
    if "@" in name:
        name, _, mention = name.partition("@")
        expected = (bot_username or "").lstrip("@").lower()
        if expected and mention.lower() != expected:
            return None
    name = name.strip().lower()
    if not name:
        return None
    args = tuple(rest.split()) if rest.strip() else ()
    return name, args


class TelegramCommandHandler:
    """执行只读命令并生成中文回复文本。"""

    def __init__(self, database: Database, router: ExchangeRouter,
                 started_at: datetime | None = None) -> None:
        self.database = database
        self.router = router
        self.started_at = started_at or datetime.now(timezone.utc)

    def help_text(self) -> str:
        return (
            "可用命令：\n"
            "/start - 确认机器人在线并显示帮助\n"
            "/status - 查看运行状态、活动交易与账户权益\n"
            "/help - 显示本帮助\n\n"
            "说明：命令为只读操作，不会下单或平仓，回复直接发回本聊天。"
            "命令仅对来源频道和白名单私聊生效；来源频道中不以 / 开头的文本"
            "仍按交易公告解析。"
        )

    async def execute(self, name: str, args: tuple[str, ...] = ()) -> str:
        if name in {"start", "help"}:
            return self.help_text()
        if name == "status":
            return await self.status_text()
        raise UnknownCommand(name)

    async def status_text(self) -> str:
        """汇总本地数据库与交易所只读状态，任何单项失败都不影响整体回复。"""
        sections = [self._header()]

        states = await self._safe_fetch(
            "SELECT state, COUNT(*) AS c FROM trade_instances GROUP BY state ORDER BY state"
        )
        active = await self._safe_fetch(
            "SELECT trade_id,exchange,instrument_key,side,state FROM trade_instances "
            "WHERE state NOT IN (?,?,?) ORDER BY trade_id DESC LIMIT 8",
            TERMINAL_STATES,
        )
        open_orders = await self._safe_fetch(
            "SELECT COUNT(*) AS c FROM orders WHERE status IN (?,?,?)",
            OPEN_ORDER_STATES,
        )
        recent = await self._safe_fetch(
            "SELECT command_id,command_type,status FROM commands ORDER BY rowid DESC LIMIT 5"
        )
        offset = await self._safe_fetch(
            "SELECT value FROM runtime_state WHERE key='telegram_offset'"
        )

        sections.append("\n【交易统计】")
        if states:
            summary = "、".join(f"{row['state']}={row['c']}" for row in states)
            sections.append(f"  {summary}")
        else:
            sections.append("  暂无交易记录")

        sections.append("\n【活动交易】")
        if active:
            for row in active:
                sections.append(
                    f"  • {row['trade_id']} [{row['state']}] "
                    f"{row['instrument_key']} {row['side']}"
                )
        else:
            sections.append("  无")

        sections.append(f"\n【本地活动挂单】{open_orders[0]['c'] if open_orders else 0} 笔")

        sections.append("\n【交易所账户】")
        sections.extend(await self._exchange_lines())

        sections.append("\n【最近指令】")
        if recent:
            for row in recent:
                sections.append(
                    f"  • {row['command_type']} → {row['status']}（{row['command_id']}）"
                )
        else:
            sections.append("  无")

        if offset:
            sections.append(f"\n【轮询位置】offset={offset[0]['value']}")

        return _truncate("\n".join(sections))

    def _header(self) -> str:
        elapsed = datetime.now(timezone.utc) - self.started_at
        seconds = max(int(elapsed.total_seconds()), 0)
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        uptime = f"{hours}小时{minutes}分{secs}秒" if hours else f"{minutes}分{secs}秒"
        exchanges = ", ".join(exchange.value for exchange in self.router.adapters) or "无"
        return (
            "🤖 CryptoTrade 状态\n"
            f"运行时长：{uptime}\n"
            f"已启用交易所：{exchanges}"
        )

    async def _exchange_lines(self) -> list[str]:
        if not self.router.adapters:
            return ["  无可用交易所"]
        lines = []
        for exchange, adapter in self.router.adapters.items():
            try:
                equity = await asyncio.wait_for(adapter.get_equity(), timeout=EQUITY_TIMEOUT_SECONDS)
                lines.append(f"  • {exchange.value}：权益 {equity} USDT")
            except Exception as exc:  # 只读状态失败不应影响其他交易所
                logger.warning("读取 %s 权益失败：%s", exchange.value, exc)
                lines.append(f"  • {exchange.value}：读取失败（{exc}）")
        return lines

    async def _safe_fetch(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        try:
            return await self.database.fetch_all(sql, params)
        except Exception as exc:  # 数据库异常时仍返回可读状态
            logger.exception("命令查询失败：%s", sql)
            return []


def _truncate(text: str, limit: int = MAX_REPLY_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n…（内容已截断）"