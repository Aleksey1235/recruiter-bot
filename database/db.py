import asyncio
import logging
import os
import sqlite3
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone

import aiosqlite

import config

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    discord_id INTEGER PRIMARY KEY,
    username TEXT,
    static_id TEXT,
    role TEXT DEFAULT 'recruiter',
    level INTEGER DEFAULT 1,
    total_salary REAL NOT NULL DEFAULT 0,
    paid_salary REAL NOT NULL DEFAULT 0,
    warns INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (level >= 1),
    CHECK (warns >= 0)
);

CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id INTEGER,
    scheduled_start TIMESTAMP NOT NULL,
    scheduled_end TIMESTAMP NOT NULL,
    slots INTEGER NOT NULL DEFAULT 1,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    message_id INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (slots >= 0),
    CHECK (scheduled_end > scheduled_start),
    CHECK (status IN ('open', 'booked', 'active', 'completed', 'cancelled', 'missed'))
);

CREATE TABLE IF NOT EXISTS shift_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    static_id TEXT,
    status TEXT NOT NULL DEFAULT 'booked',
    actual_start TIMESTAMP,
    actual_end TIMESTAMP,
    cancel_reason TEXT,
    report_id INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(shift_id, user_id),
    FOREIGN KEY (shift_id) REFERENCES shifts(id) ON DELETE CASCADE,
    CHECK (status IN ('booked', 'active', 'completed', 'cancelled', 'removed', 'missed'))
);

CREATE TABLE IF NOT EXISTS shift_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id INTEGER NOT NULL,
    member_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    total_accepted INTEGER NOT NULL DEFAULT 0,
    came_to_base INTEGER NOT NULL DEFAULT 0,
    found_by_recruiter INTEGER NOT NULL DEFAULT 0,
    comment TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    reviewed_by INTEGER,
    reviewed_at TIMESTAMP,
    reject_reason TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(member_id),
    FOREIGN KEY (shift_id) REFERENCES shifts(id) ON DELETE CASCADE,
    FOREIGN KEY (member_id) REFERENCES shift_members(id) ON DELETE CASCADE,
    CHECK (total_accepted >= 0),
    CHECK (came_to_base >= 0),
    CHECK (found_by_recruiter >= 0),
    CHECK (came_to_base + found_by_recruiter <= total_accepted),
    CHECK (status IN ('pending', 'approved', 'rejected'))
);

CREATE TABLE IF NOT EXISTS invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    static_id TEXT NOT NULL UNIQUE,
    invited_by INTEGER NOT NULL,
    full_name TEXT,
    ticket TEXT,
    last_name_changed TEXT,
    organization TEXT,
    fraction TEXT,
    info TEXT,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    reviewed_by INTEGER,
    reviewed_at TIMESTAMP,
    reject_reason TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (status IN ('pending', 'accepted', 'rejected'))
);

CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    target_value INTEGER NOT NULL DEFAULT 0,
    current_value INTEGER NOT NULL DEFAULT 0,
    period TEXT NOT NULL DEFAULT 'неделя',
    status TEXT NOT NULL DEFAULT 'active',
    created_by INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (type IN ('люди', 'смены', 'часы')),
    CHECK (period IN ('день', 'неделя', 'месяц')),
    CHECK (status IN ('active', 'deleted')),
    CHECK (target_value >= 0),
    CHECK (current_value >= 0)
);

CREATE TABLE IF NOT EXISTS finances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    type TEXT NOT NULL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'accrued',
    created_by INTEGER,
    related_shift_id INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (amount > 0),
    CHECK (type IN ('salary', 'pay')),
    CHECK (status IN ('accrued', 'paid')),
    CHECK ((type='salary' AND status='accrued') OR (type='pay' AND status='paid'))
);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    action TEXT NOT NULL,
    object_type TEXT,
    object_id INTEGER,
    details TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 0,
    type TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, type, object_type, object_id),
    CHECK (status IN ('pending', 'sent', 'failed')),
    CHECK (attempts >= 0)
);

"""

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_shifts_start_status
    ON shifts(scheduled_start, status);
CREATE INDEX IF NOT EXISTS idx_shift_members_user_status
    ON shift_members(user_id, status);
CREATE INDEX IF NOT EXISTS idx_shift_members_shift_status
    ON shift_members(shift_id, status);
CREATE INDEX IF NOT EXISTS idx_shift_members_actual_start
    ON shift_members(actual_start);
CREATE INDEX IF NOT EXISTS idx_shift_reports_user_status
    ON shift_reports(user_id, status);
CREATE INDEX IF NOT EXISTS idx_shift_reports_created
    ON shift_reports(created_at);
CREATE INDEX IF NOT EXISTS idx_finances_user_type
    ON finances(user_id, type);
CREATE INDEX IF NOT EXISTS idx_goals_user_status
    ON goals(user_id, status);
CREATE INDEX IF NOT EXISTS idx_invites_status_created
    ON invites(status, created_at);
CREATE INDEX IF NOT EXISTS idx_logs_created
    ON logs(created_at);
CREATE INDEX IF NOT EXISTS idx_notifications_status
    ON notifications(status, updated_at);
"""



