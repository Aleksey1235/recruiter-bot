from dataclasses import dataclass

import config
from database.db import db
from services.finance_service import reconcile_all


@dataclass(frozen=True)
class HealthCheck:
    name: str
    ok: bool
    details: str = ""


async def get_domain_anomalies(limit: int = 20) -> list[str]:
    """Return human-readable invite/goal anomalies with record IDs."""
    limit = max(1, min(int(limit), 50))
    issues: list[str] = []

    invite_rows = await db.fetchall(
        """
        SELECT id, user_id, invited_by, static_id, status, ticket, last_name_changed,
               organization, fraction, info
        FROM invites
        ORDER BY id DESC
        """
    )
    for row in invite_rows:
        reasons = []
        if row["static_id"] is None or not str(row["static_id"]).strip():
            reasons.append("пустой статик")
        if row["invited_by"] is None:
            reasons.append("нет рекрутера")
        if row["status"] not in ("pending", "accepted", "rejected"):
            reasons.append(f"неверный статус={row['status']!r}")
        if row["user_id"] is not None and row["user_id"] == row["invited_by"]:
            reasons.append("инвайт создан на самого рекрутера")
        for column in ("ticket", "last_name_changed", "organization", "fraction", "info"):
            if row[column] not in ("yes", "no"):
                reasons.append(f"{column}={row[column]!r}")
        if reasons:
            issues.append(f"INVITE #{row['id']} ({row['static_id'] or '—'}): " + "; ".join(reasons))
            if len(issues) >= limit:
                return issues

    duplicate_users = await db.fetchall(
        """
        SELECT user_id, GROUP_CONCAT(id) AS ids, COUNT(*) AS count
        FROM invites
        WHERE user_id IS NOT NULL AND status IN ('pending','accepted')
        GROUP BY user_id HAVING COUNT(*)>1
        ORDER BY count DESC
        """
    )
    for row in duplicate_users:
        issues.append(f"INVITES user={row['user_id']}: несколько pending/accepted записей, ID: {row['ids']}")
        if len(issues) >= limit:
            return issues

    goal_rows = await db.fetchall(
        """
        SELECT id, user_id, type, target_value, current_value, period, status
        FROM goals
        ORDER BY id DESC
        """
    )
    for row in goal_rows:
        reasons = []
        if row["type"] not in ("люди", "смены", "часы"):
            reasons.append(f"тип={row['type']!r}")
        if row["period"] not in ("день", "неделя", "месяц"):
            reasons.append(f"период={row['period']!r}")
        if row["status"] not in ("active", "deleted"):
            reasons.append(f"статус={row['status']!r}")
        if row["target_value"] is None:
            reasons.append("target_value=NULL")
        elif row["status"] == "active" and row["target_value"] <= 0:
            reasons.append(f"активная цель с target={row['target_value']}")
        if row["current_value"] is None:
            reasons.append("current_value=NULL")
        elif row["current_value"] < 0:
            reasons.append(f"current_value={row['current_value']}")
        if reasons:
            issues.append(f"GOAL #{row['id']} user={row['user_id']}: " + "; ".join(reasons))
            if len(issues) >= limit:
                return issues

    duplicate_goals = await db.fetchall(
        """
        SELECT user_id, type, GROUP_CONCAT(id) AS ids, COUNT(*) AS count
        FROM goals
        WHERE status='active'
        GROUP BY user_id, type HAVING COUNT(*)>1
        ORDER BY count DESC
        """
    )
    for row in duplicate_goals:
        issues.append(f"GOALS user={row['user_id']} type={row['type']}: несколько активных целей, ID: {row['ids']}")
        if len(issues) >= limit:
            break

    return issues


