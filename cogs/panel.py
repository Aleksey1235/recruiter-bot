import logging
import os
from datetime import datetime, timedelta

import disnake
from disnake.ext import commands

import config
from cogs.invites import build_invite_view
from cogs.shifts import FinishShiftModal, ResubmitReportModal, build_shift_view, update_shift_message
from database.db import db
from services import finance_service, goal_service, invite_service, shift_service, statistics_service
from services.errors import UserFacingError
from utils.checks import is_recruiter_or_higher, is_senior_or_admin
from utils.formatting import money
from utils.time_utils import local_now, parse_db, utc_now

logger = logging.getLogger(__name__)
PANEL_FOOTER = "Recruiter Department • Панель управления"
LEGACY_PANEL_FOOTERS = {PANEL_FOOTER, "Recruiter Bot • control-panel-v1"}


def _is_admin(member) -> bool:
    return config.ADMIN_ROLE_ID in {role.id for role in getattr(member, "roles", [])}


def _panel_embed() -> disnake.Embed:
    embed = disnake.Embed(
        title="📌 RECRUITER • ЦЕНТР УПРАВЛЕНИЯ",
        description=(
            "Вся работа отдела — в одном месте.\n"
            "Нажмите нужную кнопку ниже. Личные меню видны только вам."
        ),
        color=disnake.Color.blurple(),
    )
    embed.add_field(
        name="🕐 Смены",
        value="Начать • завершить • выйти • расписание",
        inline=False,
    )
    embed.add_field(
        name="👥 Рекрутинг",
        value="Инвайты • профиль • статистика • финансы • цели",
        inline=False,
    )
    embed.add_field(
        name="🛡️ Управление",
        value="Старший состав и администраторы открывают свои разделы отдельными кнопками.",
        inline=False,
    )
    embed.set_footer(text=PANEL_FOOTER)
    return embed


async def _profile_embed(member) -> disnake.Embed:
    user = await db.fetchone("SELECT * FROM users WHERE discord_id=?", (member.id,))
    if not user:
        raise UserFacingError("Профиль ещё не создан. Сначала возьмите смену или создайте инвайт.")

    report_stats = await db.fetchone(
        """
        SELECT COUNT(*) AS shifts, COALESCE(SUM(total_accepted),0) AS accepted
        FROM shift_reports WHERE user_id=? AND status='approved'
        """,
        (member.id,),
    )
    invites = await db.fetchone(
        "SELECT COUNT(*) AS count FROM invites WHERE invited_by=? AND status='accepted'",
        (member.id,),
    )
    accrued, paid, available = await finance_service.get_balance(member.id)

    embed = disnake.Embed(title=f"👤 ПРОФИЛЬ: {member.display_name}", color=disnake.Color.blue())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="🆔 Статик", value=user["static_id"] or "—", inline=True)
    embed.add_field(name="📋 Смен", value=str(report_stats["shifts"] or 0), inline=True)
    embed.add_field(name="✅ Принято", value=str(report_stats["accepted"] or 0), inline=True)
    embed.add_field(name="👥 Инвайтов", value=str(invites["count"] or 0), inline=True)
    embed.add_field(name="💰 Начислено", value=money(accrued), inline=True)
    embed.add_field(name="📊 К выплате", value=money(available), inline=True)
    return embed


async def _finance_embed(user_id: int) -> disnake.Embed:
    accrued, paid, available = await finance_service.get_balance(user_id)
    ops = await db.fetchall(
        "SELECT * FROM finances WHERE user_id=? ORDER BY id DESC LIMIT 8",
        (user_id,),
    )
    embed = disnake.Embed(title="💰 МОИ ФИНАНСЫ", color=disnake.Color.green())
    embed.add_field(name="💵 Начислено", value=money(accrued), inline=True)
    embed.add_field(name="💸 Выплачено", value=money(paid), inline=True)
    embed.add_field(name="📊 К выплате", value=money(available), inline=True)
    if not ops:
        embed.add_field(name="📋 Операции", value="Пока нет финансовых операций.", inline=False)
    else:
        lines = []
        for op in ops:
            if op["type"] == "salary":
                lines.append(f"💰 +{money(op['amount'])} — {(op['reason'] or 'Начисление')[:80]}")
            else:
                lines.append(f"💸 -{money(op['amount'])} — Выплата")
        embed.add_field(name="📋 Последние операции", value="\n".join(lines), inline=False)
    return embed


