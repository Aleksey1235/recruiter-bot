import asyncio
import os
import sqlite3
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class AsyncCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    async def fetchone(self):
        return self._cursor.fetchone()

    async def fetchall(self):
        return self._cursor.fetchall()


class AsyncConnection:
    def __init__(self, path, timeout=10):
        self._connection = sqlite3.connect(path, timeout=timeout)
        self._connection.row_factory = sqlite3.Row

    @property
    def row_factory(self):
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._connection.row_factory = value

    async def execute(self, query, params=()):
        return AsyncCursor(self._connection.execute(query, params))

    async def executescript(self, script):
        self._connection.executescript(script)

    async def commit(self):
        self._connection.commit()

    async def rollback(self):
        self._connection.rollback()

    async def close(self):
        self._connection.close()

    async def backup(self, target):
        self._connection.backup(target)


async def fake_connect(path, timeout=10):
    return AsyncConnection(path, timeout=timeout)


# The execution environment used for these tests may not have aiosqlite installed.
# The production code still depends on real aiosqlite; this adapter deliberately
# exercises the same SQL and transaction flow against sqlite3.
fake_aiosqlite = types.ModuleType("aiosqlite")
fake_aiosqlite.connect = fake_connect
fake_aiosqlite.Row = sqlite3.Row
fake_aiosqlite.Connection = AsyncConnection
sys.modules.setdefault("aiosqlite", fake_aiosqlite)

import config
from database.db import _reserve_notification, db
from services import database_service, finance_service, invite_service, shift_service, statistics_service
from services.errors import UserFacingError


async def expect_user_error(awaitable, contains=""):
    try:
        await awaitable
    except UserFacingError as exc:
        if contains:
            assert contains.lower() in str(exc).lower()
        return
    raise AssertionError("Ожидался UserFacingError")


def temporary_database_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return path


async def reset_database(path):
    await db.close()
    config.DATABASE_PATH = path
    await db.connect()


def test_full_business_flow_and_safety_guards():
    async def scenario():
        path = temporary_database_path()
        try:
            await reset_database(path)
            now = datetime.now().replace(microsecond=0)

            shift_id = await shift_service.create_shift(
                900,
                now - timedelta(minutes=5),
                now + timedelta(hours=1),
                1,
                "service smoke",
            )
            member_id = await shift_service.take_shift(shift_id, 1, "one", "111")
            await expect_user_error(
                shift_service.take_shift(shift_id, 2, "two", "222"),
                "мест",
            )

            shift = await db.fetchone("SELECT slots, status FROM shifts WHERE id=?", (shift_id,))
            assert shift["slots"] == 0

            found = await shift_service.find_shift_to_start(1)
            assert found["id"] == member_id
            await shift_service.start_shift(member_id, 1)

            finished = await shift_service.finish_shift(member_id, 1, 7, 3, 4, "ok")
            assert finished.report["total_accepted"] == 7
            assert finished.report["came_to_base"] == 3
            assert finished.report["found_by_recruiter"] == 4

            await expect_user_error(
                shift_service.resubmit_report(finished.report["id"], 1, 3, 2, 2, "invalid"),
                "сумма",
            )

            rejected = await shift_service.reject_report(finished.report["id"], 901, "исправить")
            assert rejected["status"] == "rejected"
            corrected = await shift_service.resubmit_report(
                finished.report["id"], 1, 8, 3, 4, "fixed"
            )
            assert corrected.report["status"] == "pending"
            assert corrected.report["total_accepted"] == 8

            approved = await shift_service.approve_report(finished.report["id"], 901)
            assert approved["status"] == "approved"
            await expect_user_error(
                shift_service.approve_report(finished.report["id"], 902),
                "обработан",
            )

            _, balance = await finance_service.accrue(1, "one", 100, "test", 900)
            assert abs(balance[2] - 100) < 1e-9
            _, balance = await finance_service.pay(1, "one", 40, 900)
            assert abs(balance[2] - 60) < 1e-9
            await expect_user_error(finance_service.pay(1, "one", 61, 900), "доступно")

            invite_id = await invite_service.create_invite(
                50,
                1,
                "one",
                "ABC",
                "Test User",
                {
                    "ticket": "yes",
                    "last_name": "yes",
                    "organization": "yes",
                    "fraction": "no",
                    "info": "yes",
                },
            )
            rejected_invite = await invite_service.reject_invite(invite_id, 900, "wrong")
            assert rejected_invite["status"] == "rejected"

            resubmitted_id = await invite_service.create_invite(
                50,
                1,
                "one",
                "ABC",
                "Test User 2",
                {
                    "ticket": "yes",
                    "last_name": "yes",
                    "organization": "yes",
                    "fraction": "yes",
                    "info": "yes",
                },
            )
            assert resubmitted_id == invite_id
            accepted_invite, finance_id = await invite_service.approve_invite(invite_id, 900, 25)
            assert accepted_invite["status"] == "accepted"
            assert finance_id is not None
            await expect_user_error(invite_service.approve_invite(invite_id, 901, 25), "обработан")

            balance = await finance_service.get_balance(1)
            assert abs(balance[0] - 125) < 1e-9
            assert abs(balance[2] - 85) < 1e-9

            stats = await statistics_service.user_statistics(1, "всё время")
            assert stats["total_accepted"] == 8
            assert stats["completed_shifts"] == 1
            assert stats["approved_reports"] == 1
            top = await statistics_service.top_statistics("всё время")
            assert any(row["user_id"] == 1 for row in top)

            member = await db.fetchone(
                "SELECT status, report_id FROM shift_members WHERE id=?", (member_id,)
            )
            assert member["status"] == "completed"
            assert member["report_id"] == finished.report["id"]
        finally:
            await db.close()
            if os.path.exists(path):
                os.remove(path)

    asyncio.run(scenario())


