import asyncio
import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from datetime import timedelta

import aiosqlite
import disnake
import pytest

import config
from database.db import db, Database, ensure_user
from services import advertising_service as ads, shift_service, finance_service
from services.errors import UserFacingError
from utils.time_utils import local_now, utc_now, to_db
from cogs.advertising import Advertising, upload_modal, review_embed, proof_marker, add_summary_field


def run(tmp_path, monkeypatch, scenario):
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "ADS_CHECK_PERCENT", 0)
    monkeypatch.setattr(config, "ADS_INTERVAL_MINUTES", 3)
    monkeypatch.setattr(config, "ADS_RETENTION_DAYS", 90)
    monkeypatch.setattr(config, "GUILD_ID", 500)
    monkeypatch.setattr(config, "RECRUITER_ROLE_ID", 501)
    monkeypatch.setattr(config, "SENIOR_ROLE_ID", 502)
    monkeypatch.setattr(config, "ADMIN_ROLE_ID", 503)
    monkeypatch.setattr(config, "ADS_REVIEW_CHANNEL_ID", 999)

    async def wrapped():
        await db.connect()
        assert isinstance(db.db, aiosqlite.Connection)
        try:
            await scenario()
        finally:
            await db.close()
    asyncio.run(wrapped())


async def active(user=1):
    now = local_now()
    shift = await shift_service.create_shift(900, now + timedelta(minutes=2), now + timedelta(hours=1), 5)
    member = await shift_service.take_shift(shift, user, f"user{user}")
    await shift_service.start_shift(member, user)
    return shift, member


async def age_publications():
    await db.execute("UPDATE ad_attempts SET published_at=datetime('now','-4 minutes') WHERE published_at IS NOT NULL")


async def submit(attempt, digest=None):
    claimed = await ads.claim_upload(attempt["id"], attempt["user_id"], digest or f"hash{attempt['id']}", 999, "proof.png")
    assert await ads.complete_upload(attempt["id"], claimed["upload_token"], 1000 + attempt["id"], 2000 + attempt["id"])
    return await ads.get_attempt(attempt["id"])


def test_join_without_static_and_reuse_saved_static(tmp_path, monkeypatch):
    async def scenario():
        shift, member = await active()
        user = await db.fetchone("SELECT * FROM users WHERE discord_id=1")
        row = await db.fetchone("SELECT * FROM shift_members WHERE id=?", (member,))
        assert user["static_id"] is None and row["static_id"] is None
        await ensure_user(2, "two", "STATIC2")
        second = await shift_service.take_shift(shift, 2, "two")
        row = await db.fetchone("SELECT static_id FROM shift_members WHERE id=?", (second,))
        assert row["static_id"] == "STATIC2"
    run(tmp_path, monkeypatch, scenario)


def test_prepare_requires_active_shift_and_is_idempotent(tmp_path, monkeypatch):
    async def scenario():
        with pytest.raises(UserFacingError):
            await ads.prepare(1, "one", "Game Name")
        await active()
        draws = Mock(return_value=99)
        monkeypatch.setattr(ads.secrets, "randbelow", draws)
        attempts = await asyncio.gather(*[ads.prepare(1, "one", "Game Name") for _ in range(10)])
        assert len({r["id"] for r in attempts}) == 1
        draws.assert_called_once_with(100)
        assert attempts[0]["discord_name"] == "Game Name"
        assert attempts[0]["family_name"] == "DeSanta"
        assert (await ads.summary(1))["total"] == 0
    run(tmp_path, monkeypatch, scenario)


@pytest.mark.parametrize("draw,expected", [(0, 1), (19, 1), (20, 0), (99, 0)])
def test_random_20_percent_boundary(tmp_path, monkeypatch, draw, expected):
    async def scenario():
        await active()
        monkeypatch.setattr(config, "ADS_CHECK_PERCENT", 20)
        monkeypatch.setattr(ads.secrets, "randbelow", lambda n: draw)
        attempt = await ads.prepare(1, "one", "Game Name")
        assert attempt["requires_proof"] == expected
    run(tmp_path, monkeypatch, scenario)


