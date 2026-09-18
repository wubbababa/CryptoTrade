"""确定性业务校验，任何自然语言结果都不得绕过本模块。"""

from __future__ import annotations

from decimal import Decimal

from models import CommandType, PositionSide, TradeCommand, normalize_asset
from settings import Settings


class ValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("；".join(errors))


class CommandValidator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def validate(self, command: TradeCommand) -> None:
        errors: list[str] = []
        if command.exchange not in self.settings.enabled_exchanges():
            errors.append(f"交易所 {command.exchange.value} 未启用")
        norm_asset = normalize_asset(command.base_asset)
        if norm_asset not in self.settings.whitelist and command.base_asset not in self.settings.whitelist:
            errors.append(f"币种 {command.base_asset} 不在白名单")
        parser_cfg = self.settings.raw.get("deepseek") or self.settings.raw.get("codex") or {}
        minimum = Decimal(str(parser_cfg.get("minimum_confidence", "0.90")))
        if command.confidence < minimum:
            errors.append(f"解析置信度 {command.confidence} 低于阈值 {minimum}")
        if command.ambiguities:
            errors.append("公告存在歧义：" + "、".join(command.ambiguities))
        if command.command_type == CommandType.OPEN_POSITION:
            self._validate_open(command, errors)
        else:
            self._validate_amendment(command, errors)
        if errors:
            raise ValidationError(errors)

    @staticmethod
    def _validate_open(command: TradeCommand, errors: list[str]) -> None:
        if command.entry is None:
            errors.append("缺少进场价格")
            return
        if command.entry.low <= 0 or command.entry.high <= 0 or command.entry.low > command.entry.high:
            errors.append("进场价格范围无效")
        if command.stop_loss is None:
            errors.append("缺少止损")
        if not command.take_profits:
            errors.append("缺少止盈")
        reference = command.entry.reference_price
        if command.side == PositionSide.LONG:
            if command.stop_loss is not None and command.stop_loss >= command.entry.low:
                errors.append("多单止损必须低于进场区间")
            if any(tp <= reference for tp in command.take_profits):
                errors.append("多单止盈必须高于进场价")
        else:
            if command.stop_loss is not None and command.stop_loss <= command.entry.high:
                errors.append("空单止损必须高于进场区间")
            if any(tp >= reference for tp in command.take_profits):
                errors.append("空单止盈必须低于进场价")

    @staticmethod
    def _validate_amendment(command: TradeCommand, errors: list[str]) -> None:
        if not command.trade_id:
            errors.append("修改类指令必须包含唯一交易编号")
        if command.command_type == CommandType.AMEND_ENTRY and command.entry is None:
            errors.append("改进场指令必须包含新的进场价格")
        if command.command_type == CommandType.ADD_POSITION:
            if command.entry is None or command.entry.low <= 0 or command.entry.high <= 0:
                errors.append("补仓指令必须包含有效的补仓价格")
        if command.command_type == CommandType.MOVE_STOP and command.stop_loss is not None and command.stop_loss <= 0:
            errors.append("止损价格必须大于零")
        if command.command_type == CommandType.AMEND_TAKE_PROFIT:
            if not command.take_profits or command.take_profits[0] <= 0:
                errors.append("止盈价格必须大于零")
