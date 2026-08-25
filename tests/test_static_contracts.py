from pathlib import Path

ROOT = Path(__file__).parents[1]
SOURCE = "\n".join(
    path.read_text("utf-8")
    for path in ROOT.rglob("*.py")
    if "tests" not in path.parts
)


def test_no_legacy_tables():
    assert "shift_assignments" not in SOURCE
    assert "FROM reports" not in SOURCE
    assert "JOIN reports" not in SOURCE


def test_no_removed_points_field():
    assert "total_points" not in SOURCE


def test_tasks_are_started():
    tasks_source = (ROOT / "cogs" / "tasks.py").read_text("utf-8")
    for name in ("check_shifts", "check_reports", "check_suspicious", "weekly_report"):
        assert f"self.{name}.start()" in tasks_source



def test_database_admin_cog_is_loaded():
    main_source = (ROOT / "main.py").read_text("utf-8")
    assert '"cogs.database_admin"' in main_source
    cog_source = (ROOT / "cogs" / "database_admin.py").read_text("utf-8")
    assert 'name="база"' in cog_source
    assert 'name="пользователь"' in cog_source
    assert 'name="найти"' in cog_source


def test_control_panel_cog_is_loaded():
    main_source = (ROOT / "main.py").read_text("utf-8")
    panel_source = (ROOT / "cogs" / "panel.py").read_text("utf-8")
    config_source = (ROOT / "config.py").read_text("utf-8")
    assert '"cogs.panel"' in main_source
    assert 'custom_id="panel:shifts"' in panel_source
    assert 'custom_id="panel:invites"' in panel_source
    assert 'PANEL_CHANNEL_ID' in config_source


def test_active_shift_keeps_join_controls_and_service_accepts_active():
    shifts_source = (ROOT / "cogs" / "shifts.py").read_text("utf-8")
    service_source = (ROOT / "services" / "shift_service.py").read_text("utf-8")
    assert 'interactive_statuses = ("open", "booked", "active")' in shifts_source
    assert 'if shift["status"] not in ("open", "booked", "active")' in service_source
    assert "status IN ('open', 'booked', 'active')" in service_source


def test_button_panel_covers_operational_command_groups():
    panel = (ROOT / "cogs" / "panel.py").read_text("utf-8")
    # Recruiter operations.
    for label in (
        "▶️ Начать", "✅ Завершить", "🚪 Выйти", "📅 Расписание", "♻️ Исправить отчёт",
        "➕ Новый инвайт", "👥 Мои инвайты", "👤 Мой профиль", "🔎 Профиль рекрутера",
    ):
        assert label in panel
    # Senior operations.
    for label in (
        "➕ Создать смену", "👤 Снять со смены", "⛔ Отменить смену", "📋 Отчёты",
        "👥 Инвайты", "📊 Статистика рекрутера", "💰 Финансы рекрутера", "💵 Общие финансы",
        "🎯 Цели", "📝 Заметка",
    ):
        assert label in panel
    # Admin operations.
    for label in (
        "🕐 Время", "🔔 Уведомления", "📝 Логи", "📦 Бэкап", "🩺 Здоровье",
        "💰 Финансы", "🗄️ База", "➕ Начислить", "💸 Выплатить", "🔎 Сверить",
        "🛠️ Исправить кеш", "👤 Карточка пользователя", "💰 Финоперация",
    ):
        assert label in panel


def test_invite_checklist_shows_saved_selection_and_gates_create_button():
    panel = (ROOT / "cogs" / "panel.py").read_text("utf-8")
    assert "📋 **Выполненные пункты**" in panel
    assert "✅ **Выбор сохранён.** Проверьте список выше" in panel
    assert "⬜" in panel
    assert 'custom_id="panel:invite_create"' in panel
    assert "self.set_create_enabled(False)" in panel
    assert "self.view.set_create_enabled(not invalid)" in panel
    assert "Некорректный выбор" in panel



def test_blacklist_module_permissions_panel_and_invite_guards_exist():
    main = (ROOT / "main.py").read_text("utf-8")
    panel = (ROOT / "cogs" / "panel.py").read_text("utf-8")
    cog = (ROOT / "cogs" / "blacklist.py").read_text("utf-8")
    service = (ROOT / "services" / "blacklist_service.py").read_text("utf-8")
    invite_service = (ROOT / "services" / "invite_service.py").read_text("utf-8")
    health = (ROOT / "services" / "health_service.py").read_text("utf-8")
    assert '"cogs.blacklist"' in main
    assert 'name="чс"' in cog
    assert 'from utils.checks import is_admin' in cog
    assert '@is_senior()' not in cog
    assert cog.count('@is_admin()') >= 7
    assert 'name="снять"' in cog and '@is_admin()' in cog
    senior_menu = panel[panel.index("class SeniorMenuView"):panel.index("class AdminMenuView")]
    assert "Чёрный список" not in senior_menu
    assert "blacklist" not in senior_menu.lower()
    for label in ("🚫 Чёрный список", "➕ Добавить", "🆔 Добавить по ID", "📝 Дополнить", "🔎 Найти", "📋 Активный ЧС", "📜 История", "♻️ Снять с ЧС"):
        assert label in panel
    assert "Снимать с ЧС может только Admin" in panel
    assert "Discord ID:" in panel and "Discord ID:" in cog and "Discord ID:" in service
    assert "BLACKLIST_BLOCK_INVITE" in invite_service
    assert "BLACKLIST_BLOCK_APPROVE" in invite_service
    assert 'HealthCheck("Чёрный список"' in health