def test_double_confirmation_count_and_three_minute_interval(tmp_path, monkeypatch):
    async def scenario():
        await active()
        attempt = await ads.prepare(1, "one", "Game Name")
        results = await asyncio.gather(ads.confirm(attempt["id"], 1), ads.confirm(attempt["id"], 1))
        assert all(r["status"] == "counted" for r in results)
        assert (await ads.summary(1))["total"] == 1
        with pytest.raises(UserFacingError, match="Между публикациями"):
            await ads.prepare(1, "one", "Game Name")
        await ads.cancel(attempt["id"], 1, "Ошибочное нажатие")
        assert (await ads.summary(1))["total"] == 0
        with pytest.raises(UserFacingError, match="Между публикациями"):
            await ads.prepare(1, "one", "Game Name")
        await age_publications()
        assert (await ads.prepare(1, "one", "Game Name"))["id"] != attempt["id"]
    run(tmp_path, monkeypatch, scenario)


def test_cancel_preserves_required_proof_across_restart(tmp_path, monkeypatch):
    async def scenario():
        await active()
        monkeypatch.setattr(config, "ADS_CHECK_PERCENT", 100)
        attempt = await ads.prepare(1, "one", "Game Name")
        with pytest.raises(UserFacingError, match="требуется фото"):
            await ads.confirm(attempt["id"], 1)
        await ads.cancel(attempt["id"], 1, "Заявку не приняли в игре")
        await db.close()
        await db.connect()
        monkeypatch.setattr(config, "ADS_CHECK_PERCENT", 0)
        next_attempt = await ads.prepare(1, "one", "Game Name")
        assert next_attempt["requires_proof"] == 1
        await ads.cancel(next_attempt["id"], 1, "Отмена снова")
        assert (await ads.prepare(1, "one", "Game Name"))["requires_proof"] == 1
    run(tmp_path, monkeypatch, scenario)


def test_ownership_shift_change_and_revoked_role(tmp_path, monkeypatch):
    async def scenario():
        _, member = await active()
        attempt = await ads.prepare(1, "one", "Game Name")
        with pytest.raises(UserFacingError):
            await ads.confirm(attempt["id"], 2)
        with pytest.raises(UserFacingError):
            await ads.cancel(attempt["id"], 2, "Чужая попытка")
        await shift_service.finish_shift(member, 1, 0, 0, 0, "")
        with pytest.raises(UserFacingError):
            await ads.confirm(attempt["id"], 1)
        cog, inter, _ = fake_ui(role=0)
        await cog.on_button_click(inter)
        assert inter.response.send_message.await_count == 1
        assert (await ads.summary(1))["total"] == 0
    run(tmp_path, monkeypatch, scenario)


def test_proof_review_no_self_approval_no_double_review_no_finance(tmp_path, monkeypatch):
    async def scenario():
        await active()
        monkeypatch.setattr(config, "ADS_CHECK_PERCENT", 100)
        attempt = await ads.prepare(1, "one", "Game Name")
        await submit(attempt)
        assert (await ads.summary(1))["pending"] == 1
        assert (await ads.summary(1))["total"] == 0
        with pytest.raises(UserFacingError, match="собственную"):
            await ads.review(attempt["id"], 1, True)
        result = await asyncio.gather(ads.review(attempt["id"], 2, True), ads.review(attempt["id"], 3, False, "Ошибка"), return_exceptions=True)
        assert sum(isinstance(x, UserFacingError) for x in result) == 1
        assert (await ads.summary(1))["total"] == 1
        assert await finance_service.get_balance(1) == (0, 0, 0)
        assert not await ads.anomalies()
    run(tmp_path, monkeypatch, scenario)


