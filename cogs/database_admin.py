from __future__ import annotations

import logging

import disnake
from disnake.ext import commands

from services.database_service import (
    add_user_note,
    get_finance_operation,
    get_user_overview,
    list_user_finances,
    list_user_invites,
    list_user_logs,
    list_user_reports,
    list_user_shifts,
    search_users,
    update_user_static,
)
from services.errors import UserFacingError
from utils.checks import is_admin
from utils.formatting import money

logger = logging.getLogger(__name__)


def _clip(value, limit=1000):
    text = str(value or "—")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _status(value: str | None) -> str:
    icons = {
        "open": "🟢",
        "booked": "🟡",
        "active": "🟢",
        "completed": "✅",
        "cancelled": "⚫",
        "removed": "🟠",
        "missed": "🔴",
        "pending": "🟡",
        "approved": "✅",
        "accepted": "✅",
        "rejected": "❌",
        "accrued": "➕",
        "paid": "💸",
    }
    return f"{icons.get(value, '•')} {value or '—'}"


async def build_user_embed(member, overview: dict) -> disnake.Embed:
    user = overview["user"]
    shifts = overview["shifts"]
    reports = overview["reports"]
    invites = overview["invites"]

    embed = disnake.Embed(
        title="🗄️ БАЗА — КАРТОЧКА ПОЛЬЗОВАТЕЛЯ",
        description=f"{member.mention}\n`{member.id}`",
        color=disnake.Color.blurple(),
    )
    try:
        embed.set_thumbnail(url=member.display_avatar.url)
    except Exception:
        pass

    embed.add_field(
        name="👤 Профиль",
        value=(
            f"Имя в БД: **{_clip(user.get('username'), 100)}**\n"
            f"Статик: **{_clip(user.get('static_id'), 100)}**\n"
            f"Роль в БД: **{_clip(user.get('role'), 50)}**\n"
            f"Уровень: **{user.get('level') or 1}** | Варны: **{user.get('warns') or 0}**"
        ),
        inline=False,
    )
    embed.add_field(
        name="📋 Смены",
        value=(
            f"Всего: **{shifts.get('total') or 0}** | Завершено: **{shifts.get('completed') or 0}**\n"
            f"Забронировано: **{shifts.get('booked') or 0}** | Активно: **{shifts.get('active') or 0}**\n"
            f"Пропущено: **{shifts.get('missed') or 0}** | Снято/отменено: **{(shifts.get('removed') or 0) + (shifts.get('cancelled') or 0)}**"
        ),
        inline=False,
    )
    embed.add_field(
        name="📊 Отчёты",
        value=(
            f"Всего: **{reports.get('total') or 0}** | ✅ **{reports.get('approved') or 0}** | "
            f"🟡 **{reports.get('pending') or 0}** | ❌ **{reports.get('rejected') or 0}**\n"
            f"Принято по одобренным: **{reports.get('accepted') or 0}**"
        ),
        inline=False,
    )
    embed.add_field(
        name="👥 Инвайты",
        value=(
            f"Всего: **{invites.get('total') or 0}** | ✅ **{invites.get('accepted') or 0}** | "
            f"🟡 **{invites.get('pending') or 0}** | ❌ **{invites.get('rejected') or 0}**"
        ),
        inline=False,
    )
    embed.add_field(
        name="💰 Финансы",
        value=(
            f"Начислено: **{money(overview['accrued'])}**\n"
            f"Выплачено: **{money(overview['paid'])}**\n"
            f"К выплате: **{money(overview['available'])}**"
        ),
        inline=True,
    )
    embed.add_field(name="🎯 Активных целей", value=str(overview["active_goals"]), inline=True)
    if user.get("notes"):
        embed.add_field(name="📝 Последние заметки", value=_clip(user["notes"][-1000:]), inline=False)
    embed.set_footer(text="Кнопки действуют 5 минут. Критичные статусы и деньги меняются только профильными командами.")
    return embed


