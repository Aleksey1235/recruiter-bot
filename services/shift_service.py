from dataclasses import dataclass
from datetime import timedelta

import config
from database.db import db, ensure_user, log
from services.errors import UserFacingError
from utils.time_utils import local_now, parse_db, to_db


@dataclass
class FinishResult:
    report: object
    member: object
    shift: object


def _validate_report_values(total_accepted: int, came_to_base: int, found_by_recruiter: int):
    values = (total_accepted, came_to_base, found_by_recruiter)
    if any(value < 0 for value in values):
        raise UserFacingError("Значения отчёта не могут быть отрицательными.")
    if came_to_base > total_accepted or found_by_recruiter > total_accepted:
        raise UserFacingError("Показатели «на особняке» и «самостоятельно» не могут быть больше общего числа принятых.")
    if came_to_base + found_by_recruiter > total_accepted:
        raise UserFacingError("Сумма «на особняке» и «самостоятельно» не может быть больше общего числа принятых.")


async def _recalculate_shift_status(tx, shift_id: int):
    shift = await tx.fetchone("SELECT * FROM shifts WHERE id=?", (shift_id,))
    if not shift or shift["status"] == "cancelled":
        return

    counts = await tx.fetchone(
        """
        SELECT
            SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active_count,
            SUM(CASE WHEN status='booked' THEN 1 ELSE 0 END) AS booked_count,
            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed_count,
            SUM(CASE WHEN status='missed' THEN 1 ELSE 0 END) AS missed_count
        FROM shift_members
        WHERE shift_id=?
        """,
        (shift_id,),
    )
    active = counts["active_count"] or 0
    booked = counts["booked_count"] or 0
    completed = counts["completed_count"] or 0
    missed = counts["missed_count"] or 0
    end = parse_db(shift["scheduled_end"])
    now = local_now()

    if active:
        status = "active"
    elif booked:
        status = "booked" if (shift["slots"] or 0) <= 0 else "open"
    elif end and now < end and (shift["slots"] or 0) > 0:
        status = "open"
    elif completed:
        status = "completed"
    elif missed:
        status = "missed"
    else:
        status = "completed" if end and now >= end else "open"

    await tx.execute("UPDATE shifts SET status=? WHERE id=?", (status, shift_id))


async def create_shift(creator_id: int, start, end, slots: int, description: str = "") -> int:
    description = description.strip()
    if len(description) > 1000:
        raise UserFacingError("Описание смены не может быть длиннее 1000 символов.")
    if slots < 1:
        raise UserFacingError("Количество мест должно быть минимум 1.")
    if end <= start:
        raise UserFacingError("Время окончания должно быть позже начала.")
    if end <= local_now():
        raise UserFacingError("Нельзя создать смену, которая уже закончилась.")

    async with db.transaction() as tx:
        cursor = await tx.execute(
            """
            INSERT INTO shifts
                (creator_id, scheduled_start, scheduled_end, slots, description, status)
            VALUES (?, ?, ?, ?, ?, 'open')
            """,
            (creator_id, to_db(start), to_db(end), slots, description or None),
        )
        shift_id = cursor.lastrowid
        await log(
            creator_id,
            "SHIFT_CREATED",
            "shift",
            shift_id,
            f"{to_db(start)} - {to_db(end)}, мест: {slots}",
            tx=tx,
        )
        return shift_id


async def set_shift_message_id(shift_id: int, message_id: int):
    await db.execute("UPDATE shifts SET message_id=? WHERE id=?", (message_id, shift_id))


