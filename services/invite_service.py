import sqlite3

import config
from database.db import db, ensure_user, log
from services.errors import UserFacingError
from utils.formatting import normalize_amount


async def create_invite(invited_user_id: int, invited_by: int, inviter_name: str, static_id: str, full_name: str, checklist: dict):
    static_id = static_id.strip()
    full_name = full_name.strip()
    if not static_id:
        raise UserFacingError("Статик не может быть пустым.")
    if len(static_id) > 32:
        raise UserFacingError("Статик не может быть длиннее 32 символов.")
    if not full_name:
        raise UserFacingError("Имя и фамилия не могут быть пустыми.")
    if len(full_name) > 100:
        raise UserFacingError("Имя и фамилия не могут быть длиннее 100 символов.")

    async with db.transaction() as tx:
        existing = await tx.fetchone("SELECT * FROM invites WHERE static_id=?", (static_id,))
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
                    reject_reason=NULL, created_at=CURRENT_TIMESTAMP
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


async def approve_invite(invite_id: int, reviewer_id: int, amount=0):
    try:
        amount = normalize_amount(amount)
    except ValueError as exc:
        raise UserFacingError(str(exc)) from exc
    if amount < 0:
        raise UserFacingError("Сумма начисления не может быть отрицательной.")
    if amount > config.MAX_FINANCE_AMOUNT:
        raise UserFacingError(f"Сумма превышает допустимый максимум: {config.MAX_FINANCE_AMOUNT:,.2f}.")

    async with db.transaction() as tx:
        invite = await tx.fetchone("SELECT * FROM invites WHERE id=?", (invite_id,))
        if not invite:
            raise UserFacingError("Инвайт не найден.")
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