def test_rejection_requires_reason_and_does_not_change_frozen_next_choice(tmp_path, monkeypatch):
    async def scenario():
        await active()
        monkeypatch.setattr(config, "ADS_CHECK_PERCENT", 100)
        attempt = await ads.prepare(1, "one", "Game Name")
        await submit(attempt)
        await age_publications()
        monkeypatch.setattr(config, "ADS_CHECK_PERCENT", 0)
        next_attempt = await ads.prepare(1, "one", "Game Name")
        with pytest.raises(UserFacingError):
            await ads.review(attempt["id"], 2, False, "   ")
        await ads.review(attempt["id"], 2, False, "На фото нет времени")
        assert (await ads.get_attempt(next_attempt["id"]))["requires_proof"] == 0
        assert (await ads.summary(1))["rejected"] == 1
        assert (await ads.summary(1))["total"] == 0
    run(tmp_path, monkeypatch, scenario)


def test_exact_duplicate_proof_rejected_even_after_retention(tmp_path, monkeypatch):
    async def scenario():
        await active()
        monkeypatch.setattr(config, "ADS_CHECK_PERCENT", 100)
        first = await ads.prepare(1, "one", "Game Name")
        await submit(first, "samehash")
        await ads.review(first["id"], 2, True)
        await ads.mark_proof_deleted(first["id"])
        await age_publications()
        second = await ads.prepare(1, "one", "Game Name")
        with pytest.raises(UserFacingError, match="уже использовался"):
            await ads.claim_upload(second["id"], 1, "samehash", 999, "proof.png")
        assert (await ads.get_attempt(second["id"]))["status"] == "prepared"
    run(tmp_path, monkeypatch, scenario)


def test_only_one_upload_and_token_guards_recovery(tmp_path, monkeypatch):
    async def scenario():
        await active()
        monkeypatch.setattr(config, "ADS_CHECK_PERCENT", 100)
        attempt = await ads.prepare(1, "one", "Game Name")
        results = await asyncio.gather(
            ads.claim_upload(attempt["id"], 1, "h1", 999, "proof.png"),
            ads.claim_upload(attempt["id"], 1, "h2", 999, "proof.png"), return_exceptions=True
        )
        claimed = next(x for x in results if not isinstance(x, Exception))
        assert sum(isinstance(x, UserFacingError) for x in results) == 1
        assert not await ads.complete_upload(attempt["id"], "wrong-token", 10, 11)
        await ads.release_upload(attempt["id"], "wrong-token")
        assert (await ads.get_attempt(attempt["id"]))["status"] == "uploading"
        await ads.release_upload(attempt["id"], claimed["upload_token"])
        row = await ads.get_attempt(attempt["id"])
        assert row["status"] == "prepared" and row["requires_proof"] == 1 and row["proof_sha256"] is None
    run(tmp_path, monkeypatch, scenario)


def test_retention_does_not_delete_pending_or_statistics(tmp_path, monkeypatch):
    async def scenario():
        await active()
        monkeypatch.setattr(config, "ADS_CHECK_PERCENT", 100)
        first = await ads.prepare(1, "one", "Game Name")
        await submit(first)
        await ads.review(first["id"], 2, True)
        await db.execute("UPDATE ad_attempts SET reviewed_at=datetime('now','-91 days') WHERE id=?", (first["id"],))
        await age_publications()
        second = await ads.prepare(1, "one", "Game Name")
        await submit(second)
        assert [r["id"] for r in await ads.cleanup_candidates()] == [first["id"]]
        await ads.mark_proof_deleted(first["id"])
        assert await ads.cleanup_candidates() == []
        data = await ads.summary(1, "всё время")
        assert data["total"] == 1 and data["pending"] == 1
    run(tmp_path, monkeypatch, scenario)


def test_stats_period_shift_and_ranking(tmp_path, monkeypatch):
    async def scenario():
        shift, _ = await active()
        attempt = await ads.prepare(1, "one", "Game Name")
        await ads.confirm(attempt["id"], 1)
        assert (await ads.summary(1, shift_id=shift))["total"] == 1
        assert (await ads.rankings("всё время"))[0]["user_id"] == 1
        await db.execute("UPDATE ad_attempts SET published_at='2000-01-01 00:00:00'")
        assert (await ads.summary(1, "сегодня"))["total"] == 0
        assert (await ads.summary(1, "всё время"))["total"] == 1
        assert (await ads.summary(None, "всё время"))["total"] == 1
    run(tmp_path, monkeypatch, scenario)


