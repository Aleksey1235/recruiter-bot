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
