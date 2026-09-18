"""Telegram 对话命令层：只读状态查询 + 受严格核对的人工指令入口。

查询命令（/start、/help、/status、/trades）永不触碰交易所写接口；
人工指令（/amend_entry、/cancel_order、/cancel_stop、/move_stop、/close_position）
会被翻译成统一的 TradeCommand，交由 TradingService 执行。真正的前置校验不在本模块，
而是由 TradingService._load_trade 及各分支在触碰交易所前完成：交易编号必须存在，且
交易所、合约、方向、远程订单/仓位的组合必须唯一；任何一项无法确认即安全拒绝。
本模块只负责解析与回复，绝不直接调用交易所适配器。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from database import Database
from exchange_router import ExchangeRouter
from models import BreakevenSpec, CommandType, EntrySpec, Exchange, PositionSide, TradeCommand
from telegram_menu import (
    COMMAND_LABELS,
    CONFIRMATION_TEXT,
    PRICE_COMMANDS,
    READONLY_COMMANDS,
    STATE_ACTIONS,
    WRITE_COMMANDS,
    back_keyboard,
    confirm_keyboard,
    decode_callback,
    fingerprint,
    main_menu_keyboard,
    manual_panel_keyboard,
    status_keyboard,
    trades_keyboard,
)
from trading_service import TradingService

logger = logging.getLogger(__name__)

# Telegram 单条消息上限 4096 字符，留出余量避免发送失败。
MAX_REPLY_CHARS = 3500

# 这些状态代表交易已经结束，不再计入“活动交易”。
TERMINAL_STATES = ("CLOSED", "CANCELLED", "REJECTED")

# 交易所开放订单状态（与 monitor/trading_service 保持一致）。
OPEN_ORDER_STATES = ("NEW", "OPEN", "PARTIALLY_FILLED")

# 读取账户权益的等待上限，避免 /status 因网络问题长时间卡住。
EQUITY_TIMEOUT_SECONDS = 5

# 人工指令不做自然语言解析，来源可信，因此使用固定置信度。
MANUAL_CONFIDENCE = Decimal("1")

# 帮助文本中逐条列出的人工指令说明，避免文档与实现脱节。
MANUAL_HELP_LINES = (
    "/amend_entry <trade_id> <价格> - 修改未成交进场挂单价格（仅 PENDING_ENTRY）",
    "/cancel_order <trade_id> - 未成交时撤进场挂单；已开仓时取消止损",
    "/cancel_stop <trade_id> - 语义化别名：明确取消止损并保留持仓与止盈",
    "/move_stop <trade_id> <价格> - 修改止损；已达保本条件时会校验只向有利方向移动",
    "/move_stop <trade_id> breakeven - 无参数恢复止损，恢复至进场均价浮盈 1% 处",
    "/close_position <trade_id> - 明确市价平仓（必须先唯一核对远程持仓）",
)


# 人工指令命令名 → 统一领域枚举；本层只做参数解析，权限与核对仍在用例层。
MANUAL_COMMAND_TYPES: dict[str, CommandType] = {
    "amend_entry": CommandType.AMEND_ENTRY,
    "cancel_order": CommandType.CANCEL_ORDER,
    "cancel_stop": CommandType.CANCEL_ORDER,
    "move_stop": CommandType.MOVE_STOP,
    "close_position": CommandType.CLOSE_POSITION,
}


@dataclass(frozen=True, slots=True)
class CommandReply:
    """一条命令的回复文本与可选内联按钮键盘。

    ``keyboard`` 为 ``None`` 表示纯文本；为列表时下发内联按钮（可点击执行）。
    """

    text: str
    keyboard: list[list[dict[str, Any]]] | None = None
    # 是否用「就地编辑」替换原消息（按钮点击后为 True，避免连续刷屏）。
    edit: bool = False


class UnknownCommand(ValueError):
    """命令不在支持列表内。"""


class CommandRejected(ValueError):
    """人工指令被安全拒绝：参数不完整、存在歧义或目标无法核对。"""


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


def parse_price(raw: str, field: str) -> Decimal:
    """解析人工指令中的正数价格，拒绝 NaN/Infinity 等无效输入。"""
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise CommandRejected(f"{field} 不是有效数字：{raw}") from exc
    if not value.is_finite() or value <= 0:
        raise CommandRejected(f"{field} 必须为正数：{raw}")
    return value


class TelegramCommandHandler:
    """执行查询命令与人工指令，并生成中文回复文本。

    人工指令不在此处调用交易所，而是复用交易用例编排层：交易编号与
    交易所/合约/方向在 ``TradingService._load_trade()`` 校验，远程开放订单与
    持仓由各分支按客户订单号核对，无法唯一关联时拒绝。
    """

    def __init__(self, database: Database, router: ExchangeRouter,
                 service: TradingService | None = None,
                 started_at: datetime | None = None) -> None:
        self.database = database
        self.router = router
        self.service = service
        self.started_at = started_at or datetime.now(timezone.utc)

    def main_menu(self) -> list[list[dict[str, Any]]]:
        """主菜单键盘；供回复未知命令/拒绝等场景复用。"""
        return main_menu_keyboard()

    def help_text(self) -> str:
        lines = [
            "可用命令：",
            "/start - 确认机器人在线并显示可点击按钮",
            "/status - 查看运行状态、活动交易与账户权益",
            "/trades - 列出活动交易编号与可用操作按钮",
            "/help - 显示本帮助",
            "",
            "人工指令（也可直接用下方按钮点击执行；无法唯一核对时安全拒绝）：",
            *MANUAL_HELP_LINES,
            "",
            "说明：人工指令会先核对交易所、合约、方向与远程订单/持仓，"
            "且仅对来源频道和 .env 白名单私聊生效；来源频道中不以 / 开头的文本仍按交易公告解析。",
        ]
        return _truncate("\n".join(lines))

    async def execute(self, name: str, args: tuple[str, ...] = (),
                      command_id: str | None = None) -> str:
        """兼容入口：只返回回复文本（供测试与纯文本调用方使用）。"""
        return (await self.execute_reply(name, args, command_id)).text

    async def execute_reply(self, name: str, args: tuple[str, ...] = (),
                            command_id: str | None = None) -> CommandReply:
        """执行一条命令并返回「回复文本 + 可选内联按钮」。

        ``command_id`` 由调用方（main）传入 Telegram 消息级唯一编号：消息层已按
        ``(chat_id, message_id)`` 去重，这里再把它写进指令编号，既可防止 Telegram
        重投递造成重复下单，又不会让「失败后原样重试」被误判为重复指令。
        """
        if name in {"start", "help"}:
            return CommandReply(self.help_text(), main_menu_keyboard())
        if name == "status":
            return CommandReply(await self.status_text(), status_keyboard())
        if name == "trades":
            return CommandReply(await self.trades_text(), await self._trades_keyboard())
        if name in MANUAL_COMMAND_TYPES:
            return CommandReply(await self._execute_manual(name, args, command_id))
        raise UnknownCommand(name)

    # ------------------------------------------------------------------
    # 内联按钮点击
    # ------------------------------------------------------------------

    async def handle_callback(self, data: str) -> CommandReply:
        """处理一次按钮点击；返回应显示的新文本与键盘。

        写操作（改价/撤单/改止损/平仓）必须点两次：第一次只看确认文案，
        第二次才真正通过 ``TradingService`` 执行，避免误触直接下单。
        """
        try:
            action = decode_callback(data)
        except ValueError as exc:
            raise CommandRejected(str(exc)) from exc
        if action.kind == "menu":
            return CommandReply(self.help_text(), main_menu_keyboard(), edit=True)
        if action.kind == "panel":
            return CommandReply(self.manual_panel_text(), manual_panel_keyboard(), edit=True)
        if action.kind == "run":
            return await self._run_readonly(action.name, edit=True)
        if action.kind == "pick":
            return await self._pick_action(action.name, action.fingerprint)
        if action.kind == "go":
            return await self._confirm_action(action.name, action.fingerprint)
        raise CommandRejected(f"不支持的回调动作：{action.kind}")

    async def _run_readonly(self, name: str, edit: bool) -> CommandReply:
        """只读按钮：直接执行并就地编辑。"""
        if name not in READONLY_COMMANDS:
            raise CommandRejected(f"按钮只能触发只读或人工指令，收到：{name}")
        if name == "status":
            return CommandReply(await self.status_text(), status_keyboard(), edit=edit)
        if name == "trades":
            return CommandReply(await self.trades_text(), await self._trades_keyboard(), edit=edit)
        if name in {"start", "help"}:
            return CommandReply(self.help_text(), main_menu_keyboard(), edit=edit)

    async def _pick_action(self, name: str, print_fp: str) -> CommandReply:
        """点击某笔交易的动作按钮。

        一键动作 → 展示确认键盘；需要价格的指令 → 回填交易编号的命令模板。
        """
        if name not in WRITE_COMMANDS:
            raise CommandRejected(f"不允许通过按钮执行该指令：{name}")
        trade = await self._resolve_trade(print_fp)
        label = f"{trade['trade_id']} [{trade['state']}] {trade['instrument_key']} {trade['side']}"
        if name in PRICE_COMMANDS:
            placeholder = PRICE_COMMANDS[name]
            template = f"/{name} {trade['trade_id']} {placeholder}"
            text = (
                f"📍 {label}\n"
                f"{CONFIRMATION_TEXT.get(name, name)}需要填一个价格。\n"
                f"复制下面一行并改掉 {placeholder} 即可：\n\n"
                f"{template}"
            )
            return CommandReply(text, back_keyboard(), edit=True)
        text = (
            f"📍 {label}\n"
            f"即将{CONFIRMATION_TEXT.get(name, name)}。\n"
            f"请确认后点击「✅ 确认执行」。"
        )
        return CommandReply(text, confirm_keyboard(name, trade["trade_id"]), edit=True)

    async def _confirm_action(self, name: str, print_fp: str) -> CommandReply:
        """已确认的写操作：按指纹还原真实交易编号后交给 TradingService。"""
        if name not in WRITE_COMMANDS:
            raise CommandRejected(f"不允许通过按钮执行该指令：{name}")
        trade = await self._resolve_trade(print_fp)
        trade_id = trade["trade_id"]
        # 复用与手输命令完全相同的人工指令路径，避免两套执行逻辑。
        report = await self._execute_manual(name, (trade_id,), f"cb-{fingerprint(trade_id)}-{name}")
        text = f"📍 {trade_id}\n{report}"
        return CommandReply(text, back_keyboard(), edit=True)

    async def _resolve_trade(self, print_fp: str) -> dict[str, Any]:
        """按指纹在活动交易中回查真实编号；命中 0 条或多条都必须拒绝。"""
        rows = await self._safe_fetch(
            "SELECT trade_id,exchange,instrument_key,side,state FROM trade_instances "
            "WHERE state NOT IN (?,?,?) ORDER BY trade_id DESC LIMIT 200",
            TERMINAL_STATES,
        )
        matches = [row for row in rows if fingerprint(str(row["trade_id"])) == print_fp]
        if not matches:
            raise CommandRejected(
                "按钮对应的交易已不在活动列表中（可能已平仓或重启清库）。\n"
                "请重新点击「📋 交易与操作」获取最新按钮。"
            )
        if len(matches) > 1:
            raise CommandRejected(f"指纹 {print_fp} 命中多笔交易，拒绝猜测，请直接手输完整编号。")
        return matches[0]

    def manual_panel_text(self) -> str:
        """人工指令面板说明：写操作一律两步确认。"""
        return (
            "✏️ 人工指令面板\n"
            "点击「📋 选择交易并操作」会列出每笔活动交易的可用动作按钮。\n\n"
            "说明：\n"
            "• 撤进场挂单、取消止损、市价平仓属于一键动作，仍需再点一次「确认执行」；\n"
            "• 改挂单价、改止损价会返回已填好交易编号的命令，只需补一个价格；\n"
            "• 所有动作都会先核对交易所、合约、方向与远程订单/持仓，无法唯一关联即拒绝。"
        )

    # ------------------------------------------------------------------
    # 人工指令：解析 → 构造 TradeCommand → 交给 TradingService
    # ------------------------------------------------------------------

    async def _execute_manual(self, name: str, args: tuple[str, ...],
                              command_id: str | None = None) -> str:
        if self.service is None:
            raise CommandRejected("人工指令入口未接入交易服务，请检查启动装配")
        command = await self._build_manual_command(name, args, command_id)
        # TradingService 内部已完成状态、交易所、合约、方向与远程对象核对；
        # 失败会抛异常，由 main 记录审计并回复拒绝原因。
        report = await self.service.execute(command)
        return _truncate(report)

    async def _build_manual_command(self, name: str, args: tuple[str, ...],
                                       command_id: str | None = None) -> TradeCommand:
        """把人工指令参数解析为 TradeCommand。

        交易编号是唯一的目标定位手段，因此先按编号读取本地交易并据此填充
        交易所/合约/方向；TradingService 会再次比对，确保报文中不出现矛盾信息。
        """
        if not args:
            raise CommandRejected(f"/{name} 缺少参数：\n" + self._manual_usage(name))
        trade_id = args[0].strip()
        trade = await self._load_trade_for_command(trade_id)
        command_type = MANUAL_COMMAND_TYPES[name]
        extra = args[1:]
        entry: EntrySpec | None = None
        stop_loss: Decimal | None = None

        if name == "amend_entry":
            if len(extra) != 1:
                raise CommandRejected("用法：\n" + self._manual_usage(name))
            price = parse_price(extra[0], "进场价格")
            entry = EntrySpec(type="LIMIT", low=price, high=price)
        elif name == "move_stop":
            if len(extra) > 1:
                raise CommandRejected("用法：\n" + self._manual_usage(name))
            if extra:
                keyword = extra[0].strip().lower()
                # 「保本」等关键词等价于恢复止损：留空由服务层按进场均价计算。
                if keyword not in {"breakeven", "be", "保本", "恢复"}:
                    stop_loss = parse_price(extra[0], "止损价格")
        elif extra:
            raise CommandRejected(f"/{name} 不接受额外参数：{' '.join(extra)}")

        return TradeCommand(
            command_id=self._manual_command_id(name, trade_id, command_id),
            command_type=command_type,
            exchange=Exchange(trade["exchange"]),
            base_asset=_asset_of(trade["instrument_key"]),
            side=PositionSide(trade["side"]),
            entry=entry,
            take_profits=(),
            stop_loss=stop_loss,
            quantity=None,
            confidence=MANUAL_CONFIDENCE,
            ambiguities=(),
            trade_id=trade_id,
            breakeven=BreakevenSpec(),
        )

    async def _load_trade_for_command(self, trade_id: str) -> dict[str, Any]:
        """按编号读取本地交易；不存在时直接拒绝，绝不以币种猜目标。"""
        rows = await self.database.fetch_all(
            "SELECT trade_id,exchange,instrument_key,side,state FROM trade_instances WHERE trade_id=?",
            (trade_id,),
        )
        if not rows:
            raise CommandRejected(
                f"交易编号不存在：{trade_id}\n请先用 /trades 查看当前活动交易编号。"
            )
        return rows[0]

    @staticmethod
    def _manual_command_id(name: str, trade_id: str, message_id: str | None) -> str:
        """人工指令编号：优先绑定 Telegram 消息编号，便于审计追溯到具体消息。

        未提供消息编号时（例如单元测试直接调用）退化为随机编号，保证「失败后原样
        重试」不会被指令表当作重复指令静默忽略。
        """
        if message_id:
            return f"{message_id}-{name}"
        return f"tg-manual-{name}-{uuid4().hex[:12]}"

    def _manual_usage(self, name: str) -> str:
        """列出该命令的全部用法行（move_stop 同时支持指定价格与保本恢复）。"""
        matches = [line for line in MANUAL_HELP_LINES if line.startswith(f"/{name} ")]
        return "\n".join(matches) if matches else f"/{name} <trade_id>"

    async def _active_trades(self) -> list[dict[str, Any]]:
        """读取活动交易（按钮与文本视图共用同一份数据）。"""
        return await self._safe_fetch(
            "SELECT trade_id,exchange,instrument_key,side,state FROM trade_instances "
            "WHERE state NOT IN (?,?,?) ORDER BY trade_id DESC LIMIT 30",
            TERMINAL_STATES,
        )

    async def trades_text(self) -> str:
        """列出可用于人工指令的交易编号与当前状态，便于人工核对。"""
        rows = await self._active_trades()
        sections = ["📋 活动交易与可用操作", "可直接点击下方按钮，或手输完整编号："]
        if not rows:
            sections.append("  当前没有活动交易")
        for row in rows:
            actions = STATE_ACTIONS.get(str(row["state"]).upper(), ())
            labels = "、".join(COMMAND_LABELS[name] for name in actions) or "无可执行动作"
            sections.append(
                f"  • {row['trade_id']}\n"
                f"     {row['exchange']} {row['instrument_key']} {row['side']} [{row['state']}]\n"
                f"     可用：{labels}"
            )
        sections.append("\n需要额外价格的指令：点击按钮后按提示补一个价格即可。")
        return _truncate("\n".join(sections))

    async def _trades_keyboard(self) -> list[list[dict[str, Any]]]:
        """按当前活动交易生成可用动作按钮。"""
        return trades_keyboard(await self._active_trades())

    # ------------------------------------------------------------------
    # 只读查询
    # ------------------------------------------------------------------

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
        except Exception:  # 数据库异常时仍返回可读状态
            logger.exception("命令查询失败：%s", sql)
            return []


def _asset_of(instrument_key: str) -> str:
    """从 ``ETH/USDT:PERP`` 还原标的代码，供 TradeCommand 校验使用。"""
    return instrument_key.split("/", 1)[0].strip().upper()


def _truncate(text: str, limit: int = MAX_REPLY_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n…（内容已截断）"