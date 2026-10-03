"""Exercise real disnake response types; never talk to Discord or the live database."""
import asyncio
import ast
import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import disnake
import pytest
from disnake.ext import commands
from disnake.webhook.async_ import async_context

import config
from cogs import advertising, panel, shifts
from cogs.database_admin import DatabaseUserView, StaticModal, NoteModal
from cogs.invites import Invites, ApproveInviteModal, RejectInviteModal
from services.errors import UserFacingError
from test_advertising import active, run


@pytest.fixture(autouse=True)
def distinct_roles(monkeypatch):
    monkeypatch.setattr(config, "GUILD_ID", 500)
    monkeypatch.setattr(config, "RECRUITER_ROLE_ID", 501)
    monkeypatch.setattr(config, "SENIOR_ROLE_ID", 502)
    monkeypatch.setattr(config, "ADMIN_ROLE_ID", 503)


class WireAdapter:
    """Discord's response routing, with the real SDK producing the wire payload."""
    def __init__(self):
        self.interactions = {}
        self.callbacks = []
        self.edits = []

    async def create_interaction_response(self, interaction_id, token, *, type, data=None, **kwargs):
        inter = self.interactions[token]
        self.callbacks.append((type, copy.deepcopy(data)))
        if type == disnake.InteractionResponseType.deferred_message_update.value:
            inter.destination = inter.public_message
        elif type in (4, 5):
            inter.destination = copy.deepcopy(inter.public_message)
            inter.destination.update(id=str(inter.id + 100), content="", embeds=[], components=[],
                                     flags=(data or {}).get("flags", 0))
            if type == 4:
                inter.destination.update(data or {})

    async def edit_original_interaction_response(self, application_id, token, *, payload, **kwargs):
        inter = self.interactions[token]
        assert inter.destination is not None, "No response was acknowledged"
        inter.destination.update(payload)
        self.edits.append((int(inter.destination["id"]), copy.deepcopy(payload)))
        return copy.deepcopy(inter.destination)

    async def get_original_interaction_response(self, application_id, token, **kwargs):
        return copy.deepcopy(self.interactions[token].destination)


class WireInteraction:
    def __init__(self, adapter, *, cid="panel:ads", user_id=1, roles=None,
                 interaction_type=disnake.InteractionType.component):
        self.type = interaction_type
        self.id = 10000 + len(adapter.interactions)
        self.token = f"offline-{self.id}"
        self.application_id = 9000
        self._session = None
        self._client = commands.InteractionBot(intents=disnake.Intents.none())
        self._state = self._client._connection
        self._original_response = None
        self.guild = SimpleNamespace(id=config.GUILD_ID, get_member=lambda _: None)
        self.channel_id = 222
        self.channel = SimpleNamespace(id=self.channel_id, guild=self.guild)
        role_ids = roles if roles is not None else [config.RECRUITER_ROLE_ID]
        self.author = SimpleNamespace(id=user_id, name=f"user{user_id}", display_name=f"Game {user_id}",
                                      mention=f"<@{user_id}>", roles=[SimpleNamespace(id=r) for r in role_ids])
        self.public_message = {
            "id": "1234", "channel_id": "222", "content": "Public card",
            "author": {"id": "9000", "username": "Bot", "discriminator": "0", "avatar": None, "bot": True},
            "timestamp": "2026-10-03T12:00:00+00:00", "edited_timestamp": None,
            "tts": False, "mention_everyone": False, "mentions": [], "mention_roles": [],
            "attachments": [], "embeds": [{"title": "Original shared panel"}], "components": [],
            "pinned": False, "type": 0, "flags": 0,
        }
        self.message = SimpleNamespace(id=1234, embeds=[], components=[])
        self.component = SimpleNamespace(custom_id=cid)
        self.custom_id = cid
        self.values = ["1"]
        self.text_values = {}
        self.resolved_values = {}
        self.data = {}
        self.destination = None
        self.response = disnake.InteractionResponse(self)
        adapter.interactions[self.token] = self

    async def edit_original_response(self, **kwargs):
        return await disnake.Interaction.edit_original_response(self, **kwargs)

    async def original_response(self):
        return await disnake.Interaction.original_response(self)


