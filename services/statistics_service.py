from datetime import timedelta

from database.db import db
from services.finance_service import get_balance, get_general_balance
from utils.time_utils import local_now, period_start, to_db


def _period_clause(start, expression: str):
    if start is None:
        return "", ()
    return f" AND {expression} >= ?", (to_db(start),)


async def user_statistics(user_id: int, period: str):
    start = period_start(period)
    member_clause, member_params = _period_clause(start, "COALESCE(sm.actual_start, s.scheduled_start)")
    report_clause, report_params = _period_clause(start, "COALESCE(sm.actual_start, s.scheduled_start)")

    members = await db.fetchone(
        f"""
        SELECT
            COUNT(*) AS total_shifts,
            SUM(CASE WHEN sm.status='completed' THEN 1 ELSE 0 END) AS completed_members,
            SUM(CASE WHEN sm.status='missed' THEN 1 ELSE 0 END) AS missed_shifts,
            SUM(CASE WHEN sm.status='cancelled' THEN 1 ELSE 0 END) AS cancelled_shifts,
            SUM(CASE WHEN sm.status='removed' THEN 1 ELSE 0 END) AS removed_shifts,
            COALESCE(SUM(
                CASE WHEN sm.actual_start IS NOT NULL AND sm.actual_end IS NOT NULL
                     THEN (julianday(sm.actual_end)-julianday(sm.actual_start))*24.0
                     ELSE 0 END
            ), 0) AS total_hours
        FROM shift_members sm
        JOIN shifts s ON s.id=sm.shift_id
        WHERE sm.user_id=? {member_clause}
        """,
        (user_id, *member_params),
    )

    reports = await db.fetchone(
        f"""
        SELECT
            SUM(CASE WHEN r.status='approved' THEN 1 ELSE 0 END) AS approved_reports,
            SUM(CASE WHEN r.status='pending' THEN 1 ELSE 0 END) AS pending_reports,
            SUM(CASE WHEN r.status='rejected' THEN 1 ELSE 0 END) AS rejected_reports,
            COALESCE(SUM(CASE WHEN r.status='approved' THEN r.total_accepted ELSE 0 END), 0) AS total_accepted,
            COALESCE(SUM(CASE WHEN r.status='approved' THEN r.came_to_base ELSE 0 END), 0) AS total_base,
            COALESCE(SUM(CASE WHEN r.status='approved' THEN r.found_by_recruiter ELSE 0 END), 0) AS total_self
        FROM shift_reports r
        JOIN shift_members sm ON sm.id=r.member_id
        JOIN shifts s ON s.id=r.shift_id
        WHERE r.user_id=? {report_clause}
        """,
        (user_id, *report_params),
    )

    approved = int(reports["approved_reports"] or 0)
    completed = int(members["completed_members"] or 0)
    missed = int(members["missed_shifts"] or 0)
    total_accepted = int(reports["total_accepted"] or 0)
    total_self = int(reports["total_self"] or 0)
    attendance_denominator = completed + missed
    attendance = (completed / attendance_denominator * 100) if attendance_denominator else None

    accrued, paid, available = await get_balance(user_id)
    goals = await db.fetchone(
        "SELECT COUNT(*) AS count FROM goals WHERE user_id=? AND status='active'",
        (user_id,),
    )

    ranking_clause, ranking_params = _period_clause(start, "COALESCE(sm.actual_start, s.scheduled_start)")
    ranking_rows = await db.fetchall(
        f"""
        SELECT r.user_id, COALESCE(SUM(r.total_accepted),0) AS accepted
        FROM shift_reports r
        JOIN shift_members sm ON sm.id=r.member_id
        JOIN shifts s ON s.id=r.shift_id
        WHERE r.status='approved' {ranking_clause}
        GROUP BY r.user_id
        ORDER BY accepted DESC, r.user_id ASC
        """,
        ranking_params,
    )
    rank = None
    for index, row in enumerate(ranking_rows, start=1):
        if row["user_id"] == user_id:
            rank = index
            break

    return {
        "total_shifts": int(members["total_shifts"] or 0),
        "completed_shifts": completed,
        "approved_reports": approved,
        "pending_shifts": int(reports["pending_reports"] or 0),
        "rejected_shifts": int(reports["rejected_reports"] or 0),
        "missed_shifts": missed,
        "cancelled_shifts": int(members["cancelled_shifts"] or 0),
        "removed_shifts": int(members["removed_shifts"] or 0),
        "total_hours": float(members["total_hours"] or 0),
        "total_accepted": total_accepted,
        "total_base": int(reports["total_base"] or 0),
        "total_self": total_self,
        "avg_per_shift": (total_accepted / approved) if approved else 0,
        "avg_self_per_shift": (total_self / approved) if approved else 0,
        "self_percent": (total_self / total_accepted * 100) if total_accepted else 0,
        "attendance": attendance,
        "active_goals": int(goals["count"] or 0),
        "accrued": accrued,
        "paid": paid,
        "available": available,
        "rank": rank,
    }


