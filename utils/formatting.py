from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math


def _to_decimal(value) -> Decimal:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Сумма должна быть конечным числом")
    raw = str(0 if value is None or value == "" else value).strip().replace(" ", "").replace(",", ".")
    try:
        amount = Decimal(raw).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Сумма должна быть корректным числом") from exc
    if not amount.is_finite():
        raise ValueError("Сумма должна быть конечным числом")
    return amount


def normalize_amount(value) -> float:
    return float(_to_decimal(value))


def money(value) -> str:
    amount = _to_decimal(value)
    if amount == amount.to_integral():
        return f"${amount:,.0f}"
    return f"${amount:,.2f}"
