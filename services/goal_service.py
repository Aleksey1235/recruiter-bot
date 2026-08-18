from database.db import db
from utils.time_utils import period_start, to_db


async def calculate_progress(user_id: int, goal_type: str, period: str) -> int:
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