async def _goals_embed(user_id: int) -> disnake.Embed:
    type_labels = {"люди": "👥 Люди", "смены": "📋 Смены", "часы": "⏱ Часы"}
    period_labels = {"день": "за сегодня", "неделя": "за неделю", "месяц": "за месяц"}
    goals = await db.fetchall(
        "SELECT * FROM goals WHERE user_id=? AND status='active' ORDER BY id",
        (user_id,),
    )
    embed = disnake.Embed(title="🎯 МОИ ЦЕЛИ", color=disnake.Color.blue())
    if not goals:
        embed.description = "Нет активных целей."
        return embed
    for goal in goals:
        current = await goal_service.calculate_progress(user_id, goal["type"], goal["period"])
        await db.execute("UPDATE goals SET current_value=? WHERE id=?", (current, goal["id"]))
        target = max(int(goal["target_value"] or 0), 1)
        pct = min(int(current / target * 100), 100)
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        embed.add_field(
            name=f"{type_labels.get(goal['type'], goal['type'])} ({period_labels.get(goal['period'], goal['period'])})",
            value=f"{bar} **{current} / {goal['target_value']}** ({pct}%)",
            inline=False,
        )
    return embed


async def _stats_embed(member, period: str) -> disnake.Embed:
    data = await statistics_service.user_statistics(member.id, period)
    embed = disnake.Embed(
        title=f"📊 СТАТИСТИКА | {member.display_name}",
        description=f"Период: {period}",
        color=disnake.Color.blue(),
    )
    hours = data["total_hours"]
    embed.add_field(
        name="📋 СМЕНЫ",
        value=(
            f"Всего записей: {data['total_shifts']}\n"
            f"✅ Завершено: {data['completed_shifts']}\n"
            f"🟢 Одобрено отчётов: {data['approved_reports']}\n"
            f"🟡 На проверке: {data['pending_shifts']}\n"
            f"🚫 Отклонено: {data['rejected_shifts']}\n"
            f"❌ Пропущено: {data['missed_shifts']}\n"
            f"⏱ Отработано: {int(hours)}ч {int((hours % 1) * 60)}м"
        ),
        inline=False,
    )
    embed.add_field(
        name="👥 РЕКРУТИНГ",
        value=(
            f"Принято: {data['total_accepted']}\n"
            f"🏠 На особняке: {data['total_base']}\n"
            f"👤 Самостоятельно: {data['total_self']}\n"
            f"📈 Самостоятельный %: {data['self_percent']:.1f}%"
        ),
        inline=False,
    )
    attendance = "—" if data["attendance"] is None else f"{data['attendance']:.1f}%"
    embed.add_field(
        name="📈 ЭФФЕКТИВНОСТЬ",
        value=(
            f"Среднее за смену: {data['avg_per_shift']:.1f}\n"
            f"Посещаемость: {attendance}\n"
            f"⭐ Место: #{data['rank']}" if data["rank"] else f"Среднее за смену: {data['avg_per_shift']:.1f}\nПосещаемость: {attendance}\n⭐ Место: —"
        ),
        inline=False,
    )
    return embed


async def _schedule_embed() -> disnake.Embed:
    now = local_now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    shifts = await shift_service.get_schedule(day_start, day_end)
    embed = disnake.Embed(title="📅 РАСПИСАНИЕ СМЕН", color=disnake.Color.blue())
    if not shifts:
        embed.description = "На сегодня смен нет."
        return embed
    for shift in shifts[:20]:
        start = parse_db(shift["scheduled_start"])
        end = parse_db(shift["scheduled_end"])
        time_text = f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}" if start and end else "Время неизвестно"
        if shift["status"] == "cancelled":
            status = "⚫ Отменена"
        elif shift["status"] == "completed":
            status = "🔵 Завершена"
        elif shift["status"] == "missed":
            status = "🔴 Пропущена"
        elif shift["status"] == "active":
            status = "🟢 Идёт"
        elif (shift["slots"] or 0) <= 0:
            status = "🔴 Мест нет"
        else:
            status = f"🟢 Свободно мест: {shift['slots']}"
        embed.add_field(name=f"🕐 {time_text} | Смена #{shift['id']}", value=status, inline=False)
    return embed


async def _my_invites_embed(user_id: int) -> disnake.Embed:
    rows = await db.fetchall(
        "SELECT * FROM invites WHERE invited_by=? ORDER BY created_at DESC LIMIT 20",
        (user_id,),
    )
    embed = disnake.Embed(title="👥 МОИ ИНВАЙТЫ", color=disnake.Color.blue())
    if not rows:
        embed.description = "У вас нет инвайтов."
        return embed
    for inv in rows:
        status = {"pending": "🟡", "accepted": "✅", "rejected": "❌"}.get(inv["status"], "❓")
        embed.add_field(
            name=f"{status} {inv['static_id']}",
            value=f"Имя: {inv['full_name'] or '—'}\nДата: {str(inv['created_at'])[:16]}",
            inline=True,
        )
    return embed


