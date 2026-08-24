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
from services import database_service, finance_service, goal_service, invite_service, shift_service, statistics_service
from services.errors import UserFacingError
from services.health_service import run_health_checks
from utils.time_utils import format_utc_db, local_now, to_db


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
            now = local_now().replace(microsecond=0)

            shift_id = await shift_service.create_shift(
                900,
                now + timedelta(minutes=5),
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
            backup_paths = list(Path(path).parent.glob(Path(path).name + ".pre_v3_*.db"))
            assert len(backup_paths) == 1
            member_columns = {row["name"] for row in await db.fetchall("PRAGMA table_info(shift_members)")}
            notification_columns = {row["name"] for row in await db.fetchall("PRAGMA table_info(notifications)")}
            shift_columns = {row["name"] for row in await db.fetchall("PRAGMA table_info(shifts)")}
            report_columns = {row["name"] for row in await db.fetchall("PRAGMA table_info(shift_reports)")}
            invite_columns = {row["name"] for row in await db.fetchall("PRAGMA table_info(invites)")}

            assert "created_at" in member_columns
            assert {"attempts", "updated_at", "last_error"} <= notification_columns
            assert "message_id" in shift_columns
            assert "message_id" in report_columns
            assert "message_id" in invite_columns

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
            assert version[0] == 3

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
            now = local_now().replace(microsecond=0)
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
            now = local_now().replace(microsecond=0)
            first = await shift_service.create_shift(900, now + timedelta(minutes=5), now + timedelta(hours=1), 1, "first")
            first_member = await shift_service.take_shift(first, 30, "thirty", "30")
            await shift_service.start_shift(first_member, 30)

            # Имитируем старую/ручную запись, которая могла появиться до новой защиты.
            second = await shift_service.create_shift(900, now + timedelta(minutes=6), now + timedelta(hours=2), 1, "second")
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
            now = local_now().replace(microsecond=0)
            shift_id = await shift_service.create_shift(900, now + timedelta(minutes=5), now + timedelta(hours=1), 2, "db panel")
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


def test_recruiter_can_leave_booked_shift_and_slot_returns():
    async def scenario():
        path = temporary_database_path()
        try:
            await reset_database(path)
            now = local_now().replace(microsecond=0)
            shift_id = await shift_service.create_shift(900, now + timedelta(minutes=20), now + timedelta(hours=1), 1, "leave test")
            await shift_service.take_shift(shift_id, 901, "recruit", "123")
            shift = await db.fetchone("SELECT * FROM shifts WHERE id=?", (shift_id,))
            assert shift["slots"] == 0
            left = await shift_service.leave_shift(901, shift_id, "дела")
            assert left == shift_id
            member = await db.fetchone("SELECT * FROM shift_members WHERE shift_id=? AND user_id=?", (shift_id, 901))
            shift = await db.fetchone("SELECT * FROM shifts WHERE id=?", (shift_id,))
            assert member["status"] == "removed"
            assert "Самостоятельный выход" in (member["cancel_reason"] or "")
            assert shift["slots"] == 1
        finally:
            await db.close()
            if os.path.exists(path):
                os.remove(path)
    asyncio.run(scenario())

def test_active_multislot_shift_stays_joinable_until_slots_are_full():
    async def scenario():
        path = temporary_database_path()
        try:
            await reset_database(path)
            now = local_now().replace(microsecond=0)
            shift_id = await shift_service.create_shift(
                900, now + timedelta(minutes=5), now + timedelta(hours=1), 3, "multi active"
            )
            first_member = await shift_service.take_shift(shift_id, 101, "one", "101")
            await shift_service.start_shift(first_member, 101)
            shift = await db.fetchone("SELECT status, slots FROM shifts WHERE id=?", (shift_id,))
            assert shift["status"] == "active"
            assert shift["slots"] == 2

            # Active is a lifecycle state, not a lock on remaining capacity.
            await shift_service.take_shift(shift_id, 102, "two", "102")
            shift = await db.fetchone("SELECT status, slots FROM shifts WHERE id=?", (shift_id,))
            assert shift["status"] == "active"
            assert shift["slots"] == 1

            await shift_service.take_shift(shift_id, 103, "three", "103")
            shift = await db.fetchone("SELECT status, slots FROM shifts WHERE id=?", (shift_id,))
            assert shift["status"] == "active"
            assert shift["slots"] == 0

            await expect_user_error(
                shift_service.take_shift(shift_id, 104, "four", "104"),
                "мест",
            )
        finally:
            await db.close()
            if os.path.exists(path):
                os.remove(path)
    asyncio.run(scenario())


def test_leave_is_blocked_after_official_start_even_if_member_is_still_booked():
    async def scenario():
        path = temporary_database_path()
        try:
            await reset_database(path)
            now = local_now().replace(microsecond=0)
            shift_id = await shift_service.create_shift(900, now + timedelta(minutes=5), now + timedelta(hours=1), 1, "late leave")
            await shift_service.take_shift(shift_id, 201, "user201", "201")
            await db.execute("UPDATE shifts SET scheduled_start=? WHERE id=?", (to_db(now - timedelta(minutes=1)), shift_id))
            await expect_user_error(shift_service.leave_shift(201, shift_id, "поздно"), "уже началась")
            member = await db.fetchone("SELECT status FROM shift_members WHERE shift_id=? AND user_id=?", (shift_id, 201))
            assert member["status"] == "booked"
        finally:
            await db.close()
            if os.path.exists(path): os.remove(path)
    asyncio.run(scenario())


def test_self_leave_can_rejoin_but_senior_removed_member_cannot():
    async def scenario():
        path = temporary_database_path()
        try:
            await reset_database(path)
            now = local_now().replace(microsecond=0)
            first = await shift_service.create_shift(900, now + timedelta(minutes=20), now + timedelta(hours=1), 1, "self leave")
            await shift_service.take_shift(first, 202, "user202", "202")
            await shift_service.leave_shift(202, first, "дела")
            await shift_service.take_shift(first, 202, "user202", "202")
            row = await db.fetchone("SELECT status FROM shift_members WHERE shift_id=? AND user_id=?", (first, 202))
            assert row["status"] == "booked"

            second = await shift_service.create_shift(900, now + timedelta(minutes=30), now + timedelta(hours=2), 1, "senior remove")
            await shift_service.take_shift(second, 203, "user203", "203")
            await shift_service.remove_member(999, 203, "решение руководства", second)
            await expect_user_error(shift_service.take_shift(second, 203, "user203", "203"), "сняты")
        finally:
            await db.close()
            if os.path.exists(path): os.remove(path)
    asyncio.run(scenario())


def test_overnight_shift_is_visible_on_both_calendar_days():
    async def scenario():
        path = temporary_database_path()
        try:
            await reset_database(path)
            now = local_now().replace(microsecond=0)
            first_day = (now + timedelta(days=1)).replace(hour=23, minute=50, second=0)
            end = (first_day + timedelta(days=1)).replace(hour=0, minute=10)
            shift_id = await shift_service.create_shift(900, first_day, end, 2, "overnight")
            d1 = first_day.replace(hour=0, minute=0, second=0)
            d2 = d1 + timedelta(days=1)
            d3 = d2 + timedelta(days=1)
            day1 = await shift_service.get_schedule(d1, d2)
            day2 = await shift_service.get_schedule(d2, d3)
            assert shift_id in {r["id"] for r in day1}
            assert shift_id in {r["id"] for r in day2}
        finally:
            await db.close()
            if os.path.exists(path): os.remove(path)
    asyncio.run(scenario())


def test_booking_closes_at_official_start_but_early_active_shift_remains_joinable_before_it():
    async def scenario():
        path = temporary_database_path()
        try:
            await reset_database(path)
            now = local_now().replace(microsecond=0)
            shift_id = await shift_service.create_shift(900, now + timedelta(minutes=5), now + timedelta(hours=1), 2, "early active")
            first = await shift_service.take_shift(shift_id, 204, "user204", "204")
            await shift_service.start_shift(first, 204)
            # Первый уже работает, но до официального старта второе место ещё доступно.
            await shift_service.take_shift(shift_id, 205, "user205", "205")
            await shift_service.leave_shift(205, shift_id, "тест")
            await db.execute("UPDATE shifts SET scheduled_start=? WHERE id=?", (to_db(now - timedelta(seconds=1)), shift_id))
            await expect_user_error(shift_service.take_shift(shift_id, 206, "user206", "206"), "запись на смену закрывается")
        finally:
            await db.close()
            if os.path.exists(path): os.remove(path)
    asyncio.run(scenario())


def test_shift_booking_protects_profile_static_and_duplicate_static():
    async def scenario():
        path = temporary_database_path()
        try:
            await reset_database(path)
            now = local_now().replace(microsecond=0)
            s1 = await shift_service.create_shift(900, now + timedelta(minutes=20), now + timedelta(hours=1), 2, "static")
            await shift_service.take_shift(s1, 207, "user207", "AAA")
            s2 = await shift_service.create_shift(900, now + timedelta(hours=2), now + timedelta(hours=3), 2, "static2")
            await expect_user_error(shift_service.take_shift(s2, 207, "user207", "TYPO"), "профиле уже указан")
            await expect_user_error(shift_service.take_shift(s2, 208, "user208", "AAA"), "другому профилю")
        finally:
            await db.close()
            if os.path.exists(path): os.remove(path)
    asyncio.run(scenario())


def test_shift_creation_guards_implausible_duration_slots_and_past_start():
    async def scenario():
        path = temporary_database_path()
        try:
            await reset_database(path)
            now = local_now().replace(microsecond=0)
            await expect_user_error(
                shift_service.create_shift(900, now + timedelta(minutes=5), now + timedelta(hours=config.MAX_SHIFT_DURATION_HOURS + 1), 1, "too long"),
                "длиннее",
            )
            await expect_user_error(
                shift_service.create_shift(900, now + timedelta(minutes=5), now + timedelta(hours=1), config.MAX_SHIFT_SLOTS + 1, "too many"),
                "не может быть больше",
            )
            await expect_user_error(
                shift_service.create_shift(900, now - timedelta(minutes=1), now + timedelta(hours=1), 1, "past"),
                "уже началась",
            )
        finally:
            await db.close()
            if os.path.exists(path): os.remove(path)
    asyncio.run(scenario())


def test_empty_expired_shift_is_finalized_instead_of_staying_open_forever():
    async def scenario():
        path = temporary_database_path()
        try:
            await reset_database(path)
            now = local_now().replace(microsecond=0)
            shift_id = await shift_service.create_shift(900, now + timedelta(minutes=5), now + timedelta(hours=1), 2, "empty")
            await db.execute(
                "UPDATE shifts SET scheduled_start=?, scheduled_end=?, status='open' WHERE id=?",
                (to_db(now - timedelta(hours=2)), to_db(now - timedelta(hours=1)), shift_id),
            )
            changed = await shift_service.finalize_expired_shifts()
            assert shift_id in changed
            row = await db.fetchone("SELECT status FROM shifts WHERE id=?", (shift_id,))
            assert row["status"] == "missed"
        finally:
            await db.close()
            if os.path.exists(path): os.remove(path)
    asyncio.run(scenario())


def test_invite_rejects_self_and_duplicate_discord_target():
    async def scenario():
        path = temporary_database_path()
        checklist = {"ticket":"yes","last_name":"yes","organization":"yes","fraction":"no","info":"yes"}
        try:
            await reset_database(path)
            await expect_user_error(invite_service.create_invite(301, 301, "same", "S1", "Self", checklist), "самого себя")
            await invite_service.create_invite(302, 301, "recruiter", "S2", "Target", checklist)
            await expect_user_error(invite_service.create_invite(302, 303, "other", "S3", "Target again", checklist), "уже есть")
        finally:
            await db.close()
            if os.path.exists(path): os.remove(path)
    asyncio.run(scenario())


def test_weekly_summary_finances_are_period_only_not_all_time():
    async def scenario():
        path = temporary_database_path()
        try:
            await reset_database(path)
            now = local_now().replace(microsecond=0)
            week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            week_end = week_start + timedelta(days=7)
            from utils.time_utils import local_to_utc_naive
            inside = local_to_utc_naive(week_start + timedelta(hours=12))
            outside = local_to_utc_naive(week_start - timedelta(days=1))
            await db.execute(
                "INSERT INTO finances(user_id,amount,type,status,reason,created_at) VALUES(1,100,'salary','accrued','inside',?)",
                (to_db(inside),),
            )
            await db.execute(
                "INSERT INTO finances(user_id,amount,type,status,reason,created_at) VALUES(1,999,'salary','accrued','outside',?)",
                (to_db(outside),),
            )
            await db.execute(
                "INSERT INTO finances(user_id,amount,type,status,reason,created_at) VALUES(1,40,'pay','paid','inside pay',?)",
                (to_db(inside),),
            )
            _, _, _, finance = await statistics_service.weekly_summary(week_start, week_end)
            assert finance == (100.0, 40.0, 60.0)
        finally:
            await db.close()
            if os.path.exists(path): os.remove(path)
    asyncio.run(scenario())


def test_schedule_uses_exclusive_day_end_boundary():
    async def scenario():
        path = temporary_database_path()
        try:
            await reset_database(path)
            now = local_now().replace(microsecond=0)
            d1 = (now + timedelta(days=2)).replace(hour=0, minute=0, second=0)
            d2 = d1 + timedelta(days=1)
            d3 = d2 + timedelta(days=1)
            at_midnight = await shift_service.create_shift(900, d2, d2 + timedelta(hours=1), 1, "midnight")
            day1 = await shift_service.get_schedule(d1, d2)
            day2 = await shift_service.get_schedule(d2, d3)
            assert at_midnight not in {r["id"] for r in day1}
            assert at_midnight in {r["id"] for r in day2}
        finally:
            await db.close()
            if os.path.exists(path): os.remove(path)
    asyncio.run(scenario())


def test_removed_partial_shift_does_not_count_as_completed_work_hours():
    async def scenario():
        path = temporary_database_path()
        try:
            await reset_database(path)
            now = local_now().replace(microsecond=0)
            shift_id = await shift_service.create_shift(900, now + timedelta(minutes=5), now + timedelta(hours=1), 1, "removed")
            member_id = await shift_service.take_shift(shift_id, 401, "user401", "401")
            await shift_service.start_shift(member_id, 401)
            await db.execute("UPDATE shift_members SET actual_start=? WHERE id=?", (to_db(now - timedelta(hours=1)), member_id))
            await shift_service.remove_member(999, 401, "снят", shift_id)
            stats = await statistics_service.user_statistics(401, "всё время")
            assert stats["completed_shifts"] == 0
            assert abs(stats["total_hours"]) < 1e-9
        finally:
            await db.close()
            if os.path.exists(path): os.remove(path)
    asyncio.run(scenario())


def test_finance_accepts_russian_decimal_comma_and_rounds_consistently():
    async def scenario():
        path = temporary_database_path()
        try:
            await reset_database(path)
            _, balance = await finance_service.accrue(402, "user402", "100,125", "comma", 900)
            assert abs(balance[0] - 100.13) < 1e-9
            _, balance = await finance_service.pay(402, "user402", "40,12", 900)
            assert abs(balance[2] - 60.01) < 1e-9
        finally:
            await db.close()
            if os.path.exists(path): os.remove(path)
    asyncio.run(scenario())


def test_static_length_limit_is_consistent_across_shift_invite_and_database_edit():
    async def scenario():
        path = temporary_database_path()
        old_limit = config.MAX_STATIC_ID_LENGTH
        try:
            await reset_database(path)
            config.MAX_STATIC_ID_LENGTH = 8
            now = local_now().replace(microsecond=0)
            shift_id = await shift_service.create_shift(900, now + timedelta(minutes=20), now + timedelta(hours=1), 1, "static limit")
            await expect_user_error(shift_service.take_shift(shift_id, 403, "user403", "123456789"), "8")
            checklist = {"ticket":"yes","last_name":"yes","organization":"yes","fraction":"no","info":"yes"}
            await expect_user_error(invite_service.create_invite(404, 403, "user403", "123456789", "Target", checklist), "8")
            await expect_user_error(database_service.update_user_static(403, "user403", "123456789", 900), "8")
        finally:
            config.MAX_STATIC_ID_LENGTH = old_limit
            await db.close()
            if os.path.exists(path): os.remove(path)
    asyncio.run(scenario())


def test_invite_notes_are_capped_and_message_ids_are_saved():
    async def scenario():
        path = temporary_database_path()
        try:
            await reset_database(path)
            checklist = {"ticket":"yes","last_name":"yes","organization":"yes","fraction":"no","info":"yes"}
            invite_id = await invite_service.create_invite(405, 406, "recruiter", "405", "Target", checklist)
            await invite_service.set_invite_message_id(invite_id, 123456)
            await db.execute("UPDATE invites SET notes=? WHERE id=?", ("x" * 19950, invite_id))
            notes = await invite_service.add_invite_note(invite_id, "y" * 500, 900, "admin")
            row = await db.fetchone("SELECT message_id, notes FROM invites WHERE id=?", (invite_id,))
            assert row["message_id"] == 123456
            assert len(notes) <= 20000
            assert len(row["notes"]) <= 20000
        finally:
            await db.close()
            if os.path.exists(path): os.remove(path)
    asyncio.run(scenario())


def test_report_message_id_column_and_setter_work():
    async def scenario():
        path = temporary_database_path()
        try:
            await reset_database(path)
            now = local_now().replace(microsecond=0)
            shift_id = await shift_service.create_shift(900, now + timedelta(minutes=5), now + timedelta(hours=1), 1, "report message")
            member_id = await shift_service.take_shift(shift_id, 407, "user407", "407")
            await shift_service.start_shift(member_id, 407)
            result = await shift_service.finish_shift(member_id, 407, 1, 1, 0, "")
            await shift_service.set_report_message_id(result.report["id"], 987654)
            row = await db.fetchone("SELECT message_id FROM shift_reports WHERE id=?", (result.report["id"],))
            assert row["message_id"] == 987654
        finally:
            await db.close()
            if os.path.exists(path): os.remove(path)
    asyncio.run(scenario())



def test_goal_service_replaces_same_type_validates_and_deletes_transactionally():
    async def scenario():
        path = temporary_database_path()
        try:
            await reset_database(path)
            first = await goal_service.set_goal(501, "user501", "люди", 5, "неделя", 900)
            second = await goal_service.set_goal(501, "user501", "люди", 8, "месяц", 901)
            assert second != first
            rows = await db.fetchall("SELECT id, type, target_value, period, status FROM goals WHERE user_id=? ORDER BY id", (501,))
            assert len(rows) == 2
            assert rows[0]["status"] == "deleted"
            assert rows[1]["status"] == "active"
            assert rows[1]["target_value"] == 8
            assert rows[1]["period"] == "месяц"

            third = await goal_service.set_goal(501, "user501", "смены", 3, "неделя", 901)
            active = await db.fetchall("SELECT id, type FROM goals WHERE user_id=? AND status='active' ORDER BY id", (501,))
            assert {(row["id"], row["type"]) for row in active} == {(second, "люди"), (third, "смены")}

            await expect_user_error(goal_service.set_goal(501, "user501", "неизвестно", 1, "неделя", 900), "тип")
            await expect_user_error(goal_service.set_goal(501, "user501", "люди", 0, "неделя", 900), "больше 0")
            await expect_user_error(goal_service.set_goal(501, "user501", "люди", 1_000_001, "неделя", 900), "больше")
            await expect_user_error(goal_service.set_goal(501, "user501", "люди", 1, "год", 900), "период")

            deleted = await goal_service.delete_active_goals(501, 902)
            assert len(deleted) == 2
            remaining = await db.fetchone("SELECT COUNT(*) AS count FROM goals WHERE user_id=? AND status='active'", (501,))
            assert remaining["count"] == 0
        finally:
            await db.close()
            if os.path.exists(path):
                os.remove(path)
    asyncio.run(scenario())


def test_v2_to_v3_migration_preserves_existing_report_and_invite_rows():
    async def scenario():
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            PRAGMA user_version=2;
            CREATE TABLE shift_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shift_id INTEGER,
                member_id INTEGER,
                user_id INTEGER,
                total_accepted INTEGER DEFAULT 0,
                came_to_base INTEGER DEFAULT 0,
                found_by_recruiter INTEGER DEFAULT 0,
                comment TEXT,
                status TEXT DEFAULT 'pending',
                reviewed_by INTEGER,
                reviewed_at TIMESTAMP,
                reject_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE invites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                static_id TEXT,
                invited_by INTEGER,
                full_name TEXT,
                ticket TEXT,
                last_name_changed TEXT,
                organization TEXT,
                fraction TEXT,
                info TEXT,
                notes TEXT,
                status TEXT DEFAULT 'pending',
                reviewed_by INTEGER,
                reviewed_at TIMESTAMP,
                reject_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO shift_reports(id, shift_id, member_id, user_id, total_accepted, status)
            VALUES(7, 3, 9, 501, 4, 'pending');
            INSERT INTO invites(id, user_id, static_id, invited_by, full_name, status)
            VALUES(8, 777, '777', 501, 'Old Invite', 'pending');
            """
        )
        connection.commit()
        connection.close()

        backups = []
        try:
            await reset_database(path)
            backups = list(Path(path).parent.glob(Path(path).name + ".pre_v3_*.db"))
            assert len(backups) == 1
            report = await db.fetchone("SELECT id, user_id, total_accepted, message_id FROM shift_reports WHERE id=7")
            invite = await db.fetchone("SELECT id, user_id, static_id, full_name, message_id FROM invites WHERE id=8")
            assert dict(report) == {"id": 7, "user_id": 501, "total_accepted": 4, "message_id": None}
            assert dict(invite) == {"id": 8, "user_id": 777, "static_id": "777", "full_name": "Old Invite", "message_id": None}
            version = await db.fetchone("PRAGMA user_version")
            assert version[0] == 3
        finally:
            await db.close()
            if os.path.exists(path):
                os.remove(path)
            for backup in backups:
                if backup.exists():
                    backup.unlink()
    asyncio.run(scenario())



def test_health_service_detects_clean_database_and_slot_anomaly():
    class DummyLoop:
        def is_running(self):
            return True

    class DummyTasks:
        check_shifts = DummyLoop()
        check_reports = DummyLoop()
        check_suspicious = DummyLoop()
        weekly_report = DummyLoop()

    class DummyBot:
        def get_guild(self, _guild_id):
            return None

        def get_cog(self, name):
            return DummyTasks() if name == "Tasks" else None

    async def scenario():
        path = temporary_database_path()
        try:
            await reset_database(path)
            checks = await run_health_checks(DummyBot())
            by_name = {check.name: check for check in checks}
            assert by_name["База данных"].ok
            assert by_name["SQLite"].ok
            assert by_name["Схема БД"].ok
            assert by_name["Данные смен/отчётов"].ok
            assert by_name["Финансы"].ok
            assert by_name["Фоновые задачи"].ok

            now = local_now().replace(microsecond=0)
            shift_id = await shift_service.create_shift(900, now + timedelta(minutes=30), now + timedelta(hours=1), 1, "health")
            await db.execute("UPDATE shifts SET slots=? WHERE id=?", (config.MAX_SHIFT_SLOTS + 1, shift_id))
            checks = await run_health_checks(DummyBot())
            by_name = {check.name: check for check in checks}
            assert not by_name["Данные смен/отчётов"].ok
        finally:
            await db.close()
            if os.path.exists(path):
                os.remove(path)
    asyncio.run(scenario())


def test_sqlite_utc_timestamps_are_rendered_in_bot_timezone():
    old_timezone = config.TIMEZONE
    try:
        config.TIMEZONE = "Europe/Moscow"
        assert format_utc_db("2026-08-18 20:00:00") == "18.08.2026 23:00"
    finally:
        config.TIMEZONE = old_timezone



def test_resubmission_drops_stale_public_message_binding():
    async def scenario():
        path = temporary_database_path()
        try:
            await reset_database(path)
            now = local_now().replace(microsecond=0)
            shift_id = await shift_service.create_shift(900, now + timedelta(minutes=5), now + timedelta(hours=1), 1, "resubmit binding")
            member_id = await shift_service.take_shift(shift_id, 601, "user601", "601")
            await shift_service.start_shift(member_id, 601)
            result = await shift_service.finish_shift(member_id, 601, 2, 1, 1, "")
            await shift_service.set_report_message_id(result.report["id"], 111111)
            await shift_service.reject_report(result.report["id"], 900, "fix")
            corrected = await shift_service.resubmit_report(result.report["id"], 601, 3, 1, 1, "fixed")
            assert corrected.report["message_id"] is None

            checklist = {"ticket":"yes","last_name":"yes","organization":"yes","fraction":"no","info":"yes"}
            invite_id = await invite_service.create_invite(602, 601, "user601", "602", "Invite User", checklist)
            await invite_service.set_invite_message_id(invite_id, 222222)
            await invite_service.reject_invite(invite_id, 900, "fix")
            same_id = await invite_service.create_invite(602, 601, "user601", "602", "Invite User Fixed", checklist)
            assert same_id == invite_id
            invite = await db.fetchone("SELECT message_id, status FROM invites WHERE id=?", (invite_id,))
            assert invite["status"] == "pending"
            assert invite["message_id"] is None
        finally:
            await db.close()
            if os.path.exists(path):
                os.remove(path)
    asyncio.run(scenario())
