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