async def take_shift(shift_id: int, user_id: int, username: str, static_id: str):
    static_id = static_id.strip()
    if not static_id:
        raise UserFacingError("Укажите статик.")
    if len(static_id) > 20:
        raise UserFacingError("Статик слишком длинный.")

    async with db.transaction() as tx:
        shift = await tx.fetchone("SELECT * FROM shifts WHERE id=?", (shift_id,))
        if not shift:
            raise UserFacingError("Смена не найдена.")
        if shift["status"] not in ("open", "booked"):
            raise UserFacingError("Эта смена больше недоступна для записи.")
        end = parse_db(shift["scheduled_end"])
        if end and local_now() >= end:
            raise UserFacingError("Эта смена уже закончилась.")

        conflict = await tx.fetchone(
            """
            SELECT sm.id
            FROM shift_members sm
            JOIN shifts s ON s.id=sm.shift_id
            WHERE sm.user_id=?
              AND sm.status IN ('booked', 'active')
              AND s.id<>?
              AND s.scheduled_start < ?
              AND s.scheduled_end > ?
            LIMIT 1
            """,
            (user_id, shift_id, shift["scheduled_end"], shift["scheduled_start"]),
        )
        if conflict:
            raise UserFacingError("У вас уже есть другая смена, которая пересекается по времени.")

        existing = await tx.fetchone(
            "SELECT * FROM shift_members WHERE shift_id=? AND user_id=?",
            (shift_id, user_id),
        )
        if existing and existing["status"] in ("booked", "active"):
            raise UserFacingError("Вы уже записаны на эту смену.")
        if existing and existing["status"] == "completed":
            raise UserFacingError("Вы уже завершили эту смену.")
        if existing and existing["status"] == "missed":
            raise UserFacingError("Эта смена уже отмечена для вас как пропущенная.")

        cursor = await tx.execute(
            """
            UPDATE shifts
            SET slots=slots-1
            WHERE id=? AND slots>0 AND status IN ('open', 'booked')
            """,
            (shift_id,),
        )
        if cursor.rowcount != 1:
            raise UserFacingError("На смене больше нет свободных мест.")

        if existing:
            await tx.execute(
                """
                UPDATE shift_members
                SET static_id=?, status='booked', actual_start=NULL, actual_end=NULL,
                    cancel_reason=NULL, report_id=NULL
                WHERE id=?
                """,
                (static_id, existing["id"]),
            )
            member_id = existing["id"]
        else:
            cursor = await tx.execute(
                """
                INSERT INTO shift_members (shift_id, user_id, static_id, status)
                VALUES (?, ?, ?, 'booked')
                """,
                (shift_id, user_id, static_id),
            )
            member_id = cursor.lastrowid

        await ensure_user(user_id, username=username, static_id=static_id, tx=tx)
        updated = await tx.fetchone("SELECT slots FROM shifts WHERE id=?", (shift_id,))
        new_status = "booked" if updated["slots"] <= 0 else "open"
        await tx.execute("UPDATE shifts SET status=? WHERE id=?", (new_status, shift_id))
        await log(user_id, "SHIFT_TAKEN", "shift", shift_id, f"Статик: {static_id}", tx=tx)
        return member_id


async def find_shift_to_start(user_id: int):
    now = local_now()
    early_limit = now + timedelta(minutes=config.EARLY_START_MINUTES)
    rows = await db.fetchall(
        """
        SELECT sm.*, s.scheduled_start, s.scheduled_end, s.status AS shift_status
        FROM shift_members sm
        JOIN shifts s ON s.id=sm.shift_id
        WHERE sm.user_id=?
          AND sm.status='booked'
          AND s.status NOT IN ('cancelled', 'completed', 'missed')
          AND s.scheduled_end > ?
          AND s.scheduled_start <= ?
        ORDER BY s.scheduled_start ASC
        LIMIT 2
        """,
        (user_id, to_db(now), to_db(early_limit)),
    )
    if rows:
        return rows[0]

    next_shift = await db.fetchone(
        """
        SELECT sm.*, s.scheduled_start, s.scheduled_end
        FROM shift_members sm
        JOIN shifts s ON s.id=sm.shift_id
        WHERE sm.user_id=? AND sm.status='booked' AND s.scheduled_end>?
        ORDER BY s.scheduled_start ASC
        LIMIT 1
        """,
        (user_id, to_db(now)),
    )
    if next_shift:
        start = parse_db(next_shift["scheduled_start"])
        raise UserFacingError(
            f"Смену можно начать не раньше чем за {config.EARLY_START_MINUTES} мин. до начала. "
            f"Ближайшая смена #{next_shift['shift_id']} — {start.strftime('%d.%m %H:%M') if start else 'время неизвестно'}."
        )
    raise UserFacingError("У вас нет забронированной смены, которую можно начать.")