class Transaction(AbstractAsyncContextManager):
    def __init__(self, database: "Database"):
        self.database = database
        self.active = False

    async def __aenter__(self):
        if self.database.db is None:
            raise RuntimeError("База данных не подключена")
        await self.database._lock.acquire()
        try:
            await self.database.db.execute("BEGIN IMMEDIATE")
            self.active = True
            return self
        except Exception:
            self.database._lock.release()
            raise

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if self.active:
                if exc_type is None:
                    await self.database.db.commit()
                else:
                    await self.database.db.rollback()
        finally:
            self.active = False
            self.database._lock.release()
        return False

    async def execute(self, query: str, params=()):
        return await self.database.db.execute(query, params)

    async def fetchone(self, query: str, params=()):
        cursor = await self.database.db.execute(query, params)
        return await cursor.fetchone()

    async def fetchall(self, query: str, params=()):
        cursor = await self.database.db.execute(query, params)
        return await cursor.fetchall()


class Database:
    def __init__(self):
        self.db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self):
        if self.db is not None:
            return

        if config.DATABASE_PATH != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(config.DATABASE_PATH)), exist_ok=True)

        self.db = await aiosqlite.connect(config.DATABASE_PATH, timeout=10)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        await self.db.execute("PRAGMA journal_mode = WAL")
        await self.db.execute("PRAGMA synchronous = NORMAL")
        await self.db.execute("PRAGMA busy_timeout = 10000")
        await self._backup_before_migration_if_needed()
        await self.db.executescript(SCHEMA_SQL)
        await self._migrate_additive_columns()
        await self._repair_notification_null_users()
        await self.db.executescript(INDEX_SQL)
        await self.db.execute("PRAGMA user_version = 2")
        await self.db.commit()

    async def _backup_before_migration_if_needed(self):
        if not config.AUTO_MIGRATION_BACKUP or config.DATABASE_PATH == ":memory:":
            return

        version_cursor = await self.db.execute("PRAGMA user_version")
        version_row = await version_cursor.fetchone()
        version = int(version_row[0] if version_row else 0)
        if version >= 2:
            return

        tables_cursor = await self.db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        tables_row = await tables_cursor.fetchone()
        if not tables_row or int(tables_row[0] or 0) == 0:
            return

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = f"{config.DATABASE_PATH}.pre_v2_{stamp}.db"
        target = sqlite3.connect(backup_path)
        try:
            await self.db.backup(target)
        finally:
            target.close()
        logger.warning("Создан предмиграционный бэкап: %s", backup_path)

    async def _migrate_additive_columns(self):
        migrations = {
            "users": {
                "username": "TEXT",
                "static_id": "TEXT",
                "role": "TEXT DEFAULT 'recruiter'",
                "level": "INTEGER DEFAULT 1",
                "total_salary": "REAL DEFAULT 0",
                "paid_salary": "REAL DEFAULT 0",
                "warns": "INTEGER DEFAULT 0",
                "notes": "TEXT",
                "created_at": "TIMESTAMP",
            },
            "shifts": {
                "creator_id": "INTEGER",
                "scheduled_start": "TIMESTAMP",
                "scheduled_end": "TIMESTAMP",
                "slots": "INTEGER DEFAULT 1",
                "description": "TEXT",
                "status": "TEXT DEFAULT 'open'",
                "message_id": "INTEGER",
                "created_at": "TIMESTAMP",
            },
            "shift_members": {
                "shift_id": "INTEGER",
                "user_id": "INTEGER",
                "static_id": "TEXT",
                "status": "TEXT DEFAULT 'booked'",
                "actual_start": "TIMESTAMP",
                "actual_end": "TIMESTAMP",
                "cancel_reason": "TEXT",
                "report_id": "INTEGER",
                "created_at": "TIMESTAMP",
            },
            "shift_reports": {
                "shift_id": "INTEGER",
                "member_id": "INTEGER",
                "user_id": "INTEGER",
                "total_accepted": "INTEGER DEFAULT 0",
                "came_to_base": "INTEGER DEFAULT 0",
                "found_by_recruiter": "INTEGER DEFAULT 0",
                "comment": "TEXT",
                "status": "TEXT DEFAULT 'pending'",
                "reviewed_by": "INTEGER",
                "reviewed_at": "TIMESTAMP",
                "reject_reason": "TEXT",
                "created_at": "TIMESTAMP",
            },
            "invites": {
                "user_id": "INTEGER",
                "static_id": "TEXT",
                "invited_by": "INTEGER",
                "full_name": "TEXT",
                "ticket": "TEXT",
                "last_name_changed": "TEXT",
                "organization": "TEXT",
                "fraction": "TEXT",
                "info": "TEXT",
                "notes": "TEXT",
                "status": "TEXT DEFAULT 'pending'",
                "reviewed_by": "INTEGER",
                "reviewed_at": "TIMESTAMP",
                "reject_reason": "TEXT",
                "created_at": "TIMESTAMP",
            },
            "goals": {
                "user_id": "INTEGER",
                "type": "TEXT",
                "target_value": "INTEGER DEFAULT 0",
                "current_value": "INTEGER DEFAULT 0",
                "period": "TEXT DEFAULT 'неделя'",
                "status": "TEXT DEFAULT 'active'",
                "created_by": "INTEGER",
                "created_at": "TIMESTAMP",
            },
            "finances": {
                "user_id": "INTEGER",
                "amount": "REAL DEFAULT 0",
                "type": "TEXT",
                "reason": "TEXT",
                "status": "TEXT DEFAULT 'accrued'",
                "created_by": "INTEGER",
                "related_shift_id": "INTEGER",
                "created_at": "TIMESTAMP",
            },
            "logs": {
                "user_id": "INTEGER",
                "action": "TEXT",
                "object_type": "TEXT",
                "object_id": "INTEGER",
                "details": "TEXT",
                "created_at": "TIMESTAMP",
            },
            "notifications": {
                "user_id": "INTEGER DEFAULT 0",
                "type": "TEXT",
                "object_type": "TEXT",
                "object_id": "INTEGER",
                "status": "TEXT DEFAULT 'pending'",
                "attempts": "INTEGER DEFAULT 0",
                "last_error": "TEXT",
                "created_at": "TIMESTAMP",
                "updated_at": "TIMESTAMP",
            },
        }
        added_timestamp_columns: list[tuple[str, str]] = []
        for table, columns in migrations.items():
            existing = await self.get_columns(table)
            for name, definition in columns.items():
                if name in existing:
                    continue
                try:
                    await self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                    logger.info("Добавлена колонка %s.%s", table, name)
                    if name in {"created_at", "updated_at"}:
                        added_timestamp_columns.append((table, name))
                except Exception:
                    logger.exception("Не удалось добавить колонку %s.%s", table, name)
                    raise

        # SQLite не позволяет надёжно добавить DEFAULT CURRENT_TIMESTAMP через
        # ALTER TABLE в непустую старую таблицу. Поэтому timestamp-колонки
        # добавляются nullable, старые строки заполняются, а для будущих вставок
        # создаётся AFTER INSERT trigger. На чистой БД используются DEFAULT из SCHEMA_SQL.
        for table, column in added_timestamp_columns:
            await self.db.execute(
                f"UPDATE {table} SET {column}=CURRENT_TIMESTAMP WHERE {column} IS NULL"
            )
            trigger = f"trg_{table}_{column}_default"
            await self.db.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {trigger}
                AFTER INSERT ON {table}
                FOR EACH ROW WHEN NEW.{column} IS NULL
                BEGIN
                    UPDATE {table} SET {column}=CURRENT_TIMESTAMP WHERE rowid=NEW.rowid;
                END
                """
            )


    async def _repair_notification_null_users(self):
        columns = await self.get_columns("notifications")
        if not columns:
            return
        # В старой версии системные маркеры записывались с NULL user_id. Для новых
        # маркеров используется 0, чтобы UNIQUE действительно работал в SQLite.
        try:
            await self.db.execute(
                """
                DELETE FROM notifications
                WHERE id NOT IN (
                    SELECT MIN(id)
                    FROM notifications
                    GROUP BY COALESCE(user_id, 0), type, object_type, object_id
                )
                """
            )
            await self.db.execute("UPDATE notifications SET user_id=0 WHERE user_id IS NULL")
        except Exception:
            logger.exception("Не удалось нормализовать notifications.user_id")

    async def get_columns(self, table_name: str) -> set[str]:
        cursor = await self.db.execute(f"PRAGMA table_info({table_name})")
        rows = await cursor.fetchall()
        return {row["name"] for row in rows}

    def transaction(self) -> Transaction:
        return Transaction(self)

    async def execute(self, query: str, params=()):
        if self.db is None:
            raise RuntimeError("База данных не подключена")
        async with self._lock:
            cursor = await self.db.execute(query, params)
            await self.db.commit()
            return cursor

    async def fetchone(self, query: str, params=()):
        if self.db is None:
            raise RuntimeError("База данных не подключена")
        async with self._lock:
            cursor = await self.db.execute(query, params)
            return await cursor.fetchone()

    async def fetchall(self, query: str, params=()):
        if self.db is None:
            raise RuntimeError("База данных не подключена")
        async with self._lock:
            cursor = await self.db.execute(query, params)
            return await cursor.fetchall()

    async def backup_to(self, destination: str):
        if self.db is None:
            raise RuntimeError("База данных не подключена")
        os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
        async with self._lock:
            target = sqlite3.connect(destination)
            try:
                await self.db.backup(target)
            finally:
                target.close()

    async def close(self):
        if self.db is None:
            return
        async with self._lock:
            await self.db.close()
            self.db = None


db = Database()


async def log(user_id, action, object_type=None, object_id=None, details=None, tx: Transaction | None = None):
    query = """
        INSERT INTO logs (user_id, action, object_type, object_id, details)
        VALUES (?, ?, ?, ?, ?)
    """
    params = (user_id, action, object_type, object_id, details)
    if tx is not None:
        await tx.execute(query, params)
    else:
        await db.execute(query, params)


async def ensure_user(user_id: int, username: str | None = None, static_id: str | None = None, tx: Transaction | None = None):
    executor = tx if tx is not None else db
    if tx is not None:
        await executor.execute(
            "INSERT OR IGNORE INTO users (discord_id, username, static_id) VALUES (?, ?, ?)",
            (user_id, username, static_id),
        )
        await executor.execute(
            """
            UPDATE users
            SET username=COALESCE(?, username), static_id=COALESCE(?, static_id)
            WHERE discord_id=?
            """,
            (username, static_id, user_id),
        )
    else:
        async with db.transaction() as inner:
            await ensure_user(user_id, username, static_id, tx=inner)


async def _reserve_notification(user_id: int, notif_type: str, object_type: str, object_id: int, max_attempts: int) -> bool:
    async with db.transaction() as tx:
        existing = await tx.fetchone(
            """
            SELECT * FROM notifications
            WHERE user_id=? AND type=? AND object_type=? AND object_id=?
            """,
            (user_id, notif_type, object_type, object_id),
        )
        if existing:
            status = existing["status"]
            attempts = existing["attempts"] or 0
            if status == "sent" or attempts >= max_attempts:
                return False
            if status == "pending":
                stale = await tx.fetchone(
                    "SELECT datetime(?) <= datetime('now', ?) AS stale",
                    (
                        existing["updated_at"],
                        f"-{config.NOTIFICATION_PENDING_TIMEOUT_MINUTES} minutes",
                    ),
                )
                if not stale or not stale["stale"]:
                    return False
            await tx.execute(
                """
                UPDATE notifications
                SET status='pending', attempts=attempts+1, last_error=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (existing["id"],),
            )
            return True

        cursor = await tx.execute(
            """
            INSERT OR IGNORE INTO notifications
                (user_id, type, object_type, object_id, status, attempts)
            VALUES (?, ?, ?, ?, 'pending', 1)
            """,
            (user_id, notif_type, object_type, object_id),
        )
        return cursor.rowcount == 1