def assert_private(adapter, inter, original):
    assert inter.public_message == original, "A private response overwrote the public message"
    assert inter.response.type in (disnake.InteractionResponseType.channel_message,
                                   disnake.InteractionResponseType.deferred_channel_message)
    assert inter.destination["flags"] & disnake.MessageFlags.ephemeral.flag
    assert all(message_id != 1234 for message_id, _ in adapter.edits)


async def wire_scenario(callback, *, cid="panel:ads", roles=None, interaction_type=disnake.InteractionType.component):
    adapter = WireAdapter()
    inter = WireInteraction(adapter, cid=cid, roles=roles, interaction_type=interaction_type)
    original = copy.deepcopy(inter.public_message)
    token = async_context.set(adapter)
    try:
        await callback(inter)
        assert_private(adapter, inter, original)
        return inter, adapter
    finally:
        async_context.reset(token)


def test_advertising_entry_never_replaces_shared_panel(tmp_path, monkeypatch):
    async def scenario():
        inter, _ = await wire_scenario(advertising.show_advertising_menu)
        assert inter.destination["embeds"][0]["title"] == "📢 РЕКЛАМА СЕМЬИ"
        assert any(b["custom_id"] == "ads:prepare" for r in inter.destination["components"] for b in r["components"])
    run(tmp_path, monkeypatch, scenario)


@pytest.mark.parametrize("fails", [False, True])
def test_booking_success_and_rejection_are_private(tmp_path, monkeypatch, fails):
    async def scenario():
        await active()
        if fails:
            take = AsyncMock(side_effect=UserFacingError("Вы были сняты с этой смены"))
        else:
            take = AsyncMock(return_value=12)
        monkeypatch.setattr(shifts.shift_service, "take_shift", take)
        update = AsyncMock(return_value=True)
        monkeypatch.setattr(shifts, "update_shift_message", update)
        cog = shifts.Shifts(SimpleNamespace())
        inter, _ = await wire_scenario(cog.on_button_click, cid="shift:take:7")
        assert ("❌" if fails else "✅") in inter.destination["content"]
        assert update.await_count == (0 if fails else 1)
    run(tmp_path, monkeypatch, scenario)


@pytest.mark.parametrize("cid", ["ads:current", "ads:history:0", "ads:stats", "ads:prepare"])
def test_advertising_private_navigation_and_errors(tmp_path, monkeypatch, cid):
    async def scenario():
        cog = object.__new__(advertising.Advertising)
        monkeypatch.setattr(cog, "channel", Mock(side_effect=UserFacingError("Канал недоступен")))
        await wire_scenario(cog.on_button_click, cid=cid)
    run(tmp_path, monkeypatch, scenario)


def test_admin_health_response_is_private(monkeypatch):
    async def scenario():
        monkeypatch.setattr(panel, "run_health_checks", AsyncMock(return_value=[]))
        view = panel.AdminMenuView(SimpleNamespace(), 1)
        inter, _ = await wire_scenario(view.health.callback, roles=[config.ADMIN_ROLE_ID])
        assert inter.destination["embeds"][0]["title"] == "🩺 ЗДОРОВЬЕ БОТА"
    asyncio.run(scenario())


@pytest.mark.parametrize("cid", ["ads:approve:1", "ads:reject:1", "ads:queue:0", "ads:top:1",
                                "ads:department:1", "ads:choose_user", "ads:userstats:1:1"])
