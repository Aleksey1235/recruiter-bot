"""Профили, недельные отчёты, цели рекламы и обновление существующих БД."""
import asyncio
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest

import config
from cogs import tasks as task_module
from cogs.goals import Goals
from cogs.panel import _profile_embed, _goals_embed, GoalTypeSelect, GoalValueModal
from cogs.profile import Profile
from database.db import db, Database, SCHEMA_SQL, SCHEMA_VERSION, ensure_user
from services import advertising_service as ads, goal_service, health_service
from services.errors import UserFacingError
from utils.time_utils import local_to_utc_naive, to_db
from test_advertising import run, active, submit, age_publications


async def seed_ad(user_id, status, published, *, prepared=None):
    proof = status in ("pending", "approved", "rejected", "uploading")
    await db.execute(
        "INSERT INTO ad_attempts(user_id,discord_name,family_name,requires_proof,status,prepared_at,published_at) "
        "VALUES(?,?,'DeSanta',?,?,?,?)",
        (user_id, f"User {user_id}", int(proof), status, to_db(prepared or published),
         None if status in ("prepared", "uploading") else to_db(published)),
    )


def member(user_id):
    return SimpleNamespace(id=user_id, name=f"user{user_id}", display_name=f"Game {user_id}",
                           mention=f"<@{user_id}>", display_avatar=SimpleNamespace(url="https://example.com/avatar.png"),
                           roles=[SimpleNamespace(id=config.RECRUITER_ROLE_ID)])


def test_ad_goal_uses_existing_progress_and_tracks_review_and_cancellation(tmp_path, monkeypatch):
    async def scenario():
        await active()
        ordinary = await ads.prepare(1, "one", "Game One")
        await ads.confirm(ordinary["id"], 1)
        people = await goal_service.set_goal(1, "one", "люди", 10, "неделя", 900)
        goal = await goal_service.set_goal(1, "one", "рекламы", 10, "неделя", 900)
        assert (await db.fetchone("SELECT current_value FROM goals WHERE id=?", (goal,)))[0] == 1
        await age_publications()
        monkeypatch.setattr(config, "ADS_CHECK_PERCENT", 100)
        photo = await ads.prepare(1, "one", "Game One")
        await submit(photo)
        assert await goal_service.calculate_progress(1, "рекламы", "неделя") == 1
        await ads.review(photo["id"], 900, True)
        assert await goal_service.calculate_progress(1, "рекламы", "неделя") == 2
        await ads.cancel(ordinary["id"], 1, "Ошибочная отметка")
        embed = await _goals_embed(1)
        field = next(f for f in embed.fields if "Рекламы" in f.name)
        assert "1 / 10" in field.value
        assert (await db.fetchone("SELECT current_value FROM goals WHERE id=?", (goal,)))[0] == 1
        await age_publications()
        rejected = await ads.prepare(1, "one", "Game One")
        await submit(rejected)
        await ads.review(rejected["id"], 900, False, "Чужое объявление")
        assert await goal_service.calculate_progress(1, "рекламы", "неделя") == 1
        replacement = await goal_service.set_goal(1, "one", "рекламы", 20, "месяц", 900)
        old = await db.fetchone("SELECT status FROM goals WHERE id=?", (goal,))
        assert old[0] == "deleted"
        active_goals = await db.fetchall("SELECT id FROM goals WHERE user_id=1 AND status='active'")
        assert {row[0] for row in active_goals} == {people, replacement}
        backup_embed = await Goals(None)._build_goals_embed(1, "Цели")
        assert any("Рекламы" in f.name and "1 / 20" in f.value for f in backup_embed.fields)
        await goal_service.delete_active_goals(1, 900)
        assert not await db.fetchall("SELECT id FROM goals WHERE status='active'")
    run(tmp_path, monkeypatch, scenario)


@pytest.mark.parametrize("period,boundary", [
    ("день", datetime(2026, 10, 3)), ("неделя", datetime(2026, 9, 28)), ("месяц", datetime(2026, 10, 1)),
])
def test_ad_goal_periods_use_local_boundaries_and_publication_time(tmp_path, monkeypatch, period, boundary):
    async def scenario():
        import utils.time_utils as times
        monkeypatch.setattr(config, "TIMEZONE", "Europe/Moscow")
        monkeypatch.setattr(times, "local_now", lambda: datetime(2026, 10, 3, 12))
        stamp = local_to_utc_naive(boundary)
        await seed_ad(1, "counted", stamp - timedelta(seconds=1))
        await seed_ad(1, "counted", stamp, prepared=stamp - timedelta(hours=1))
        await seed_ad(1, "approved", stamp + timedelta(seconds=1))
        await seed_ad(1, "pending", stamp + timedelta(seconds=2))
        await seed_ad(1, "rejected", stamp + timedelta(seconds=3))
        await seed_ad(1, "cancelled", stamp + timedelta(seconds=4))
        await seed_ad(2, "counted", stamp + timedelta(seconds=1))
        assert await goal_service.calculate_progress(1, "рекламы", period) == 2
    run(tmp_path, monkeypatch, scenario)