async def get_blacklist_anomalies(limit: int = 20) -> list[str]:
    """Return human-readable blacklist anomalies with record IDs."""
    limit = max(1, min(int(limit), 50))
    issues: list[str] = []
    rows = await db.fetchall(
        """
        SELECT id, discord_id, discord_tag, static_id, reason, status, created_by,
               removed_by, removed_at, remove_reason
        FROM blacklist
        ORDER BY id DESC
        """
    )
    for row in rows:
        reasons = []
        try:
            discord_id = int(row["discord_id"])
        except (TypeError, ValueError, OverflowError):
            discord_id = 0
        if discord_id <= 0 or discord_id > 2**63 - 1:
            reasons.append("некорректный Discord ID")
        if row["discord_tag"] is None or not str(row["discord_tag"]).strip():
            reasons.append("пустой Discord-тег")
        if row["reason"] is None or not str(row["reason"]).strip():
            reasons.append("пустая причина")
        if row["created_by"] is None:
            reasons.append("нет автора записи")
        if row["status"] not in ("active", "removed"):
            reasons.append(f"неверный статус={row['status']!r}")
        if row["status"] == "active":
            if row["removed_by"] is not None or row["removed_at"] is not None or row["remove_reason"]:
                reasons.append("активная запись содержит данные о снятии")
        elif row["status"] == "removed":
            if row["removed_by"] is None:
                reasons.append("нет администратора снятия")
            if row["removed_at"] is None:
                reasons.append("нет даты снятия")
            if row["remove_reason"] is None or not str(row["remove_reason"]).strip():
                reasons.append("нет причины снятия")
        if reasons:
            issues.append(f"BLACKLIST #{row['id']} Discord ID={row['discord_id'] or '—'}: " + "; ".join(reasons))
            if len(issues) >= limit:
                return issues

    dup_discord = await db.fetchall(
        """
        SELECT discord_id, GROUP_CONCAT(id) AS ids, COUNT(*) AS count
        FROM blacklist WHERE status='active'
        GROUP BY discord_id HAVING COUNT(*)>1
        """
    )
    for row in dup_discord:
        issues.append(f"BLACKLIST Discord ID={row['discord_id']}: несколько активных записей, ID: {row['ids']}")
        if len(issues) >= limit:
            return issues

    dup_static = await db.fetchall(
        """
        SELECT static_id, GROUP_CONCAT(id) AS ids, COUNT(*) AS count
        FROM blacklist
        WHERE status='active' AND static_id IS NOT NULL AND TRIM(static_id)<>''
        GROUP BY static_id HAVING COUNT(*)>1
        """
    )
    for row in dup_static:
        issues.append(f"BLACKLIST статик={row['static_id']}: несколько активных записей, ID: {row['ids']}")
        if len(issues) >= limit:
            break
    return issues