async def start_shift(member_id: int, user_id: int):
    async with db.transaction() as tx:
        member = await tx.fetchone(
            """
            SELECT sm.*, s.scheduled_start, s.scheduled_end, s.status AS shift_status
            FROM shift_members sm JOIN shifts s ON s.id=sm.shift_id
            WHERE sm.id=? AND sm.user_id=?
            """,
            (member_id, user_id),
        )
        if not member or member["status"] != "booked":
            raise UserFacingError("Эта запись на смену уже изменилась. Обновите команду и попробуйте снова.")
        if member["shift_status"] in ("cancelled", "completed", "missed"):
            raise UserFacingError("Эту смену уже нельзя начать.")

        other_active = await tx.fetchone(
            "SELECT id FROM shift_members WHERE user_id=? AND status='active' AND id<>? LIMIT 1",
            (user_id, member_id),
        )
        if other_active:
            raise UserFacingError(
                "У вас уже есть другая активная смена. Сначала завершите её или обратитесь к старшему составу."
            )

        now = local_now()
        start = parse_db(member["scheduled_start"])
        end = parse_db(member["scheduled_end"])
        if end and now >= end:
            raise UserFacingError("Время этой смены уже закончилось.")
        if start and now < start - timedelta(minutes=config.EARLY_START_MINUTES):
            raise UserFacingError(f"Смену можно начать только за {config.EARLY_START_MINUTES} мин. до начала.")

        cursor = await tx.execute(
            "UPDATE shift_members SET status='active', actual_start=? WHERE id=? AND status='booked'",
            (to_db(now), member_id),
        )
        if cursor.rowcount != 1:
            raise UserFacingError("Смена уже была начата или состояние изменилось.")
        await tx.execute("UPDATE shifts SET status='active' WHERE id=?", (member["shift_id"],))
        await log(user_id, "SHIFT_STARTED", "shift", member["shift_id"], None, tx=tx)
        return member["shift_id"]


async def find_active_member(user_id: int):
    rows = await db.fetchall(
        """
        SELECT sm.*, s.scheduled_start, s.scheduled_end
        FROM shift_members sm JOIN shifts s ON s.id=sm.shift_id
        WHERE sm.user_id=? AND sm.status='active'
        ORDER BY sm.actual_start ASC
        LIMIT 2
        """,
        (user_id,),
    )
    if not rows:
        raise UserFacingError("У вас нет активной смены.")
    if len(rows) > 1:
        raise UserFacingError("Обнаружено несколько активных смен. Обратитесь к администратору для исправления данных.")
    return rows[0]