def test_v4_migration_preserves_all_original_records_and_backup(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript((Path(__file__).parent / "fixtures/schema_v4.sql").read_text("utf-8"))
    conn.executescript("""
        INSERT INTO users(discord_id,username,static_id,total_salary,paid_salary,notes) VALUES(1,'Old','123',100,40,'KEEP');
        INSERT INTO shifts(id,scheduled_start,scheduled_end,slots,status) VALUES(7,'2026-01-01 10:00:00','2026-01-01 11:00:00',1,'completed');
        INSERT INTO shift_members(id,shift_id,user_id,static_id,status,actual_start,actual_end,report_id) VALUES(8,7,1,'123','completed','2026-01-01 10:00:00','2026-01-01 11:00:00',9);
        INSERT INTO shift_reports(id,shift_id,member_id,user_id,total_accepted,status,comment) VALUES(9,7,8,1,4,'approved','KEEP');
        INSERT INTO finances(user_id,amount,type,status) VALUES(1,100,'salary','accrued'),(1,40,'pay','paid');
        INSERT INTO invites(user_id,static_id,invited_by,status,notes) VALUES(2,'777',1,'accepted','KEEP');
        INSERT INTO goals(user_id,type,target_value,current_value) VALUES(1,'люди',10,4);
        INSERT INTO blacklist(discord_id,discord_tag,reason,created_by) VALUES(3,'Banned','KEEP',1);
        INSERT INTO logs(user_id,action,details) VALUES(1,'OLD','KEEP');
        INSERT INTO notifications(user_id,type,object_type,object_id,status) VALUES(1,'OLD','shift',7,'sent');
    """)
    tables = ["users", "shifts", "shift_members", "shift_reports", "finances", "invites", "goals", "blacklist", "logs", "notifications"]
    before = {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables}
    conn.commit(); conn.close()
    monkeypatch.setattr(config, "DATABASE_PATH", str(path))

    async def scenario():
        await db.connect()
        try:
            for table in tables:
                assert [tuple(r) for r in await db.fetchall(f"SELECT * FROM {table} ORDER BY rowid")] == before[table]
            assert (await db.fetchone("PRAGMA user_version"))[0] == 6
            assert (await db.fetchone("PRAGMA quick_check"))[0] == "ok"
            assert await db.fetchall("PRAGMA foreign_key_check") == []
            backups = list(tmp_path.glob("legacy.db.pre_v6_*.db"))
            assert len(backups) == 1
            backup = sqlite3.connect(backups[0])
            assert backup.execute("PRAGMA user_version").fetchone()[0] == 4
            assert backup.execute("SELECT notes FROM users").fetchone()[0] == "KEEP"
            backup.close()
        finally:
            await db.close()
        await db.connect()
        await db.close()
        assert len(list(tmp_path.glob("legacy.db.pre_v6_*.db"))) == 1
    asyncio.run(scenario())


def test_future_database_version_is_not_downgraded(tmp_path, monkeypatch):
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path); conn.execute("PRAGMA user_version=99"); conn.close()
    monkeypatch.setattr(config, "DATABASE_PATH", str(path))
    async def scenario():
        with pytest.raises(RuntimeError, match="более новой"):
            await db.connect()
        assert db.db is None
    asyncio.run(scenario())
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 99
    conn.close()