async def get_shift_anomalies(limit: int = 20) -> list[str]:
    limit = max(1, min(int(limit), 50))
    issues: list[str] = []
    rows = await db.fetchall(
        """
        SELECT s.id, s.status, s.slots, s.scheduled_start, s.scheduled_end
        FROM shifts s
        WHERE s.slots < 0 OR s.slots > ?
           OR s.scheduled_start IS NULL OR s.scheduled_end IS NULL
           OR s.scheduled_end <= s.scheduled_start
           OR s.status IS NULL OR s.status NOT IN ('open','booked','active','completed','cancelled','missed')
           OR (s.status='active' AND NOT EXISTS (SELECT 1 FROM shift_members sm WHERE sm.shift_id=s.id AND sm.status='active'))
           OR (s.status IN ('completed','cancelled','missed') AND EXISTS (SELECT 1 FROM shift_members sm WHERE sm.shift_id=s.id AND sm.status IN ('booked','active'))
           )
        ORDER BY s.id DESC LIMIT ?
        """, (config.MAX_SHIFT_SLOTS, limit)
    )
    for row in rows:
        issues.append(f"SHIFT #{row['id']}: status={row['status']!r}, slots={row['slots']}, {row['scheduled_start'] or 'NULL'} → {row['scheduled_end'] or 'NULL'}")
        if len(issues) >= limit: return issues

    rows = await db.fetchall(
        """
        SELECT sm.id, sm.shift_id, sm.user_id, sm.status, sm.report_id
        FROM shift_members sm LEFT JOIN shifts s ON s.id=sm.shift_id
        WHERE s.id IS NULL
           OR sm.status IS NULL OR sm.status NOT IN ('booked','active','completed','cancelled','removed','missed')
           OR (sm.status='active' AND sm.actual_start IS NULL)
           OR (sm.status='completed' AND (sm.actual_start IS NULL OR sm.actual_end IS NULL OR sm.report_id IS NULL))
           OR (sm.status='completed' AND NOT EXISTS (
                SELECT 1 FROM shift_reports r WHERE r.id=sm.report_id AND r.member_id=sm.id AND r.shift_id=sm.shift_id AND r.user_id=sm.user_id
           ))
           OR (sm.actual_start IS NOT NULL AND sm.actual_end IS NOT NULL AND sm.actual_end < sm.actual_start)
        ORDER BY sm.id DESC LIMIT ?
        """, (limit,)
    )
    for row in rows:
        issues.append(f"MEMBER #{row['id']} shift=#{row['shift_id']} user={row['user_id']}: status={row['status']!r}, report={row['report_id'] or '—'}")
        if len(issues) >= limit: return issues

    rows = await db.fetchall(
        """
        SELECT r.id, r.shift_id, r.member_id, r.user_id, r.status
        FROM shift_reports r LEFT JOIN shift_members sm ON sm.id=r.member_id
        WHERE sm.id IS NULL OR sm.shift_id<>r.shift_id OR sm.user_id<>r.user_id
           OR r.status IS NULL OR r.status NOT IN ('pending','approved','rejected')
           OR r.total_accepted IS NULL OR r.came_to_base IS NULL OR r.found_by_recruiter IS NULL
           OR r.total_accepted<0 OR r.came_to_base<0 OR r.found_by_recruiter<0
           OR r.came_to_base+r.found_by_recruiter>r.total_accepted
        ORDER BY r.id DESC LIMIT ?
        """, (limit,)
    )
    for row in rows:
        issues.append(f"REPORT #{row['id']} shift=#{row['shift_id']} member=#{row['member_id']} user={row['user_id']}: status={row['status']!r}")
        if len(issues) >= limit: return issues

    rows = await db.fetchall(
        """SELECT user_id, GROUP_CONCAT(id) AS ids FROM shift_members WHERE status='active' GROUP BY user_id HAVING COUNT(*)>1 ORDER BY user_id LIMIT ?""",
        (limit,)
    )
    for row in rows:
        issues.append(f"USER {row['user_id']}: несколько active shift_members, ID: {row['ids']}")
        if len(issues) >= limit: return issues

    rows = await db.fetchall(
        """
        SELECT DISTINCT sm1.user_id, sm1.shift_id AS shift1, sm2.shift_id AS shift2
        FROM shift_members sm1
        JOIN shift_members sm2 ON sm2.user_id=sm1.user_id AND sm2.id>sm1.id
        JOIN shifts s1 ON s1.id=sm1.shift_id
        JOIN shifts s2 ON s2.id=sm2.shift_id
        WHERE sm1.status IN ('booked','active') AND sm2.status IN ('booked','active')
          AND s1.status IN ('open','booked','active') AND s2.status IN ('open','booked','active')
          AND s1.scheduled_start < s2.scheduled_end
          AND s1.scheduled_end > s2.scheduled_start
        ORDER BY sm1.user_id LIMIT ?
        """, (limit,)
    )
    for row in rows:
        issues.append(f"USER {row['user_id']}: пересекаются текущие смены #{row['shift1']} и #{row['shift2']}")
        if len(issues) >= limit: break
    return issues


async def get_notification_anomalies(limit: int = 20) -> list[str]:
    limit = max(1, min(int(limit), 50))
    rows = await db.fetchall(
        """
        SELECT id, user_id, type, object_type, object_id, status, attempts, created_at, updated_at
        FROM notifications
        WHERE user_id IS NULL OR user_id<0
           OR type IS NULL OR TRIM(type)=''
           OR object_type IS NULL OR TRIM(object_type)=''
           OR object_id IS NULL
           OR status IS NULL OR status NOT IN ('pending','sent','failed')
           OR attempts IS NULL OR attempts<0
           OR created_at IS NULL OR updated_at IS NULL
        ORDER BY id DESC LIMIT ?
        """, (limit,)
    )
    return [f"NOTIFY #{r['id']}: user={r['user_id']}, type={r['type']!r}, object={r['object_type']!r}#{r['object_id']}, status={r['status']!r}, attempts={r['attempts']}" for r in rows]


