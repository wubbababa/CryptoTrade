"""交易所独立风险限额检查。"""

from __future__ import annotations

from decimal import Decimal

from models import TradeCommand
from settings import Settings

# 名义价值比较的相对容差。仓位数量由「权益 × 保证金比例 × 杠杆 ÷ 价格」得出，Decimal 除法
# 会留下极小舍入误差（例如上限 3998 被算成 3998.000000000000000000000001）。没有容差时，
# 恰好用满交易所额度的合规指令会被误判为超限并整笔拒绝（线上表现为「失败：超过该交易所最大持仓名义价值」）。
_NOTIONAL_TOLERANCE_RATIO = Decimal("1e-9")


class RiskError(ValueError):
    pass


class RiskManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def check_open(self, command: TradeCommand, equity: Decimal,
                   current_notional: Decimal = Decimal("0")) -> None:
        if not self.settings.raw["trading"].get("new_positions_enabled", True):
            raise RiskError("全局已禁止新开仓")
        if command.entry is None or command.quantity is None or command.stop_loss is None:
            raise RiskError("风险计算缺少价格、数量或止损")
        cfg = self.settings.exchange_config(command.exchange)
        notional = command.entry.reference_price * command.quantity
        maximum_notional = equity * Decimal(str(cfg["max_position_notional_ratio"]))
        # 仅在舍入噪声范围内视为「正好用满上限」；真正超限仍照旧熔断。
        tolerance = maximum_notional * _NOTIONAL_TOLERANCE_RATIO
        if current_notional + notional > maximum_notional + tolerance:
            raise RiskError("超过该交易所最大持仓名义价值")