def test_recruiter_denied_privileged_advertising_buttons(monkeypatch, cid):
    async def scenario():
        cog = object.__new__(advertising.Advertising)
        lookup = AsyncMock(side_effect=AssertionError("Protected data must not be read"))
        monkeypatch.setattr(advertising.ads, "get_attempt", lookup)
        inter, adapter = await wire_scenario(cog.on_button_click, cid=cid)
        assert "Недостаточно прав" in inter.destination["content"]
        assert adapter.callbacks[0][0] == 4
        lookup.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("cid", ["ads:queue_select", "ads:user_select"])
def test_recruiter_denied_privileged_advertising_selects(monkeypatch, cid):
    async def scenario():
        cog = object.__new__(advertising.Advertising)
        inter, _ = await wire_scenario(cog.on_dropdown, cid=cid)
        assert "Недостаточно прав" in inter.destination["content"]
    asyncio.run(scenario())


def test_recruiter_denied_advertising_rejection_modal():
    async def scenario():
        cog = object.__new__(advertising.Advertising)
        inter, _ = await wire_scenario(cog.on_modal_submit, cid="ads:reject:7:1",
                                      interaction_type=disnake.InteractionType.modal_submit)
        assert "Недостаточно прав" in inter.destination["content"]
    asyncio.run(scenario())


@pytest.mark.parametrize("button_name", ["senior", "admin"])
def test_main_panel_denies_recruiter_privileged_sections(button_name):
    async def scenario():
        view = panel.MainPanelView(SimpleNamespace())
        inter, _ = await wire_scenario(getattr(view, button_name).callback)
        assert "Доступ только" in inter.destination["content"]
    asyncio.run(scenario())


def protected_view(name, bot, target):
    factories = {
        "senior": lambda: panel.SeniorMenuView(bot, 1),
        "admin": lambda: panel.AdminMenuView(bot, 1),
        "senior_goals": lambda: panel.SeniorGoalsMenuView(bot, 1),
        "admin_finance": lambda: panel.AdminFinanceMenuView(bot, 1),
        "admin_database": lambda: panel.AdminDatabaseMenuView(1),
        "blacklist": lambda: panel.BlacklistMenuView(1, True),
        "blacklist_user": lambda: panel.BlacklistUserSelectView(1),
        "database_user": lambda: DatabaseUserView(1, target),
        "domain_repair": lambda: panel.DomainRepairView(1),
        "reports": lambda: panel.ReportSeniorMenuView(1),
        "invites": lambda: panel.SeniorInviteMenuView(1),
        "pending_reports": lambda: panel.PendingReportSelectView(1, []),
        "pending_invites": lambda: panel.PendingInviteSelectView(1, []),
        "goal_setup": lambda: panel.GoalSetupView(1, target, bot),
        "user_stats": lambda: panel.StatsForUserView(1, target),
    }
    if name.startswith("action:"):
        return panel.MemberActionView(1, name.split(":", 1)[1], bot)
    return factories[name]()


SENIOR_VIEWS = ["senior", "senior_goals", "reports", "invites", "pending_reports", "pending_invites",
                "goal_setup", "user_stats", "action:stats", "action:finance_view", "action:goals_view",
                "action:note", "action:remove_shift", "action:goal_set", "action:goal_delete"]
ADMIN_VIEWS = ["admin", "admin_finance", "admin_database", "blacklist", "blacklist_user", "database_user",
               "domain_repair", "action:finance_accrue", "action:finance_pay", "action:database_user"]


@pytest.mark.parametrize("name", SENIOR_VIEWS + ADMIN_VIEWS)
def test_recruiter_cannot_dispatch_any_button_in_protected_views(name):
    async def scenario():
        bot = SimpleNamespace()
        target = SimpleNamespace(id=2, name="two", display_name="Game Two", mention="<@2>",
                                 roles=[SimpleNamespace(id=config.RECRUITER_ROLE_ID)])
        view = protected_view(name, bot, target)
        for child in view.children:
            operation = AsyncMock()
            child.callback = operation
            view.on_error = AsyncMock()
            async def dispatch(inter):
                await view._scheduled_task(child, inter)
            inter, _ = await wire_scenario(dispatch)
            assert "только" in inter.destination["content"]
            operation.assert_not_awaited()
            view.on_error.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("name", ADMIN_VIEWS)
