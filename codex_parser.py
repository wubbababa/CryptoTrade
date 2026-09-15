"""调用本机 Codex CLI，将公告转换为严格结构化指令。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from decimal import Decimal, InvalidOperation
from typing import Any

from models import BreakevenSpec, CommandType, EntrySpec, Exchange, PositionSide, TradeCommand, normalize_asset
from settings import Settings


SYSTEM_PROMPT = """你是交易公告解析器，只输出一个 JSON 对象，禁止 Markdown。
字段必须包含 command_type、exchange、base_asset、side、entry、take_profits、stop_loss、
quantity、confidence、ambiguities，可选 trade_id。exchange 不明确时输出 null；数字输出字符串。
base_asset 必须为标准英文交易代码大写（如 BTC、ETH、SOL、XAU、PAXG 等），若原文使用中文名称/别名（如黄金、大饼、以太等），必须转换为对应标准英文代码，绝对禁止输出中文字符。
遇到范围/区间进场价（如 "60000 - 60500"）时，一律以前面（前一个）数值为准。
entry 格式为 {"type":"RANGE","low":"...","high":"..."}，若为范围时 low 与 high 均设置为该前一个数值。
quantity 始终输出 null，仓位由本地资金策略计算，不能把数量缺失列为歧义。
公告未指定交易所时 exchange 输出 null，系统会使用本地默认交易所，不能列为歧义。
“浮盈一半做保本”等价于盈利进度 50% 时触发，trigger_ratio 输出 "0.50"；
“保本/浮盈保护”默认移动到进场价浮盈 1%，profit_price_ratio 输出 "0.01"，不能列为歧义。
OPEN_POSITION 的 trade_id 输出 null；只有修改已有交易且原文包含唯一编号时才填写。
不要推测缺失的方向、进场价、止盈或止损，将真正缺失或不明确的内容写入 ambiguities。
"""


class ParseError(ValueError):
    pass


class CodexParser:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        configured = str(settings.raw["codex"].get("command", "codex"))
        self.command = resolve_command(configured)

    async def parse(self, text: str, command_id: str) -> TradeCommand:
        cfg = self.settings.raw["codex"]
        prompt = f"{SYSTEM_PROMPT}\ncommand_id={command_id}\n公告：\n{text}"
        schema_path = Path(__file__).resolve().parent / "schemas" / "trade_command.schema.json"
        descriptor, output_path_text = tempfile.mkstemp(prefix="crypto-trade-codex-", suffix=".json")
        os.close(descriptor)
        output_path = Path(output_path_text)
        try:
            executable, arguments = build_subprocess_command(self.command, [
                "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-rules",
                "--sandbox", "read-only", "--color", "never",
                "--output-schema", str(schema_path),
                "--output-last-message", str(output_path), "-",
            ])
            process = await asyncio.create_subprocess_exec(
                executable, *arguments,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout, stderr = await asyncio.wait_for(
                    process.communicate(prompt.encode("utf-8")),
                    timeout=int(cfg.get("timeout_seconds", 45)),
                )
            except TimeoutError as exc:
                process.kill()
                await process.wait()
                raise ParseError("Codex 解析超时") from exc
            if process.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace")[-500:]
                raise ParseError(f"Codex 解析失败：{detail}")
            final_message = output_path.read_text(encoding="utf-8")
            return command_from_json(_extract_json(final_message), command_id, self.settings)
        finally:
            output_path.unlink(missing_ok=True)


def resolve_command(command: str) -> str:
    """启动前解析 CLI 路径，让配置错误尽早暴露。"""
    resolved = shutil.which(command)
    if resolved is None:
        raise RuntimeError(
            f"找不到 Codex CLI：{command}；请先安装并确认在终端可运行 codex --version"
        )
    return resolved


def build_subprocess_command(command: str, arguments: list[str]) -> tuple[str, list[str]]:
    """Windows 的 .cmd/.bat 需要经 cmd.exe 执行，原生程序直接启动。"""
    if os.name == "nt" and Path(command).suffix.lower() in {".cmd", ".bat"}:
        command_line = subprocess.list2cmdline([command, *arguments])
        return os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"), ["/d", "/s", "/c", command_line]
    return command, arguments


def _extract_json(output: str) -> dict[str, Any]:
    """兼容 CLI 附带少量状态文本，但只接受最后一个完整 JSON 对象。"""
    # --output-last-message 正常情况下就是完整 JSON，必须优先整体解析，
    # 否则逐个扫描会误选 entry 或 breakeven 等嵌套对象。
    try:
        value = json.loads(output.strip())
        if isinstance(value, dict) and "command_type" in value:
            return value
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(output[index:])
            if isinstance(value, dict) and "command_type" in value:
                candidates.append(value)
        except json.JSONDecodeError:
            continue
    if not candidates:
        raise ParseError("Codex 未返回有效 JSON")
    return candidates[-1]


def command_from_json(data: dict[str, Any], command_id: str, settings: Settings) -> TradeCommand:
    try:
        exchange_defaulted = not data.get("exchange")
        exchange = Exchange(str(data.get("exchange") or settings.default_exchange.value).upper())
        entry_data = data.get("entry")
        entry = None if not entry_data else EntrySpec(
            type=str(entry_data.get("type", "RANGE")).upper(),
            low=Decimal(str(entry_data["low"])), high=Decimal(str(entry_data["high"])),
        )
        quantity_value = data.get("quantity")
        # 仓位数量由确定性资金管理模块计算，不采信公告或模型给出的数量。
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
        raise ParseError(f"结构化指令字段无效：{exc}") from exc
