from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config

DB_FORMAT = "%Y-%m-%d %H:%M:%S"


def local_now() -> datetime:
    """Локальное расписание бота как naive datetime для хранения в SQLite."""
    if config.TIMEZONE.lower() == "system":
        return datetime.now().replace(microsecond=0)
    return datetime.now(ZoneInfo(config.TIMEZONE)).replace(tzinfo=None, microsecond=0)


def utc_now() -> datetime:
    """UTC как naive datetime для сравнения с SQLite CURRENT_TIMESTAMP."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def to_db(value: datetime) -> str:
    return value.replace(microsecond=0).strftime(DB_FORMAT)


def parse_db(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    for fmt in (DB_FORMAT, "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    return None


def period_start(period: str, now: datetime | None = None) -> datetime | None:
    now = now or local_now()
    if period in {"сегодня", "день"}:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "неделя":
        start = now - timedelta(days=now.weekday())
        return start.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "месяц":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return None