def test_senior_cannot_dispatch_admin_only_buttons(name):
    async def scenario():
        target = SimpleNamespace(id=2, name="two", display_name="Game Two", mention="<@2>")
        view = protected_view(name, SimpleNamespace(), target)
        operation = AsyncMock()
        view.children[0].callback = operation
        async def dispatch(inter):
            await view._scheduled_task(view.children[0], inter)
        inter, _ = await wire_scenario(dispatch, roles=[config.SENIOR_ROLE_ID])
        assert "администратор" in inter.destination["content"] or "Admin" in inter.destination["content"]
        operation.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("name", SENIOR_VIEWS + ADMIN_VIEWS)
def test_current_role_and_owner_are_checked_on_old_views(name):
    async def scenario():
        target = SimpleNamespace(id=2, name="two", display_name="Game Two", mention="<@2>")
        view = protected_view(name, SimpleNamespace(), target)
        adapter = WireAdapter()
        inter = WireInteraction(adapter, roles=[config.ADMIN_ROLE_ID])
        token = async_context.set(adapter)
        try:
            assert await view.interaction_check(inter) is True
            inter.author.roles = [SimpleNamespace(id=config.RECRUITER_ROLE_ID)]
            assert await view.interaction_check(inter) is False
            assert inter.destination["flags"] & 64
            other = WireInteraction(adapter, user_id=55, roles=[config.ADMIN_ROLE_ID])
            assert await view.interaction_check(other) is False
            assert "другого пользователя" in other.destination["content"] or "не ваше меню" in other.destination["content"]
        finally:
            async_context.reset(token)
    asyncio.run(scenario())


@pytest.mark.parametrize("cid", ["report:approve:7", "report:reject:7", "approve_report", "reject_report"])
def test_recruiter_cannot_review_shift_reports(cid):
    async def scenario():
        inter, _ = await wire_scenario(shifts.Shifts(SimpleNamespace()).on_button_click, cid=cid)
        assert "старший состав" in inter.destination["content"]
    asyncio.run(scenario())


@pytest.mark.parametrize("cid", ["invite:accept:7", "invite:reject:7", "invite_accept", "invite_reject"])
def test_recruiter_cannot_review_invites(cid):
    async def scenario():
        inter, _ = await wire_scenario(Invites(SimpleNamespace()).on_button_click, cid=cid)
        assert "Недостаточно прав" in inter.destination["content"]
    asyncio.run(scenario())


@pytest.mark.parametrize("modal_name", [
    "create_shift", "remove_recruiter", "cancel_shift", "approve_report", "reject_report", "reject_report_card",
    "invite_approve", "invite_reject", "invite_lookup", "goal", "note", "finance_accrue", "finance_pay",
    "blacklist_add", "blacklist_manual", "blacklist_details", "blacklist_search", "blacklist_remove",
    "database_search", "finance_operation", "invite_card_approve", "invite_card_reject",
    "database_static", "database_note",
])
def test_recruiter_denied_privileged_modals_even_after_role_change(modal_name):
    async def scenario():
        target = SimpleNamespace(id=2, name="two", display_name="Game Two", mention="<@2>",
                                 roles=[SimpleNamespace(id=config.RECRUITER_ROLE_ID)])
        factories = {
            "create_shift": panel.CreateShiftModal,
            "remove_recruiter": lambda: panel.RemoveRecruiterModal(target),
            "cancel_shift": panel.CancelShiftModal,
            "approve_report": panel.ApproveReportByIdModal,
            "reject_report": panel.RejectReportByIdModal,
            "reject_report_card": lambda: shifts.RejectReportModal(7),
            "invite_approve": panel.InviteApproveByIdModal,
            "invite_reject": panel.InviteRejectByIdModal,
            "invite_lookup": lambda: panel.InviteLookupModal("base"),
            "goal": lambda: panel.GoalValueModal(target, None, "рекламы", "неделя"),
            "note": lambda: panel.PanelUserNoteModal(target),
            "finance_accrue": lambda: panel.FinanceAccrueModal(target, None),
            "finance_pay": lambda: panel.FinancePayModal(target, None),
            "blacklist_add": lambda: panel.BlacklistAddModal(target),
            "blacklist_manual": panel.BlacklistManualAddModal,
            "blacklist_details": panel.BlacklistDetailsModal,
            "blacklist_search": panel.BlacklistSearchModal,
            "blacklist_remove": panel.BlacklistRemoveModal,
            "database_search": panel.DatabaseSearchModal,
            "finance_operation": panel.FinanceOperationModal,
            "invite_card_approve": lambda: ApproveInviteModal(7),
            "invite_card_reject": lambda: RejectInviteModal(7),
            "database_static": lambda: StaticModal(target),
            "database_note": lambda: NoteModal(target),
        }
        modal = factories[modal_name]()
        inter, _ = await wire_scenario(modal.callback, interaction_type=disnake.InteractionType.modal_submit)
        assert "только" in inter.destination["content"] or "Недостаточно прав" in inter.destination["content"]
    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["advertising.py", "panel.py", "shifts.py"])
