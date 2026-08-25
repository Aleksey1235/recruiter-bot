import sqlite3

import config
from database.db import db, log
from services.errors import UserFacingError
from utils.ids import maybe_positive_sqlite_int, parse_positive_sqlite_int


MAX_TAG_LENGTH = 100
MAX_FULL_NAME_LENGTH = 100
MAX_REASON_LENGTH = 1000
MAX_EVIDENCE_LENGTH = 1500
MAX_NOTES_LENGTH = 2000
MAX_REMOVE_REASON_LENGTH = 1000


def _clean_optional(value: str | None, max_length: int, label: str) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    if len(value) > max_length:
        raise UserFacingError(f"{label} не может быть длиннее {max_length} символов.")
    return value


def _clean_required(value: str | None, max_length: int, label: str) -> str:
    value = (value or "").strip()
    if not value:
        raise UserFacingError(f"{label} не может быть пустым.")
    if len(value) > max_length:
        raise UserFacingError(f"{label} не может быть длиннее {max_length} символов.")
    return value


def identity_text(row) -> str:
    """Human-readable identity. Always includes stored tag and Discord ID."""
    tag = (row["discord_tag"] or "Неизвестный тег").strip()
    discord_id = int(row["discord_id"])
    return f"{tag} • Discord ID: {discord_id}"


def blocked_message(row) -> str:
    static = row["static_id"] or "—"
    reason = row["reason"] or "Причина не указана"
    return (
        "Пользователь находится в чёрном списке.\n"
        f"Пользователь: {identity_text(row)}\n"
        f"Статик: {static}\n"
        f"Причина: {reason}"
    )


async def get_entry(entry_id: int, tx=None):
    executor = tx if tx is not None else db
    return await executor.fetchone("SELECT * FROM blacklist WHERE id=?", (int(entry_id),))


async def get_active_match(discord_id: int | None = None, static_id: str | None = None, tx=None):
    executor = tx if tx is not None else db
    static_id = (static_id or "").strip() or None
    safe_discord_id = maybe_positive_sqlite_int(discord_id) if discord_id is not None else None
    if safe_discord_id is None and static_id is None:
        return None
    if safe_discord_id is not None and static_id is not None:
        return await executor.fetchone(
            """
            SELECT * FROM blacklist
            WHERE status='active' AND (discord_id=? OR static_id=?)
            ORDER BY CASE WHEN discord_id=? THEN 0 ELSE 1 END, id DESC
            LIMIT 1
            """,
            (safe_discord_id, static_id, safe_discord_id),
        )
    if safe_discord_id is not None:
        return await executor.fetchone(
            "SELECT * FROM blacklist WHERE status='active' AND discord_id=? ORDER BY id DESC LIMIT 1",
            (safe_discord_id,),
        )
    return await executor.fetchone(
        "SELECT * FROM blacklist WHERE status='active' AND static_id=? ORDER BY id DESC LIMIT 1",
        (static_id,),
    )


async def add_entry(
    discord_id: int,
    discord_tag: str,
    static_id: str | None,
    full_name: str | None,
    reason: str,
    evidence: str | None,
    notes: str | None,
    actor_id: int,
):
    try:
        discord_id = parse_positive_sqlite_int(discord_id, label="Discord ID")
    except ValueError as exc:
        raise UserFacingError(str(exc)) from exc
    if discord_id == int(actor_id):
        raise UserFacingError("Нельзя добавить самого себя в ЧС.")

    discord_tag = _clean_required(discord_tag, MAX_TAG_LENGTH, "Discord-тег")
    static_id = _clean_optional(static_id, config.MAX_STATIC_ID_LENGTH, "Статик")
    full_name = _clean_optional(full_name, MAX_FULL_NAME_LENGTH, "Имя и фамилия")
    reason = _clean_required(reason, MAX_REASON_LENGTH, "Причина")
    evidence = _clean_optional(evidence, MAX_EVIDENCE_LENGTH, "Доказательство")
    notes = _clean_optional(notes, MAX_NOTES_LENGTH, "Заметка")

    async with db.transaction() as tx:
        existing = await get_active_match(discord_id, static_id, tx=tx)
        if existing:
            matched_by = "Discord ID" if int(existing["discord_id"]) == discord_id else "статик"
            raise UserFacingError(
                f"Этот человек уже находится в ЧС (запись #{existing['id']}, совпадение по {matched_by})."
            )
        try:
            cursor = await tx.execute(
                """
                INSERT INTO blacklist
                    (discord_id, discord_tag, static_id, full_name, reason, evidence, notes, status, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
                """,
                (discord_id, discord_tag, static_id, full_name, reason, evidence, notes, actor_id),
            )
        except sqlite3.IntegrityError as exc:
            raise UserFacingError("Для этого Discord ID или статика уже существует активная запись ЧС.") from exc
        entry_id = cursor.lastrowid
        await log(
            actor_id,
            "BLACKLIST_ADD",
            "blacklist",
            entry_id,
            f"{discord_tag} | discord_id={discord_id} | static={static_id or '—'} | reason={reason[:500]}",
            tx=tx,
        )
        return await tx.fetchone("SELECT * FROM blacklist WHERE id=?", (entry_id,))


