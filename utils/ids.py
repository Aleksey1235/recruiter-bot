"""Validation helpers for Discord/SQLite integer identifiers."""

MAX_SQLITE_INTEGER = (1 << 63) - 1


def parse_positive_sqlite_int(value, *, label: str = "ID") -> int:
    text = str(value).strip()
    if not text or not text.isdigit():
        raise ValueError(f"{label} должен быть положительным числом.")
    number = int(text)
    if number <= 0:
        raise ValueError(f"{label} должен быть положительным числом.")
    if number > MAX_SQLITE_INTEGER:
        raise ValueError(f"{label} слишком большой.")
    return number


def maybe_positive_sqlite_int(value) -> int | None:
    try:
        return parse_positive_sqlite_int(value)
    except (TypeError, ValueError):
        return None
