import os
from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    pass


def _int_env(name: str, default: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должен быть целым числом, получено: {raw!r}") from exc


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on", "да"}:
        return True
    if normalized in {"0", "false", "no", "off", "нет"}:
        return False
    raise ConfigError(f"{name} должен быть true/false, получено: {raw!r}")


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "recruiter_bot.db").strip() or "recruiter_bot.db"
TIMEZONE = os.getenv("TIMEZONE", "system").strip() or "system"
AUTO_MIGRATION_BACKUP = _bool_env("AUTO_MIGRATION_BACKUP", True)
PING_RECRUITERS_ON_SHIFT_CREATE = _bool_env("PING_RECRUITERS_ON_SHIFT_CREATE", True)

GUILD_ID = _int_env("GUILD_ID")
RECRUITER_ROLE_ID = _int_env("RECRUITER_ROLE_ID")
SENIOR_ROLE_ID = _int_env("SENIOR_ROLE_ID")
ADMIN_ROLE_ID = _int_env("ADMIN_ROLE_ID")

SHIFTS_CHANNEL_ID = _int_env("SHIFTS_CHANNEL_ID")
REPORTS_CHANNEL_ID = _int_env("REPORTS_CHANNEL_ID")
STATS_CHANNEL_ID = _int_env("STATS_CHANNEL_ID")
CONTROL_CHANNEL_ID = _int_env("CONTROL_CHANNEL_ID")
LOGS_CHANNEL_ID = _int_env("LOGS_CHANNEL_ID")

LATE_START_WARNING_MINUTES = _int_env("LATE_START_WARNING_MINUTES", 10)
MISS_AFTER_END_MINUTES = _int_env("MISS_AFTER_END_MINUTES", 0)
REPORT_REMINDER_AFTER_MINUTES = _int_env("REPORT_REMINDER_AFTER_MINUTES", 15)
REVIEW_REMINDER_AFTER_MINUTES = _int_env("REVIEW_REMINDER_AFTER_MINUTES", 30)
SUSPICIOUS_SHORT_MINUTES = _int_env("SUSPICIOUS_SHORT_MINUTES", 10)
EARLY_START_MINUTES = _int_env("EARLY_START_MINUTES", 10)

WEEKLY_REPORT_HOUR = _int_env("WEEKLY_REPORT_HOUR", 23)
WEEKLY_REPORT_MINUTE = _int_env("WEEKLY_REPORT_MINUTE", 0)
MAX_NOTIFICATION_ATTEMPTS = _int_env("MAX_NOTIFICATION_ATTEMPTS", 3)
MAX_FINANCE_AMOUNT = _int_env("MAX_FINANCE_AMOUNT", 1_000_000_000)
NOTIFICATION_PENDING_TIMEOUT_MINUTES = _int_env("NOTIFICATION_PENDING_TIMEOUT_MINUTES", 10)


def validate_config() -> None:
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")

    required_ids = {
        "GUILD_ID": GUILD_ID,
        "RECRUITER_ROLE_ID": RECRUITER_ROLE_ID,
        "SENIOR_ROLE_ID": SENIOR_ROLE_ID,
        "ADMIN_ROLE_ID": ADMIN_ROLE_ID,
        "SHIFTS_CHANNEL_ID": SHIFTS_CHANNEL_ID,
        "REPORTS_CHANNEL_ID": REPORTS_CHANNEL_ID,
        "STATS_CHANNEL_ID": STATS_CHANNEL_ID,
        "CONTROL_CHANNEL_ID": CONTROL_CHANNEL_ID,
        "LOGS_CHANNEL_ID": LOGS_CHANNEL_ID,
    }
    missing.extend(name for name, value in required_ids.items() if value <= 0)

    if missing:
        raise ConfigError("Не заданы обязательные переменные окружения: " + ", ".join(missing))

    numeric_non_negative = {
        "LATE_START_WARNING_MINUTES": LATE_START_WARNING_MINUTES,
        "MISS_AFTER_END_MINUTES": MISS_AFTER_END_MINUTES,
        "REPORT_REMINDER_AFTER_MINUTES": REPORT_REMINDER_AFTER_MINUTES,
        "REVIEW_REMINDER_AFTER_MINUTES": REVIEW_REMINDER_AFTER_MINUTES,
        "SUSPICIOUS_SHORT_MINUTES": SUSPICIOUS_SHORT_MINUTES,
        "EARLY_START_MINUTES": EARLY_START_MINUTES,
        "NOTIFICATION_PENDING_TIMEOUT_MINUTES": NOTIFICATION_PENDING_TIMEOUT_MINUTES,
    }
    invalid = [name for name, value in numeric_non_negative.items() if value < 0]
    if invalid:
        raise ConfigError("Настройки не могут быть отрицательными: " + ", ".join(invalid))

    if not 0 <= WEEKLY_REPORT_HOUR <= 23:
        raise ConfigError("WEEKLY_REPORT_HOUR должен быть от 0 до 23")
    if not 0 <= WEEKLY_REPORT_MINUTE <= 59:
        raise ConfigError("WEEKLY_REPORT_MINUTE должен быть от 0 до 59")
    if MAX_NOTIFICATION_ATTEMPTS < 1:
        raise ConfigError("MAX_NOTIFICATION_ATTEMPTS должен быть >= 1")
    if MAX_FINANCE_AMOUNT <= 0:
        raise ConfigError("MAX_FINANCE_AMOUNT должен быть > 0")

    if TIMEZONE.lower() != "system":
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(TIMEZONE)
        except Exception as exc:
            raise ConfigError(f"Неизвестная временная зона TIMEZONE={TIMEZONE!r}") from exc
