"""自动保本的纯计算逻辑，便于独立测试。"""

from decimal import Decimal

from models import PositionSide


def calculate_breakeven(side: PositionSide, average_price: Decimal, final_take_profit: Decimal,
                         trigger_ratio: Decimal, profit_price_ratio: Decimal) -> tuple[Decimal, Decimal]:
    if not (Decimal("0") < trigger_ratio <= Decimal("1")):
        raise ValueError("保本触发比例必须在 (0, 1] 内")
    if not (Decimal("0") <= profit_price_ratio < Decimal("1")):
        raise ValueError("保本利润比例必须在 [0, 1) 内")
    distance = abs(final_take_profit - average_price)
    if side == PositionSide.LONG:
        return average_price + distance * trigger_ratio, average_price * (Decimal("1") + profit_price_ratio)
    return average_price - distance * trigger_ratio, average_price * (Decimal("1") - profit_price_ratio)


def should_trigger(side: PositionSide, market_price: Decimal, trigger_price: Decimal) -> bool:
    return market_price >= trigger_price if side == PositionSide.LONG else market_price <= trigger_price


def stop_only_improves(side: PositionSide, old_stop: Decimal, new_stop: Decimal) -> bool:
    return new_stop > old_stop if side == PositionSide.LONG else new_stop < old_stop