async def remove_entry(entry_id: int, actor_id: int, reason: str):
    reason = _clean_required(reason, MAX_REMOVE_REASON_LENGTH, "Причина снятия")
    async with db.transaction() as tx:
        entry = await get_entry(entry_id, tx=tx)
        if not entry:
            raise UserFacingError("Запись ЧС не найдена.")
        if entry["status"] != "active":
            raise UserFacingError("Эта запись уже снята с ЧС.")
        cursor = await tx.execute(
            """
            UPDATE blacklist
            SET status='removed', removed_by=?, removed_at=CURRENT_TIMESTAMP, remove_reason=?
            WHERE id=? AND status='active'
            """,
            (actor_id, reason, int(entry_id)),
        )
        if cursor.rowcount != 1:
            raise UserFacingError("Состояние записи изменилось. Повторите попытку.")
        await log(
            actor_id,
            "BLACKLIST_REMOVE",
            "blacklist",
            int(entry_id),
            f"discord_id={entry['discord_id']} | reason={reason[:500]}",
            tx=tx,
        )
        return await tx.fetchone("SELECT * FROM blacklist WHERE id=?", (int(entry_id),))


async def update_entry_details(entry_id: int, actor_id: int, evidence: str | None = None, note: str | None = None):
    evidence = _clean_optional(evidence, MAX_EVIDENCE_LENGTH, "Доказательство")
    note = _clean_optional(note, 1000, "Заметка")
    if evidence is None and note is None:
        raise UserFacingError("Укажите доказательство или заметку.")
    from utils.time_utils import local_now
    async with db.transaction() as tx:
        entry = await get_entry(entry_id, tx=tx)
        if not entry:
            raise UserFacingError("Запись ЧС не найдена.")
        notes = entry["notes"] or ""
        if note:
            stamp = local_now().strftime("%d.%m.%Y %H:%M")
            addition = f"\n[{stamp}] <{actor_id}>: {note}"
            notes = (notes + addition)[-MAX_NOTES_LENGTH:]
        new_evidence = evidence if evidence is not None else entry["evidence"]
        await tx.execute("UPDATE blacklist SET evidence=?, notes=? WHERE id=?", (new_evidence, notes or None, int(entry_id)))
        details = []
        if evidence is not None: details.append("обновлено доказательство")
        if note is not None: details.append("добавлена заметка")
        await log(actor_id, "BLACKLIST_UPDATE", "blacklist", int(entry_id), "; ".join(details), tx=tx)
        return await tx.fetchone("SELECT * FROM blacklist WHERE id=?", (int(entry_id),))


async def search_entries(query: str, include_removed: bool = True, limit: int = 20):
    query = (query or "").strip()
    if not query:
        raise UserFacingError("Введите Discord ID, статик, тег или имя.")
    limit = max(1, min(int(limit), 50))
    where_status = "" if include_removed else "AND status='active'"
    numeric_value = maybe_positive_sqlite_int(query) if query.isdigit() else None
    params = []
    clauses = []
    if numeric_value is not None:
        clauses.extend(["discord_id=?", "id=?"])
        params.extend([numeric_value, numeric_value])
    like = f"%{query}%"
    clauses.extend(["static_id LIKE ?", "discord_tag LIKE ?", "full_name LIKE ?"])
    params.extend([like, like, like])
    sql = f"""
        SELECT * FROM blacklist
        WHERE ({' OR '.join(clauses)}) {where_status}
        ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END, id DESC
        LIMIT ?
    """
    params.append(limit)
    return await db.fetchall(sql, tuple(params))


async def list_active(limit: int = 25):
    limit = max(1, min(int(limit), 50))
    return await db.fetchall(
        "SELECT * FROM blacklist WHERE status='active' ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    )


async def list_history(limit: int = 25):
    limit = max(1, min(int(limit), 50))
    return await db.fetchall(
        "SELECT * FROM blacklist ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    )


async def count_active() -> int:
    row = await db.fetchone("SELECT COUNT(*) AS count FROM blacklist WHERE status='active'")
    return int(row["count"] or 0)
