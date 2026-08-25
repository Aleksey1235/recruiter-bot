from __future__ import annotations

import config

from database.db import db, ensure_user, log
from services.errors import UserFacingError
from services.finance_service import get_balance
from utils.ids import maybe_positive_sqlite_int
from utils.time_utils import local_now


async def get_user_overview(user_id: int) -> dict | None:
    user = await db.fetchone("SELECT * FROM users WHERE discord_id=?", (user_id,))
    if not user:
        return None

    shifts = await db.fetchone(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status='booked' THEN 1 ELSE 0 END) AS booked,
            SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active,
            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
            SUM(CASE WHEN status='missed' THEN 1 ELSE 0 END) AS missed,
            SUM(CASE WHEN status='removed' THEN 1 ELSE 0 END) AS removed,
            SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) AS cancelled
        FROM shift_members
        WHERE user_id=?
        """,
        (user_id,),
    )
    reports = await db.fetchone(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) AS approved,
            SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected,
            COALESCE(SUM(CASE WHEN status='approved' THEN total_accepted ELSE 0 END), 0) AS accepted
        FROM shift_reports
        WHERE user_id=?
        """,
        (user_id,),
    )
    invites = await db.fetchone(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN status='accepted' THEN 1 ELSE 0 END) AS accepted,
            SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected
        FROM invites
        WHERE invited_by=?
        """,
        (user_id,),
    )
    goals = await db.fetchone(
        "SELECT COUNT(*) AS active FROM goals WHERE user_id=? AND status='active'",
        (user_id,),
    )
    blacklist = await db.fetchone(
        """
        SELECT * FROM blacklist
        WHERE status='active' AND (discord_id=? OR (static_id IS NOT NULL AND static_id=?))
        ORDER BY CASE WHEN discord_id=? THEN 0 ELSE 1 END, id DESC
        LIMIT 1
        """,
        (user_id, user["static_id"], user_id),
    )
    accrued, paid, available = await get_balance(user_id)
    return {
        "user": dict(user),
        "shifts": dict(shifts),
        "reports": dict(reports),
        "invites": dict(invites),
        "active_goals": int(goals["active"] or 0),
        "blacklist": dict(blacklist) if blacklist else None,
        "accrued": accrued,
        "paid": paid,
        "available": available,
    }


async def search_users(query: str, limit: int = 10):
    query = query.strip()
    if not query:
        raise UserFacingError("Введите Discord ID, статик или часть имени.")
    limit = max(1, min(int(limit), 20))

    if query.isdigit():
        discord_id = maybe_positive_sqlite_int(query)
        if discord_id is not None:
            exact = await db.fetchall(
                """
                SELECT * FROM users
                WHERE discord_id=? OR static_id=?
                ORDER BY CASE WHEN discord_id=? THEN 0 ELSE 1 END, discord_id
                LIMIT ?
                """,
                (discord_id, query, discord_id, limit),
            )
        else:
            exact = await db.fetchall(
                "SELECT * FROM users WHERE static_id=? ORDER BY discord_id LIMIT ?",
                (query, limit),
            )
        if exact:
            return exact

    pattern = f"%{query}%"
    return await db.fetchall(
        """
        SELECT * FROM users
        WHERE username LIKE ? COLLATE NOCASE
           OR static_id LIKE ? COLLATE NOCASE
        ORDER BY username COLLATE NOCASE, discord_id
        LIMIT ?
        """,
        (pattern, pattern, limit),
    )


async def list_user_finances(user_id: int, limit: int = 10):
    return await db.fetchall(
        """
        SELECT * FROM finances
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, max(1, min(int(limit), 20))),
    )


async def list_user_shifts(user_id: int, limit: int = 10):
    return await db.fetchall(
        """
        SELECT
            sm.id AS member_id,
            sm.shift_id,
            sm.status AS member_status,
            sm.static_id,
            sm.actual_start,
            sm.actual_end,
            sm.cancel_reason,
            sm.report_id,
            s.scheduled_start,
            s.scheduled_end,
            s.status AS shift_status,
            s.description
        FROM shift_members sm
        JOIN shifts s ON s.id=sm.shift_id
        WHERE sm.user_id=?
        ORDER BY s.scheduled_start DESC, sm.id DESC
        LIMIT ?
        """,
        (user_id, max(1, min(int(limit), 20))),
    )