def test_legacy_database_migration_with_existing_rows():
    async def scenario():
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE users (
                discord_id INTEGER PRIMARY KEY,
                username TEXT,
                total_salary REAL DEFAULT 0,
                paid_salary REAL DEFAULT 0
            );
            CREATE TABLE shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER,
                scheduled_start TIMESTAMP,
                scheduled_end TIMESTAMP,
                slots INTEGER DEFAULT 1,
                description TEXT,
                status TEXT DEFAULT 'open',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE shift_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shift_id INTEGER,
                user_id INTEGER,
                static_id TEXT,
                status TEXT DEFAULT 'booked',
                actual_start TIMESTAMP,
                actual_end TIMESTAMP,
                cancel_reason TEXT,
                report_id INTEGER,
                UNIQUE(shift_id, user_id)
            );
            CREATE TABLE notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                type TEXT,
                object_type TEXT,
                object_id INTEGER,
                status TEXT DEFAULT 'sent',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, type, object_type, object_id)
            );
            """
        )
        connection.execute(
            "INSERT INTO users(discord_id, username) VALUES(1, 'legacy')"
        )
        connection.execute(
            "INSERT INTO shift_members(shift_id, user_id, static_id) VALUES(1, 1, '111')"
        )
        connection.execute(
            "INSERT INTO notifications(user_id,type,object_type,object_id,status) "
            "VALUES(NULL,'WEEKLY_REPORT','week',1,'sent')"
        )
        connection.commit()
        connection.close()

        backup_paths = []
        try:
            await reset_database(path)
            backup_paths = list(Path(path).parent.glob(Path(path).name + ".pre_v2_*.db"))
            assert len(backup_paths) == 1
            member_columns = {row["name"] for row in await db.fetchall("PRAGMA table_info(shift_members)")}
            notification_columns = {row["name"] for row in await db.fetchall("PRAGMA table_info(notifications)")}
            shift_columns = {row["name"] for row in await db.fetchall("PRAGMA table_info(shifts)")}

            assert "created_at" in member_columns
            assert {"attempts", "updated_at", "last_error"} <= notification_columns
            assert "message_id" in shift_columns

            marker = await db.fetchone(
                "SELECT user_id FROM notifications WHERE type='WEEKLY_REPORT'"
            )
            assert marker["user_id"] == 0

            indexes = {
                row["name"]
                for row in await db.fetchall(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            assert "idx_notifications_status" in indexes
            assert "idx_shift_members_actual_start" in indexes
            version = await db.fetchone("PRAGMA user_version")
            assert version[0] == 2

            # New rows in columns added to a populated legacy table must still
            # receive timestamps via migration triggers.
            await db.execute(
                "INSERT INTO users(discord_id, username) VALUES(2, 'new-after-migration')"
            )
            user = await db.fetchone("SELECT created_at FROM users WHERE discord_id=2")
            assert user["created_at"] is not None
        finally:
            await db.close()
            if os.path.exists(path):
                os.remove(path)
            for backup_path in backup_paths:
                if backup_path.exists():
                    backup_path.unlink()

    asyncio.run(scenario())


def test_notification_reservation_is_idempotent_and_recovers_stale_pending():
    async def scenario():
        path = temporary_database_path()
        old_timeout = config.NOTIFICATION_PENDING_TIMEOUT_MINUTES
        try:
            await reset_database(path)
            config.NOTIFICATION_PENDING_TIMEOUT_MINUTES = 10

            assert await _reserve_notification(1, "TEST", "object", 10, 3) is True
            assert await _reserve_notification(1, "TEST", "object", 10, 3) is False

            await db.execute(
                "UPDATE notifications SET updated_at=datetime('now','-20 minutes') "
                "WHERE user_id=1 AND type='TEST' AND object_type='object' AND object_id=10"
            )
            assert await _reserve_notification(1, "TEST", "object", 10, 3) is True
            row = await db.fetchone(
                "SELECT attempts, status FROM notifications "
                "WHERE user_id=1 AND type='TEST' AND object_type='object' AND object_id=10"
            )
            assert row["attempts"] == 2
            assert row["status"] == "pending"
        finally:
            config.NOTIFICATION_PENDING_TIMEOUT_MINUTES = old_timeout
            await db.close()
            if os.path.exists(path):
                os.remove(path)

    asyncio.run(scenario())


def test_concurrent_last_slot_and_double_payment_are_serialized():
    async def scenario():
        path = temporary_database_path()
        try:
            await reset_database(path)
            now = datetime.now().replace(microsecond=0)
            shift_id = await shift_service.create_shift(
                900,
                now + timedelta(minutes=30),
                now + timedelta(hours=2),
                1,
                "concurrency",
            )

            results = await asyncio.gather(
                shift_service.take_shift(shift_id, 10, "ten", "10"),
                shift_service.take_shift(shift_id, 11, "eleven", "11"),
                return_exceptions=True,
            )
            successes = [result for result in results if isinstance(result, int)]
            failures = [result for result in results if isinstance(result, UserFacingError)]
            assert len(successes) == 1
            assert len(failures) == 1

            row = await db.fetchone("SELECT slots FROM shifts WHERE id=?", (shift_id,))
            members = await db.fetchall(
                "SELECT * FROM shift_members WHERE shift_id=? AND status='booked'", (shift_id,)
            )
            assert row["slots"] == 0
            assert len(members) == 1

            await finance_service.accrue(20, "twenty", 100, "salary", 900)
            payments = await asyncio.gather(
                finance_service.pay(20, "twenty", 80, 901),
                finance_service.pay(20, "twenty", 80, 902),
                return_exceptions=True,
            )
            payment_successes = [result for result in payments if not isinstance(result, Exception)]
            payment_failures = [result for result in payments if isinstance(result, UserFacingError)]
            assert len(payment_successes) == 1
            assert len(payment_failures) == 1
            _, paid, available = await finance_service.get_balance(20)
            assert abs(paid - 80) < 1e-9
            assert abs(available - 20) < 1e-9
        finally:
            await db.close()
            if os.path.exists(path):
                os.remove(path)

    asyncio.run(scenario())


def test_start_shift_refuses_second_active_shift_even_with_legacy_inconsistent_data():
    async def scenario():
        path = temporary_database_path()
        try:
            await reset_database(path)
            now = datetime.now().replace(microsecond=0)
            first = await shift_service.create_shift(900, now - timedelta(minutes=5), now + timedelta(hours=1), 1, "first")
            first_member = await shift_service.take_shift(first, 30, "thirty", "30")
            await shift_service.start_shift(first_member, 30)

            # Имитируем старую/ручную запись, которая могла появиться до новой защиты.
            second = await shift_service.create_shift(900, now - timedelta(minutes=1), now + timedelta(hours=2), 1, "second")
            async with db.transaction() as tx:
                cursor = await tx.execute(
                    "INSERT INTO shift_members(shift_id,user_id,static_id,status) VALUES(?,?,?,'booked')",
                    (second, 30, "30"),
                )
                second_member = cursor.lastrowid
                await tx.execute("UPDATE shifts SET slots=0,status='booked' WHERE id=?", (second,))

            await expect_user_error(shift_service.start_shift(second_member, 30), "уже есть другая активная")
            state = await db.fetchone("SELECT status FROM shift_members WHERE id=?", (second_member,))
            assert state["status"] == "booked"
        finally:
            await db.close()
            if os.path.exists(path):
                os.remove(path)

    asyncio.run(scenario())



def test_database_admin_service_safe_edits():
    async def scenario():
        path = temporary_database_path()
        try:
            await reset_database(path)
            now = datetime.now().replace(microsecond=0)
            shift_id = await shift_service.create_shift(900, now, now + timedelta(hours=1), 2, "db panel")
            member_id = await shift_service.take_shift(shift_id, 1, "one", "OLD")
            await finance_service.accrue(1, "one", 250, "panel test", 900)
            invite_id = await invite_service.create_invite(
                50, 1, "one", "INV-1", "Invite Person",
                {"ticket": "yes", "last_name": "yes", "organization": "yes", "fraction": "no", "info": "yes"},
            )

            overview = await database_service.get_user_overview(1)
            assert overview is not None
            assert overview["shifts"]["total"] == 1
            assert overview["invites"]["total"] == 1
            assert abs(overview["available"] - 250) < 1e-9

            old, new = await database_service.update_user_static(1, "one", "NEW", 900)
            assert old == "OLD"
            assert new == "NEW"
            user = await db.fetchone("SELECT static_id FROM users WHERE discord_id=1")
            member = await db.fetchone("SELECT static_id FROM shift_members WHERE id=?", (member_id,))
            assert user["static_id"] == "NEW"
            assert member["static_id"] == "NEW"

            await database_service.add_user_note(1, "one", "important", 900, "admin")
            user = await db.fetchone("SELECT notes FROM users WHERE discord_id=1")
            assert "important" in user["notes"]

            found = await database_service.search_users("NEW")
            assert len(found) == 1 and found[0]["discord_id"] == 1
            finances = await database_service.list_user_finances(1)
            shifts = await database_service.list_user_shifts(1)
            invites = await database_service.list_user_invites(1)
            assert finances and shifts and invites and invites[0]["id"] == invite_id
        finally:
            await db.close()
            if os.path.exists(path):
                os.remove(path)

    asyncio.run(scenario())