class StaticModal(disnake.ui.Modal):
    def __init__(self, target: disnake.Member):
        self.target = target
        super().__init__(
            title=f"Изменить статик: {target.display_name}"[:45],
            components=[
                disnake.ui.TextInput(
                    label="Новый статик",
                    custom_id="static_id",
                    placeholder="Введите новый статик или CLEAR для очистки",
                    min_length=1,
                    max_length=64,
                )
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction):
        raw = inter.text_values["static_id"].strip()
        new_static = None if raw.upper() == "CLEAR" else raw
        try:
            old, new = await update_user_static(
                self.target.id,
                self.target.name,
                new_static,
                inter.author.id,
            )
        except UserFacingError as exc:
            return await inter.response.send_message(f"❌ {exc}", ephemeral=True)
        await inter.response.send_message(
            f"✅ Статик {self.target.mention}: **{old or '—'}** → **{new or '—'}**.\n"
            "Текущие booked/active смены также обновлены; исторические завершённые записи не менялись.",
            ephemeral=True,
        )


class NoteModal(disnake.ui.Modal):
    def __init__(self, target: disnake.Member):
        self.target = target
        super().__init__(
            title=f"Заметка: {target.display_name}"[:45],
            components=[
                disnake.ui.TextInput(
                    label="Служебная заметка",
                    custom_id="note",
                    style=disnake.TextInputStyle.paragraph,
                    min_length=1,
                    max_length=1000,
                )
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction):
        try:
            await add_user_note(
                self.target.id,
                self.target.name,
                inter.text_values["note"],
                inter.author.id,
                inter.author.name,
            )
        except UserFacingError as exc:
            return await inter.response.send_message(f"❌ {exc}", ephemeral=True)
        await inter.response.send_message(f"✅ Заметка добавлена для {self.target.mention}.", ephemeral=True)


class DatabaseUserView(disnake.ui.View):
    def __init__(self, actor_id: int, target: disnake.Member):
        super().__init__(timeout=300)
        self.actor_id = actor_id
        self.target = target

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id != self.actor_id:
            await inter.response.send_message("❌ Это админ-панель другого пользователя.", ephemeral=True)
            return False
        return True

    async def _edit_with_lines(self, inter, title: str, lines: list[str]):
        embed = disnake.Embed(title=title, color=disnake.Color.blurple())
        embed.description = _clip("\n".join(lines) if lines else "Записей нет.", 3900)
        embed.set_footer(text=f"Пользователь: {self.target.id} • показаны последние записи")
        await inter.response.edit_message(embed=embed, view=self)

    @disnake.ui.button(label="💰 Финансы", style=disnake.ButtonStyle.primary, row=0)
    async def finances(self, button, inter):
        rows = await list_user_finances(self.target.id, 10)
        lines = [
            f"`#{r['id']}` {_status(r['status'])} **{money(r['amount'])}** • `{r['type']}` • {_clip(r['reason'], 100)} • {str(r['created_at'])[:16]}"
            for r in rows
        ]
        await self._edit_with_lines(inter, "💰 Последние финансовые операции", lines)

    @disnake.ui.button(label="📋 Смены", style=disnake.ButtonStyle.primary, row=0)
    async def shifts(self, button, inter):
        rows = await list_user_shifts(self.target.id, 10)
        lines = [
            f"Смена **#{r['shift_id']}** • {_status(r['member_status'])} • {str(r['scheduled_start'])[:16]} → {str(r['scheduled_end'])[:16]} • отчёт `{r['report_id'] or '—'}`"
            for r in rows
        ]
        await self._edit_with_lines(inter, "📋 Последние смены", lines)

    @disnake.ui.button(label="📊 Отчёты", style=disnake.ButtonStyle.primary, row=0)
    async def reports(self, button, inter):
        rows = await list_user_reports(self.target.id, 10)
        lines = [
            f"`#{r['id']}` смена **#{r['shift_id']}** • {_status(r['status'])} • всего **{r['total_accepted']}**, база **{r['came_to_base']}**, сам **{r['found_by_recruiter']}** • {str(r['created_at'])[:16]}"
            for r in rows
        ]
        await self._edit_with_lines(inter, "📊 Последние отчёты", lines)

    @disnake.ui.button(label="👥 Инвайты", style=disnake.ButtonStyle.primary, row=0)
    async def invites(self, button, inter):
        rows = await list_user_invites(self.target.id, 10)
        lines = [
            f"`#{r['id']}` **{_clip(r['static_id'], 50)}** • {_status(r['status'])} • {_clip(r['full_name'], 100)} • {str(r['created_at'])[:16]}"
            for r in rows
        ]
        await self._edit_with_lines(inter, "👥 Последние инвайты рекрутера", lines)

    @disnake.ui.button(label="📝 Логи", style=disnake.ButtonStyle.secondary, row=1)
    async def logs(self, button, inter):
        rows = await list_user_logs(self.target.id, 10)
        lines = [
            f"`#{r['id']}` **{_clip(r['action'], 80)}** • {_clip(r['details'], 150)} • {str(r['created_at'])[:16]}"
            for r in rows
        ]
        await self._edit_with_lines(inter, "📝 Последние действия пользователя", lines)

    @disnake.ui.button(label="✏️ Статик", style=disnake.ButtonStyle.secondary, row=1)
    async def static(self, button, inter):
        await inter.response.send_modal(StaticModal(self.target))

    @disnake.ui.button(label="🗒️ Заметка", style=disnake.ButtonStyle.secondary, row=1)
    async def note(self, button, inter):
        await inter.response.send_modal(NoteModal(self.target))

    @disnake.ui.button(label="🔄 Обзор", style=disnake.ButtonStyle.success, row=1)
    async def refresh(self, button, inter):
        overview = await get_user_overview(self.target.id)
        if not overview:
            return await inter.response.send_message("❌ Профиль в БД не найден.", ephemeral=True)
        embed = await build_user_embed(self.target, overview)
        await inter.response.edit_message(embed=embed, view=self)


class DatabaseAdmin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(name="база", description="Безопасная админ-панель базы данных")
    @is_admin()
    async def database(self, inter):
        pass

    @database.sub_command(name="пользователь", description="Открыть карточку пользователя из базы")
    async def user(self, inter, пользователь: disnake.Member):
        await inter.response.defer(ephemeral=True)
        overview = await get_user_overview(пользователь.id)
        if not overview:
            return await inter.edit_original_response(
                content="❌ Пользователь ещё не существует в таблице users. Он появится после первого действия в боте."
            )
        embed = await build_user_embed(пользователь, overview)
        await inter.edit_original_response(
            embed=embed,
            view=DatabaseUserView(inter.author.id, пользователь),
        )

    @database.sub_command(name="найти", description="Найти пользователя по Discord ID, статику или имени")
    async def find(self, inter, запрос: str):
        await inter.response.defer(ephemeral=True)
        try:
            rows = await search_users(запрос, 10)
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")
        if not rows:
            return await inter.edit_original_response(content="Ничего не найдено.")

        if len(rows) == 1:
            row = rows[0]
            member = inter.guild.get_member(row["discord_id"])
            if member:
                overview = await get_user_overview(row["discord_id"])
                embed = await build_user_embed(member, overview)
                return await inter.edit_original_response(
                    embed=embed,
                    view=DatabaseUserView(inter.author.id, member),
                )

        embed = disnake.Embed(title="🔎 Результаты поиска в базе", color=disnake.Color.blurple())
        for row in rows[:10]:
            embed.add_field(
                name=f"{_clip(row['username'], 100)} • {_clip(row['static_id'], 50)}",
                value=f"<@{row['discord_id']}> • `{row['discord_id']}` • DB role: `{row['role']}`",
                inline=False,
            )
        embed.set_footer(text="Для интерактивной карточки используйте /база пользователь @человек")
        await inter.edit_original_response(embed=embed)

    @database.sub_command(name="статик", description="Безопасно изменить статик пользователя")
    async def static(self, inter, пользователь: disnake.Member, новый_статик: str):
        await inter.response.defer(ephemeral=True)
        raw = новый_статик.strip()
        new_static = None if raw.upper() == "CLEAR" else raw
        try:
            old, new = await update_user_static(
                пользователь.id,
                пользователь.name,
                new_static,
                inter.author.id,
            )
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")
        await inter.edit_original_response(
            content=f"✅ Статик {пользователь.mention}: **{old or '—'}** → **{new or '—'}**."
        )

    @database.sub_command(name="заметка", description="Добавить служебную заметку пользователю")
    async def note(self, inter, пользователь: disnake.Member, текст: str):
        await inter.response.defer(ephemeral=True)
        try:
            await add_user_note(
                пользователь.id,
                пользователь.name,
                текст,
                inter.author.id,
                inter.author.name,
            )
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")
        await inter.edit_original_response(content=f"✅ Заметка добавлена для {пользователь.mention}.")

    @database.sub_command(name="финоперация", description="Посмотреть финансовую операцию по ID")
    async def finance_operation(self, inter, id: int):
        await inter.response.defer(ephemeral=True)
        row = await get_finance_operation(id)
        if not row:
            return await inter.edit_original_response(content="❌ Финансовая операция не найдена.")
        embed = disnake.Embed(title=f"💰 Финансовая операция #{row['id']}", color=disnake.Color.gold())
        embed.add_field(name="Пользователь", value=f"<@{row['user_id']}>\n`{row['user_id']}`", inline=False)
        embed.add_field(name="Сумма", value=money(row["amount"]), inline=True)
        embed.add_field(name="Тип", value=str(row["type"]), inline=True)
        embed.add_field(name="Статус", value=_status(row["status"]), inline=True)
        embed.add_field(name="Причина", value=_clip(row["reason"], 1000), inline=False)
        embed.add_field(name="Создал", value=f"<@{row['created_by']}>" if row["created_by"] else "Система", inline=True)
        embed.add_field(name="Связь со сменой", value=f"#{row['related_shift_id']}" if row["related_shift_id"] else "—", inline=True)
        embed.add_field(name="Дата", value=str(row["created_at"]), inline=True)
        embed.set_footer(text="Эта команда только читает журнал. Для изменения денег используйте /финансы.")
        await inter.edit_original_response(embed=embed)


def setup(bot):
    bot.add_cog(DatabaseAdmin(bot))
