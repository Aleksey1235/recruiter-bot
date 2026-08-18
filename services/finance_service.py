import config
from database.db import db, ensure_user, log
from services.errors import UserFacingError
from utils.formatting import normalize_amount


async def get_balance(user_id: int):
    row = await db.fetchone(
        """
        SELECT
            COALESCE(SUM(CASE WHEN type='salary' THEN amount ELSE 0 END), 0) AS accrued,
            COALESCE(SUM(CASE WHEN type='pay' THEN amount ELSE 0 END), 0) AS paid
        FROM finances WHERE user_id=?
        """,
        (user_id,),
    )
    accrued = float(row["accrued"] or 0)
    paid = float(row["paid"] or 0)
    return accrued, paid, accrued - paid


async def get_general_balance():
    row = await db.fetchone(
        """
        SELECT
            COALESCE(SUM(CASE WHEN type='salary' THEN amount ELSE 0 END), 0) AS accrued,
            COALESCE(SUM(CASE WHEN type='pay' THEN amount ELSE 0 END), 0) AS paid
        FROM finances
        """
    )
    accrued = float(row["accrued"] or 0)
    paid = float(row["paid"] or 0)
    return accrued, paid, accrued - paid


async def accrue(user_id: int, username: str, amount, reason: str, actor_id: int):
    try:
        amount = normalize_amount(amount)
    except ValueError as exc:
        raise UserFacingError(str(exc)) from exc
    if amount <= 0:
        raise UserFacingError("Сумма должна быть больше 0.")
    if amount > config.MAX_FINANCE_AMOUNT:
        raise UserFacingError(f"Сумма превышает допустимый максимум: {config.MAX_FINANCE_AMOUNT:,.2f}.")
    reason = reason.strip()
    if len(reason) > 500:
        raise UserFacingError("Причина не может быть длиннее 500 символов.")

    async with db.transaction() as tx:
        await ensure_user(user_id, username=username, tx=tx)
        cursor = await tx.execute(
            """
            INSERT INTO finances (user_id, amount, type, reason, status, created_by)
            VALUES (?, ?, 'salary', ?, 'accrued', ?)
            """,
            (user_id, amount, reason or None, actor_id),
        )
        fin_id = cursor.lastrowid
        await tx.execute(
            "UPDATE users SET total_salary=COALESCE(total_salary,0)+? WHERE discord_id=?",
            (amount, user_id),
        )
        await log(actor_id, "FIN_ACCRUE", "finance", fin_id, f"user={user_id}; amount={amount}; {reason}", tx=tx)
    return fin_id, await get_balance(user_id)


async def pay(user_id: int, username: str, amount, actor_id: int):
    try:
        amount = normalize_amount(amount)
    except ValueError as exc:
        raise UserFacingError(str(exc)) from exc
    if amount <= 0:
        raise UserFacingError("Сумма должна быть больше 0.")
    if amount > config.MAX_FINANCE_AMOUNT:
        raise UserFacingError(f"Сумма превышает допустимый максимум: {config.MAX_FINANCE_AMOUNT:,.2f}.")

    async with db.transaction() as tx:
        await ensure_user(user_id, username=username, tx=tx)
        balance = await tx.fetchone(
            """
            SELECT
                COALESCE(SUM(CASE WHEN type='salary' THEN amount ELSE 0 END), 0) AS accrued,
                COALESCE(SUM(CASE WHEN type='pay' THEN amount ELSE 0 END), 0) AS paid
            FROM finances WHERE user_id=?
            """,
            (user_id,),
        )
        available = float(balance["accrued"] or 0) - float(balance["paid"] or 0)
        if amount > available + 1e-9:
            raise UserFacingError(f"Нельзя выплатить больше доступного. Доступно: {available:.2f}")

        cursor = await tx.execute(
            """
            INSERT INTO finances (user_id, amount, type, reason, status, created_by)
            VALUES (?, ?, 'pay', 'Выплата', 'paid', ?)
            """,
            (user_id, amount, actor_id),
        )
        fin_id = cursor.lastrowid
        await tx.execute(
            "UPDATE users SET paid_salary=COALESCE(paid_salary,0)+? WHERE discord_id=?",
            (amount, user_id),
        )
        await log(actor_id, "FIN_PAY", "finance", fin_id, f"user={user_id}; amount={amount}", tx=tx)
    return fin_id, await get_balance(user_id)


async def reconcile_user(user_id: int, fix: bool = False):
    ledger = await get_balance(user_id)
    user = await db.fetchone("SELECT * FROM users WHERE discord_id=?", (user_id,))
    if not user:
        return ledger, (0.0, 0.0), False
    cached = (float(user["total_salary"] or 0), float(user["paid_salary"] or 0))
    mismatch = abs(cached[0] - ledger[0]) > 0.005 or abs(cached[1] - ledger[1]) > 0.005
    if fix and mismatch:
        await db.execute(
            "UPDATE users SET total_salary=?, paid_salary=? WHERE discord_id=?",
            (ledger[0], ledger[1], user_id),
        )
    return ledger, cached, mismatch


async def reconcile_all(fix: bool = False):
    rows = await db.fetchall(
        """
        SELECT
            u.discord_id,
            COALESCE(u.total_salary, 0) AS cached_accrued,
            COALESCE(u.paid_salary, 0) AS cached_paid,
            COALESCE(SUM(CASE WHEN f.type='salary' THEN f.amount ELSE 0 END), 0) AS ledger_accrued,
            COALESCE(SUM(CASE WHEN f.type='pay' THEN f.amount ELSE 0 END), 0) AS ledger_paid
        FROM users u
        LEFT JOIN finances f ON f.user_id=u.discord_id
        GROUP BY u.discord_id
        """
    )
    mismatches = []
    for row in rows:
        ledger_accrued = float(row["ledger_accrued"] or 0)
        ledger_paid = float(row["ledger_paid"] or 0)
        cached_accrued = float(row["cached_accrued"] or 0)
        cached_paid = float(row["cached_paid"] or 0)
        if abs(cached_accrued - ledger_accrued) > 0.005 or abs(cached_paid - ledger_paid) > 0.005:
            ledger = (ledger_accrued, ledger_paid, ledger_accrued - ledger_paid)
            cached = (cached_accrued, cached_paid)
            mismatches.append((row["discord_id"], ledger, cached))

    if fix and mismatches:
        async with db.transaction() as tx:
            for user_id, ledger, _cached in mismatches:
                await tx.execute(
                    "UPDATE users SET total_salary=?, paid_salary=? WHERE discord_id=?",
                    (ledger[0], ledger[1], user_id),
                )
            await log(
                None,
                "FIN_RECONCILE",
                "system",
                None,
                f"Исправлено профилей: {len(mismatches)}",
                tx=tx,
            )
    return mismatches
