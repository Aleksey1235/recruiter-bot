"""Учёт рекламы. Все изменения состояния выполняются под BEGIN IMMEDIATE.

Изображения здесь не хранятся: только SHA-256 и идентификаторы Discord.
Выбор проверки записывается до подачи; отмена переносит требование фото.
"""
import secrets
import sqlite3
from datetime import timedelta

import config
from database.db import db, ensure_user, log
from services.errors import UserFacingError
from utils.time_utils import utc_now, parse_db, to_db, period_start, local_to_utc_naive

OPEN_STATUSES = ("prepared", "uploading")
STATUS_LABELS = {
    "prepared": "Ожидает публикации / фото", "uploading": "Фото передаётся в Discord",
    "pending": "Ожидает проверки", "counted": "Учтено без проверки",
    "approved": "Подтверждено по фото", "rejected": "Отклонено", "cancelled": "Отменено",
}


async def get_attempt(attempt_id: int):
    return await db.fetchone("SELECT * FROM ad_attempts WHERE id=?", (attempt_id,))


async def current_attempt(user_id: int):
    return await db.fetchone(
        "SELECT * FROM ad_attempts WHERE user_id=? AND status IN ('prepared','uploading')", (user_id,)
    )


async def _owned(tx, attempt_id, user_id):
    row = await tx.fetchone("SELECT * FROM ad_attempts WHERE id=? AND user_id=?", (attempt_id, user_id))
    if not row:
        raise UserFacingError("Попытка не найдена или принадлежит другому рекрутеру.")
    return row


async def _active_member(tx, user_id):
    rows = await tx.fetchall(
        "SELECT sm.id, sm.shift_id FROM shift_members sm JOIN shifts s ON s.id=sm.shift_id "
        "WHERE sm.user_id=? AND sm.status='active' AND s.status='active' LIMIT 2", (user_id,)
    )
    if len(rows) != 1:
        raise UserFacingError("Для подачи рекламы нужна ровно одна активная смена. Начните смену через панель.")
    return rows[0]


async def _check_interval(tx, user_id):
    last = await tx.fetchone(
        "SELECT MAX(published_at) AS stamp FROM ad_attempts WHERE user_id=?", (user_id,)
    )
    stamp = parse_db(last["stamp"]) if last else None
    if stamp:
        until = stamp + timedelta(minutes=config.ADS_INTERVAL_MINUTES)
        seconds = int((until - utc_now()).total_seconds())
        if seconds > 0:
            raise UserFacingError(f"Между публикациями — {config.ADS_INTERVAL_MINUTES} мин. Подождите ещё {seconds} сек.")


async def prepare(user_id: int, username: str, discord_name: str):
    async with db.transaction() as tx:
        existing = await tx.fetchone(
            "SELECT * FROM ad_attempts WHERE user_id=? AND status IN ('prepared','uploading')", (user_id,)
        )
        if existing:
            return existing  # Никакого нового random при повторном нажатии.
        member = await _active_member(tx, user_id)
        await _check_interval(tx, user_id)
        await ensure_user(user_id, username=username, tx=tx)
        state = await tx.fetchone("SELECT proof_required FROM ad_state WHERE user_id=?", (user_id,))
        required = bool(state and state["proof_required"]) or secrets.randbelow(100) < config.ADS_CHECK_PERCENT
        cursor = await tx.execute(
            "INSERT INTO ad_attempts(user_id, discord_name, family_name, shift_id, member_id, requires_proof) "
            "VALUES(?,?,?,?,?,?)",
            (user_id, discord_name[:100], config.ADS_FAMILY_NAME, member["shift_id"], member["id"], int(required)),
        )
        await log(user_id, "AD_PREPARE", "advertisement", cursor.lastrowid, f"photo={int(required)}", tx=tx)
        return await tx.fetchone("SELECT * FROM ad_attempts WHERE id=?", (cursor.lastrowid,))