async def finish_shift(member_id: int, user_id: int, total_accepted: int, came_to_base: int, found_by_recruiter: int, comment: str) -> FinishResult:
    _validate_report_values(total_accepted, came_to_base, found_by_recruiter)
    comment = comment.strip()
    if len(comment) > 1000:
        raise UserFacingError("Комментарий не может быть длиннее 1000 символов.")

    async with db.transaction() as tx:
        member = await tx.fetchone("SELECT * FROM shift_members WHERE id=? AND user_id=?", (member_id, user_id))
        if not member:
            raise UserFacingError("Запись на смену не найдена.")
        if member["status"] != "active":
            raise UserFacingError("Смена уже завершена или больше не активна.")

        now = to_db(local_now())
        cursor = await tx.execute(
            """
            INSERT INTO shift_reports
                (shift_id, member_id, user_id, total_accepted, came_to_base,
                 found_by_recruiter, comment, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                member["shift_id"], member_id, user_id, total_accepted,
                came_to_base, found_by_recruiter, comment or None,
            ),
        )
        report_id = cursor.lastrowid
        cursor = await tx.execute(
            """
            UPDATE shift_members
            SET status='completed', actual_end=?, report_id=?
            WHERE id=? AND status='active'
            """,
            (now, report_id, member_id),
        )
        if cursor.rowcount != 1:
            raise UserFacingError("Состояние смены изменилось во время сохранения отчёта.")

        await _recalculate_shift_status(tx, member["shift_id"])
        await log(
            user_id,
            "SHIFT_REPORT_CREATED",
            "shift_report",
            report_id,
            f"Смена #{member['shift_id']}, принято: {total_accepted}, особняк: {came_to_base}, самостоятельно: {found_by_recruiter}",
            tx=tx,
        )
        report = await tx.fetchone("SELECT * FROM shift_reports WHERE id=?", (report_id,))
        updated_member = await tx.fetchone("SELECT * FROM shift_members WHERE id=?", (member_id,))
        shift = await tx.fetchone("SELECT * FROM shifts WHERE id=?", (member["shift_id"],))
        return FinishResult(report, updated_member, shift)


async def find_rejected_report(user_id: int, report_id: int | None = None):
    if report_id is not None:
        row = await db.fetchone(
            "SELECT * FROM shift_reports WHERE id=? AND user_id=? AND status='rejected'",
            (report_id, user_id),
        )
        if not row:
            raise UserFacingError("Отклонённый отчёт не найден или он принадлежит другому пользователю.")
        return row

    rows = await db.fetchall(
        "SELECT * FROM shift_reports WHERE user_id=? AND status='rejected' ORDER BY reviewed_at DESC, id DESC LIMIT 2",
        (user_id,),
    )
    if not rows:
        raise UserFacingError("У вас нет отклонённых отчётов для исправления.")
    if len(rows) > 1:
        raise UserFacingError("У вас несколько отклонённых отчётов. Укажите параметр «отчёт» с ID нужного отчёта.")
    return rows[0]


async def resubmit_report(
    report_id: int,
    user_id: int,
    total_accepted: int,
    came_to_base: int,
    found_by_recruiter: int,
    comment: str,
) -> FinishResult:
    _validate_report_values(total_accepted, came_to_base, found_by_recruiter)
    comment = comment.strip()
    if len(comment) > 1000:
        raise UserFacingError("Комментарий не может быть длиннее 1000 символов.")
    async with db.transaction() as tx:
        report = await tx.fetchone(
            "SELECT * FROM shift_reports WHERE id=? AND user_id=?",
            (report_id, user_id),
        )
        if not report:
            raise UserFacingError("Отчёт не найден.")
        cursor = await tx.execute(
            """
            UPDATE shift_reports
            SET total_accepted=?, came_to_base=?, found_by_recruiter=?, comment=?,
                status='pending', reviewed_by=NULL, reviewed_at=NULL, reject_reason=NULL,
                created_at=CURRENT_TIMESTAMP
            WHERE id=? AND user_id=? AND status='rejected'
            """,
            (
                total_accepted, came_to_base, found_by_recruiter, comment or None,
                report_id, user_id,
            ),
        )
        if cursor.rowcount != 1:
            raise UserFacingError("Этот отчёт уже не находится в статусе «отклонён».")

        # Разрешаем новый цикл уведомлений и напоминаний для того же отчёта.
        await tx.execute(
            """
            DELETE FROM notifications
            WHERE object_type='shift_report' AND object_id=?
              AND ((user_id=? AND type IN ('REPORT_REJECTED','REPORT_APPROVED'))
                   OR (user_id=0 AND type='REVIEW_REMINDER'))
            """,
            (report_id, user_id),
        )
        await log(
            user_id,
            "REPORT_RESUBMITTED",
            "shift_report",
            report_id,
            f"принято={total_accepted}; особняк={came_to_base}; самостоятельно={found_by_recruiter}",
            tx=tx,
        )
        updated = await tx.fetchone("SELECT * FROM shift_reports WHERE id=?", (report_id,))
        member = await tx.fetchone("SELECT * FROM shift_members WHERE id=?", (updated["member_id"],))
        shift = await tx.fetchone("SELECT * FROM shifts WHERE id=?", (updated["shift_id"],))
        return FinishResult(updated, member, shift)


async def approve_report(report_id: int, reviewer_id: int):
    async with db.transaction() as tx:
        report = await tx.fetchone("SELECT * FROM shift_reports WHERE id=?", (report_id,))
        if not report:
            raise UserFacingError("Отчёт не найден.")
        cursor = await tx.execute(
            """
            UPDATE shift_reports
            SET status='approved', reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP, reject_reason=NULL
            WHERE id=? AND status='pending'
            """,
            (reviewer_id, report_id),
        )
        if cursor.rowcount != 1:
            raise UserFacingError("Этот отчёт уже обработан другим пользователем.")
        await log(reviewer_id, "REPORT_APPROVED", "shift_report", report_id, None, tx=tx)
        return await tx.fetchone("SELECT * FROM shift_reports WHERE id=?", (report_id,))


async def reject_report(report_id: int, reviewer_id: int, reason: str):
    reason = reason.strip()
    if len(reason) > 1000:
        raise UserFacingError("Причина не может быть длиннее 1000 символов.")
    if not reason:
        raise UserFacingError("Укажите причину отклонения.")
    async with db.transaction() as tx:
        report = await tx.fetchone("SELECT * FROM shift_reports WHERE id=?", (report_id,))
        if not report:
            raise UserFacingError("Отчёт не найден.")
        cursor = await tx.execute(
            """
            UPDATE shift_reports
            SET status='rejected', reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP, reject_reason=?
            WHERE id=? AND status='pending'
            """,
            (reviewer_id, reason, report_id),
        )
        if cursor.rowcount != 1:
            raise UserFacingError("Этот отчёт уже обработан другим пользователем.")
        await log(reviewer_id, "REPORT_REJECTED", "shift_report", report_id, reason, tx=tx)
        return await tx.fetchone("SELECT * FROM shift_reports WHERE id=?", (report_id,))


async def _select_member_for_removal(tx, user_id: int, shift_id: int | None):
    if shift_id is not None:
        return await tx.fetchone(
            """
            SELECT * FROM shift_members
            WHERE user_id=? AND shift_id=? AND status IN ('booked', 'active')
            """,
            (user_id, shift_id),
        )
    rows = await tx.fetchall(
        """
        SELECT * FROM shift_members
        WHERE user_id=? AND status IN ('booked', 'active')
        ORDER BY id DESC LIMIT 2
        """,
        (user_id,),
    )
    if len(rows) > 1:
        raise UserFacingError("У рекрутера несколько текущих смен. Укажите параметр «смена» с ID нужной смены.")
    return rows[0] if rows else None


async def remove_member(actor_id: int, user_id: int, reason: str, shift_id: int | None = None):
    reason = reason.strip()
    if len(reason) > 1000:
        raise UserFacingError("Причина не может быть длиннее 1000 символов.")
    if not reason:
        raise UserFacingError("Укажите причину снятия.")
    async with db.transaction() as tx:
        member = await _select_member_for_removal(tx, user_id, shift_id)
        if not member:
            raise UserFacingError("Рекрутер не записан на указанную активную смену.")
        now = to_db(local_now())
        cursor = await tx.execute(
            """
            UPDATE shift_members
            SET status='removed', actual_end=CASE WHEN status='active' THEN ? ELSE actual_end END,
                cancel_reason=?
            WHERE id=? AND status IN ('booked', 'active')
            """,
            (now, f"Снят старшим: {reason}", member["id"]),
        )
        if cursor.rowcount != 1:
            raise UserFacingError("Состояние смены уже изменилось.")
        await tx.execute("UPDATE shifts SET slots=slots+1 WHERE id=?", (member["shift_id"],))
        await _recalculate_shift_status(tx, member["shift_id"])
        await log(actor_id, "MEMBER_REMOVED", "shift", member["shift_id"], f"user={user_id}; {reason}", tx=tx)
        return member["shift_id"]


async def cancel_shift(actor_id: int, shift_id: int, reason: str):
    reason = reason.strip()
    if len(reason) > 1000:
        raise UserFacingError("Причина не может быть длиннее 1000 символов.")
    if not reason:
        raise UserFacingError("Укажите причину отмены.")
    async with db.transaction() as tx:
        shift = await tx.fetchone("SELECT * FROM shifts WHERE id=?", (shift_id,))
        if not shift:
            raise UserFacingError("Смена не найдена.")
        cursor = await tx.execute(
            """
            UPDATE shifts SET status='cancelled'
            WHERE id=? AND status NOT IN ('completed', 'cancelled', 'missed')
            """,
            (shift_id,),
        )
        if cursor.rowcount != 1:
            raise UserFacingError("Эту смену уже нельзя отменить.")
        members = await tx.fetchall(
            "SELECT * FROM shift_members WHERE shift_id=? AND status IN ('booked', 'active')",
            (shift_id,),
        )
        await tx.execute(
            """
            UPDATE shift_members
            SET status='cancelled', cancel_reason=?,
                actual_end=CASE WHEN status='active' THEN ? ELSE actual_end END
            WHERE shift_id=? AND status IN ('booked', 'active')
            """,
            (f"Смена отменена: {reason}", to_db(local_now()), shift_id),
        )
        await log(actor_id, "SHIFT_CANCELLED", "shift", shift_id, reason, tx=tx)
        return members


async def mark_missed(member_id: int):
    async with db.transaction() as tx:
        member = await tx.fetchone("SELECT * FROM shift_members WHERE id=?", (member_id,))
        if not member or member["status"] != "booked":
            return None
        cursor = await tx.execute(
            "UPDATE shift_members SET status='missed' WHERE id=? AND status='booked'",
            (member_id,),
        )
        if cursor.rowcount != 1:
            return None
        await tx.execute("UPDATE shifts SET slots=slots+1 WHERE id=?", (member["shift_id"],))
        await _recalculate_shift_status(tx, member["shift_id"])
        await log(member["user_id"], "SHIFT_MISSED", "shift", member["shift_id"], None, tx=tx)
        return member


async def get_schedule(day_start, day_end):
    return await db.fetchall(
        """
        SELECT s.*,
               SUM(CASE WHEN sm.status='booked' THEN 1 ELSE 0 END) AS booked_count,
               SUM(CASE WHEN sm.status='active' THEN 1 ELSE 0 END) AS active_count,
               GROUP_CONCAT(CASE WHEN sm.status IN ('booked','active') THEN sm.user_id END) AS member_ids
        FROM shifts s
        LEFT JOIN shift_members sm ON sm.shift_id=s.id
        WHERE s.scheduled_start BETWEEN ? AND ?
        GROUP BY s.id
        ORDER BY s.scheduled_start
        """,
        (to_db(day_start), to_db(day_end)),
    )