async def top_statistics(period: str):
    start = period_start(period)
    clause, params = _period_clause(start, "COALESCE(sm.actual_start, s.scheduled_start)")
    rows = await db.fetchall(
        f"""
        SELECT
            r.user_id,
            COALESCE(SUM(r.total_accepted),0) AS accepted,
            COALESCE(SUM(r.found_by_recruiter),0) AS self_found,
            COUNT(*) AS shifts,
            COALESCE(AVG(r.total_accepted),0) AS avg_per_shift
        FROM shift_reports r
        JOIN shift_members sm ON sm.id=r.member_id
        JOIN shifts s ON s.id=r.shift_id
        WHERE r.status='approved' {clause}
        GROUP BY r.user_id
        """,
        params,
    )
    return [dict(row) for row in rows]


async def current_week_by_day(user_id: int):
    now = local_now()
    start = period_start("неделя", now)
    end = start + timedelta(days=7)
    rows = await db.fetchall(
        """
        SELECT
            date(COALESCE(sm.actual_start, s.scheduled_start)) AS day,
            COUNT(*) AS shifts,
            COALESCE(SUM(r.total_accepted),0) AS accepted,
            COALESCE(SUM(r.came_to_base),0) AS base_count,
            COALESCE(SUM(r.found_by_recruiter),0) AS self_found
        FROM shift_reports r
        JOIN shift_members sm ON sm.id=r.member_id
        JOIN shifts s ON s.id=r.shift_id
        WHERE r.user_id=? AND r.status='approved'
          AND COALESCE(sm.actual_start, s.scheduled_start) >= ?
          AND COALESCE(sm.actual_start, s.scheduled_start) < ?
        GROUP BY day
        ORDER BY day
        """,
        (user_id, to_db(start), to_db(end)),
    )
    by_day = {row["day"]: dict(row) for row in rows}
    result = []
    for offset in range(7):
        day = start + timedelta(days=offset)
        data = by_day.get(day.strftime("%Y-%m-%d"), {})
        result.append(
            {
                "date": day,
                "shifts": int(data.get("shifts", 0)),
                "accepted": int(data.get("accepted", 0)),
                "base": int(data.get("base_count", 0)),
                "self": int(data.get("self_found", 0)),
            }
        )
    return result


async def weekly_summary(start=None, end=None):
    start = start or period_start("неделя")
    if end is None:
        end = start + timedelta(days=7)
    rows = await db.fetchall(
        """
        SELECT
            r.user_id,
            COUNT(*) AS shifts,
            COALESCE(SUM(r.total_accepted),0) AS accepted,
            COALESCE(SUM(r.came_to_base),0) AS base_count,
            COALESCE(SUM(r.found_by_recruiter),0) AS self_found
        FROM shift_reports r
        JOIN shift_members sm ON sm.id=r.member_id
        JOIN shifts s ON s.id=r.shift_id
        WHERE r.status='approved'
          AND COALESCE(sm.actual_start, s.scheduled_start) >= ?
          AND COALESCE(sm.actual_start, s.scheduled_start) < ?
        GROUP BY r.user_id
        ORDER BY accepted DESC
        """,
        (to_db(start), to_db(end)),
    )
    total = {
        "shifts": sum(int(row["shifts"] or 0) for row in rows),
        "accepted": sum(int(row["accepted"] or 0) for row in rows),
        "base": sum(int(row["base_count"] or 0) for row in rows),
        "self": sum(int(row["self_found"] or 0) for row in rows),
    }
    accrued, paid, available = await get_general_balance()
    return start, [dict(row) for row in rows], total, (accrued, paid, available)
