from dataclasses import dataclass

import config
from database.db import db
from services.finance_service import reconcile_all


@dataclass(frozen=True)
class HealthCheck:
    name: str
    ok: bool
    details: str = ""


async def run_health_checks(bot, guild=None) -> list[HealthCheck]:
    checks: list[HealthCheck] = []

    try:
        row = await db.fetchone("SELECT 1 AS ok")
        checks.append(HealthCheck("База данных", bool(row and row["ok"] == 1)))

        quick = await db.fetchone("PRAGMA quick_check")
        quick_ok = bool(quick and quick[0] == "ok")
        checks.append(HealthCheck("SQLite", quick_ok, "quick_check" if quick_ok else str(quick[0] if quick else "нет ответа")))

        foreign_key_errors = await db.fetchall("PRAGMA foreign_key_check")
        checks.append(HealthCheck("Внешние ключи", not foreign_key_errors, f"ошибок: {len(foreign_key_errors)}" if foreign_key_errors else ""))

        version = await db.fetchone("PRAGMA user_version")
        version_value = int(version[0] if version else 0)
        checks.append(HealthCheck("Схема БД", version_value >= 3, f"версия: {version_value}"))

        bad_shifts = await db.fetchone(
            """
            SELECT COUNT(*) AS count
            FROM shifts s
            WHERE s.slots < 0 OR s.slots > ?
               OR s.scheduled_start IS NULL OR s.scheduled_end IS NULL
               OR s.scheduled_end <= s.scheduled_start
               OR s.status IS NULL OR s.status NOT IN ('open','booked','active','completed','cancelled','missed')
               OR (s.status='active' AND NOT EXISTS (
                    SELECT 1 FROM shift_members sm WHERE sm.shift_id=s.id AND sm.status='active'
               ))
               OR (s.status IN ('completed','cancelled','missed') AND EXISTS (
                    SELECT 1 FROM shift_members sm WHERE sm.shift_id=s.id AND sm.status IN ('booked','active')
               ))
            """,
            (config.MAX_SHIFT_SLOTS,),
        )
        bad_members = await db.fetchone(
            """
            SELECT COUNT(*) AS count
            FROM shift_members sm
            LEFT JOIN shifts s ON s.id=sm.shift_id
            WHERE s.id IS NULL
               OR sm.status IS NULL OR sm.status NOT IN ('booked','active','completed','cancelled','removed','missed')
               OR (sm.status='active' AND sm.actual_start IS NULL)
               OR (sm.status='completed' AND (sm.actual_start IS NULL OR sm.actual_end IS NULL OR sm.report_id IS NULL))
               OR (sm.actual_start IS NOT NULL AND sm.actual_end IS NOT NULL AND sm.actual_end < sm.actual_start)
            """
        )
        bad_reports = await db.fetchone(
            """
            SELECT COUNT(*) AS count
            FROM shift_reports r
            LEFT JOIN shift_members sm ON sm.id=r.member_id
            WHERE sm.id IS NULL OR sm.shift_id<>r.shift_id OR sm.user_id<>r.user_id
               OR r.status IS NULL OR r.status NOT IN ('pending','approved','rejected')
               OR r.total_accepted IS NULL OR r.came_to_base IS NULL OR r.found_by_recruiter IS NULL
               OR r.total_accepted<0 OR r.came_to_base<0 OR r.found_by_recruiter<0
               OR r.came_to_base+r.found_by_recruiter>r.total_accepted
            """
        )
        data_errors = int(bad_shifts["count"] or 0) + int(bad_members["count"] or 0) + int(bad_reports["count"] or 0)
        checks.append(HealthCheck("Данные смен/отчётов", data_errors == 0, f"аномалий: {data_errors}" if data_errors else ""))

        duplicate_statics = await db.fetchone(
            """
            SELECT COUNT(*) AS count FROM (
                SELECT static_id FROM users
                WHERE static_id IS NOT NULL AND TRIM(static_id)<>''
                GROUP BY static_id HAVING COUNT(*)>1
            )
            """
        )
        dup_count = int(duplicate_statics["count"] or 0)
        checks.append(HealthCheck("Уникальность статиков", dup_count == 0, f"дубликатов: {dup_count}" if dup_count else ""))

        bad_invites = await db.fetchone(
            """
            SELECT COUNT(*) AS count FROM invites
            WHERE static_id IS NULL OR TRIM(static_id)='' OR invited_by IS NULL
               OR status IS NULL OR status NOT IN ('pending','accepted','rejected')
               OR (user_id IS NOT NULL AND user_id=invited_by)
               OR ticket IS NULL OR ticket NOT IN ('yes','no')
               OR last_name_changed IS NULL OR last_name_changed NOT IN ('yes','no')
               OR organization IS NULL OR organization NOT IN ('yes','no')
               OR fraction IS NULL OR fraction NOT IN ('yes','no')
               OR info IS NULL OR info NOT IN ('yes','no')
            """
        )
        duplicate_active_invite_users = await db.fetchone(
            """
            SELECT COUNT(*) AS count FROM (
                SELECT user_id FROM invites
                WHERE user_id IS NOT NULL AND status IN ('pending','accepted')
                GROUP BY user_id HAVING COUNT(*)>1
            )
            """
        )
        bad_goals = await db.fetchone(
            """
            SELECT COUNT(*) AS count FROM goals
            WHERE type IS NULL OR type NOT IN ('люди','смены','часы')
               OR period IS NULL OR period NOT IN ('день','неделя','месяц')
               OR status IS NULL OR status NOT IN ('active','deleted')
               OR target_value IS NULL OR current_value IS NULL
               OR (status='active' AND target_value<=0) OR current_value<0
            """
        )
        duplicate_active_goals = await db.fetchone(
            """
            SELECT COUNT(*) AS count FROM (
                SELECT user_id, type FROM goals
                WHERE status='active'
                GROUP BY user_id, type HAVING COUNT(*)>1
            )
            """
        )
        domain_errors = (
            int(bad_invites["count"] or 0)
            + int(duplicate_active_invite_users["count"] or 0)
            + int(bad_goals["count"] or 0)
            + int(duplicate_active_goals["count"] or 0)
        )
        checks.append(HealthCheck("Инвайты и цели", domain_errors == 0, f"аномалий: {domain_errors}" if domain_errors else ""))

        bad_finances = await db.fetchone(
            """
            SELECT COUNT(*) AS count FROM finances
            WHERE amount IS NULL OR amount <= 0 OR type IS NULL OR type NOT IN ('salary','pay')
               OR status IS NULL OR status NOT IN ('accrued','paid')
               OR (type='salary' AND status<>'accrued') OR (type='pay' AND status<>'paid')
            """
        )
        negative_balances = await db.fetchone(
            """
            SELECT COUNT(*) AS count FROM (
                SELECT user_id,
                       COALESCE(SUM(CASE WHEN type='salary' THEN amount ELSE 0 END),0)
                     - COALESCE(SUM(CASE WHEN type='pay' THEN amount ELSE 0 END),0) AS balance
                FROM finances GROUP BY user_id
            ) balances WHERE balance < -0.005
            """
        )
        mismatches = await reconcile_all(fix=False)
        fin_errors = int(bad_finances["count"] or 0) + int(negative_balances["count"] or 0) + len(mismatches)
        checks.append(HealthCheck("Финансы", fin_errors == 0, f"аномалий: {fin_errors}" if fin_errors else ""))
    except Exception as exc:
        checks.append(HealthCheck("Диагностика БД", False, type(exc).__name__))

    guild = guild or bot.get_guild(config.GUILD_ID)
    checks.append(HealthCheck("Discord-сервер", guild is not None))
    if guild is not None:
        channel_specs = (
            ("смен", config.SHIFTS_CHANNEL_ID, ("view_channel", "send_messages", "embed_links", "read_message_history")),
            ("отчётов", config.REPORTS_CHANNEL_ID, ("view_channel", "send_messages", "embed_links", "read_message_history")),
            ("статистики", config.STATS_CHANNEL_ID, ("view_channel", "send_messages", "embed_links")),
            ("контроля", config.CONTROL_CHANNEL_ID, ("view_channel", "send_messages")),
            ("логов", config.LOGS_CHANNEL_ID, ("view_channel", "send_messages", "attach_files")),
            ("панели", config.PANEL_CHANNEL_ID, ("view_channel", "send_messages", "embed_links", "read_message_history")),
        )
        channel_errors = []
        me = getattr(guild, "me", None)
        if me is None:
            channel_errors.append("не удалось определить участника бота для проверки прав")
        for label, channel_id, required in channel_specs:
            channel = guild.get_channel(channel_id)
            if channel is None:
                channel_errors.append(f"{label}: не найден")
                continue
            if me is None:
                continue
            if not hasattr(channel, "permissions_for"):
                channel_errors.append(f"{label}: невозможно проверить права")
                continue
            perms = channel.permissions_for(me)
            missing = [name for name in required if not getattr(perms, name, False)]
            if missing:
                channel_errors.append(f"{label}: нет {', '.join(missing)}")
        checks.append(HealthCheck("Каналы и права", not channel_errors, "; ".join(channel_errors)[:900]))

        role_errors = []
        for label, role_id in (
            ("recruiter", config.RECRUITER_ROLE_ID),
            ("senior", config.SENIOR_ROLE_ID),
            ("admin", config.ADMIN_ROLE_ID),
        ):
            if guild.get_role(role_id) is None:
                role_errors.append(label)
        checks.append(HealthCheck("Роли", not role_errors, f"не найдены: {', '.join(role_errors)}" if role_errors else ""))

    tasks_cog = bot.get_cog("Tasks")
    task_errors = []
    if tasks_cog is None:
        task_errors.append("Tasks Cog")
    else:
        for attr, title in (
            ("check_shifts", "смены"),
            ("check_reports", "отчёты"),
            ("check_suspicious", "аномалии"),
            ("weekly_report", "недельный отчёт"),
        ):
            loop = getattr(tasks_cog, attr, None)
            if not loop or not loop.is_running():
                task_errors.append(title)
    checks.append(HealthCheck("Фоновые задачи", not task_errors, f"не работают: {', '.join(task_errors)}" if task_errors else ""))
    return checks