def fake_ui(role=None, action="prepare", author_id=1):
    role = config.RECRUITER_ROLE_ID if role is None else role
    member = SimpleNamespace(id=author_id, name="user", display_name="Game Name", roles=[SimpleNamespace(id=role)])
    channel = Mock(spec=disnake.TextChannel)
    channel.id = 999
    channel.permissions_for.side_effect = lambda obj: SimpleNamespace(
        view_channel=getattr(obj, "id", 0) == 9000, send_messages=True, embed_links=True,
        attach_files=True, read_message_history=True,
    )
    guild = SimpleNamespace(id=config.GUILD_ID, me=SimpleNamespace(id=9000), default_role=SimpleNamespace(id=5000),
                            get_role=lambda rid: SimpleNamespace(id=rid), get_channel=lambda cid: channel if cid == 999 else None)
    response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), send_modal=AsyncMock(), is_done=lambda: response.defer.await_count > 0)
    inter = SimpleNamespace(guild=guild, author=member, response=response, edit_original_response=AsyncMock(),
                            component=SimpleNamespace(custom_id=f"ads:{action}"), channel_id=999,
                            message=SimpleNamespace(id=1001))
    bot = SimpleNamespace(get_guild=lambda gid: guild, user=SimpleNamespace(id=9000))
    cog = object.__new__(Advertising)
    cog.bot=bot; cog._upload_slots=asyncio.Semaphore(2); cog._card_lock=asyncio.Lock(); cog._report_lock=asyncio.Lock()
    return cog, inter, channel


def test_real_file_upload_modal_payload(tmp_path, monkeypatch):
    async def scenario():
        from main import Bot
        bot = Bot()
        try:
            modal = upload_modal(7, 1)
            component = modal.components[0].to_component_dict()
            assert component["type"] == 18 and component["component"]["type"] == 19
            assert component["component"]["max_values"] == 1
            payload = dict(id="123", application_id="456", type=5, token="local-test", version=1, locale="ru",
                           attachment_size_limit=20*1024*1024,
                           channel=dict(id="111", type=1, recipients=[]),
                           user=dict(id="1", username="user", discriminator="0", avatar=None),
                           data=dict(custom_id=modal.custom_id, components=[dict(type=18, component=dict(type=19, custom_id="proof", values=["789"]))],
                                     resolved=dict(attachments={"789":dict(id="789", filename="proof.png", size=100,
                                                                          url="https://cdn.discordapp.com/a", proxy_url="https://media.discordapp.net/a")})))
            interaction = disnake.ModalInteraction(data=payload, state=bot._connection)
            assert isinstance(interaction.resolved_values["proof"][0], disnake.Attachment)
            assert interaction.custom_id == "ads:upload:7:1"
        finally:
            await bot.close()
    run(tmp_path, monkeypatch, scenario)


def test_upload_copies_into_one_discord_message_no_local_files(tmp_path, monkeypatch):
    async def scenario():
        await active()
        monkeypatch.setattr(config, "ADS_CHECK_PERCENT", 100)
        attempt = await ads.prepare(1, "one", "Game Name")
        cog, inter, channel = fake_ui()
        data = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32
        photo = SimpleNamespace(id=3001, url="https://cdn.discordapp.com/new-url")
        message = SimpleNamespace(id=1001, author=cog.bot.user, attachments=[photo], edit=AsyncMock())
        channel.send=AsyncMock(return_value=message); channel.fetch_message=AsyncMock(return_value=message)
        before_files = set(tmp_path.iterdir())
        await cog._upload(inter, attempt["id"], SimpleNamespace(size=len(data), read=AsyncMock(return_value=data)))
        assert channel.send.await_count == 1
        row = await ads.get_attempt(attempt["id"])
        assert row["status"] == "pending" and row["proof_message_id"] == 1001
        assert row["proof_sha256"] == hashlib.sha256(data).hexdigest()
        assert row["proof_attachment_id"] == 3001
        assert set(tmp_path.iterdir()) == before_files
        assert message.edit.await_count == 1
    run(tmp_path, monkeypatch, scenario)