async def _top_embed(period: str = "неделя") -> disnake.Embed:
    rows = await statistics_service.top_statistics(period)
    embed = disnake.Embed(title="🏆 ТОП РЕКРУТЕРОВ", description=f"Период: {period}", color=disnake.Color.gold())
    medals = ["🥇", "🥈", "🥉", "4.", "5."]
    ordered = sorted(rows, key=lambda x: x["accepted"], reverse=True)[:5]
    accepted = "\n".join(f"{medals[i]} <@{row['user_id']}> — {row['accepted']} чел" for i, row in enumerate(ordered))
    embed.add_field(name="👥 ПО ПРИНЯТЫМ", value=accepted or "Нет данных", inline=False)
    ordered = sorted(rows, key=lambda x: x["shifts"], reverse=True)[:5]
    shifts = "\n".join(f"{medals[i]} <@{row['user_id']}> — {row['shifts']} смен" for i, row in enumerate(ordered))
    embed.add_field(name="📋 ПО СМЕНАМ", value=shifts or "Нет данных", inline=False)
    return embed


class CreateShiftModal(disnake.ui.Modal):
    def __init__(self):
        super().__init__(
            title="➕ Создать смену",
            custom_id="panel:create_shift_modal",
            components=[
                disnake.ui.TextInput(label="Дата", custom_id="date", placeholder="24.08.2026", required=True, max_length=10),
                disnake.ui.TextInput(label="Начало", custom_id="start", placeholder="19:00", required=True, max_length=5),
                disnake.ui.TextInput(label="Конец", custom_id="end", placeholder="20:00", required=True, max_length=5),
                disnake.ui.TextInput(label="Количество мест", custom_id="slots", placeholder="1", required=True, max_length=2),
                disnake.ui.TextInput(label="Описание", custom_id="description", required=False, max_length=500, style=disnake.TextInputStyle.paragraph),
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction):
        if not is_senior_or_admin(inter.author):
            return await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        try:
            start = datetime.strptime(f"{inter.text_values['date']} {inter.text_values['start']}", "%d.%m.%Y %H:%M")
            end = datetime.strptime(f"{inter.text_values['date']} {inter.text_values['end']}", "%d.%m.%Y %H:%M")
            if end <= start:
                end += timedelta(days=1)
            slots = int(inter.text_values["slots"].strip())
        except ValueError:
            return await inter.edit_original_response(content="❌ Проверьте дату, время и количество мест.")
        try:
            shift_id = await shift_service.create_shift(
                inter.author.id, start, end, slots, inter.text_values.get("description", "")
            )
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")

        shift = await db.fetchone("SELECT * FROM shifts WHERE id=?", (shift_id,))
        channel = inter.guild.get_channel(config.SHIFTS_CHANNEL_ID)
        if not channel:
            return await inter.edit_original_response(content=f"⚠️ Смена #{shift_id} создана, но канал смен не найден.")
        from utils.embeds import EmbedGenerator
        message = await channel.send(
            content=(f"<@&{config.RECRUITER_ROLE_ID}>" if config.PING_RECRUITERS_ON_SHIFT_CREATE else None),
            embed=EmbedGenerator.create_shift_embed(shift, []),
            view=build_shift_view(shift_id),
            allowed_mentions=disnake.AllowedMentions(roles=True),
        )
        await shift_service.set_shift_message_id(shift_id, message.id)
        await inter.edit_original_response(content=f"✅ Смена **#{shift_id}** создана.")


class LeaveShiftSelect(disnake.ui.StringSelect):
    def __init__(self, shifts):
        options = []
        for row in shifts[:25]:
            start = parse_db(row["scheduled_start"])
            label = f"Смена #{row['shift_id']}"
            description = start.strftime("%d.%m %H:%M") if start else "Время неизвестно"
            options.append(disnake.SelectOption(label=label, value=str(row["shift_id"]), description=description))
        super().__init__(placeholder="Выберите смену", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, inter: disnake.MessageInteraction):
        self.view.shift_id = int(self.values[0])
        await inter.response.edit_message(content=f"Выбрана смена **#{self.view.shift_id}**. Подтвердите выход.", view=self.view)


class LeaveShiftView(disnake.ui.View):
    def __init__(self, shifts, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.shift_id = None
        self.add_item(LeaveShiftSelect(shifts))

    async def interaction_check(self, inter):
        if inter.author.id != self.user_id:
            await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True)
            return False
        return True

    @disnake.ui.button(label="🚪 Выйти со смены", style=disnake.ButtonStyle.danger, row=1)
    async def confirm(self, button, inter):
        if self.shift_id is None:
            return await inter.response.send_message("❌ Сначала выберите смену.", ephemeral=True)
        try:
            shift_id = await shift_service.leave_shift(inter.author.id, self.shift_id)
        except UserFacingError as exc:
            return await inter.response.send_message(f"❌ {exc}", ephemeral=True)
        await inter.response.edit_message(content=f"✅ Вы вышли со смены **#{shift_id}**. Место снова свободно.", view=None)
        await update_shift_message(inter.guild, shift_id)


class RejectedReportSelect(disnake.ui.StringSelect):
    def __init__(self, reports):
        options = [
            disnake.SelectOption(label=f"Отчёт #{r['id']}", value=str(r["id"]), description=f"Смена #{r['shift_id']}")
            for r in reports[:25]
        ]
        super().__init__(placeholder="Выберите отклонённый отчёт", options=options, min_values=1, max_values=1)

    async def callback(self, inter):
        await inter.response.send_modal(ResubmitReportModal(int(self.values[0])))


class RejectedReportView(disnake.ui.View):
    def __init__(self, reports, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.add_item(RejectedReportSelect(reports))

    async def interaction_check(self, inter):
        if inter.author.id != self.user_id:
            await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True)
            return False
        return True


class InviteIdentityModal(disnake.ui.Modal):
    def __init__(self, target):
        self.target = target
        super().__init__(
            title="👤 Новый инвайт",
            custom_id=f"panel:invite_identity:{target.id}",
            components=[
                disnake.ui.TextInput(label="Статик", custom_id="static", placeholder="12345", required=True, max_length=20),
                disnake.ui.TextInput(label="Имя Фамилия", custom_id="name", placeholder="Ivan Ivanov", required=True, max_length=100),
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction):
        view = InviteChecklistView(
            inter.bot,
            inter.author.id,
            self.target,
            inter.text_values["static"].strip(),
            inter.text_values["name"].strip(),
        )
        await inter.response.send_message(
            "Отметьте, что приглашённый уже выполнил, затем нажмите **Создать отчёт**.",
            view=view,
            ephemeral=True,
        )


class InviteUserSelect(disnake.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Выберите приглашённого пользователя", min_values=1, max_values=1)

    async def callback(self, inter: disnake.MessageInteraction):
        target = self.values[0]
        await inter.response.send_modal(InviteIdentityModal(target))


class InviteUserSelectView(disnake.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.add_item(InviteUserSelect())

    async def interaction_check(self, inter):
        if inter.author.id != self.user_id:
            await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True)
            return False
        return True


class InviteChecklistSelect(disnake.ui.StringSelect):
    def __init__(self):
        options = [
            disnake.SelectOption(label="🎫 Заполнил тикет", value="ticket"),
            disnake.SelectOption(label="📝 Сменил фамилию", value="last_name"),
            disnake.SelectOption(label="🏢 Вступил в организацию", value="organization"),
            disnake.SelectOption(label="⚔️ Вступил во фракцию", value="fraction"),
            disnake.SelectOption(label="📢 Прослушал информацию", value="info"),
            disnake.SelectOption(label="❌ Ничего из списка", value="none"),
        ]
        super().__init__(placeholder="Выберите выполненные пункты", min_values=1, max_values=5, options=options, row=0)

    async def callback(self, inter):
        self.view.selected = set(self.values)
        if "none" in self.view.selected and len(self.view.selected) > 1:
            text = "⚠️ Нельзя выбрать «Ничего» вместе с выполненными пунктами. Измените выбор."
        else:
            text = "✅ Выбор сохранён. Нажмите **Создать отчёт**."
        await inter.response.edit_message(content=text, view=self.view)


class InviteChecklistView(disnake.ui.View):
    def __init__(self, bot, user_id: int, target, static_id: str, full_name: str):
        super().__init__(timeout=300)
        self.bot = bot
        self.user_id = user_id
        self.target = target
        self.static_id = static_id
        self.full_name = full_name
        self.selected = None
        self.add_item(InviteChecklistSelect())

    async def interaction_check(self, inter):
        if inter.author.id != self.user_id:
            await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True)
            return False
        return True

    @disnake.ui.button(label="✅ Создать отчёт", style=disnake.ButtonStyle.green, row=1)
    async def create(self, button, inter):
        if self.selected is None:
            return await inter.response.send_message("❌ Сначала выберите пункты чек-листа.", ephemeral=True)
        if "none" in self.selected and len(self.selected) > 1:
            return await inter.response.send_message("❌ Исправьте выбор чек-листа.", ephemeral=True)
        done = set() if "none" in self.selected else self.selected
        checklist = {
            "ticket": "yes" if "ticket" in done else "no",
            "last_name": "yes" if "last_name" in done else "no",
            "organization": "yes" if "organization" in done else "no",
            "fraction": "yes" if "fraction" in done else "no",
            "info": "yes" if "info" in done else "no",
        }
        await inter.response.defer(ephemeral=True)
        try:
            invite_id = await invite_service.create_invite(
                self.target.id,
                inter.author.id,
                inter.author.name,
                self.static_id,
                self.full_name,
                checklist,
            )
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}", view=None)

        channel = inter.guild.get_channel(config.REPORTS_CHANNEL_ID)
        if channel:
            embed = disnake.Embed(title="👤 НОВЫЙ ИНВАЙТ", color=disnake.Color.blue())
            embed.add_field(name="Приглашённый", value=self.target.mention, inline=True)
            embed.add_field(name="Статик", value=self.static_id, inline=True)
            embed.add_field(name="Имя", value=self.full_name, inline=True)
            embed.add_field(name="Рекрутер", value=inter.author.mention, inline=True)
            checklist_text = (
                f"Тикет: {'✅' if checklist['ticket']=='yes' else '❌'}\n"
                f"Фамилия: {'✅' if checklist['last_name']=='yes' else '❌'}\n"
                f"Организация: {'✅' if checklist['organization']=='yes' else '❌'}\n"
                f"Фракция: {'✅' if checklist['fraction']=='yes' else '❌'}\n"
                f"Инфо: {'✅' if checklist['info']=='yes' else '❌'}"
            )
            embed.add_field(name="📋 Чек-лист", value=checklist_text, inline=False)
            embed.set_footer(text=f"Инвайт #{invite_id}")
            await channel.send(
                content=f"<@&{config.SENIOR_ROLE_ID}> <@&{config.ADMIN_ROLE_ID}>",
                embed=embed,
                view=build_invite_view(invite_id),
            )
        await inter.edit_original_response(content=f"✅ Отчёт создан. ID: **#{invite_id}**", view=None)


class StatsPeriodSelect(disnake.ui.StringSelect):
    def __init__(self):
        options = [disnake.SelectOption(label=p.capitalize(), value=p) for p in ["сегодня", "неделя", "месяц", "всё время"]]
        super().__init__(placeholder="Период статистики", options=options, min_values=1, max_values=1)

    async def callback(self, inter):
        await inter.response.edit_message(embed=await _stats_embed(inter.author, self.values[0]), view=self.view)


class StatsView(disnake.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.add_item(StatsPeriodSelect())

    async def interaction_check(self, inter):
        if inter.author.id != self.user_id:
            await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True)
            return False
        return True

    @disnake.ui.button(label="🏆 Топ недели", style=disnake.ButtonStyle.secondary, row=1)
    async def top(self, button, inter):
        await inter.response.edit_message(embed=await _top_embed("неделя"), view=self)


class ShiftMenuView(disnake.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id

    async def interaction_check(self, inter):
        if inter.author.id != self.user_id:
            await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True)
            return False
        return True

    @disnake.ui.button(label="▶️ Начать", style=disnake.ButtonStyle.green, row=0)
    async def start(self, button, inter):
        try:
            member = await shift_service.find_shift_to_start(inter.author.id)
            shift_id = await shift_service.start_shift(member["id"], inter.author.id)
        except UserFacingError as exc:
            return await inter.response.send_message(f"❌ {exc}", ephemeral=True)
        await inter.response.send_message(f"🟢 Смена **#{shift_id}** начата.", ephemeral=True)
        await update_shift_message(inter.guild, shift_id)

    @disnake.ui.button(label="✅ Завершить", style=disnake.ButtonStyle.primary, row=0)
    async def finish(self, button, inter):
        try:
            member = await shift_service.find_active_member(inter.author.id)
        except UserFacingError as exc:
            return await inter.response.send_message(f"❌ {exc}", ephemeral=True)
        await inter.response.send_modal(FinishShiftModal(member["id"], member["shift_id"]))

    @disnake.ui.button(label="🚪 Выйти", style=disnake.ButtonStyle.danger, row=0)
    async def leave(self, button, inter):
        rows = await db.fetchall(
            """
            SELECT sm.shift_id, s.scheduled_start
            FROM shift_members sm JOIN shifts s ON s.id=sm.shift_id
            WHERE sm.user_id=? AND sm.status='booked'
            ORDER BY s.scheduled_start ASC LIMIT 25
            """,
            (inter.author.id,),
        )
        if not rows:
            return await inter.response.send_message("❌ У вас нет забронированных смен для выхода.", ephemeral=True)
        await inter.response.send_message("Выберите смену, с которой хотите выйти:", view=LeaveShiftView(rows, inter.author.id), ephemeral=True)

    @disnake.ui.button(label="📅 Расписание", style=disnake.ButtonStyle.secondary, row=1)
    async def schedule(self, button, inter):
        await inter.response.send_message(embed=await _schedule_embed(), ephemeral=True)

    @disnake.ui.button(label="♻️ Исправить отчёт", style=disnake.ButtonStyle.secondary, row=1)
    async def resubmit(self, button, inter):
        rows = await db.fetchall(
            "SELECT * FROM shift_reports WHERE user_id=? AND status='rejected' ORDER BY id DESC LIMIT 25",
            (inter.author.id,),
        )
        if not rows:
            return await inter.response.send_message("❌ У вас нет отклонённых отчётов.", ephemeral=True)
        if len(rows) == 1:
            return await inter.response.send_modal(ResubmitReportModal(rows[0]["id"]))
        await inter.response.send_message("Выберите отчёт:", view=RejectedReportView(rows, inter.author.id), ephemeral=True)


class InviteMenuView(disnake.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id

    async def interaction_check(self, inter):
        if inter.author.id != self.user_id:
            await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True)
            return False
        return True

    @disnake.ui.button(label="➕ Новый инвайт", style=disnake.ButtonStyle.green)
    async def new_invite(self, button, inter):
        await inter.response.send_message("Выберите пользователя Discord, которого пригласили:", view=InviteUserSelectView(inter.author.id), ephemeral=True)

    @disnake.ui.button(label="👥 Мои инвайты", style=disnake.ButtonStyle.primary)
    async def my_invites(self, button, inter):
        await inter.response.send_message(embed=await _my_invites_embed(inter.author.id), ephemeral=True)


class SeniorMenuView(disnake.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id

    async def interaction_check(self, inter):
        if inter.author.id != self.user_id:
            await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True)
            return False
        if not is_senior_or_admin(inter.author):
            await inter.response.send_message("❌ Доступ только старшему составу.", ephemeral=True)
            return False
        return True

    @disnake.ui.button(label="➕ Создать смену", style=disnake.ButtonStyle.green, row=0)
    async def create_shift(self, button, inter):
        await inter.response.send_modal(CreateShiftModal())

    @disnake.ui.button(label="📋 Инвайты на проверке", style=disnake.ButtonStyle.primary, row=0)
    async def pending_invites(self, button, inter):
        rows = await db.fetchall("SELECT * FROM invites WHERE status='pending' ORDER BY created_at DESC LIMIT 20")
        embed = disnake.Embed(title="📋 ИНВАЙТЫ НА ПРОВЕРКЕ", color=disnake.Color.yellow())
        if not rows:
            embed.description = "Нет инвайтов на проверке."
        for inv in rows:
            embed.add_field(
                name=f"#{inv['id']} | {inv['static_id']}",
                value=f"{inv['full_name'] or '—'} • <@{inv['invited_by']}>",
                inline=False,
            )
        embed.set_footer(text="Принимать/отклонять удобно кнопками в канале отчётов")
        await inter.response.send_message(embed=embed, ephemeral=True)

    @disnake.ui.button(label="💰 Общие финансы", style=disnake.ButtonStyle.secondary, row=1)
    async def general_finance(self, button, inter):
        accrued, paid, available = await finance_service.get_general_balance()
        embed = disnake.Embed(title="💰 ОБЩИЕ ФИНАНСЫ", color=disnake.Color.green())
        embed.add_field(name="💵 Начислено", value=money(accrued), inline=True)
        embed.add_field(name="💸 Выплачено", value=money(paid), inline=True)
        embed.add_field(name="📊 К выплате", value=money(available), inline=True)
        await inter.response.send_message(embed=embed, ephemeral=True)


class AdminMenuView(disnake.ui.View):
    def __init__(self, bot, user_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.user_id = user_id

    async def interaction_check(self, inter):
        if inter.author.id != self.user_id:
            await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True)
            return False
        if not _is_admin(inter.author):
            await inter.response.send_message("❌ Доступ только администратору.", ephemeral=True)
            return False
        return True

    @disnake.ui.button(label="🕐 Время", style=disnake.ButtonStyle.primary, row=0)
    async def time(self, button, inter):
        embed = disnake.Embed(title="🕐 ВРЕМЯ БОТА", color=disnake.Color.blue())
        embed.add_field(name="TIMEZONE", value=f"`{config.TIMEZONE}`", inline=False)
        embed.add_field(name="Время бота", value=local_now().strftime("%d.%m.%Y %H:%M:%S"), inline=True)
        embed.add_field(name="UTC", value=utc_now().strftime("%d.%m.%Y %H:%M:%S"), inline=True)
        embed.add_field(name="База", value=f"`{config.DATABASE_PATH}`", inline=False)
        await inter.response.send_message(embed=embed, ephemeral=True)

    @disnake.ui.button(label="🔔 Уведомления", style=disnake.ButtonStyle.primary, row=0)
    async def notifications(self, button, inter):
        rows = await db.fetchall("SELECT * FROM notifications ORDER BY id DESC LIMIT 10")
        embed = disnake.Embed(title="🔔 ДИАГНОСТИКА УВЕДОМЛЕНИЙ", color=disnake.Color.blue())
        if not rows:
            embed.description = "Записей уведомлений пока нет."
        for row in rows:
            embed.add_field(
                name=f"#{row['id']} | {row['type']} | {row['status']}",
                value=f"<@{row['user_id']}> • попыток: {row['attempts']}\nОшибка: {(row['last_error'] or '—')[:160]}",
                inline=False,
            )
        await inter.response.send_message(embed=embed, ephemeral=True)

    @disnake.ui.button(label="📦 Бэкап", style=disnake.ButtonStyle.green, row=1)
    async def backup(self, button, inter):
        admin_cog = self.bot.get_cog("Admin")
        if not admin_cog or not hasattr(admin_cog, "_make_backup"):
            return await inter.response.send_message("❌ Модуль бэкапа не найден.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        path = None
        try:
            path = await admin_cog._make_backup()
            await inter.edit_original_response(content="📦 Бэкап готов:", file=disnake.File(path))
        finally:
            if path and os.path.exists(path):
                os.remove(path)

    @disnake.ui.button(label="🩺 Быстрая проверка", style=disnake.ButtonStyle.secondary, row=1)
    async def health(self, button, inter):
        checks = []
        try:
            row = await db.fetchone("SELECT 1 AS ok")
            checks.append(("База", bool(row and row["ok"] == 1)))
            quick = await db.fetchone("PRAGMA quick_check")
            checks.append(("SQLite", bool(quick and quick[0] == "ok")))
            guild = inter.guild
            checks.append(("Канал смен", guild.get_channel(config.SHIFTS_CHANNEL_ID) is not None))
            checks.append(("Канал отчётов", guild.get_channel(config.REPORTS_CHANNEL_ID) is not None))
            checks.append(("Панель", guild.get_channel(config.PANEL_CHANNEL_ID) is not None))
            checks.append(("Роль рекрутера", guild.get_role(config.RECRUITER_ROLE_ID) is not None))
        except Exception:
            logger.exception("Ошибка быстрой проверки из панели")
            checks.append(("Внутренняя проверка", False))
        embed = disnake.Embed(title="🩺 БЫСТРАЯ ПРОВЕРКА", color=disnake.Color.green() if all(v for _, v in checks) else disnake.Color.orange())
        embed.description = "\n".join(f"{'✅' if ok else '❌'} {name}" for name, ok in checks)
        embed.set_footer(text="Полная диагностика всё ещё доступна командой /админ здоровье")
        await inter.response.send_message(embed=embed, ephemeral=True)


class MainPanelView(disnake.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _recruiter_check(self, inter) -> bool:
        if not is_recruiter_or_higher(inter.author):
            await inter.response.send_message("❌ Панель доступна рекрутерам и старшему составу.", ephemeral=True)
            return False
        return True

    @disnake.ui.button(label="🕐 Смена", style=disnake.ButtonStyle.green, custom_id="panel:shifts", row=0)
    async def shifts(self, button, inter):
        if not await self._recruiter_check(inter):
            return
        embed = disnake.Embed(title="🕐 СМЕНЫ", description="Выберите действие.", color=disnake.Color.blue())
        await inter.response.send_message(embed=embed, view=ShiftMenuView(inter.author.id), ephemeral=True)

    @disnake.ui.button(label="👤 Профиль", style=disnake.ButtonStyle.primary, custom_id="panel:profile", row=0)
    async def profile(self, button, inter):
        if not await self._recruiter_check(inter):
            return
        try:
            embed = await _profile_embed(inter.author)
        except UserFacingError as exc:
            return await inter.response.send_message(f"❌ {exc}", ephemeral=True)
        await inter.response.send_message(embed=embed, ephemeral=True)

    @disnake.ui.button(label="📈 Статистика", style=disnake.ButtonStyle.primary, custom_id="panel:stats", row=0)
    async def stats(self, button, inter):
        if not await self._recruiter_check(inter):
            return
        await inter.response.send_message(embed=await _stats_embed(inter.author, "неделя"), view=StatsView(inter.author.id), ephemeral=True)

    @disnake.ui.button(label="💳 Финансы", style=disnake.ButtonStyle.primary, custom_id="panel:finance", row=1)
    async def finance(self, button, inter):
        if not await self._recruiter_check(inter):
            return
        await inter.response.send_message(embed=await _finance_embed(inter.author.id), ephemeral=True)

    @disnake.ui.button(label="🎯 Цели", style=disnake.ButtonStyle.primary, custom_id="panel:goals", row=1)
    async def goals(self, button, inter):
        if not await self._recruiter_check(inter):
            return
        await inter.response.send_message(embed=await _goals_embed(inter.author.id), ephemeral=True)

    @disnake.ui.button(label="👥 Инвайты", style=disnake.ButtonStyle.primary, custom_id="panel:invites", row=0)
    async def invites(self, button, inter):
        if not await self._recruiter_check(inter):
            return
        await inter.response.send_message(
            embed=disnake.Embed(title="👥 ИНВАЙТЫ", description="Создайте новый отчёт или посмотрите свои.", color=disnake.Color.blue()),
            view=InviteMenuView(inter.author.id),
            ephemeral=True,
        )

    @disnake.ui.button(label="🛡️ Руководство", style=disnake.ButtonStyle.secondary, custom_id="panel:senior", row=1)
    async def senior(self, button, inter):
        if not is_senior_or_admin(inter.author):
            return await inter.response.send_message("❌ Доступ только старшему составу.", ephemeral=True)
        await inter.response.send_message(
            embed=disnake.Embed(title="🛡️ ПАНЕЛЬ СТАРШЕГО СОСТАВА", description="Основные действия старшего состава.", color=disnake.Color.orange()),
            view=SeniorMenuView(inter.author.id),
            ephemeral=True,
        )

    @disnake.ui.button(label="⚙️ Админ", style=disnake.ButtonStyle.danger, custom_id="panel:admin", row=1)
    async def admin(self, button, inter):
        if not _is_admin(inter.author):
            return await inter.response.send_message("❌ Доступ только администратору.", ephemeral=True)
        await inter.response.send_message(
            embed=disnake.Embed(title="⚙️ АДМИН-ПАНЕЛЬ", description="Диагностика и обслуживание.", color=disnake.Color.red()),
            view=AdminMenuView(self.bot, inter.author.id),
            ephemeral=True,
        )


class Panel(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._synced = False

    @commands.Cog.listener()
    async def on_ready(self):
        if self._synced:
            return
        self._synced = True
        try:
            await self.ensure_panel()
        except Exception:
            self._synced = False
            logger.exception("Не удалось создать/обновить панель управления")

    async def ensure_panel(self):
        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            raise RuntimeError("Сервер для панели не найден")
        channel = guild.get_channel(config.PANEL_CHANNEL_ID)
        if channel is None:
            raise RuntimeError(f"PANEL_CHANNEL_ID={config.PANEL_CHANNEL_ID} не найден")

        found = None
        try:
            async for message in channel.history(limit=100):
                if message.author.id != self.bot.user.id or not message.embeds:
                    continue
                footer = message.embeds[0].footer.text if message.embeds[0].footer else ""
                if footer in LEGACY_PANEL_FOOTERS:
                    found = message
                    break
        except Exception:
            logger.warning("Не удалось просмотреть историю канала панели; будет создано новое сообщение", exc_info=True)

        view = MainPanelView(self.bot)
        if found:
            await found.edit(embed=_panel_embed(), view=view)
            logger.info("Панель управления обновлена: message_id=%s", found.id)
            return

        message = await channel.send(embed=_panel_embed(), view=view)
        logger.info("Панель управления создана: message_id=%s", message.id)
        try:
            await message.pin(reason="Постоянная панель Recruiter Bot")
        except Exception:
            logger.info("Панель не закреплена: у бота нет права Manage Messages или канал не поддерживает pin")


def setup(bot):
    bot.add_view(MainPanelView(bot))
    bot.add_cog(Panel(bot))