def test_all_private_deferrals_in_button_modules_create_private_messages(name):
    tree = ast.parse((Path(__file__).parents[1] / "cogs" / name).read_text("utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "defer":
            args = {kw.arg: kw.value for kw in node.keywords}
            if isinstance(args.get("ephemeral"), ast.Constant) and args["ephemeral"].value is True:
                assert isinstance(args.get("with_message"), ast.Constant) and args["with_message"].value is True, (name, node.lineno)


async def messages(items):
    for item in items:
        yield item


@pytest.mark.parametrize("pinned", [True, False])
def test_startup_restores_overwritten_panel_in_place_without_duplicates(monkeypatch, pinned):
    async def scenario():
        old = SimpleNamespace(id=1234, author=SimpleNamespace(id=9000), content="Leaked personal reply",
                              embeds=[advertising.menu_embed()], edit=AsyncMock(),
                              components=[SimpleNamespace(children=[SimpleNamespace(custom_id="ads:prepare")])])
        channel = SimpleNamespace(pins=lambda **kw: messages([old] if pinned else []),
                                  history=lambda **kw: messages([old]), send=AsyncMock())
        guild = SimpleNamespace(get_channel=Mock(return_value=channel))
        bot = SimpleNamespace(user=SimpleNamespace(id=9000), get_guild=Mock(return_value=guild))
        cog = panel.Panel(bot)
        monkeypatch.setattr(cog, "_disable_old_panel_in_shifts_channel", AsyncMock())
        await cog.on_ready()
        await cog.on_ready()
        channel.send.assert_not_awaited()
        assert old.edit.await_count == 1
        kwargs = old.edit.call_args.kwargs
        assert kwargs["content"] is None
        assert kwargs["embed"].footer.text == panel.PANEL_FOOTER
        assert {b.custom_id for b in kwargs["view"].children} >= {"panel:ads", "panel:shifts", "panel:admin"}
    asyncio.run(scenario())


def test_panel_restore_never_accepts_another_authors_message_or_a_proof_card():
    wrong_author = SimpleNamespace(author=SimpleNamespace(id=42), embeds=[panel._panel_embed()], components=[])
    proof = SimpleNamespace(author=SimpleNamespace(id=9000), embeds=[disnake.Embed(title="Proof")],
                            components=[SimpleNamespace(children=[SimpleNamespace(custom_id="ads:approve:7")])])
    assert panel._is_panel_message(wrong_author, 9000) is False
    assert panel._is_panel_message(proof, 9000) is False


@pytest.mark.parametrize("content", ["❌ Вы были сняты с этой смены", "✅ Вы записались на смену **#7**.", "<@&501>"])
def test_shift_refresh_removes_only_leaked_booking_replies(tmp_path, monkeypatch, content):
    async def scenario():
        shift_id, _ = await active()
        await shifts.shift_service.set_shift_message_id(shift_id, 1234)
        message = SimpleNamespace(id=1234, content=content, edit=AsyncMock())
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
        guild = SimpleNamespace(get_channel=Mock(return_value=channel))
        before = [tuple(row) for row in await shifts.db.fetchall("SELECT * FROM shift_members")]
        assert await shifts.update_shift_message(guild, shift_id) is True
        kwargs = message.edit.call_args.kwargs
        if content.startswith("<@"):
            assert "content" not in kwargs
        else:
            assert kwargs["content"] is None
        assert kwargs["embed"].footer.text == f"Смена #{shift_id}"
        assert [tuple(row) for row in await shifts.db.fetchall("SELECT * FROM shift_members")] == before
    run(tmp_path, monkeypatch, scenario)


@pytest.mark.parametrize("role_name", ["senior", "admin"])
def test_authorized_staff_can_open_private_advertising_queue(tmp_path, monkeypatch, role_name):
    async def scenario():
        cog = object.__new__(advertising.Advertising)
        role = config.SENIOR_ROLE_ID if role_name == "senior" else config.ADMIN_ROLE_ID
        inter, _ = await wire_scenario(cog.on_button_click, cid="ads:queue:0", roles=[role])
        assert inter.destination["embeds"][0]["title"] == "📝 ОЖИДАЮТ ПРОВЕРКИ"
    run(tmp_path, monkeypatch, scenario)


@pytest.mark.parametrize("kind", ["entry", "button", "select", "modal"])
def test_no_role_cannot_open_advertising(kind):
    async def scenario():
        cog = object.__new__(advertising.Advertising)
        callbacks = {
            "entry": (advertising.show_advertising_menu, "panel:ads"),
            "button": (cog.on_button_click, "ads:prepare"),
            "select": (cog.on_dropdown, "ads:history_select"),
            "modal": (cog.on_modal_submit, "ads:upload:7:1"),
        }
        callback, cid = callbacks[kind]
        inter, _ = await wire_scenario(callback, cid=cid, roles=[],
                                      interaction_type=(disnake.InteractionType.modal_submit if kind == "modal"
                                                        else disnake.InteractionType.component))
        assert inter.destination["content"].startswith("❌")
    asyncio.run(scenario())


def test_startup_cleans_corrupted_shift_card_once_and_preserves_database(tmp_path, monkeypatch):
    async def scenario():
        shift_id, _ = await active()
        await shifts.shift_service.set_shift_message_id(shift_id, 1234)
        embed = disnake.Embed(title="Shift")
        embed.set_footer(text=f"Смена #{shift_id}")
        message = SimpleNamespace(id=1234, author=SimpleNamespace(id=9000), embeds=[embed],
                                  content="❌ Вы были сняты с этой смены", edit=AsyncMock())
        unrelated = SimpleNamespace(id=5678, author=SimpleNamespace(id=55), embeds=[embed],
                                    content="❌ Other user message", edit=AsyncMock())
        channel = SimpleNamespace(history=lambda **kw: messages([message, unrelated]),
                                  fetch_message=AsyncMock(return_value=message))
        guild = SimpleNamespace(get_channel=Mock(return_value=channel))
        bot = SimpleNamespace(get_guild=Mock(return_value=guild), user=SimpleNamespace(id=9000))
        before = [tuple(row) for row in await shifts.db.fetchall("SELECT * FROM shifts")]
        cog = shifts.Shifts(bot)
        await cog.on_ready()
        await cog.on_ready()
        assert message.edit.await_count == 1
        assert message.edit.call_args.kwargs["content"] is None
        unrelated.edit.assert_not_awaited()
        assert [tuple(row) for row in await shifts.db.fetchall("SELECT * FROM shifts")] == before
    run(tmp_path, monkeypatch, scenario)