async def list_user_reports(user_id: int, limit: int = 10):
    return await db.fetchall(
        """
        SELECT * FROM shift_reports
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, max(1, min(int(limit), 20))),
    )


async def list_user_invites(user_id: int, limit: int = 10):
    return await db.fetchall(
        """
        SELECT * FROM invites
        WHERE invited_by=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, max(1, min(int(limit), 20))),
    )


async def list_user_logs(user_id: int, limit: int = 10):
    return await db.fetchall(
        """
        SELECT * FROM logs
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, max(1, min(int(limit), 20))),
    )


async def get_finance_operation(finance_id: int):
    return await db.fetchone("SELECT * FROM finances WHERE id=?", (finance_id,))


async def update_user_static(user_id: int, username: str | None, new_static: str | None, actor_id: int):
    if new_static is not None:
        new_static = new_static.strip()
        if not new_static:
            new_static = None
        elif len(new_static) > config.MAX_STATIC_ID_LENGTH:
            raise UserFacingError(f"Статик не может быть длиннее {config.MAX_STATIC_ID_LENGTH} символов.")

    async with db.transaction() as tx:
        if new_static:
            duplicate = await tx.fetchone(
                "SELECT discord_id FROM users WHERE static_id=? AND discord_id<>? LIMIT 1",
                (new_static, user_id),
            )
            if duplicate:
                raise UserFacingError("Этот статик уже привязан к другому пользователю.")
        await ensure_user(user_id, username=username, tx=tx)
        previous = await tx.fetchone("SELECT static_id FROM users WHERE discord_id=?", (user_id,))
        old_static = previous["static_id"] if previous else None
        await tx.execute(
            "UPDATE users SET static_id=?, username=COALESCE(?, username) WHERE discord_id=?",
            (new_static, username, user_id),
        )
        # Для будущих/текущих смен статик должен совпадать с профилем. Исторические
        # завершённые записи оставляем как снимок того, что было указано на смене.
        await tx.execute(
            """
            UPDATE shift_members
            SET static_id=?
            WHERE user_id=? AND status IN ('booked', 'active')
            """,
            (new_static, user_id),
        )
        await log(
            actor_id,
            "DB_STATIC_UPDATE",
            "user",
            user_id,
            f"{old_static or '—'} -> {new_static or '—'}",
            tx=tx,
        )
    return old_static, new_static


async def add_user_note(user_id: int, username: str | None, text: str, actor_id: int, actor_name: str):
    text = text.strip()
    if not text:
        raise UserFacingError("Заметка не может быть пустой.")
    if len(text) > 1000:
        raise UserFacingError("Заметка не может быть длиннее 1000 символов.")

    stamp = local_now().strftime("%d.%m.%Y %H:%M")
    addition = f"\n[{stamp}] {actor_name}: {text}"
    async with db.transaction() as tx:
        await ensure_user(user_id, username=username, tx=tx)
        current = await tx.fetchone("SELECT COALESCE(notes, '') AS notes FROM users WHERE discord_id=?", (user_id,))
        notes = (current["notes"] or "") + addition
        # Не даём технической колонке бесконтрольно расти годами. Старые заметки
        # остаются в logs; в профиле храним последние ~20k символов.
        if len(notes) > 20_000:
            notes = notes[-20_000:]
        await tx.execute("UPDATE users SET notes=? WHERE discord_id=?", (notes, user_id))
        await log(actor_id, "DB_NOTE_ADD", "user", user_id, text, tx=tx)
    return notes


async def list_recent_invites(limit: int = 15):
    return await db.fetchall(
        "SELECT * FROM invites ORDER BY id DESC LIMIT ?",
        (max(1, min(int(limit), 25)),),
    )


async def list_recent_goals(limit: int = 15):
    return await db.fetchall(
        "SELECT * FROM goals ORDER BY id DESC LIMIT ?",
        (max(1, min(int(limit), 25)),),
    )


async def list_recent_finances(limit: int = 15):
    return await db.fetchall(
        "SELECT * FROM finances ORDER BY id DESC LIMIT ?",
        (max(1, min(int(limit), 25)),),
    )


async def get_invite(invite_id: int):
    return await db.fetchone("SELECT * FROM invites WHERE id=?", (invite_id,))


async def get_goal(goal_id: int):
    return await db.fetchone("SELECT * FROM goals WHERE id=?", (goal_id,))