@pytest.mark.parametrize("mode", ["missing", "found", "unavailable"])
def test_recovery_after_ambiguous_discord_send(tmp_path, monkeypatch, mode):
    async def scenario():
        await active()
        monkeypatch.setattr(config, "ADS_CHECK_PERCENT", 100)
        attempt = await ads.prepare(1, "one", "Game Name")
        row = await ads.claim_upload(attempt["id"], 1, "digest", 999, "proof.png")
        await db.execute("UPDATE ad_attempts SET uploading_at=datetime('now','-6 minutes') WHERE id=?", (row["id"],))
        cog, _, channel = fake_ui()
        cog._sync_card = AsyncMock()
        found = SimpleNamespace(id=1001, author=cog.bot.user, attachments=[SimpleNamespace(id=3001)],
                                embeds=[disnake.Embed().set_footer(text=proof_marker(row))])
        async def history(**kwargs):
            if mode == "unavailable":
                raise RuntimeError("Discord temporarily unavailable")
            if mode == "found":
                yield found
        channel.history = history
        await cog.maintenance.coro(cog)
        updated = await ads.get_attempt(attempt["id"])
        assert updated["status"] == {"missing":"prepared", "found":"pending", "unavailable":"uploading"}[mode]
        assert updated["requires_proof"] == 1
    run(tmp_path, monkeypatch, scenario)


def test_file_size_and_non_image_rejected_before_discord_send(tmp_path, monkeypatch):
    async def scenario():
        cog, inter, channel = fake_ui()
        with pytest.raises(UserFacingError, match="8 МБ"):
            await cog._upload(inter, 1, SimpleNamespace(size=20*1024*1024))
        with pytest.raises(UserFacingError, match="скриншот"):
            await cog._upload(inter, 1, SimpleNamespace(size=5, read=AsyncMock(return_value=b"hello")))
        assert channel.send.call_count == 0
    run(tmp_path, monkeypatch, scenario)


def test_missing_proof_cannot_be_approved_and_role_checked_on_submit(tmp_path, monkeypatch):
    async def scenario():
        await active()
        monkeypatch.setattr(config, "ADS_CHECK_PERCENT", 100)
        attempt = await ads.prepare(1, "one", "Game Name")
        await submit(attempt)
        cog, inter, _ = fake_ui(role=config.SENIOR_ROLE_ID, author_id=2)
        cog._proof_message=AsyncMock(return_value=SimpleNamespace(attachments=[]))
        with pytest.raises(UserFacingError, match="Фото отсутствует"):
            await cog._review(inter, attempt["id"], True)
        assert (await ads.get_attempt(attempt["id"]))["status"] == "pending"
        inter.author.roles=[]
        inter.custom_id=f"ads:reject:{attempt['id']}:2"
        inter.text_values={"reason":"test"}
        await cog.on_modal_submit(inter)
        assert (await ads.get_attempt(attempt["id"]))["status"] == "pending"
    run(tmp_path, monkeypatch, scenario)


def test_panel_creates_profile_only_for_authorized_member(tmp_path, monkeypatch):
    async def scenario():
        from cogs.panel import MainPanelView
        cog, inter, _ = fake_ui()
        panel = MainPanelView(cog.bot)
        assert await panel.interaction_check(inter)
        user = await db.fetchone("SELECT * FROM users WHERE discord_id=1")
        assert user["username"] == "user" and user["static_id"] is None
        inter.author=SimpleNamespace(id=2, name="other", roles=[])
        assert not await panel.interaction_check(inter)
        assert await db.fetchone("SELECT * FROM users WHERE discord_id=2") is None
    run(tmp_path, monkeypatch, scenario)


def test_all_cogs_load_offline_and_persistent_buttons_are_registered(tmp_path, monkeypatch):
    async def scenario():
        from main import Bot
        bot = Bot()
        try:
            for name in ("shifts", "invites", "blacklist", "statistics", "finance", "goals", "profile", "admin", "database_admin", "panel", "advertising", "tasks", "help"):
                bot.load_extension(f"cogs.{name}")
            assert len(bot.cogs) == 13
            assert bot.get_cog("Advertising").maintenance.is_running()
            ids = [item.custom_id for view in bot.persistent_views for item in view.children]
            assert "panel:ads" in ids and "panel:shifts" in ids
            assert not bot.intents.message_content
        finally:
            for cog in list(bot.cogs):
                bot.remove_cog(cog)
            await bot.close()
            await asyncio.sleep(0)
    run(tmp_path, monkeypatch, scenario)