async def confirm(attempt_id: int, user_id: int):
    async with db.transaction() as tx:
        row = await _owned(tx, attempt_id, user_id)
        if row["status"] == "counted":
            return row  # Повторная доставка того же нажатия безопасна.
        if row["status"] != "prepared":
            raise UserFacingError("Состояние попытки изменилось. Откройте текущую попытку.")
        if row["requires_proof"]:
            raise UserFacingError("Для этой публикации требуется фото; подтвердить её без фото нельзя.")
        member = await _active_member(tx, user_id)
        if member["id"] != row["member_id"]:
            raise UserFacingError("Эта попытка относится к другой смене. Отмените её и начните новую.")
        await _check_interval(tx, user_id)
        await tx.execute(
            "UPDATE ad_attempts SET status='counted', published_at=CURRENT_TIMESTAMP WHERE id=? AND status='prepared'",
            (attempt_id,),
        )
        await log(user_id, "AD_COUNTED", "advertisement", attempt_id, "Без независимой проверки", tx=tx)
        return await tx.fetchone("SELECT * FROM ad_attempts WHERE id=?", (attempt_id,))


async def cancel(attempt_id: int, user_id: int, reason: str):
    reason = reason.strip()
    if not reason or len(reason) > 500:
        raise UserFacingError("Укажите причину отмены от 1 до 500 символов.")
    async with db.transaction() as tx:
        row = await _owned(tx, attempt_id, user_id)
        if row["status"] not in ("prepared", "counted"):
            raise UserFacingError("Эту попытку уже нельзя отменить самостоятельно.")
        if row["requires_proof"]:
            await tx.execute(
                "INSERT INTO ad_state(user_id,proof_required) VALUES(?,1) "
                "ON CONFLICT(user_id) DO UPDATE SET proof_required=1", (user_id,)
            )
        # published_at не очищается: отмена не сокращает интервал публикаций.
        await tx.execute("UPDATE ad_attempts SET status='cancelled',cancel_reason=? WHERE id=?", (reason, attempt_id))
        await log(user_id, "AD_CANCEL", "advertisement", attempt_id, reason, tx=tx)


def image_extension(data: bytes) -> str:
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 24:
        return "png"
    if data.startswith(b'\xff\xd8\xff') and len(data) > 4:
        return "jpg"
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP' and len(data) > 16:
        return "webp"
    raise UserFacingError("Нужен скриншот PNG, JPEG или WebP, а не другой файл.")


async def claim_upload(attempt_id: int, user_id: int, digest: str, channel_id: int, filename: str):
    async with db.transaction() as tx:
        row = await _owned(tx, attempt_id, user_id)
        if row["status"] != "prepared" or not row["requires_proof"]:
            raise UserFacingError("Для этой попытки загрузка фото сейчас недоступна. Откройте текущую попытку.")
        await _check_interval(tx, user_id)
        token = secrets.token_hex(16)
        try:
            await tx.execute(
                "UPDATE ad_attempts SET status='uploading',proof_sha256=?,proof_channel_id=?,proof_filename=?,"
                "upload_token=?,uploading_at=CURRENT_TIMESTAMP WHERE id=?",
                (digest, channel_id, filename, token, attempt_id),
            )
        except sqlite3.IntegrityError as exc:
            raise UserFacingError("Этот скриншот уже использовался. Нужен новый скриншот этой публикации.") from exc
        return await tx.fetchone("SELECT * FROM ad_attempts WHERE id=?", (attempt_id,))


async def complete_upload(attempt_id: int, token: str, message_id: int, attachment_id: int):
    async with db.transaction() as tx:
        row = await tx.fetchone("SELECT * FROM ad_attempts WHERE id=?", (attempt_id,))
        if not row or row["status"] != "uploading" or row["upload_token"] != token:
            return False
        await tx.execute(
            "UPDATE ad_attempts SET status='pending',proof_message_id=?,proof_attachment_id=?,"
            "published_at=uploading_at WHERE id=? AND status='uploading' AND upload_token=?",
            (message_id, attachment_id, attempt_id, token),
        )
        await tx.execute("INSERT INTO ad_state(user_id,proof_required) VALUES(?,0) "
                         "ON CONFLICT(user_id) DO UPDATE SET proof_required=0", (row["user_id"],))
        await log(row["user_id"], "AD_PROOF_SUBMITTED", "advertisement", attempt_id, f"message={message_id}", tx=tx)
        return True


async def release_upload(attempt_id: int, token: str):
    """Только после достоверного отказа Discord или сверки истории канала."""
    await db.execute(
        "UPDATE ad_attempts SET status='prepared',proof_sha256=NULL,proof_channel_id=NULL,proof_filename=NULL,"
        "upload_token=NULL,uploading_at=NULL WHERE id=? AND status='uploading' AND upload_token=?",
        (attempt_id, token),
    )