async def get_finance_anomalies(limit: int = 20) -> list[str]:
    limit = max(1, min(int(limit), 50))
    issues=[]
    rows=await db.fetchall(
        """SELECT id,user_id,amount,type,status FROM finances
           WHERE amount IS NULL OR amount<=0 OR type IS NULL OR type NOT IN ('salary','pay')
              OR status IS NULL OR status NOT IN ('accrued','paid')
              OR (type='salary' AND status<>'accrued') OR (type='pay' AND status<>'paid')
           ORDER BY id DESC LIMIT ?""", (limit,)
    )
    for r in rows:
        issues.append(f"FINANCE #{r['id']} user={r['user_id']}: amount={r['amount']}, type={r['type']!r}, status={r['status']!r}")
    if len(issues)<limit:
        rows=await db.fetchall(
            """SELECT user_id, COALESCE(SUM(CASE WHEN type='salary' THEN amount ELSE 0 END),0)-COALESCE(SUM(CASE WHEN type='pay' THEN amount ELSE 0 END),0) AS balance
               FROM finances GROUP BY user_id HAVING balance < -0.005 LIMIT ?""", (limit-len(issues),)
        )
        for r in rows: issues.append(f"FINANCE user={r['user_id']}: отрицательный баланс {float(r['balance']):.2f}")
    mismatches=await reconcile_all(fix=False)
    for item in mismatches[:max(0,limit-len(issues))]:
        uid=item[0] if isinstance(item,(tuple,list)) and item else item.get('user_id','—') if isinstance(item,dict) else '—'
        issues.append(f"FINANCE user={uid}: кеш профиля не совпадает с журналом")
    return issues


