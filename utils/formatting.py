from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math


def normalize_amount(value) -> float:
    try:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Сумма должна быть корректным числом") from exc
    if not amount.is_finite():
        raise ValueError("Сумма должна быть конечным числом")
    return float(amount)


def money(value) -> str:
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount == amount.to_integral():
        return f"${amount:,.0f}"
    return f"${amount:,.2f}"