def test_summary_field_updates_without_duplicate_and_uses_current_counts(tmp_path, monkeypatch):
    async def scenario():
        shift, _ = await active()
        embed=disnake.Embed(title="Report")
        await add_summary_field(embed, 1, shift_id=shift)
        attempt=await ads.prepare(1, "one", "Game Name")
        await ads.confirm(attempt["id"], 1)
        await add_summary_field(embed, 1, shift_id=shift)
        assert len(embed.fields) == 1 and "Зачтено: 1" in embed.fields[0].value
    run(tmp_path, monkeypatch, scenario)


def test_button_opens_stateless_upload_form(tmp_path, monkeypatch):
    async def scenario():
        await active()
        monkeypatch.setattr(config, "ADS_CHECK_PERCENT", 100)
        attempt = await ads.prepare(1, "one", "Game Name")
        cog, inter, _ = fake_ui(action=f"upload:{attempt['id']}")
        await cog.on_button_click(inter)
        kwargs = inter.response.send_modal.await_args.kwargs
        assert kwargs["custom_id"] == f"ads:upload:{attempt['id']}:1"
        assert kwargs["components"][0].to_component_dict()["component"]["type"] == 19
        assert "modal" not in kwargs  # Нет локального callback-кеша.
    run(tmp_path, monkeypatch, scenario)


def test_cleanup_deletes_own_reviewed_card_only_and_preserves_db(tmp_path, monkeypatch):
    async def scenario():
        await active()
        monkeypatch.setattr(config, "ADS_CHECK_PERCENT", 100)
        first = await ads.prepare(1, "one", "Game Name")
        first = await submit(first)
        await ads.review(first["id"], 2, True)
        await db.execute("UPDATE ad_attempts SET reviewed_at=datetime('now','-91 days'),card_status=status,report_synced_status=status WHERE id=?", (first["id"],))
        await age_publications()
        second = await ads.prepare(1, "one", "Game Name")
        second = await submit(second)
        await db.execute("UPDATE ad_attempts SET card_status=status WHERE id=?", (second["id"],))
        cog, _, channel = fake_ui()
        old_message = SimpleNamespace(author=cog.bot.user, delete=AsyncMock())
        channel.fetch_message = AsyncMock(return_value=old_message)
        await cog.maintenance.coro(cog)
        old_message.delete.assert_awaited_once()
        channel.fetch_message.assert_awaited_once_with(first["proof_message_id"])
        assert (await ads.get_attempt(first["id"]))["proof_deleted_at"]
        assert (await ads.get_attempt(second["id"]))["proof_deleted_at"] is None
        assert (await ads.summary(1, "всё время"))["total"] == 1
    run(tmp_path, monkeypatch, scenario)


def test_ambiguous_send_keeps_upload_claim_and_does_not_count(tmp_path, monkeypatch):
    async def scenario():
        await active()
        monkeypatch.setattr(config, "ADS_CHECK_PERCENT", 100)
        attempt=await ads.prepare(1, "one", "Game Name")
        cog, inter, channel=fake_ui()
        channel.send=AsyncMock(side_effect=TimeoutError("response lost"))
        data=b'\x89PNG\r\n\x1a\n'+b'\x00'*32
        await cog._upload(inter, attempt["id"], SimpleNamespace(size=len(data), read=AsyncMock(return_value=data)))
        row=await ads.get_attempt(attempt["id"])
        assert row["status"] == "uploading" and row["upload_token"]
        assert (await ads.summary(1))["total"] == 0
        assert (await ads.prepare(1, "one", "Game Name"))["id"] == row["id"]
    run(tmp_path, monkeypatch, scenario)


def test_public_or_reused_work_channel_cannot_receive_proofs(tmp_path, monkeypatch):
    async def scenario():
        cog, inter, channel=fake_ui()
        channel.permissions_for.return_value=SimpleNamespace(view_channel=True)
        channel.permissions_for.side_effect=None
        with pytest.raises(UserFacingError, match="закрыт"):
            cog.channel(inter.guild)
        monkeypatch.setattr(config, "REPORTS_CHANNEL_ID", 999)
        with pytest.raises(UserFacingError, match="отдельный"):
            cog.channel(inter.guild)
    run(tmp_path, monkeypatch, scenario)
