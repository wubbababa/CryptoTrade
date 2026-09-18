"""Telegram 内联按钮菜单的纯逻辑层（不依赖 aiohttp，便于单元测试）。

目标：让 ``/start`` 返回**可直接点击执行**的指令，而不是只能手抄的文本。

设计要点：

- 按钮一律用紧凑 ``callback_data``（``cb`` 前缀），Telegram 硬限制 64 字节，
  因此交易编号用 8 位 SHA-256 指纹代替；点击后再按指纹回查数据库还原真实编号，
  命中 0 条或多条都拒绝，绝不猜测目标交易。
- 只读按钮（状态/编号/帮助）点击即执行。
- 需要交易编号的写操作按钮出现在 ``/trades`` 的逐笔交易旁，并按交易状态只展示
  合法动作；取消止损、平仓等一键动作仍要再点一次「确认执行」。
- 需要额外价格的指令（改挂单价、改止损价）点击后返回**已填好交易编号**的命令，
  人工只需补一个价格，避免手抄长编号。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

# Telegram 回调数据硬上限：回调数据与回调查询各 64 字节。
CALLBACK_DATA_LIMIT = 64

# 统一前缀，便于识别本系统产生的按钮。
CALLBACK_PREFIX = "cb"

# 交易编号指纹长度（十六进制字符）。8 位 = 32 bit，碰撞概率可忽略；
# 且解析时要求「在活动交易中唯一命中」，命中多条一律拒绝。
FINGERPRINT_LENGTH = 8

# 需要人工补一个价格的指令：点击按钮后回填交易编号。
PRICE_COMMANDS: dict[str, str] = {
    "amend_entry": "<新进场价>",
    "move_stop": "<新止损价|breakeven>",
}

# 一键执行（仅需确认）的写指令。
ONE_CLICK_COMMANDS: tuple[str, ...] = ("cancel_order", "cancel_stop", "close_position")

# 按钮文案与确认文案。
COMMAND_LABELS: dict[str, str] = {
    "amend_entry": "✏️ 改挂单价",
    "cancel_order": "🗑 撤进场挂单",
    "cancel_stop": "🛑 取消止损",
    "move_stop": "🎯 改止损",
    "close_position": "💰 市价平仓",
}

CONFIRMATION_TEXT: dict[str, str] = {
    "amend_entry": "修改未成交进场挂单价格",
    "cancel_order": "撤销进场挂单（未成交）或取消止损（已开仓）",
    "cancel_stop": "取消止损，保留持仓与止盈单",
    "move_stop": "修改止损",
    "close_position": "以市价平仓",
}

# 按交易状态决定可用动作，避免展示必然被拒绝的按钮。
STATE_ACTIONS: dict[str, tuple[str, ...]] = {
    "PENDING_ENTRY": ("amend_entry", "cancel_order"),
    "OPEN": ("close_position", "cancel_stop", "move_stop"),
    "WAITING_ADD": ("move_stop",),
}

# 只读按钮允许触发的命令；按钮不得触达其它入口。
READONLY_COMMANDS: tuple[str, ...] = ("status", "trades", "help", "start")

# 键盘最多展示多少笔交易：避免按钮过多导致消息难以阅读。
TRADES_KEYBOARD_LIMIT = 6

# 全部合法的写指令名，用于校验回调里的命令名（防止伪造回调）。
WRITE_COMMANDS: tuple[str, ...] = tuple(STATE_ACTIONS["OPEN"]) + ("amend_entry", "cancel_order")


@dataclass(frozen=True, slots=True)
class CallbackAction:
    """一次按钮点击要执行的动作。"""

    kind: str                      # menu / panel / run / pick / go
    name: str = ""                 # 命令名（run/pick/go）
    fingerprint: str = ""          # pick/go：交易编号指纹
    trade_id: str = ""             # go：解析后的真实交易编号

    @property
    def is_write(self) -> bool:
        """是否会触达交易所写接口。"""
        return self.kind == "go"


def fingerprint(trade_id: str) -> str:
    """交易编号的短指纹，用于塞进 64 字节以内的 callback_data。"""
    digest = hashlib.sha256(trade_id.encode("utf-8")).hexdigest()
    return digest[:FINGERPRINT_LENGTH]


def encode_callback(kind: str, *parts: str) -> str:
    """把动作编码为紧凑的 ``callback_data``；超长时立即断言失败。"""
    data = ":".join([CALLBACK_PREFIX, kind, *parts])
    assert len(data.encode("utf-8")) <= CALLBACK_DATA_LIMIT, f"callback_data 过长：{data}"
    return data


def decode_callback(data: str) -> CallbackAction:
    """解析 ``callback_data``；不是本系统产生的按钮时抛 ``ValueError``。"""
    pieces = data.split(":")
    if len(pieces) < 2 or pieces[0] != CALLBACK_PREFIX:
        raise ValueError(f"无法识别的回调数据：{data}")
    kind, rest = pieces[1], pieces[2:]
    if kind == "menu":
        return CallbackAction("menu")
    if kind == "panel":
        return CallbackAction("panel", rest[0] if rest else "")
    if kind == "run":
        if not rest:
            raise ValueError("run 回调缺少命令名")
        return CallbackAction("run", rest[0])
    if kind == "pick":
        if len(rest) != 2:
            raise ValueError("pick 回调需要命令名与交易指纹")
        return CallbackAction("pick", rest[0], rest[1])
    if kind == "go":
        if len(rest) != 2:
            raise ValueError("go 回调需要命令名与交易指纹")
        return CallbackAction("go", rest[0], rest[1])
    raise ValueError(f"未知的回调动作：{kind}")


def _button(text: str, callback_data: str) -> dict[str, Any]:
    return {"text": text, "callback_data": callback_data}


def main_menu_keyboard() -> list[list[dict[str, Any]]]:
    """``/start`` 主菜单：只读查询 + 进入人工指令面板。"""
    return [
        [
            _button("📊 运行状态", encode_callback("run", "status")),
            _button("📋 交易与操作", encode_callback("run", "trades")),
        ],
        [
            _button("✏️ 人工指令", encode_callback("panel")),
            _button("❓ 帮助", encode_callback("run", "help")),
        ],
    ]


def manual_panel_keyboard() -> list[list[dict[str, Any]]]:
    """人工指令面板：说明逐笔交易按钮的用法。"""
    return [
        [_button("📋 选择交易并操作", encode_callback("run", "trades"))],
        [_button("⬅️ 返回主菜单", encode_callback("menu"))],
    ]


def status_keyboard() -> list[list[dict[str, Any]]]:
    """状态类只读回复的快捷键盘。"""
    return [
        [
            _button("🔄 刷新状态", encode_callback("run", "status")),
            _button("📋 交易与操作", encode_callback("run", "trades")),
        ],
        [_button("⬅️ 返回主菜单", encode_callback("menu"))],
    ]


def back_keyboard() -> list[list[dict[str, Any]]]:
    """模板类回复的返回键盘。"""
    return [
        [_button("📋 交易与操作", encode_callback("run", "trades")),
         _button("⬅️ 返回主菜单", encode_callback("menu"))],
    ]


def confirm_keyboard(name: str, trade_id: str) -> list[list[dict[str, Any]]]:
    """写操作二次确认：确认携带指纹，执行时再回查真实交易编号。"""
    assert name in ONE_CLICK_COMMANDS, f"{name} 不是一键指令"
    return [
        [
            _button("✅ 确认执行", encode_callback("go", name, fingerprint(trade_id))),
            _button("❌ 取消", encode_callback("run", "trades")),
        ],
    ]


def trade_short_label(trade_id: str) -> str:
    """把长交易编号压缩成按钮可读标签，例如 ``ETH多 001``。"""
    parts = trade_id.split("-")
    asset = parts[1] if len(parts) > 1 else trade_id[:6]
    side = "多" if "LONG" in trade_id else ("空" if "SHORT" in trade_id else "")
    sequence = parts[-1] if parts and parts[-1].isdigit() else ""
    return f"{asset}{side} {sequence}".strip()


def trades_keyboard(trades: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """逐笔交易的可用动作按钮；按状态过滤，避免展示必然被拒绝的动作。

    ``trades`` 每项需含 ``trade_id`` 与 ``state``。
    """
    rows: list[list[dict[str, Any]]] = []
    for trade in trades[:TRADES_KEYBOARD_LIMIT]:
        trade_id = str(trade["trade_id"])
        actions = STATE_ACTIONS.get(str(trade.get("state", "")).upper(), ())
        if not actions:
            continue
        label = trade_short_label(trade_id)
        print_fp = fingerprint(trade_id)
        # 每笔交易占两行：第一行是市价平仓等一键动作，第二行是需要补价格的指令。
        one_click = [name for name in actions if name in ONE_CLICK_COMMANDS]
        needs_price = [name for name in actions if name in PRICE_COMMANDS]
        if one_click:
            rows.append([
                _button(f"{COMMAND_LABELS[name]} {label}", encode_callback("pick", name, print_fp))
                for name in one_click
            ])
        if needs_price:
            rows.append([
                _button(f"{COMMAND_LABELS[name]} {label}", encode_callback("pick", name, print_fp))
                for name in needs_price
            ])
    rows.append([_button("⬅️ 返回主菜单", encode_callback("menu"))])
    return rows