async def review(attempt_id: int, reviewer_id: int, approved: bool, reason: str = ""):
    reason = reason.strip()
    if not approved and (not reason or len(reason) > 500):
        raise UserFacingError("Для отклонения нужна причина от 1 до 500 символов.")
    async with db.transaction() as tx:
        row = await tx.fetchone("SELECT * FROM ad_attempts WHERE id=?", (attempt_id,))
        if not row or row["status"] != "pending":
            raise UserFacingError("Эта проверка уже обработана или не найдена.")
        if row["user_id"] == reviewer_id:
            raise UserFacingError("Нельзя проверять собственную рекламу.")
        if not row["proof_message_id"] or not row["proof_attachment_id"]:
            raise UserFacingError("Подтверждение ещё не сохранено в Discord.")
        await tx.execute(
            "UPDATE ad_attempts SET status=?,reviewed_by=?,reviewed_at=CURRENT_TIMESTAMP,reject_reason=? "
            "WHERE id=? AND status='pending'",
            ("approved" if approved else "rejected", reviewer_id, None if approved else reason, attempt_id),
        )
        await log(reviewer_id, "AD_APPROVED" if approved else "AD_REJECTED", "advertisement", attempt_id, reason or None, tx=tx)
        return await tx.fetchone("SELECT * FROM ad_attempts WHERE id=?", (attempt_id,))


async def summary(user_id: int | None, period: str = "неделя", shift_id: int | None = None, *, start=None, end=None):
    filters, params = [], []
    if user_id is not None:
        filters.append("user_id=?"); params.append(user_id)
    if shift_id is not None:
        filters.append("shift_id=?"); params.append(shift_id)
    else:
        start = start if start is not None else period_start(period)
        if start:
            filters.append("COALESCE(published_at,prepared_at)>=?")
            params.append(to_db(local_to_utc_naive(start)))
        if end is not None:
            filters.append("COALESCE(published_at,prepared_at)<?")
            params.append(to_db(local_to_utc_naive(end)))
    where = " WHERE " + " AND ".join(filters) if filters else ""
    row = await db.fetchone(
        "SELECT COUNT(*) AS attempts, "
        "SUM(status='counted') AS counted, SUM(status='approved') AS approved, SUM(status='pending') AS pending, "
        "SUM(status='rejected') AS rejected, SUM(status='cancelled') AS cancelled, "
        "SUM(status IN ('prepared','uploading')) AS unfinished FROM ad_attempts" + where, tuple(params)
    )
    result = {key: int(row[key] or 0) for key in row.keys()}
    result["total"] = result["counted"] + result["approved"]
    return result


async def history(user_id: int, limit: int = 20):
    return await db.fetchall("SELECT * FROM ad_attempts WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))


async def pending(limit: int = 25):
    return await db.fetchall("SELECT * FROM ad_attempts WHERE status='pending' ORDER BY id LIMIT ?", (limit,))


async def rankings(period: str):
    start = period_start(period)
    clause, params = (" AND published_at>=?", (to_db(local_to_utc_naive(start)),)) if start else ("", ())
    return await db.fetchall(
        "SELECT user_id, SUM(status='counted') AS counted, SUM(status='approved') AS approved, COUNT(*) AS total "
        "FROM ad_attempts WHERE status IN ('counted','approved')" + clause +
        " GROUP BY user_id ORDER BY total DESC,user_id LIMIT 20", params
    )


async def cleanup_candidates(limit: int = 100):
    threshold = to_db(utc_now() - timedelta(days=config.ADS_RETENTION_DAYS))
    return await db.fetchall(
        "SELECT * FROM ad_attempts WHERE status IN ('approved','rejected') AND reviewed_at<=? "
        "AND proof_message_id IS NOT NULL AND proof_deleted_at IS NULL ORDER BY id LIMIT ?", (threshold, limit)
    )


async def mark_proof_deleted(attempt_id: int):
    await db.execute("UPDATE ad_attempts SET proof_deleted_at=CURRENT_TIMESTAMP WHERE id=?", (attempt_id,))


async def anomalies():
    rows = await db.fetchall(
        "SELECT id FROM ad_attempts WHERE (status IN ('pending','approved','rejected') AND "
        "(proof_message_id IS NULL OR proof_attachment_id IS NULL OR proof_sha256 IS NULL)) "
        "OR (status='counted' AND requires_proof<>0) "
        "OR (status IN ('counted','pending','approved','rejected') AND published_at IS NULL) LIMIT 10"
    )
    return [f"Реклама #{r['id']}: несогласованное состояние" for r in rows]