def test_both_profiles_show_all_time_ads_and_preserve_other_fields(tmp_path, monkeypatch):
    async def scenario():
        await ensure_user(1, "one", "STATIC1")
        stamp = datetime(2025, 1, 1)
        for status in ("counted", "approved", "pending", "rejected", "cancelled"):
            await seed_ad(1, status, stamp)
        await seed_ad(2, "counted", stamp)
        target = member(1)
        panel_embed = await _profile_embed(target)
        inter = SimpleNamespace(author=target, response=SimpleNamespace(defer=AsyncMock()),
                                edit_original_response=AsyncMock())
        await Profile.profile.callback(Profile(None), inter)
        slash_embed = inter.edit_original_response.call_args.kwargs["embed"]
        for embed in (panel_embed, slash_embed):
            fields = {f.name: f.value for f in embed.fields}
            assert fields["🆔 Статик"] == "STATIC1"
            assert "💰 Начислено" in fields
            assert fields["📢 РЕКЛАМА"] == (
                "Зачтено: 2 • без проверки: 1 • по фото: 1\nОжидает проверки: 1 • отклонено: 1"
            )
    run(tmp_path, monkeypatch, scenario)


@pytest.mark.parametrize("report_now", [datetime(2026, 10, 4, 23, 59), datetime(2026, 10, 5, 12)])
def test_weekly_report_contains_ads_for_its_exact_week_and_retries_once(tmp_path, monkeypatch, report_now):
    async def scenario():
        monkeypatch.setattr(config, "TIMEZONE", "Europe/Moscow")
        monkeypatch.setattr(task_module, "local_now", lambda: report_now)
        start = datetime(2026, 9, 28)
        end = start + timedelta(days=7)
        first, last = local_to_utc_naive(start), local_to_utc_naive(end)
        await seed_ad(1, "counted", first - timedelta(seconds=1))
        await seed_ad(1, "counted", first)
        await seed_ad(2, "approved", last - timedelta(seconds=1))
        await seed_ad(1, "pending", first + timedelta(seconds=1))
        await seed_ad(1, "rejected", first + timedelta(seconds=2))
        await seed_ad(1, "counted", last)
        channel = SimpleNamespace(send=AsyncMock(side_effect=[RuntimeError("Discord unavailable"), None]))
        task = object.__new__(task_module.Tasks)
        task.bot = SimpleNamespace(get_channel=Mock(return_value=channel))
        await task_module.Tasks.weekly_report.coro(task)
        await task_module.Tasks.weekly_report.coro(task)
        await task_module.Tasks.weekly_report.coro(task)
        assert channel.send.await_count == 2
        embed = channel.send.call_args.kwargs["embed"]
        assert "28.09.2026 — 04.10.2026" in embed.description
        fields = {f.name: f.value for f in embed.fields}
        assert fields["📢 РЕКЛАМА"] == (
            "Зачтено: 2 • без проверки: 1 • по фото: 1\nОжидает проверки: 1 • отклонено: 1"
        )
        assert "👥 РЕКРУТИНГ" in fields and "💰 ФИНАНСЫ" in fields
    run(tmp_path, monkeypatch, scenario)


def test_button_goal_modal_accepts_ads_and_rechecks_permissions(tmp_path, monkeypatch):
    async def scenario():
        assert [o.value for o in GoalTypeSelect().options] == ["люди", "смены", "часы", "рекламы"]
        target = member(1)
        actor = member(900)
        actor.roles = [SimpleNamespace(id=config.SENIOR_ROLE_ID)]
        inter = SimpleNamespace(author=actor, text_values={"value": "15"},
                                response=SimpleNamespace(send_message=AsyncMock()))
        import cogs.panel as panel
        monkeypatch.setattr(panel, "notify", AsyncMock())
        modal = GoalValueModal(target, None, "рекламы", "неделя")
        await modal.callback(inter)
        goal = await db.fetchone("SELECT * FROM goals WHERE user_id=1 AND status='active'")
        assert (goal["type"], goal["period"], goal["target_value"], goal["current_value"]) == ("рекламы", "неделя", 15, 0)
        target.roles = []
        await modal.callback(inter)
        assert "больше не относится" in inter.response.send_message.call_args.args[0]
        target.roles = [SimpleNamespace(id=config.RECRUITER_ROLE_ID)]
        actor.roles = []
        await modal.callback(inter)
        assert "Доступ только" in inter.response.send_message.call_args.args[0]
        assert (await db.fetchone("SELECT COUNT(*) FROM goals"))[0] == 1
    run(tmp_path, monkeypatch, scenario)


def test_ads_goal_is_valid_for_both_health_checks(tmp_path, monkeypatch):
    async def scenario():
        await goal_service.set_goal(1, "one", "рекламы", 10, "неделя", 900)
        assert await health_service.get_domain_anomalies() == []
        bot = SimpleNamespace(get_guild=Mock(return_value=None), get_cog=Mock(return_value=None))
        checks = await health_service.run_health_checks(bot)
        selected = [c for c in checks if c.name in ("Инвайты и цели", "Схема БД")]
        assert len(selected) == 2 and all(c.ok for c in selected)
    run(tmp_path, monkeypatch, scenario)