async def _finish_notification(user_id: int, notif_type: str, object_type: str, object_id: int, success: bool, error: str | None = None):
    await db.execute(
        """
        UPDATE notifications
        SET status=?, last_error=?, updated_at=CURRENT_TIMESTAMP
        WHERE user_id=? AND type=? AND object_type=? AND object_id=?
        """,
        ("sent" if success else "failed", error, user_id, notif_type, object_type, object_id),
    )


async def send_dm(bot, user_id: int, embed=None, content=None) -> tuple[bool, str | None]:
    if embed is None and not content:
        return False, "Empty message"
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        if embed is not None:
            await user.send(embed=embed)
        else:
            await user.send(content)
        return True, None
    except Exception as exc:
        logger.warning("Не удалось отправить ЛС пользователю %s: %s", user_id, exc)
        return False, str(exc)


async def notify(bot, user_id: int, notif_type: str, object_type: str, object_id: int, embed=None, content=None) -> bool:
    reserved = await _reserve_notification(
        user_id,
        notif_type,
        object_type,
        object_id,
        config.MAX_NOTIFICATION_ATTEMPTS,
    )
    if not reserved:
        return False

    success, error = await send_dm(bot, user_id, embed=embed, content=content)
    await _finish_notification(user_id, notif_type, object_type, object_id, success, error)
    return success


async def reserve_system_marker(notif_type: str, object_type: str, object_id: int) -> bool:
    return await _reserve_notification(
        0,
        notif_type,
        object_type,
        object_id,
        config.MAX_NOTIFICATION_ATTEMPTS,
    )


async def finish_system_marker(notif_type: str, object_type: str, object_id: int, success: bool, error: str | None = None):
    await _finish_notification(0, notif_type, object_type, object_id, success, error)
