import sqlite3

import config
from database.db import db, ensure_user, log
from services.errors import UserFacingError
from services import blacklist_service
from utils.formatting import normalize_amount
from utils.time_utils import local_now


async def assert_target_not_blacklisted(discord_id: int | None, static_id: str | None, actor_id: int, action: str = "BLACKLIST_BLOCK_INVITE"):
    """Public pre-check used by UI/services. Blocked attempts are logged."""
    entry = await blacklist_service.get_active_match(discord_id, static_id)
    if not entry:
        return
    await log(
        actor_id,
        action,
        "blacklist",
        entry["id"],
        f"discord_id={discord_id or '—'} | static={static_id or '—'} | blacklist_id={entry['id']}",
    )
    raise UserFacingError(blacklist_service.blocked_message(entry))


async def _assert_not_blacklisted_tx(discord_id: int | None, static_id: str | None, tx):
    entry = await blacklist_service.get_active_match(discord_id, static_id, tx=tx)
    if entry:
        raise UserFacingError(blacklist_service.blocked_message(entry))


async def create_invite(invited_user_id: int, invited_by: int, inviter_name: str, static_id: str, full_name: str, checklist: dict):
    if invited_user_id == invited_by:
        raise UserFacingError("Нельзя создать инвайт на самого себя.")
    static_id = static_id.strip()
    full_name = full_name.strip()
    if not static_id:
        raise UserFacingError("Статик не может быть пустым.")
    if len(static_id) > config.MAX_STATIC_ID_LENGTH:
        raise UserFacingError(f"Статик не может быть длиннее {config.MAX_STATIC_ID_LENGTH} символов.")
    if not full_name:
        raise UserFacingError("Имя и фамилия не могут быть пустыми.")
    if len(full_name) > 100:
        raise UserFacingError("Имя и фамилия не могут быть длиннее 100 символов.")

    allowed_checklist = {"ticket", "last_name", "organization", "fraction", "info"}
    if set(checklist) != allowed_checklist or any(checklist[key] not in ("yes", "no") for key in allowed_checklist):
        raise UserFacingError("Чек-лист инвайта содержит некорректные данные. Заполните его заново.")

    await assert_target_not_blacklisted(invited_user_id, static_id, invited_by, "BLACKLIST_BLOCK_INVITE")

    async with db.transaction() as tx:
        await _assert_not_blacklisted_tx(invited_user_id, static_id, tx)
        existing = await tx.fetchone("SELECT * FROM invites WHERE static_id=?", (static_id,))
        by_user = await tx.fetchone(
            "SELECT * FROM invites WHERE user_id=? AND status IN ('pending','accepted') ORDER BY id DESC LIMIT 1",
            (invited_user_id,),
        )
        if by_user and (not existing or by_user["id"] != existing["id"]):
            raise UserFacingError(
                f"Этот Discord-пользователь уже есть в инвайтах со статиком {by_user['static_id']} "
                f"и статусом {by_user['status']}."
            )
        await ensure_user(invited_by, username=inviter_name, tx=tx)

        if existing:
            if existing["status"] != "rejected":
                raise UserFacingError(
                    f"Статик {static_id} уже есть в базе. Статус: {existing['status']}."
                )
            if existing["invited_by"] != invited_by:
                raise UserFacingError(
                    f"Статик {static_id} уже был отправлен другим рекрутером и отклонён. Обратитесь к старшему составу."
                )
            cursor = await tx.execute(
                """
                UPDATE invites
                SET user_id=?, full_name=?, ticket=?, last_name_changed=?, organization=?,
                    fraction=?, info=?, status='pending', reviewed_by=NULL, reviewed_at=NULL,
                    reject_reason=NULL, message_id=NULL, created_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='rejected'
                """,
                (
                    invited_user_id, full_name, checklist["ticket"], checklist["last_name"],
                    checklist["organization"], checklist["fraction"], checklist["info"],
                    existing["id"],
                ),
            )
            if cursor.rowcount != 1:
                raise UserFacingError("Состояние инвайта изменилось. Повторите попытку.")
            invite_id = existing["id"]
            await tx.execute(
                "DELETE FROM notifications WHERE user_id=? AND object_type='invite' AND object_id=? AND type IN ('INVITE_REJECTED','INVITE_APPROVED')",
                (invited_by, invite_id),
            )
            await log(invited_by, "INVITE_RESUBMIT", "invite", invite_id, f"Статик: {static_id}", tx=tx)
            return invite_id

        try:
            cursor = await tx.execute(
                """
                INSERT INTO invites
                    (user_id, static_id, invited_by, full_name, ticket,
                     last_name_changed, organization, fraction, info, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    invited_user_id, static_id, invited_by, full_name,
                    checklist["ticket"], checklist["last_name"], checklist["organization"],
                    checklist["fraction"], checklist["info"],
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise UserFacingError(f"Статик {static_id} уже есть в базе.") from exc
        invite_id = cursor.lastrowid
        await log(invited_by, "INVITE_CREATE", "invite", invite_id, f"Статик: {static_id}", tx=tx)
        return invite_id




async def set_invite_message_id(invite_id: int, message_id: int | None):
    await db.execute("UPDATE invites SET message_id=? WHERE id=?", (message_id, invite_id))

async def approve_invite(invite_id: int, reviewer_id: int, amount=0):
    try:
        amount = normalize_amount(amount)
    except ValueError as exc:
        raise UserFacingError(str(exc)) from exc
    if amount < 0:
        raise UserFacingError("Сумма начисления не может быть отрицательной.")
    if amount > config.MAX_FINANCE_AMOUNT:
        raise UserFacingError(f"Сумма превышает допустимый максимум: {config.MAX_FINANCE_AMOUNT:,.2f}.")

    preview = await db.fetchone("SELECT user_id, static_id FROM invites WHERE id=?", (invite_id,))
    if not preview:
        raise UserFacingError("Инвайт не найден.")
    await assert_target_not_blacklisted(preview["user_id"], preview["static_id"], reviewer_id, "BLACKLIST_BLOCK_APPROVE")

    async with db.transaction() as tx:
        invite = await tx.fetchone("SELECT * FROM invites WHERE id=?", (invite_id,))
        if not invite:
            raise UserFacingError("Инвайт не найден.")
        await _assert_not_blacklisted_tx(invite["user_id"], invite["static_id"], tx)
        cursor = await tx.execute(
            """
            UPDATE invites
            SET status='accepted', reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP, reject_reason=NULL
            WHERE id=? AND status='pending'
            """,
            (reviewer_id, invite_id),
        )
        if cursor.rowcount != 1:
            raise UserFacingError("Этот инвайт уже обработан другим пользователем.")

        fin_id = None
        if amount > 0:
            await ensure_user(invite["invited_by"], tx=tx)
            fin = await tx.execute(
                """
                INSERT INTO finances (user_id, amount, type, reason, status, created_by)
                VALUES (?, ?, 'salary', 'Инвайт', 'accrued', ?)
                """,
                (invite["invited_by"], amount, reviewer_id),
            )
            fin_id = fin.lastrowid
            await tx.execute(
                "UPDATE users SET total_salary=COALESCE(total_salary,0)+? WHERE discord_id=?",
                (amount, invite["invited_by"]),
            )
        await log(
            reviewer_id,
            "INVITE_ACCEPT",
            "invite",
            invite_id,
            f"Начислено: {amount}",
            tx=tx,
        )
        updated = await tx.fetchone("SELECT * FROM invites WHERE id=?", (invite_id,))
        return updated, fin_id


async def reject_invite(invite_id: int, reviewer_id: int, reason: str):
    reason = reason.strip()
    if len(reason) > 1000:
        raise UserFacingError("Причина не может быть длиннее 1000 символов.")
    if not reason:
        raise UserFacingError("Укажите причину отклонения.")
    async with db.transaction() as tx:
        invite = await tx.fetchone("SELECT * FROM invites WHERE id=?", (invite_id,))
        if not invite:
            raise UserFacingError("Инвайт не найден.")
        cursor = await tx.execute(
            """
            UPDATE invites
            SET status='rejected', reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP, reject_reason=?
            WHERE id=? AND status='pending'
            """,
            (reviewer_id, reason, invite_id),
        )
        if cursor.rowcount != 1:
            raise UserFacingError("Этот инвайт уже обработан другим пользователем.")
        await log(reviewer_id, "INVITE_REJECT", "invite", invite_id, reason, tx=tx)
        return await tx.fetchone("SELECT * FROM invites WHERE id=?", (invite_id,))


async def add_invite_note(invite_id: int, text: str, actor_id: int, actor_name: str):
    text = text.strip()
    if not text:
        raise UserFacingError("Заметка не может быть пустой.")
    if len(text) > 1000:
        raise UserFacingError("Заметка не может быть длиннее 1000 символов.")
    stamp = local_now().strftime("%d.%m.%Y %H:%M")
    addition = f"\n[{stamp}] {actor_name}: {text}"
    async with db.transaction() as tx:
        invite = await tx.fetchone("SELECT * FROM invites WHERE id=?", (invite_id,))
        if not invite:
            raise UserFacingError("Инвайт не найден.")
        notes = (invite["notes"] or "") + addition
        if len(notes) > 20_000:
            notes = notes[-20_000:]
        await tx.execute("UPDATE invites SET notes=? WHERE id=?", (notes, invite_id))
        await log(actor_id, "INVITE_NOTE", "invite", invite_id, text, tx=tx)
    return notes