@pytest.mark.parametrize("version", [4, 5])
def test_goal_migration_preserves_rows_indexes_triggers_and_deleted_id_sequence(tmp_path, monkeypatch, version):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL.replace("'часы', 'рекламы'", "'часы'"))
    conn.executescript("""
        INSERT INTO goals(id,user_id,type,target_value,current_value,period,status,created_by,created_at)
        VALUES(7,1,'люди',10,4,'неделя','active',900,'2026-08-01 12:00:00');
        INSERT INTO goals(id,user_id,type,target_value,status) VALUES(8,1,'смены',5,'deleted');
        INSERT INTO goals(id,user_id,type,target_value) VALUES(50,1,'часы',10);
        DELETE FROM goals WHERE id=50;
        CREATE INDEX idx_goal_custom ON goals(user_id,period) WHERE status='active';
        CREATE TRIGGER trg_goal_custom AFTER INSERT ON goals BEGIN
            INSERT INTO logs(user_id,action,object_id) VALUES(NEW.user_id,'CUSTOM',NEW.id);
        END;
        INSERT INTO ad_attempts(user_id,discord_name,family_name,requires_proof,status,published_at)
        VALUES(1,'Game One','DeSanta',0,'counted','2026-09-30 12:00:00');
    """)
    before = conn.execute("SELECT * FROM goals ORDER BY id").fetchall()
    ads_before = conn.execute("SELECT * FROM ad_attempts").fetchall()
    conn.execute(f"PRAGMA user_version={version}")
    conn.commit(); conn.close()
    monkeypatch.setattr(config, "DATABASE_PATH", str(path))
    async def scenario():
        database = Database()
        await database.connect()
        try:
            assert (await database.fetchone("PRAGMA user_version"))[0] == SCHEMA_VERSION
            assert [tuple(r) for r in await database.fetchall("SELECT * FROM goals ORDER BY id")] == before
            assert [tuple(r) for r in await database.fetchall("SELECT * FROM ad_attempts")] == ads_before
            assert (await database.fetchone("SELECT seq FROM sqlite_sequence WHERE name='goals'"))[0] == 50
            objects = await database.fetchall("SELECT name FROM sqlite_master WHERE tbl_name='goals'")
            assert {r[0] for r in objects} >= {"goals", "idx_goal_custom", "trg_goal_custom", "idx_goals_user_status"}
            result = await database.execute("INSERT INTO goals(user_id,type,target_value) VALUES(1,'рекламы',10)")
            assert result.lastrowid == 51
            assert (await database.fetchone("SELECT object_id FROM logs WHERE action='CUSTOM'"))[0] == 51
            with pytest.raises(sqlite3.IntegrityError):
                await database.execute("INSERT INTO goals(user_id,type) VALUES(1,'unknown')")
            assert (await database.fetchone("PRAGMA quick_check"))[0] == "ok"
            assert await database.fetchall("PRAGMA foreign_key_check") == []
        finally:
            await database.close()
        backups = list(tmp_path.glob("legacy.db.pre_v6_*.db"))
        assert len(backups) == 1
        backup = sqlite3.connect(backups[0])
        assert backup.execute("PRAGMA user_version").fetchone()[0] == version
        assert backup.execute("SELECT * FROM goals ORDER BY id").fetchall() == before
        backup.close()
        await database.connect(); await database.close()
        assert len(list(tmp_path.glob("legacy.db.pre_v6_*.db"))) == 1
    asyncio.run(scenario())


def test_goal_migration_failure_rolls_back_table_rebuild(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL.replace("'часы', 'рекламы'", "'часы'"))
    conn.executescript("INSERT INTO goals(user_id,type,target_value) VALUES(1,'люди',10); "
                       "CREATE INDEX idx_goal_custom ON goals(user_id); PRAGMA user_version=5;")
    before = conn.execute("SELECT * FROM goals").fetchall()
    original_schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='goals'").fetchone()[0]
    conn.close()
    monkeypatch.setattr(config, "DATABASE_PATH", str(path))
    execute = aiosqlite.Connection.execute
    async def fail_recreate(connection, sql, parameters=None):
        if sql.startswith("CREATE INDEX idx_goal_custom"):
            raise sqlite3.OperationalError("injected failure")
        return await execute(connection, sql, parameters)
    monkeypatch.setattr(aiosqlite.Connection, "execute", fail_recreate)
    async def scenario():
        database = Database()
        with pytest.raises(sqlite3.OperationalError, match="injected failure"):
            await database.connect()
        assert database.db is None
    asyncio.run(scenario())
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    assert conn.execute("SELECT * FROM goals").fetchall() == before
    assert conn.execute("SELECT sql FROM sqlite_master WHERE name='goals'").fetchone()[0] == original_schema
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='goals_v6_migration'").fetchone() is None
    conn.close()
