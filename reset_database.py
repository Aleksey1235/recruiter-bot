"""Безопасно создаёт полностью новую базу данных.

Перед запуском ОБЯЗАТЕЛЬНО остановите бота. Старая база сохраняется
в database_backups/ через SQLite Backup API, затем создаётся чистая схема.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import os
from pathlib import Path
import sqlite3

from dotenv import load_dotenv

load_dotenv()

DB_PATH = Path(os.getenv("DATABASE_PATH", "recruiter_bot.db")).expanduser()


def backup_existing_database() -> Path | None:
    if not DB_PATH.exists():
        return None

    backup_dir = Path("database_backups")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"before_reset_{datetime.now():%Y%m%d_%H%M%S}.db"

    source = sqlite3.connect(DB_PATH)
    target = sqlite3.connect(backup_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    return backup_path


def remove_database_files() -> None:
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(DB_PATH) + suffix)
        if path.exists():
            path.unlink()


async def create_clean_database() -> None:
    from database.db import db

    await db.connect()
    await db.close()


def main() -> None:
    print(f"База: {DB_PATH.resolve()}")
    print("ВАЖНО: бот должен быть полностью остановлен.")
    confirm = input("Введите RESET, чтобы создать новую чистую базу: ").strip()
    if confirm != "RESET":
        print("Отменено. Ничего не изменено.")
        return

    backup = backup_existing_database()
    if backup:
        print(f"Резервная копия: {backup.resolve()}")

    remove_database_files()
    asyncio.run(create_clean_database())
    print("✅ Новая чистая база создана.")


if __name__ == "__main__":
    main()
