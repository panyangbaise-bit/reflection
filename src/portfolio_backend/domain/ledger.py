"""Decimal rewrite of legacy domain/valuation.py's moving-average replay.

Opening snapshots are not purchases. No synthetic cash flows or price fallbacks.
"""

from dataclasses import dataclass
from decimal import Decimal, localcontext

from portfolio_backend.errors import require

ZERO = Decimal("0")
MONEY = Decimal("0.00000001")


@dataclass(frozen=True)
class Balance:
    quantity: Decimal = ZERO
    cost_pool: Decimal = ZERO
    realized: Decimal = ZERO


def bounded(balance):
    require(
        balance.quantity < Decimal("1e18")
        and balance.cost_pool < Decimal("1e20")
        and abs(balance.realized) < Decimal("1e20"),
        "amount_out_of_range",
        "金额或累计数量超出支持范围",
        422,
    )
    return balance


def apply(
    balance: Balance, kind: str, quantity: Decimal, price: Decimal, fee: Decimal = ZERO
) -> Balance:
    require(
        all(n.is_finite() for n in (quantity, price, fee)),
        "invalid_amount",
        "金额必须是有限数值",
        422,
    )
    require(
        quantity > 0 and price > 0 and fee >= 0,
        "invalid_amount",
        "数量、价格须为正数，费用不能为负",
        422,
    )
    with localcontext() as ctx:
        ctx.prec = 50
        if kind in {"opening_snapshot", "buy"}:
            require(
                kind != "opening_snapshot" or balance.quantity == 0,
                "duplicate_position",
                "已有持仓不能再次录入初始快照",
            )
            return bounded(
                Balance(
                    balance.quantity + quantity,
                    (balance.cost_pool + quantity * price + fee).quantize(MONEY),
                    balance.realized,
                )
            )
        require(kind == "sell", "unsupported_event", "不支持的账本事件", 422)
        require(quantity <= balance.quantity, "excessive_sale", "卖出数量超过当前持仓")
        removed = (
            balance.cost_pool
            if quantity == balance.quantity
            else (balance.cost_pool * quantity / balance.quantity).quantize(MONEY)
        )
        return bounded(
            Balance(
                balance.quantity - quantity,
                balance.cost_pool - removed,
                (balance.realized + quantity * price - removed - fee).quantize(MONEY),
            )
        )