async def repair_safe_domain_anomalies(actor_id: int | None = None) -> list[str]:
    """Repair only legacy anomalies that have an unambiguous, non-destructive fix.

    Currently this detaches a Discord target from a historical self-invite while
    preserving the invite, recruiter, static, checklist and audit history. It also
    normalizes NULL current_value in old goals to 0. Ambiguous duplicates or invalid
    statuses are intentionally not changed automatically.
    """
    fixes: list[str] = []
    async with db.transaction() as tx:
        self_invites = await tx.fetchall(
            "SELECT id, static_id FROM invites WHERE user_id IS NOT NULL AND user_id=invited_by"
        )
        for row in self_invites:
            await tx.execute("UPDATE invites SET user_id=NULL WHERE id=?", (row["id"],))
            fixes.append(f"INVITE #{row['id']}: убрана ошибочная привязка к самому рекрутеру")

        null_goals = await tx.fetchall("SELECT id FROM goals WHERE current_value IS NULL")
        if null_goals:
            await tx.execute("UPDATE goals SET current_value=0 WHERE current_value IS NULL")
            fixes.append(f"GOALS: current_value=NULL исправлено у {len(null_goals)} записей")

        if fixes and actor_id is not None:
            await tx.execute(
                """
                INSERT INTO logs (user_id, action, object_type, object_id, details)
                VALUES (?, 'HEALTH_SAFE_REPAIR', 'database', NULL, ?)
                """,
                (actor_id, " | ".join(fixes)[:2000]),
            )
    return fixes


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
        checks.append(HealthCheck("Схема БД", version_value >= 4, f"версия: {version_value}"))

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
               OR (sm.status='completed' AND NOT EXISTS (
                    SELECT 1 FROM shift_reports linked WHERE linked.id=sm.report_id AND linked.member_id=sm.id AND linked.shift_id=sm.shift_id AND linked.user_id=sm.user_id
               ))
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
        multiple_active = await db.fetchone("SELECT COUNT(*) AS count FROM (SELECT user_id FROM shift_members WHERE status='active' GROUP BY user_id HAVING COUNT(*)>1)")
        overlapping_live = await db.fetchone(
            """
            SELECT COUNT(*) AS count FROM (
                SELECT DISTINCT sm1.user_id, sm1.shift_id, sm2.shift_id
                FROM shift_members sm1
                JOIN shift_members sm2 ON sm2.user_id=sm1.user_id AND sm2.id>sm1.id
                JOIN shifts s1 ON s1.id=sm1.shift_id
                JOIN shifts s2 ON s2.id=sm2.shift_id
                WHERE sm1.status IN ('booked','active') AND sm2.status IN ('booked','active')
                  AND s1.status IN ('open','booked','active') AND s2.status IN ('open','booked','active')
                  AND s1.scheduled_start < s2.scheduled_end
                  AND s1.scheduled_end > s2.scheduled_start
            )
            """
        )
        data_errors = (int(bad_shifts["count"] or 0) + int(bad_members["count"] or 0)
                       + int(bad_reports["count"] or 0) + int(multiple_active["count"] or 0)
                       + int(overlapping_live["count"] or 0))
        shift_lines = await get_shift_anomalies(limit=4) if data_errors else []
        data_details = f"аномалий: {data_errors}" if data_errors else ""
        if shift_lines:
            data_details += "\n" + "\n".join(f"• {x}" for x in shift_lines)
        checks.append(HealthCheck("Данные смен/отчётов", data_errors == 0, data_details))

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
        
        if domain_errors:
            domain_issue_lines = await get_domain_anomalies(limit=4)
            domain_details = f"аномалий: {domain_errors}"
            if domain_issue_lines:
                domain_details += "\n" + "\n".join(f"• {line}" for line in domain_issue_lines)
        else:
            domain_details = ""
        checks.append(HealthCheck("Инвайты и цели", domain_errors == 0, domain_details))

        blacklist_issues = await get_blacklist_anomalies(limit=4)
        blacklist_bad = await db.fetchone(
            """
            SELECT COUNT(*) AS count FROM blacklist
            WHERE discord_id IS NULL OR discord_id<=0
               OR discord_tag IS NULL OR TRIM(discord_tag)=''
               OR reason IS NULL OR TRIM(reason)=''
               OR created_by IS NULL
               OR status IS NULL OR status NOT IN ('active','removed')
               OR (status='active' AND (removed_by IS NOT NULL OR removed_at IS NOT NULL OR COALESCE(TRIM(remove_reason),'')<>''))
               OR (status='removed' AND (removed_by IS NULL OR removed_at IS NULL OR COALESCE(TRIM(remove_reason),'')=''))
            """
        )
        blacklist_dup_discord = await db.fetchone(
            """
            SELECT COUNT(*) AS count FROM (
                SELECT discord_id FROM blacklist WHERE status='active'
                GROUP BY discord_id HAVING COUNT(*)>1
            )
            """
        )
        blacklist_dup_static = await db.fetchone(
            """
            SELECT COUNT(*) AS count FROM (
                SELECT static_id FROM blacklist
                WHERE status='active' AND static_id IS NOT NULL AND TRIM(static_id)<>''
                GROUP BY static_id HAVING COUNT(*)>1
            )
            """
        )
        blacklist_errors = (
            int(blacklist_bad["count"] or 0)
            + int(blacklist_dup_discord["count"] or 0)
            + int(blacklist_dup_static["count"] or 0)
        )
        blacklist_details = f"аномалий: {blacklist_errors}" if blacklist_errors else ""
        if blacklist_issues:
            blacklist_details += ("\n" if blacklist_details else "") + "\n".join(f"• {x}" for x in blacklist_issues)
        checks.append(HealthCheck("Чёрный список", blacklist_errors == 0, blacklist_details))

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
        finance_lines = await get_finance_anomalies(limit=4) if fin_errors else []
        finance_details = f"аномалий: {fin_errors}" if fin_errors else ""
        if finance_lines:
            finance_details += "\n" + "\n".join(f"• {x}" for x in finance_lines)
        checks.append(HealthCheck("Финансы", fin_errors == 0, finance_details))

        notification_lines = await get_notification_anomalies(limit=4)
        checks.append(HealthCheck("Уведомления", not notification_lines, "\n".join(f"• {x}" for x in notification_lines)))
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
    admin_cog = bot.get_cog("Admin")
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
    if admin_cog is None:
        task_errors.append("автобэкап (Admin Cog)")
    else:
        backup_loop = getattr(admin_cog, "auto_backup", None)
        if not backup_loop or not backup_loop.is_running():
            task_errors.append("автобэкап")
    checks.append(HealthCheck("Фоновые задачи", not task_errors, f"не работают: {', '.join(task_errors)}" if task_errors else ""))
    return checks
