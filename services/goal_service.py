from database.db import db, ensure_user, log
from services.errors import UserFacingError
from services import advertising_service
from utils.time_utils import period_start, to_db

VALID_GOAL_TYPES = {"люди", "смены", "часы", "рекламы"}
VALID_GOAL_PERIODS = {"день", "неделя", "месяц"}
MAX_GOAL_VALUE = 1_000_000


async def set_goal(user_id: int, username: str | None, goal_type: str, value: int, period: str, actor_id: int) -> int:
    if goal_type not in VALID_GOAL_TYPES:
        raise UserFacingError("Неизвестный тип цели.")
    if period not in VALID_GOAL_PERIODS:
        raise UserFacingError("Неизвестный период цели.")
    if value <= 0:
        raise UserFacingError("Значение цели должно быть больше 0.")
    if value > MAX_GOAL_VALUE:
        raise UserFacingError(f"Значение цели не может быть больше {MAX_GOAL_VALUE:,}.")

    current = await calculate_progress(user_id, goal_type, period)

    async with db.transaction() as tx:
        await ensure_user(user_id, username=username, tx=tx)
        await tx.execute(
            "UPDATE goals SET status='deleted' WHERE user_id=? AND type=? AND status='active'",
            (user_id, goal_type),
        )
        cursor = await tx.execute(
            """
            INSERT INTO goals (user_id, type, target_value, current_value, period, created_by)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, goal_type, value, current, period, actor_id),
        )
        goal_id = cursor.lastrowid
        await log(
            actor_id,
            "GOAL_SET",
            "goal",
            goal_id,
            f"user={user_id}; {goal_type}={value}; current={current}; period={period}",
            tx=tx,
        )
        return goal_id


async def delete_active_goals(user_id: int, actor_id: int):
    async with db.transaction() as tx:
        goals = await tx.fetchall(
            "SELECT * FROM goals WHERE user_id=? AND status='active' ORDER BY id",
            (user_id,),
        )
        if not goals:
            return []
        await tx.execute(
            "UPDATE goals SET status='deleted' WHERE user_id=? AND status='active'",
            (user_id,),
        )
        await log(actor_id, "GOAL_DELETE", "user", user_id, None, tx=tx)
        return goals


async def calculate_progress(user_id: int, goal_type: str, period: str) -> int:
    if goal_type == "рекламы":
        return (await advertising_service.summary(user_id, period))["total"]
    start = period_start(period)
    date_clause = ""
    params = [user_id]
    if start is not None:
        date_clause = " AND COALESCE(sm.actual_start, s.scheduled_start) >= ?"
        params.append(to_db(start))

    if goal_type in ("люди", "смены"):
        aggregate = "COALESCE(SUM(r.total_accepted),0)" if goal_type == "люди" else "COUNT(*)"
        row = await db.fetchone(
            f"""
            SELECT {aggregate} AS value
            FROM shift_reports r
            JOIN shift_members sm ON sm.id=r.member_id
            JOIN shifts s ON s.id=r.shift_id
            WHERE r.user_id=? AND r.status='approved' {date_clause}
            """,
            tuple(params),
        )
        return int(row["value"] or 0)

    if goal_type == "часы":
        row = await db.fetchone(
            f"""
            SELECT COALESCE(SUM(
                CASE WHEN sm.actual_start IS NOT NULL AND sm.actual_end IS NOT NULL
                     THEN (julianday(sm.actual_end)-julianday(sm.actual_start))*24.0
                     ELSE 0 END
            ),0) AS value
            FROM shift_members sm
            JOIN shifts s ON s.id=sm.shift_id
            WHERE sm.user_id=? AND sm.status='completed' {date_clause}
            """,
            tuple(params),
        )
        return int(float(row["value"] or 0))

    return 0