def test_bot_is_interaction_only_and_does_not_request_prefix_message_content():
    main = (ROOT / "main.py").read_text("utf-8")
    assert "class Bot(commands.InteractionBot)" in main
    assert "command_prefix=" not in main


def test_background_backup_is_restart_deduplicated_and_health_checked():
    admin = (ROOT / "cogs" / "admin.py").read_text("utf-8")
    health = (ROOT / "services" / "health_service.py").read_text("utf-8")
    assert 'reserve_system_marker("AUTO_BACKUP", "day", day_marker)' in admin
    assert 'finish_system_marker("AUTO_BACKUP", "day", day_marker' in admin
    assert 'get_cog("Admin")' in health
    assert 'getattr(admin_cog, "auto_backup"' in health


def test_pending_blacklisted_invite_ui_disables_accept_and_handles_legacy_target():
    panel = (ROOT / "cogs" / "panel.py").read_text("utf-8")
    invites = (ROOT / "cogs" / "invites.py").read_text("utf-8")
    assert "accept_disabled=bool(blocked)" in panel
    assert "Discord не привязан (legacy-запись)" in panel
    assert "accept_disabled: bool = False" in invites
    assert "🚫 Принятие заблокировано ЧС" in invites


def test_help_is_panel_first_and_rechecks_roles_on_every_click():
    help_source = (ROOT / "cogs" / "help.py").read_text("utf-8")
    assert "Для обычной работы используйте **кнопочную панель**" in help_source
    assert "self.is_admin=config.ADMIN_ROLE_ID in roles" in help_source
    assert "self.is_senior=config.SENIOR_ROLE_ID in roles or self.is_admin" in help_source
    assert "Чёрный список доступен только Admin" in help_source


def test_review_flows_defer_and_surface_delivery_sync_warnings():
    shifts = (ROOT / "cogs" / "shifts.py").read_text("utf-8")
    invites = (ROOT / "cogs" / "invites.py").read_text("utf-8")
    panel = (ROOT / "cogs" / "panel.py").read_text("utf-8")
    for source in (shifts, invites, panel):
        assert "Данные в БД сохранены" in source
    assert "await inter.response.defer(ephemeral=True)" in shifts
    assert "await inter.response.defer(ephemeral=True)" in invites


def test_panel_finds_pinned_control_message_before_history_fallback():
    panel = (ROOT / "cogs" / "panel.py").read_text("utf-8")
    assert "async for message in channel.pins(limit=100)" in panel
    assert "channel.history(limit=200)" in panel


def test_critical_card_publish_failure_notifies_control_channel():
    shifts = (ROOT / "cogs" / "shifts.py").read_text("utf-8")
    invites = (ROOT / "cogs" / "invites.py").read_text("utf-8")
    panel = (ROOT / "cogs" / "panel.py").read_text("utf-8")
    helper = (ROOT / "utils" / "discord_helpers.py").read_text("utf-8")
    assert "send_control_warning" in shifts
    assert "send_control_warning" in invites
    assert "send_control_warning" in panel
    assert "CONTROL_CHANNEL_ID" in helper


def test_card_message_id_persistence_failure_does_not_claim_publish_failed():
    shifts = (ROOT / "cogs" / "shifts.py").read_text("utf-8")
    invites = (ROOT / "cogs" / "invites.py").read_text("utf-8")
    panel = (ROOT / "cogs" / "panel.py").read_text("utf-8")
    assert 'posted = True\n                try:\n                    await shift_service.set_report_message_id' in shifts
    assert 'posted = True\n                try:\n                    await invite_service.set_invite_message_id' in invites
    assert 'posted = True\n                try:\n                    await invite_service.set_invite_message_id' in panel


def test_finance_panel_normalizes_amount_before_display_and_storage():
    panel = (ROOT / "cogs" / "panel.py").read_text("utf-8")
    assert "amount=normalize_amount(inter.text_values['amount'])" in panel
    assert "value=money(amount)" in panel
