import logging
import os
from datetime import datetime, timedelta

import disnake
from disnake.ext import commands

import config
from cogs.invites import build_invite_view, sync_invite_review_message
from cogs.shifts import FinishShiftModal, LeaveShiftModal, ResubmitReportModal, publish_shift_message, sync_report_review_message, update_shift_message
from database.db import db, ensure_user, log, notify
from services import blacklist_service, database_service, finance_service, goal_service, invite_service, shift_service, statistics_service
from services.errors import UserFacingError
from services.health_service import (
    get_blacklist_anomalies, get_domain_anomalies, get_finance_anomalies,
    get_notification_anomalies, get_shift_anomalies, repair_safe_domain_anomalies, run_health_checks,
)
from utils.checks import is_recruiter_or_higher, is_senior_or_admin
from utils.formatting import money, normalize_amount
from utils.discord_helpers import send_control_warning
from utils.embeds import EmbedGenerator
from utils.time_utils import local_now, parse_db, utc_now, format_utc_db

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
        value="Старший состав: смены • отчёты • статистика. Admin: ЧС • база • финансы.",
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


async def _finance_embed(user_id: int, title: str = "💰 МОИ ФИНАНСЫ") -> disnake.Embed:
    accrued, paid, available = await finance_service.get_balance(user_id)
    ops = await db.fetchall(
        "SELECT * FROM finances WHERE user_id=? ORDER BY id DESC LIMIT 8",
        (user_id,),
    )
    embed = disnake.Embed(title=title, color=disnake.Color.green())
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
                lines.append(f"💸 -{money(op['amount'])} — {(op['reason'] or 'Выплата')[:80]}")
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
            f"⚠️ Отменено: {data['cancelled_shifts']}\n"
            f"🛠️ Снято: {data['removed_shifts']}\n"
            f"⏱ Отработано: {int(hours)}ч {int((hours % 1) * 60)}м"
        ),
        inline=False,
    )
    embed.add_field(
        name="👥 РЕКРУТИНГ",
        value=(
            f"Принято всего: {data['total_accepted']}\n"
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
            f"Самостоятельных за смену: {data['avg_self_per_shift']:.1f}\n"
            f"Посещаемость: {attendance}"
        ),
        inline=False,
    )
    embed.add_field(name="🎯 ЦЕЛИ", value=f"Активных: {data['active_goals']}", inline=True)
    embed.add_field(
        name="💰 ФИНАНСЫ (БАЛАНС ЗА ВСЁ ВРЕМЯ)",
        value=(
            f"Начислено: {money(data['accrued'])}\n"
            f"Выплачено: {money(data['paid'])}\n"
            f"К выплате: {money(data['available'])}"
        ),
        inline=True,
    )
    embed.add_field(name="⭐ РЕЙТИНГ", value=f"Место: #{data['rank']}" if data["rank"] else "Нет места", inline=True)
    return embed


async def _week_by_day_embed(user_id: int) -> disnake.Embed:
    days = await statistics_service.current_week_by_day(user_id)
    names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    embed = disnake.Embed(title="📊 НЕДЕЛЯ ПО ДНЯМ", color=disnake.Color.blue())
    totals = {"shifts": 0, "accepted": 0, "base": 0, "self": 0}
    for index, data in enumerate(days):
        for key in totals:
            totals[key] += data[key]
        bar = "█" * min(data["accepted"], 20) if data["accepted"] else "—"
        embed.add_field(
            name=f"{names[index]} — {data['accepted']} чел | {data['shifts']} смен",
            value=bar,
            inline=False,
        )
    embed.add_field(
        name="📊 ИТОГО",
        value=(
            f"Смен: {totals['shifts']}\nПринято: {totals['accepted']}\n"
            f"🏠 На особняке: {totals['base']}\n👤 Самостоятельно: {totals['self']}"
        ),
        inline=False,
    )
    return embed


async def _schedule_embed() -> disnake.Embed:
    now = local_now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    shifts = await shift_service.get_schedule(day_start, day_end)
    embed = disnake.Embed(title="📅 РАСПИСАНИЕ СМЕН", color=disnake.Color.blue())
    if not shifts:
        embed.description = "На сегодня смен нет."
        return embed
    for shift in shifts[:20]:
        start = parse_db(shift["scheduled_start"])
        end = parse_db(shift["scheduled_end"])
        time_text = ((f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}" if start.date() == end.date() else f"{start.strftime('%d.%m %H:%M')} → {end.strftime('%d.%m %H:%M')}") if start and end else "Время неизвестно")
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
            value=f"Имя: {inv['full_name'] or '—'}\nДата: {format_utc_db(inv['created_at'])}",
            inline=True,
        )
    return embed


async def _top_embed(period: str = "неделя") -> disnake.Embed:
    rows = await statistics_service.top_statistics(period)
    embed = disnake.Embed(title="🏆 ТОП РЕКРУТЕРОВ", description=f"Период: {period}", color=disnake.Color.gold())
    medals = ["🥇", "🥈", "🥉", "4.", "5."]

    def add_top(title, key, suffix=""):
        ordered = sorted(rows, key=lambda x: x[key], reverse=True)[:5]
        text = "\n".join(
            f"{medals[i]} <@{row['user_id']}> — {row[key]:.1f}{suffix}" if isinstance(row[key], float)
            else f"{medals[i]} <@{row['user_id']}> — {row[key]}{suffix}"
            for i, row in enumerate(ordered)
        )
        embed.add_field(name=title, value=text or "Нет данных", inline=False)

    add_top("👥 ТОП ПО ПРИНЯТЫМ", "accepted", " чел")
    add_top("👤 ТОП ПО САМОСТОЯТЕЛЬНЫМ", "self_found", " чел")
    efficiency = sorted(
        [row for row in rows if row["shifts"] >= 3],
        key=lambda x: x["avg_per_shift"],
        reverse=True,
    )[:5]
    efficiency_text = "\n".join(
        f"{medals[i]} <@{row['user_id']}> — {row['avg_per_shift']:.1f}/смену"
        for i, row in enumerate(efficiency)
    )
    embed.add_field(
        name="📈 ТОП ПО ЭФФЕКТИВНОСТИ",
        value=efficiency_text or "Недостаточно данных (минимум 3 одобренных смены)",
        inline=False,
    )
    add_top("📋 ТОП ПО СМЕНАМ", "shifts", " смен")
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

        try:
            await publish_shift_message(inter.guild, shift_id)
        except Exception:
            logger.exception("Не удалось опубликовать созданную через панель смену #%s", shift_id)
            try:
                await shift_service.cancel_shift(
                    inter.author.id, shift_id, "Автоотмена: карточку смены не удалось опубликовать"
                )
            except Exception:
                logger.exception("Не удалось автоотменить непубликованную смену #%s", shift_id)
            return await inter.edit_original_response(
                content=(f"❌ Смена **#{shift_id}** не опубликована и автоматически отменена. "
                         "Проверьте канал смен и права бота.")
            )
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
        if not is_recruiter_or_higher(inter.author):
            await inter.response.send_message("❌ Панель доступна только действующим рекрутерам и старшему составу.", ephemeral=True)
            return False
        return True

    @disnake.ui.button(label="🚪 Выйти со смены", style=disnake.ButtonStyle.danger, row=1)
    async def confirm(self, button, inter):
        if self.shift_id is None:
            return await inter.response.send_message("❌ Сначала выберите смену.", ephemeral=True)
        await inter.response.send_modal(LeaveShiftModal(self.shift_id))


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
        if not is_recruiter_or_higher(inter.author):
            await inter.response.send_message("❌ Панель доступна только действующим рекрутерам и старшему составу.", ephemeral=True)
            return False
        return True


class InviteIdentityModal(disnake.ui.Modal):
    def __init__(self, target):
        self.target = target
        super().__init__(
            title="👤 Новый инвайт",
            custom_id=f"panel:invite_identity:{target.id}",
            components=[
                disnake.ui.TextInput(label="Статик", custom_id="static", placeholder="12345", required=True, max_length=config.MAX_STATIC_ID_LENGTH),
                disnake.ui.TextInput(label="Имя Фамилия", custom_id="name", placeholder="Ivan Ivanov", required=True, max_length=100),
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction):
        if not is_recruiter_or_higher(inter.author):
            return await inter.response.send_message("❌ Панель доступна только действующим рекрутерам и старшему составу.", ephemeral=True)
        try:
            await invite_service.assert_target_not_blacklisted(
                self.target.id, inter.text_values["static"].strip(), inter.author.id, "BLACKLIST_BLOCK_INVITE"
            )
        except UserFacingError as exc:
            return await inter.response.send_message(
                "🚫 **ИНВАЙТ ЗАБЛОКИРОВАН**\n" + str(exc), ephemeral=True
            )
        view = InviteChecklistView(
            inter.bot,
            inter.author.id,
            self.target,
            inter.text_values["static"].strip(),
            inter.text_values["name"].strip(),
        )
        await inter.response.send_message(
            view.render_state(),
            view=view,
            ephemeral=True,
        )


class InviteUserSelect(disnake.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Выберите приглашённого пользователя", min_values=1, max_values=1)

    async def callback(self, inter: disnake.MessageInteraction):
        target = self.values[0]
        if target.id == inter.author.id:
            return await inter.response.send_message("❌ Нельзя создать инвайт на самого себя.", ephemeral=True)
        if getattr(target, "bot", False):
            return await inter.response.send_message("❌ Нельзя создать инвайт на бота.", ephemeral=True)
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
        if not is_recruiter_or_higher(inter.author):
            await inter.response.send_message("❌ Панель доступна только действующим рекрутерам и старшему составу.", ephemeral=True)
            return False
        return True


INVITE_CHECKLIST_ITEMS = (
    ("ticket", "🎫 Заполнил тикет"),
    ("last_name", "📝 Сменил фамилию"),
    ("organization", "🏢 Вступил в организацию"),
    ("fraction", "⚔️ Вступил во фракцию"),
    ("info", "📢 Прослушал информацию"),
)


class InviteChecklistSelect(disnake.ui.StringSelect):
    def __init__(self):
        options = [disnake.SelectOption(label=label, value=value) for value, label in INVITE_CHECKLIST_ITEMS]
        options.append(disnake.SelectOption(label="❌ Ничего из списка", value="none"))
        super().__init__(placeholder="Выберите выполненные пункты", min_values=1, max_values=5, options=options, row=0)

    async def callback(self, inter):
        selected = set(self.values)
        invalid = "none" in selected and len(selected) > 1
        self.view.selected = None if invalid else selected
        self.view.set_create_enabled(not invalid)
        await inter.response.edit_message(content=self.view.render_state(invalid=invalid), view=self.view)


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
        self.set_create_enabled(False)

    def set_create_enabled(self, enabled: bool) -> None:
        for item in self.children:
            if isinstance(item, disnake.ui.Button) and item.custom_id == "panel:invite_create":
                item.disabled = not enabled

    def render_state(self, invalid: bool = False) -> str:
        lines = [
            f"👤 **Приглашённый:** {self.target.mention} • Discord ID: `{self.target.id}`",
            f"🆔 **Статик:** `{self.static_id}`",
            f"📛 **Имя:** {self.full_name}",
            "",
            "📋 **Выполненные пункты**",
        ]

        selected = self.selected or set()
        if selected == {"none"}:
            lines.extend(f"⬜ {label}" for _, label in INVITE_CHECKLIST_ITEMS)
            lines.extend(["", "❌ **Отмечено: ничего из списка.**", "✅ Выбор сохранён — можно создавать отчёт."])
        else:
            for value, label in INVITE_CHECKLIST_ITEMS:
                lines.append(f"{'✅' if value in selected else '⬜'} {label}")
            if invalid:
                lines.extend([
                    "",
                    "⚠️ **Некорректный выбор:** «Ничего из списка» нельзя выбирать вместе с выполненными пунктами.",
                    "Выберите пункты заново — кнопка создания отчёта пока отключена.",
                ])
            elif selected:
                lines.extend(["", "✅ **Выбор сохранён.** Проверьте список выше и нажмите **Создать отчёт**."])
            else:
                lines.extend(["", "ℹ️ Выберите один или несколько пунктов. После выбора они останутся отмечены в списке выше."])
        return "\n".join(lines)

    async def interaction_check(self, inter):
        if inter.author.id != self.user_id:
            await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True)
            return False
        if not is_recruiter_or_higher(inter.author):
            await inter.response.send_message("❌ Панель доступна только действующим рекрутерам и старшему составу.", ephemeral=True)
            return False
        return True

    @disnake.ui.button(
        label="✅ Создать отчёт",
        style=disnake.ButtonStyle.green,
        row=1,
        custom_id="panel:invite_create",
    )
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

        posted = False
        channel = inter.guild.get_channel(config.REPORTS_CHANNEL_ID)
        if channel:
            try:
                embed = disnake.Embed(title="👤 НОВЫЙ ИНВАЙТ", color=disnake.Color.blue())
                embed.add_field(name="Приглашённый", value=f"{self.target.mention}\nDiscord ID: `{self.target.id}`", inline=True)
                embed.add_field(name="Статик", value=self.static_id, inline=True)
                embed.add_field(name="Имя", value=self.full_name, inline=True)
                embed.add_field(name="Рекрутер", value=f"{inter.author.mention}\nDiscord ID: `{inter.author.id}`", inline=True)
                checklist_text = (
                    f"Тикет: {'✅' if checklist['ticket']=='yes' else '❌'}\n"
                    f"Фамилия: {'✅' if checklist['last_name']=='yes' else '❌'}\n"
                    f"Организация: {'✅' if checklist['organization']=='yes' else '❌'}\n"
                    f"Фракция: {'✅' if checklist['fraction']=='yes' else '❌'}\n"
                    f"Инфо: {'✅' if checklist['info']=='yes' else '❌'}"
                )
                embed.add_field(name="📋 Чек-лист", value=checklist_text, inline=False)
                embed.set_footer(text=f"Инвайт #{invite_id}")
                message = await channel.send(
                    content=f"<@&{config.SENIOR_ROLE_ID}> <@&{config.ADMIN_ROLE_ID}>",
                    embed=embed,
                    view=build_invite_view(invite_id),
                )
                posted = True
                try:
                    await invite_service.set_invite_message_id(invite_id, message.id)
                except Exception:
                    logger.exception("Карточка инвайта #%s создана, но message_id не сохранён", invite_id)
            except Exception:
                logger.exception("Не удалось отправить карточку инвайта #%s", invite_id)
        if posted:
            text = f"✅ Отчёт создан и отправлен на проверку. ID: **#{invite_id}**"
        else:
            warned = await send_control_warning(
                inter.guild,
                f"⚠️ <@&{config.SENIOR_ROLE_ID}> инвайт **#{invite_id}** сохранён в БД, но публичная карточка "
                "не была создана. Проверьте инвайт через панель руководства.",
            )
            text = (f"⚠️ Инвайт **#{invite_id}** сохранён в базе, но карточку в канал отчётов отправить не удалось. "
                    + ("Старший состав уведомлён." if warned else "Откройте панель руководства и сообщите старшему составу."))
        await inter.edit_original_response(content=text, view=None)


class StatsPeriodSelect(disnake.ui.StringSelect):
    def __init__(self):
        options = [disnake.SelectOption(label=p.capitalize(), value=p) for p in ["сегодня", "неделя", "месяц", "всё время"]]
        super().__init__(placeholder="Период статистики", options=options, min_values=1, max_values=1)

    async def callback(self, inter):
        await inter.response.edit_message(embed=await _stats_embed(inter.author, self.values[0]), view=self.view)


class TopPeriodSelect(disnake.ui.StringSelect):
    def __init__(self):
        options = [
            disnake.SelectOption(label="Топ за неделю", value="неделя"),
            disnake.SelectOption(label="Топ за месяц", value="месяц"),
            disnake.SelectOption(label="Топ за всё время", value="всё время"),
        ]
        super().__init__(placeholder="🏆 Рейтинг: выберите период", options=options, min_values=1, max_values=1, row=1)

    async def callback(self, inter):
        await inter.response.edit_message(embed=await _top_embed(self.values[0]), view=self.view)


class StatsView(disnake.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.add_item(StatsPeriodSelect())
        self.add_item(TopPeriodSelect())

    async def interaction_check(self, inter):
        if inter.author.id != self.user_id:
            await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True)
            return False
        if not is_recruiter_or_higher(inter.author):
            await inter.response.send_message("❌ Панель доступна только действующим рекрутерам и старшему составу.", ephemeral=True)
            return False
        return True

    @disnake.ui.button(label="🗓️ Неделя по дням", style=disnake.ButtonStyle.secondary, row=2)
    async def week_by_day(self, button, inter):
        await inter.response.edit_message(embed=await _week_by_day_embed(inter.author.id), view=self)


class ShiftMenuView(disnake.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id

    async def interaction_check(self, inter):
        if inter.author.id != self.user_id:
            await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True)
            return False
        if not is_recruiter_or_higher(inter.author):
            await inter.response.send_message("❌ Панель доступна только действующим рекрутерам и старшему составу.", ephemeral=True)
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
        if not is_recruiter_or_higher(inter.author):
            await inter.response.send_message("❌ Панель доступна только действующим рекрутерам и старшему составу.", ephemeral=True)
            return False
        return True

    @disnake.ui.button(label="➕ Новый инвайт", style=disnake.ButtonStyle.green)
    async def new_invite(self, button, inter):
        await inter.response.send_message("Выберите пользователя Discord, которого пригласили:", view=InviteUserSelectView(inter.author.id), ephemeral=True)

    @disnake.ui.button(label="👥 Мои инвайты", style=disnake.ButtonStyle.primary)
    async def my_invites(self, button, inter):
        await inter.response.send_message(embed=await _my_invites_embed(inter.author.id), ephemeral=True)



class MemberActionSelect(disnake.ui.UserSelect):
    def __init__(self, action: str, bot=None):
        super().__init__(placeholder="Выберите рекрутера", min_values=1, max_values=1)
        self.action = action
        self.bot = bot

    async def callback(self, inter: disnake.MessageInteraction):
        target = self.values[0]
        recruiter_target_actions = {
            "stats", "finance_view", "goals_view", "note", "remove_shift", "goal_set", "goal_delete",
            "profile_view", "finance_accrue", "finance_pay"
        }
        if self.action in recruiter_target_actions and not is_recruiter_or_higher(target):
            return await inter.response.send_message(
                "❌ Выбранный пользователь не относится к рекрутерам/старшему составу.",
                ephemeral=True,
            )
        if self.action == "stats":
            return await inter.response.send_message(
                embed=await _stats_embed(target, "неделя"),
                view=StatsForUserView(inter.author.id, target),
                ephemeral=True,
            )
        if self.action == "finance_view":
            return await inter.response.send_message(embed=await _finance_embed(target.id, f"💰 ФИНАНСЫ | {target.display_name}"), ephemeral=True)
        if self.action == "goals_view":
            return await inter.response.send_message(embed=await _goals_embed(target.id), ephemeral=True)
        if self.action == "profile_view":
            try:
                embed = await _profile_embed(target)
            except UserFacingError as exc:
                return await inter.response.send_message(f"❌ {exc}", ephemeral=True)
            return await inter.response.send_message(embed=embed, ephemeral=True)
        if self.action == "note":
            return await inter.response.send_modal(PanelUserNoteModal(target))
        if self.action == "remove_shift":
            return await inter.response.send_modal(RemoveRecruiterModal(target))
        if self.action == "goal_set":
            return await inter.response.send_message(
                f"Настройте цель для {target.mention}:",
                view=GoalSetupView(inter.author.id, target, self.bot),
                ephemeral=True,
            )
        if self.action == "goal_delete":
            return await _delete_goals_from_panel(inter, target, self.bot)
        if self.action == "finance_accrue":
            return await inter.response.send_modal(FinanceAccrueModal(target, self.bot))
        if self.action == "finance_pay":
            return await inter.response.send_modal(FinancePayModal(target, self.bot))
        if self.action == "database_user":
            overview = await database_service.get_user_overview(target.id)
            if overview is None:
                return await inter.response.send_message("❌ Пользователь ещё отсутствует в базе.", ephemeral=True)
            from cogs.database_admin import DatabaseUserView, build_user_embed
            return await inter.response.send_message(
                embed=await build_user_embed(target, overview),
                view=DatabaseUserView(inter.author.id, target),
                ephemeral=True,
            )


class MemberActionView(disnake.ui.View):
    def __init__(self, user_id: int, action: str, bot=None):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.action = action
        self.add_item(MemberActionSelect(action, bot))

    async def interaction_check(self, inter):
        if inter.author.id != self.user_id:
            await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True)
            return False
        admin_actions = {"finance_accrue", "finance_pay", "database_user"}
        senior_actions = {"stats", "finance_view", "goals_view", "note", "remove_shift", "goal_set", "goal_delete"}
        if self.action in admin_actions and not _is_admin(inter.author):
            await inter.response.send_message("❌ Доступ только администратору.", ephemeral=True)
            return False
        if self.action in senior_actions and not is_senior_or_admin(inter.author):
            await inter.response.send_message("❌ Доступ только старшему составу.", ephemeral=True)
            return False
        return True


class StatsForUserPeriodSelect(disnake.ui.StringSelect):
    def __init__(self, target):
        self.target = target
        super().__init__(
            placeholder="Период статистики рекрутера",
            options=[disnake.SelectOption(label=p.capitalize(), value=p) for p in ["сегодня", "неделя", "месяц", "всё время"]],
            min_values=1,
            max_values=1,
        )

    async def callback(self, inter):
        await inter.response.edit_message(embed=await _stats_embed(self.target, self.values[0]), view=self.view)


class StatsForUserView(disnake.ui.View):
    def __init__(self, actor_id: int, target):
        super().__init__(timeout=300)
        self.actor_id = actor_id
        self.target = target
        self.add_item(StatsForUserPeriodSelect(target))

    async def interaction_check(self, inter):
        if inter.author.id != self.actor_id:
            await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True)
            return False
        if not is_senior_or_admin(inter.author):
            await inter.response.send_message("❌ Доступ только старшему составу.", ephemeral=True)
            return False
        return True


class ProfileMenuView(disnake.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id

    async def interaction_check(self, inter):
        if inter.author.id != self.user_id:
            await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True)
            return False
        if not is_recruiter_or_higher(inter.author):
            await inter.response.send_message("❌ Панель доступна только действующим рекрутерам и старшему составу.", ephemeral=True)
            return False
        return True

    @disnake.ui.button(label="👤 Мой профиль", style=disnake.ButtonStyle.primary)
    async def mine(self, button, inter):
        try:
            embed = await _profile_embed(inter.author)
        except UserFacingError as exc:
            return await inter.response.send_message(f"❌ {exc}", ephemeral=True)
        await inter.response.send_message(embed=embed, ephemeral=True)

    @disnake.ui.button(label="🔎 Профиль рекрутера", style=disnake.ButtonStyle.secondary)
    async def other(self, button, inter):
        await inter.response.send_message("Выберите пользователя:", view=MemberActionView(inter.author.id, "profile_view"), ephemeral=True)


class PanelUserNoteModal(disnake.ui.Modal):
    def __init__(self, target):
        self.target = target
        super().__init__(title=f"Заметка: {target.display_name}"[:45], components=[
            disnake.ui.TextInput(label="Заметка", custom_id="note", style=disnake.TextInputStyle.paragraph, required=True, max_length=1000)
        ])

    async def callback(self, inter):
        if not is_senior_or_admin(inter.author):
            return await inter.response.send_message("❌ Доступ только старшему составу.", ephemeral=True)
        try:
            await database_service.add_user_note(self.target.id, self.target.name, inter.text_values["note"], inter.author.id, inter.author.name)
        except UserFacingError as exc:
            return await inter.response.send_message(f"❌ {exc}", ephemeral=True)
        await inter.response.send_message(f"✅ Заметка добавлена для {self.target.mention}.", ephemeral=True)


class RemoveRecruiterModal(disnake.ui.Modal):
    def __init__(self, target):
        self.target = target
        super().__init__(title=f"Снять со смены: {target.display_name}"[:45], components=[
            disnake.ui.TextInput(label="ID смены (если несколько)", custom_id="shift_id", required=False, max_length=10),
            disnake.ui.TextInput(label="Причина", custom_id="reason", required=True, max_length=1000, style=disnake.TextInputStyle.paragraph),
        ])

    async def callback(self, inter):
        if not is_senior_or_admin(inter.author):
            return await inter.response.send_message("❌ Доступ только старшему составу.", ephemeral=True)
        raw = inter.text_values.get("shift_id", "").strip()
        try:
            shift_id = int(raw) if raw else None
            changed = await shift_service.remove_member(inter.author.id, self.target.id, inter.text_values["reason"], shift_id)
        except (ValueError, UserFacingError) as exc:
            return await inter.response.send_message(f"❌ {exc}", ephemeral=True)
        dm = disnake.Embed(title="⚠️ ВЫ СНЯТЫ СО СМЕНЫ", color=disnake.Color.orange())
        dm.add_field(name="📋 Смена", value=f"#{changed}", inline=True)
        dm.add_field(name="👤 Снял", value=inter.author.mention, inline=True)
        dm.add_field(name="📝 Причина", value=inter.text_values["reason"], inline=False)
        await notify(inter.bot, self.target.id, "REMOVED_FROM_SHIFT", "shift", changed, embed=dm)
        await inter.response.send_message(f"✅ {self.target.mention} снят со смены **#{changed}**.", ephemeral=True)
        await update_shift_message(inter.guild, changed)


class CancelShiftModal(disnake.ui.Modal):
    def __init__(self):
        super().__init__(title="Отменить смену", components=[
            disnake.ui.TextInput(label="ID смены", custom_id="shift_id", required=True, max_length=10),
            disnake.ui.TextInput(label="Причина", custom_id="reason", required=True, max_length=1000, style=disnake.TextInputStyle.paragraph),
        ])

    async def callback(self, inter):
        if not is_senior_or_admin(inter.author):
            return await inter.response.send_message("❌ Доступ только старшему составу.", ephemeral=True)
        try:
            shift_id = int(inter.text_values["shift_id"].strip())
            members = await shift_service.cancel_shift(inter.author.id, shift_id, inter.text_values["reason"])
        except (ValueError, UserFacingError) as exc:
            return await inter.response.send_message(f"❌ {exc}", ephemeral=True)
        for member in members:
            dm = disnake.Embed(title="⚠️ СМЕНА ОТМЕНЕНА", color=disnake.Color.red())
            dm.add_field(name="📋 Смена", value=f"#{shift_id}", inline=True)
            dm.add_field(name="📝 Причина", value=inter.text_values["reason"], inline=False)
            await notify(inter.bot, member["user_id"], "SHIFT_CANCELLED", "shift", shift_id, embed=dm)
        await inter.response.send_message(f"✅ Смена **#{shift_id}** отменена.", ephemeral=True)
        await update_shift_message(inter.guild, shift_id)


async def _pending_reports_embed():
    rows = await db.fetchall("SELECT * FROM shift_reports WHERE status='pending' ORDER BY id DESC LIMIT 20")
    embed = disnake.Embed(title="📋 ОТЧЁТЫ НА ПРОВЕРКЕ", color=disnake.Color.yellow())
    if not rows:
        embed.description = "Нет отчётов на проверке."
    for row in rows:
        embed.add_field(name=f"Отчёт #{row['id']} • смена #{row['shift_id']}", value=f"<@{row['user_id']}> • принято: **{row['total_accepted']}**", inline=False)
    return embed


class ApproveReportByIdModal(disnake.ui.Modal):
    def __init__(self):
        super().__init__(title="Одобрить отчёт", components=[disnake.ui.TextInput(label="ID отчёта", custom_id="id", required=True, max_length=10)])
    async def callback(self, inter):
        if not is_senior_or_admin(inter.author):
            return await inter.response.send_message("❌ Доступ только старшему составу.", ephemeral=True)
        try:
            report_id = int(inter.text_values["id"].strip())
        except ValueError:
            return await inter.response.send_message("❌ ID отчёта должен быть числом.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        try:
            report = await shift_service.approve_report(report_id, inter.author.id)
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")
        dm = disnake.Embed(title="✅ ВАШ ОТЧЁТ ОДОБРЕН", color=disnake.Color.green())
        dm.add_field(name="📋 Смена", value=f"#{report['shift_id']}", inline=True)
        dm.add_field(name="👥 Принято", value=str(report["total_accepted"]), inline=True)
        dm_sent = await notify(inter.bot, report["user_id"], "REPORT_APPROVED", "shift_report", report_id, embed=dm)
        synced = await sync_report_review_message(inter.guild, report_id, True, inter.author.mention)
        warnings=[]
        if not synced: warnings.append("публичная карточка не обновилась")
        if not dm_sent: warnings.append("ЛС рекрутеру не доставлено")
        text=f"✅ Отчёт **#{report_id}** одобрен."
        if warnings: text += "\n⚠️ " + "; ".join(warnings) + ". Данные в БД сохранены."
        await inter.edit_original_response(content=text)


class RejectReportByIdModal(disnake.ui.Modal):
    def __init__(self):
        super().__init__(title="Отклонить отчёт", components=[
            disnake.ui.TextInput(label="ID отчёта", custom_id="id", required=True, max_length=10),
            disnake.ui.TextInput(label="Причина", custom_id="reason", required=True, max_length=1000, style=disnake.TextInputStyle.paragraph),
        ])
    async def callback(self, inter):
        if not is_senior_or_admin(inter.author):
            return await inter.response.send_message("❌ Доступ только старшему составу.", ephemeral=True)
        try:
            report_id = int(inter.text_values["id"].strip())
        except ValueError:
            return await inter.response.send_message("❌ ID отчёта должен быть числом.", ephemeral=True)
        reason=inter.text_values["reason"]
        await inter.response.defer(ephemeral=True)
        try:
            report = await shift_service.reject_report(report_id, inter.author.id, reason)
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")
        dm = disnake.Embed(title="❌ ВАШ ОТЧЁТ ОТКЛОНЁН", color=disnake.Color.red())
        dm.add_field(name="📋 Смена", value=f"#{report['shift_id']}", inline=True)
        dm.add_field(name="📝 Причина", value=reason, inline=False)
        dm_sent = await notify(inter.bot, report["user_id"], "REPORT_REJECTED", "shift_report", report_id, embed=dm)
        synced = await sync_report_review_message(inter.guild, report_id, False, inter.author.mention, reason)
        warnings=[]
        if not synced: warnings.append("публичная карточка не обновилась")
        if not dm_sent: warnings.append("ЛС рекрутеру не доставлено")
        text=f"✅ Отчёт **#{report_id}** отклонён."
        if warnings: text += "\n⚠️ " + "; ".join(warnings) + ". Данные в БД сохранены."
        await inter.edit_original_response(content=text)


class PendingReportSelect(disnake.ui.StringSelect):
    def __init__(self, rows):
        options = [
            disnake.SelectOption(
                label=f"Отчёт #{r['id']} • смена #{r['shift_id']}",
                value=str(r["id"]),
                description=f"Рекрутер {r['user_id']} • принято {r['total_accepted']}",
            )
            for r in rows[:25]
        ]
        super().__init__(placeholder="Выберите отчёт для проверки", options=options, min_values=1, max_values=1)

    async def callback(self, inter):
        report_id = int(self.values[0])
        report = await db.fetchone("SELECT * FROM shift_reports WHERE id=? AND status='pending'", (report_id,))
        if not report:
            return await inter.response.send_message("❌ Отчёт уже обработан или не найден.", ephemeral=True)
        member = await db.fetchone("SELECT * FROM shift_members WHERE id=?", (report["member_id"],))
        if not member:
            return await inter.response.send_message("❌ Запись участника смены не найдена.", ephemeral=True)
        embed = EmbedGenerator.create_report_embed(report, member, f"<@{report['user_id']}>")
        embed.title = "📋 ОТЧЁТ НА ПРОВЕРКЕ"
        await inter.response.send_message(embed=embed, view=build_report_view(report_id), ephemeral=True)


class PendingReportSelectView(disnake.ui.View):
    def __init__(self, user_id: int, rows):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.add_item(PendingReportSelect(rows))

    async def interaction_check(self, inter):
        if inter.author.id != self.user_id:
            await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True)
            return False
        if not is_senior_or_admin(inter.author):
            await inter.response.send_message("❌ Доступ только старшему составу.", ephemeral=True)
            return False
        return True


class ReportSeniorMenuView(disnake.ui.View):
    def __init__(self, user_id):
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

    @disnake.ui.button(label="📋 Выбрать отчёт", style=disnake.ButtonStyle.primary)
    async def pending(self, button, inter):
        rows = await db.fetchall("SELECT * FROM shift_reports WHERE status='pending' ORDER BY id DESC LIMIT 25")
        if not rows:
            return await inter.response.send_message("✅ Отчётов на проверке нет.", ephemeral=True)
        await inter.response.send_message(
            "Выберите отчёт — откроется его карточка с кнопками **Одобрить / Отклонить**:",
            view=PendingReportSelectView(inter.author.id, rows),
            ephemeral=True,
        )

    @disnake.ui.button(label="🔧 Резерв: одобрить ID", style=disnake.ButtonStyle.secondary)
    async def approve(self, button, inter):
        await inter.response.send_modal(ApproveReportByIdModal())

    @disnake.ui.button(label="🔧 Резерв: отклонить ID", style=disnake.ButtonStyle.secondary)
    async def reject(self, button, inter):
        await inter.response.send_modal(RejectReportByIdModal())


class InviteApproveByIdModal(disnake.ui.Modal):
    def __init__(self):
        super().__init__(title="Принять инвайт", components=[
            disnake.ui.TextInput(label="ID инвайта", custom_id="id", required=True, max_length=10),
            disnake.ui.TextInput(label="Начисление (0 = без начисления)", custom_id="amount", required=True, value="0", max_length=20),
        ])
    async def callback(self, inter):
        if not is_senior_or_admin(inter.author):
            return await inter.response.send_message("❌ Доступ только старшему составу.", ephemeral=True)
        try:
            invite_id=int(inter.text_values["id"].strip())
            amount=normalize_amount(inter.text_values["amount"])
        except ValueError as exc:
            return await inter.response.send_message(f"❌ {exc}", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        try:
            invite,_=await invite_service.approve_invite(invite_id, inter.author.id, amount)
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")
        dm=disnake.Embed(title="✅ ИНВАЙТ ОДОБРЕН", color=disnake.Color.green())
        dm.add_field(name="Статик", value=invite["static_id"])
        if amount>0: dm.add_field(name="Начислено", value=money(amount))
        dm_sent=await notify(inter.bot, invite["invited_by"], "INVITE_APPROVED", "invite", invite_id, embed=dm)
        synced=await sync_invite_review_message(inter.guild, invite_id, True, inter.author.mention, amount)
        warnings=[]
        if not synced: warnings.append("публичная карточка не обновилась")
        if not dm_sent: warnings.append("ЛС рекрутеру не доставлено")
        text=f"✅ Инвайт **#{invite_id}** принят."
        if warnings: text += "\n⚠️ " + "; ".join(warnings) + ". Данные в БД сохранены."
        await inter.edit_original_response(content=text)


class InviteRejectByIdModal(disnake.ui.Modal):
    def __init__(self):
        super().__init__(title="Отклонить инвайт", components=[
            disnake.ui.TextInput(label="ID инвайта", custom_id="id", required=True, max_length=10),
            disnake.ui.TextInput(label="Причина", custom_id="reason", required=True, max_length=1000, style=disnake.TextInputStyle.paragraph),
        ])
    async def callback(self, inter):
        if not is_senior_or_admin(inter.author):
            return await inter.response.send_message("❌ Доступ только старшему составу.", ephemeral=True)
        try:
            invite_id=int(inter.text_values["id"].strip())
        except ValueError:
            return await inter.response.send_message("❌ ID инвайта должен быть числом.", ephemeral=True)
        reason=inter.text_values["reason"]
        await inter.response.defer(ephemeral=True)
        try:
            invite=await invite_service.reject_invite(invite_id, inter.author.id, reason)
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")
        dm=disnake.Embed(title="❌ ИНВАЙТ ОТКЛОНЁН", color=disnake.Color.red())
        dm.add_field(name="Статик", value=invite["static_id"])
        dm.add_field(name="Причина", value=reason, inline=False)
        dm_sent=await notify(inter.bot, invite["invited_by"], "INVITE_REJECTED", "invite", invite_id, embed=dm)
        synced=await sync_invite_review_message(inter.guild, invite_id, False, inter.author.mention, reason=reason)
        warnings=[]
        if not synced: warnings.append("публичная карточка не обновилась")
        if not dm_sent: warnings.append("ЛС рекрутеру не доставлено")
        text=f"✅ Инвайт **#{invite_id}** отклонён."
        if warnings: text += "\n⚠️ " + "; ".join(warnings) + ". Данные в БД сохранены."
        await inter.edit_original_response(content=text)


class InviteLookupModal(disnake.ui.Modal):
    def __init__(self, mode: str):
        self.mode=mode
        title={"base":"Поиск в базе", "info":"Информация по статику", "note":"Заметка к инвайту"}[mode]
        comps=[disnake.ui.TextInput(label="Статик", custom_id="static", required=(mode!="base"), max_length=config.MAX_STATIC_ID_LENGTH)]
        if mode=="note": comps.append(disnake.ui.TextInput(label="Заметка", custom_id="note", required=True, max_length=1000, style=disnake.TextInputStyle.paragraph))
        super().__init__(title=title, components=comps)
    async def callback(self, inter):
        if not is_senior_or_admin(inter.author):
            return await inter.response.send_message("❌ Доступ только старшему составу.", ephemeral=True)
        static=inter.text_values.get("static","").strip()
        if self.mode=="base":
            if static: rows=await db.fetchall("SELECT * FROM invites WHERE status='accepted' AND static_id LIKE ? ORDER BY created_at DESC LIMIT 20",(f"%{static}%",))
            else: rows=await db.fetchall("SELECT * FROM invites WHERE status='accepted' ORDER BY created_at DESC LIMIT 20")
            em=disnake.Embed(title="📋 БАЗА ПРИНЯТЫХ", color=disnake.Color.blue())
            if not rows: em.description="Ничего не найдено."
            for r in rows:
                target=f"<@{r['user_id']}> • ID: `{r['user_id']}`" if r["user_id"] else "Discord не привязан"
                blocked=await blacklist_service.get_active_match(r["user_id"],r["static_id"])
                marker=f"\n🚫 Сейчас в ЧС: запись #{blocked['id']}" if blocked else ""
                em.add_field(name=f"{r['static_id']} • {r['full_name'] or '—'}", value=f"Discord: {target}\nРекрутер: <@{r['invited_by']}> • ID: `{r['invited_by']}`{marker}", inline=False)
            return await inter.response.send_message(embed=em, ephemeral=True)
        inv=await db.fetchone("SELECT * FROM invites WHERE static_id=? ORDER BY id DESC LIMIT 1",(static,))
        if not inv: return await inter.response.send_message("❌ Статик не найден.", ephemeral=True)
        if self.mode=="info":
            em=disnake.Embed(title=f"👤 {static}", color=disnake.Color.blue())
            target=f"<@{inv['user_id']}> • ID: `{inv['user_id']}`" if inv["user_id"] else "Discord не привязан (legacy-запись)"
            em.add_field(name="Discord",value=target,inline=False); em.add_field(name="Имя",value=inv['full_name'] or '—'); em.add_field(name="Статус",value=inv['status']); em.add_field(name="Рекрутер",value=f"<@{inv['invited_by']}> • ID: `{inv['invited_by']}`"); em.add_field(name="Заметки",value=(inv['notes'] or '—')[-1000:],inline=False)
            blocked=await blacklist_service.get_active_match(inv["user_id"],inv["static_id"])
            if blocked: em.add_field(name="🚫 Чёрный список",value=f"Запись **#{blocked['id']}**\n{_blacklist_identity(blocked)}\nПричина: {(blocked['reason'] or '—')[:700]}",inline=False)
            return await inter.response.send_message(embed=em, ephemeral=True)
        try:
            await invite_service.add_invite_note(inv["id"], inter.text_values["note"], inter.author.id, inter.author.name)
        except UserFacingError as exc:
            return await inter.response.send_message(f"❌ {exc}", ephemeral=True)
        await inter.response.send_message(f"✅ Заметка добавлена к **{static}**.", ephemeral=True)


class PendingInviteSelect(disnake.ui.StringSelect):
    def __init__(self, rows):
        options = [
            disnake.SelectOption(
                label=f"#{r['id']} • {r['static_id']} • {(r['full_name'] or '—')[:45]}",
                value=str(r["id"]),
                description=f"Рекрутер {r['invited_by']}",
            )
            for r in rows[:25]
        ]
        super().__init__(placeholder="Выберите инвайт для проверки", options=options, min_values=1, max_values=1)

    async def callback(self, inter):
        invite_id = int(self.values[0])
        inv = await db.fetchone("SELECT * FROM invites WHERE id=? AND status='pending'", (invite_id,))
        if not inv:
            return await inter.response.send_message("❌ Инвайт уже обработан или не найден.", ephemeral=True)
        blocked=await blacklist_service.get_active_match(inv["user_id"],inv["static_id"])
        embed = disnake.Embed(title="🚫 ИНВАЙТ ЗАБЛОКИРОВАН ЧС" if blocked else "👤 ИНВАЙТ НА ПРОВЕРКЕ", color=disnake.Color.red() if blocked else disnake.Color.yellow())
        target=f"<@{inv['user_id']}> • ID: `{inv['user_id']}`" if inv["user_id"] else "Discord не привязан (legacy-запись)"
        embed.add_field(name="Приглашённый", value=target, inline=False)
        embed.add_field(name="Статик", value=inv["static_id"], inline=True)
        embed.add_field(name="Имя", value=inv["full_name"] or "—", inline=True)
        embed.add_field(name="Рекрутер", value=f"<@{inv['invited_by']}>", inline=True)
        checklist = (
            f"🎫 Тикет: {'✅' if inv['ticket']=='yes' else '❌'}\n"
            f"📝 Фамилия: {'✅' if inv['last_name_changed']=='yes' else '❌'}\n"
            f"🏢 Организация: {'✅' if inv['organization']=='yes' else '❌'}\n"
            f"⚔️ Фракция: {'✅' if inv['fraction']=='yes' else '❌'}\n"
            f"📢 Инфо: {'✅' if inv['info']=='yes' else '❌'}"
        )
        embed.add_field(name="📋 Чек-лист", value=checklist, inline=False)
        if blocked:
            embed.add_field(name="🚫 Причина блокировки",value=f"ЧС **#{blocked['id']}** • {_blacklist_identity(blocked)}\n{(blocked['reason'] or '—')[:800]}",inline=False)
        embed.set_footer(text=f"Инвайт #{invite_id}")
        await inter.response.send_message(embed=embed, view=build_invite_view(invite_id,accept_disabled=bool(blocked)), ephemeral=True)


class PendingInviteSelectView(disnake.ui.View):
    def __init__(self, user_id: int, rows):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.add_item(PendingInviteSelect(rows))

    async def interaction_check(self, inter):
        if inter.author.id != self.user_id:
            await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True)
            return False
        if not is_senior_or_admin(inter.author):
            await inter.response.send_message("❌ Доступ только старшему составу.", ephemeral=True)
            return False
        return True


class SeniorInviteMenuView(disnake.ui.View):
    def __init__(self, user_id):
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

    @disnake.ui.button(label="📋 Выбрать инвайт", style=disnake.ButtonStyle.primary, row=0)
    async def pending(self, button, inter):
        rows = await db.fetchall("SELECT * FROM invites WHERE status='pending' ORDER BY id DESC LIMIT 25")
        if not rows:
            return await inter.response.send_message("✅ Инвайтов на проверке нет.", ephemeral=True)
        await inter.response.send_message(
            "Выберите инвайт — откроется карточка с кнопками **Принять / Отклонить**:",
            view=PendingInviteSelectView(inter.author.id, rows),
            ephemeral=True,
        )

    @disnake.ui.button(label="🔧 Резерв: принять ID", style=disnake.ButtonStyle.secondary, row=0)
    async def approve(self, button, inter):
        await inter.response.send_modal(InviteApproveByIdModal())

    @disnake.ui.button(label="🔧 Резерв: отклонить ID", style=disnake.ButtonStyle.secondary, row=0)
    async def reject(self, button, inter):
        await inter.response.send_modal(InviteRejectByIdModal())

    @disnake.ui.button(label="📚 База", style=disnake.ButtonStyle.secondary, row=1)
    async def base(self, button, inter):
        await inter.response.send_modal(InviteLookupModal("base"))

    @disnake.ui.button(label="🔎 Инфо", style=disnake.ButtonStyle.secondary, row=1)
    async def info(self, button, inter):
        await inter.response.send_modal(InviteLookupModal("info"))

    @disnake.ui.button(label="📝 Заметка", style=disnake.ButtonStyle.secondary, row=1)
    async def note(self, button, inter):
        await inter.response.send_modal(InviteLookupModal("note"))


class GoalTypeSelect(disnake.ui.StringSelect):
    def __init__(self):
        super().__init__(
            placeholder="1. Выберите тип цели",
            options=[
                disnake.SelectOption(label="Люди", value="люди", emoji="👥"),
                disnake.SelectOption(label="Смены", value="смены", emoji="📋"),
                disnake.SelectOption(label="Часы", value="часы", emoji="⏱️"),
            ],
            min_values=1,
            max_values=1,
            row=0,
        )

    async def callback(self, inter):
        self.view.goal_type = self.values[0]
        await self.view.refresh(inter)


class GoalPeriodSelect(disnake.ui.StringSelect):
    def __init__(self):
        super().__init__(
            placeholder="2. Выберите период",
            options=[
                disnake.SelectOption(label="День", value="день"),
                disnake.SelectOption(label="Неделя", value="неделя", default=True),
                disnake.SelectOption(label="Месяц", value="месяц"),
            ],
            min_values=1,
            max_values=1,
            row=1,
        )

    async def callback(self, inter):
        self.view.period = self.values[0]
        await self.view.refresh(inter)


class GoalValueModal(disnake.ui.Modal):
    def __init__(self, target, bot, goal_type: str, period: str):
        self.target = target
        self.bot = bot
        self.goal_type = goal_type
        self.period = period
        super().__init__(
            title=f"Цель: {target.display_name}"[:45],
            components=[
                disnake.ui.TextInput(
                    label=f"Значение ({goal_type}, {period})"[:45],
                    custom_id="value",
                    required=True,
                    max_length=10,
                )
            ],
        )

    async def callback(self, inter):
        if not is_senior_or_admin(inter.author):
            return await inter.response.send_message("❌ Доступ только старшему составу.", ephemeral=True)
        if not is_recruiter_or_higher(self.target):
            return await inter.response.send_message("❌ Пользователь больше не относится к рекрутерам/старшему составу.", ephemeral=True)
        try:
            value = int(inter.text_values["value"].strip())
        except ValueError:
            return await inter.response.send_message("❌ Значение должно быть целым числом.", ephemeral=True)
        try:
            gid = await goal_service.set_goal(
                self.target.id, self.target.name, self.goal_type, value, self.period, inter.author.id
            )
        except UserFacingError as exc:
            return await inter.response.send_message(f"❌ {exc}", ephemeral=True)
        current = await goal_service.calculate_progress(self.target.id, self.goal_type, self.period)
        dm = disnake.Embed(title="🎯 ВАМ ПОСТАВЛЕНА НОВАЯ ЦЕЛЬ", color=disnake.Color.blue())
        dm.add_field(name="Цель", value=f"{self.goal_type}: {value}")
        dm.add_field(name="Период", value=self.period)
        dm.add_field(name="Прогресс", value=f"{current} / {value}")
        await notify(self.bot, self.target.id, "GOAL_SET", "goal", gid, embed=dm)
        await inter.response.send_message(f"✅ Цель поставлена для {self.target.mention}.", ephemeral=True)


class GoalSetupView(disnake.ui.View):
    def __init__(self, actor_id: int, target, bot):
        super().__init__(timeout=300)
        self.actor_id = actor_id
        self.target = target
        self.bot = bot
        self.goal_type = None
        self.period = "неделя"
        self.add_item(GoalTypeSelect())
        self.add_item(GoalPeriodSelect())

    async def interaction_check(self, inter):
        if inter.author.id != self.actor_id:
            await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True)
            return False
        if not is_senior_or_admin(inter.author):
            await inter.response.send_message("❌ Доступ только старшему составу.", ephemeral=True)
            return False
        return True

    def summary(self) -> str:
        return (
            f"Цель для {self.target.mention}\n"
            f"Тип: **{self.goal_type or 'не выбран'}**\n"
            f"Период: **{self.period}**\n"
            "После выбора нажмите **Ввести значение**."
        )

    async def refresh(self, inter):
        await inter.response.edit_message(content=self.summary(), view=self)

    @disnake.ui.button(label="✏️ Ввести значение", style=disnake.ButtonStyle.green, row=2)
    async def value(self, button, inter):
        if not self.goal_type:
            return await inter.response.send_message("❌ Сначала выберите тип цели.", ephemeral=True)
        await inter.response.send_modal(GoalValueModal(self.target, self.bot, self.goal_type, self.period))


async def _delete_goals_from_panel(inter,target,bot):
    goals = await goal_service.delete_active_goals(target.id, inter.author.id)
    if not goals:
        return await inter.response.send_message("Нет активных целей.", ephemeral=True)
    dm=disnake.Embed(title="🗑️ ЦЕЛИ УДАЛЕНЫ",color=disnake.Color.red())
    dm.description="Старший состав удалил ваши активные цели."
    marker=max(g["id"] for g in goals)
    await notify(bot,target.id,"GOAL_DELETED","goal_batch",marker,embed=dm)
    await inter.response.send_message(f"✅ Активные цели {target.mention} удалены.",ephemeral=True)


class SeniorGoalsMenuView(disnake.ui.View):
    def __init__(self,bot,user_id): super().__init__(timeout=300); self.bot=bot; self.user_id=user_id
    async def interaction_check(self,inter):
        if inter.author.id!=self.user_id: await inter.response.send_message("❌ Это не ваше меню.",ephemeral=True); return False
        if not is_senior_or_admin(inter.author): await inter.response.send_message("❌ Доступ только старшему составу.",ephemeral=True); return False
        return True
    @disnake.ui.button(label="➕ Поставить",style=disnake.ButtonStyle.green)
    async def set(self,b,i): await i.response.send_message("Выберите рекрутера:",view=MemberActionView(i.author.id,"goal_set",self.bot),ephemeral=True)
    @disnake.ui.button(label="🎯 Посмотреть",style=disnake.ButtonStyle.primary)
    async def view(self,b,i): await i.response.send_message("Выберите рекрутера:",view=MemberActionView(i.author.id,"goals_view"),ephemeral=True)
    @disnake.ui.button(label="🗑️ Удалить",style=disnake.ButtonStyle.red)
    async def delete(self,b,i): await i.response.send_message("Выберите рекрутера:",view=MemberActionView(i.author.id,"goal_delete",self.bot),ephemeral=True)


class FinanceAccrueModal(disnake.ui.Modal):
    def __init__(self,target,bot): self.target=target; self.bot=bot; super().__init__(title=f"Начислить: {target.display_name}"[:45],components=[disnake.ui.TextInput(label="Сумма",custom_id="amount",required=True,max_length=20),disnake.ui.TextInput(label="Причина",custom_id="reason",required=False,max_length=500)])
    async def callback(self,inter):
        if not _is_admin(inter.author):
            return await inter.response.send_message("❌ Доступ только администратору.", ephemeral=True)
        if not is_recruiter_or_higher(self.target):
            return await inter.response.send_message("❌ Пользователь больше не относится к рекрутерам/старшему составу.", ephemeral=True)
        try:
            amount=normalize_amount(inter.text_values['amount'])
            fid,balance=await finance_service.accrue(self.target.id,self.target.name,amount,inter.text_values.get('reason',''),inter.author.id)
        except (UserFacingError,ValueError) as exc: return await inter.response.send_message(f"❌ {exc}",ephemeral=True)
        dm=disnake.Embed(title="💰 ВАМ НАЧИСЛЕНЫ ДЕНЬГИ",color=disnake.Color.green()); dm.add_field(name="Сумма",value=money(amount)); dm.add_field(name="К выплате",value=money(balance[2])); await notify(self.bot,self.target.id,"FIN_ACCRUE","finance",fid,embed=dm)
        await inter.response.send_message(f"✅ Начислено {money(amount)} для {self.target.mention}.",ephemeral=True)


class FinancePayModal(disnake.ui.Modal):
    def __init__(self,target,bot): self.target=target; self.bot=bot; super().__init__(title=f"Выплатить: {target.display_name}"[:45],components=[disnake.ui.TextInput(label="Сумма",custom_id="amount",required=True,max_length=20)])
    async def callback(self,inter):
        if not _is_admin(inter.author):
            return await inter.response.send_message("❌ Доступ только администратору.", ephemeral=True)
        if not is_recruiter_or_higher(self.target):
            return await inter.response.send_message("❌ Пользователь больше не относится к рекрутерам/старшему составу.", ephemeral=True)
        try:
            amount=normalize_amount(inter.text_values['amount'])
            fid,balance=await finance_service.pay(self.target.id,self.target.name,amount,inter.author.id)
        except (UserFacingError,ValueError) as exc: return await inter.response.send_message(f"❌ {exc}",ephemeral=True)
        dm=disnake.Embed(title="💸 ЗАРПЛАТА ВЫПЛАЧЕНА",color=disnake.Color.blue()); dm.add_field(name="Сумма",value=money(amount)); dm.add_field(name="Остаток",value=money(balance[2])); await notify(self.bot,self.target.id,"FIN_PAY","finance",fid,embed=dm)
        await inter.response.send_message(f"✅ Выплачено {money(amount)} для {self.target.mention}.",ephemeral=True)


class AdminFinanceMenuView(disnake.ui.View):
    def __init__(self,bot,user_id): super().__init__(timeout=300); self.bot=bot; self.user_id=user_id
    async def interaction_check(self,inter):
        if inter.author.id!=self.user_id: await inter.response.send_message("❌ Это не ваше меню.",ephemeral=True); return False
        if not _is_admin(inter.author): await inter.response.send_message("❌ Доступ только администратору.",ephemeral=True); return False
        return True
    @disnake.ui.button(label="➕ Начислить",style=disnake.ButtonStyle.green,row=0)
    async def accrue(self,b,i): await i.response.send_message("Выберите рекрутера:",view=MemberActionView(i.author.id,"finance_accrue",self.bot),ephemeral=True)
    @disnake.ui.button(label="💸 Выплатить",style=disnake.ButtonStyle.primary,row=0)
    async def pay(self,b,i): await i.response.send_message("Выберите рекрутера:",view=MemberActionView(i.author.id,"finance_pay",self.bot),ephemeral=True)
    @disnake.ui.button(label="🔎 Сверить",style=disnake.ButtonStyle.secondary,row=1)
    async def reconcile(self,b,i):
        mismatches=await finance_service.reconcile_all(False); text="✅ Расхождений нет." if not mismatches else "⚠️ Расхождений: **%d**. Для исправления нажмите отдельную кнопку."%len(mismatches); await i.response.send_message(text,ephemeral=True)
    @disnake.ui.button(label="🛠️ Исправить кеш",style=disnake.ButtonStyle.danger,row=1)
    async def fix(self,b,i):
        mismatches=await finance_service.reconcile_all(True); await i.response.send_message(f"✅ Сверка завершена. Исправлено профилей: **{len(mismatches)}**.",ephemeral=True)


def _blacklist_identity(row) -> str:
    return f"<@{row['discord_id']}> • Discord ID: `{row['discord_id']}`\nТег: **{row['discord_tag']}**"


def _blacklist_entry_embed(row, title: str | None = None) -> disnake.Embed:
    active = row["status"] == "active"
    em = disnake.Embed(
        title=title or ("🚫 ЧЁРНЫЙ СПИСОК" if active else "📜 ИСТОРИЯ ЧС"),
        color=disnake.Color.red() if active else disnake.Color.dark_grey(),
    )
    em.add_field(name="👤 Discord", value=_blacklist_identity(row), inline=False)
    em.add_field(name="🆔 Статик", value=row["static_id"] or "—", inline=True)
    em.add_field(name="📛 Имя / фамилия", value=row["full_name"] or "—", inline=True)
    em.add_field(name="📌 Причина", value=(row["reason"] or "—")[:1000], inline=False)
    if row["evidence"]:
        em.add_field(name="🔗 Доказательство", value=row["evidence"][:1000], inline=False)
    if row["notes"]:
        em.add_field(name="📝 Заметка", value=row["notes"][:1000], inline=False)
    em.add_field(name="👮 Добавил", value=f"<@{row['created_by']}> • ID: `{row['created_by']}`", inline=True)
    em.add_field(name="📅 Добавлен", value=format_utc_db(row["created_at"]), inline=True)
    em.add_field(name="Статус", value="🔴 Активный ЧС" if active else "⚪ Снят с ЧС", inline=True)
    if not active:
        em.add_field(name="👑 Снял", value=f"<@{row['removed_by']}> • ID: `{row['removed_by']}`", inline=True)
        em.add_field(name="📅 Снят", value=format_utc_db(row["removed_at"]), inline=True)
        em.add_field(name="Причина снятия", value=row["remove_reason"] or "—", inline=False)
    em.set_footer(text=f"Запись ЧС #{row['id']}")
    return em


def _blacklist_list_embed(rows, title: str, history: bool = False) -> disnake.Embed:
    em = disnake.Embed(title=title, color=disnake.Color.dark_grey() if history else disnake.Color.dark_red())
    if not rows:
        em.description = "Записей нет."
        return em
    lines = []
    for row in rows:
        status = "🔴" if row["status"] == "active" else "⚪"
        lines.append(
            f"{status} `#{row['id']}` • **{row['discord_tag']}** • Discord ID: `{row['discord_id']}`\n"
            f"   Статик: **{row['static_id'] or '—'}** • {(row['reason'] or '—')[:110]}"
        )
    em.description = "\n".join(lines)[:3900]
    return em


class BlacklistAddModal(disnake.ui.Modal):
    def __init__(self, user):
        self.user = user
        super().__init__(
            title="🚫 Добавить в ЧС",
            custom_id=f"panel:blacklist_add:{user.id}",
            components=[
                disnake.ui.TextInput(label="Статик (если известен)", custom_id="static", required=False, max_length=config.MAX_STATIC_ID_LENGTH),
                disnake.ui.TextInput(label="Имя и фамилия", custom_id="full_name", required=False, max_length=100, value=user.display_name[:100]),
                disnake.ui.TextInput(label="Причина", custom_id="reason", required=True, max_length=1000, style=disnake.TextInputStyle.paragraph),
                disnake.ui.TextInput(label="Доказательство / ссылка", custom_id="evidence", required=False, max_length=1500),
                disnake.ui.TextInput(label="Заметка", custom_id="notes", required=False, max_length=2000, style=disnake.TextInputStyle.paragraph),
            ],
        )

    async def callback(self, inter):
        if not _is_admin(inter.author):
            return await inter.response.send_message("❌ Чёрный список доступен только Admin.", ephemeral=True)
        if getattr(self.user, "bot", False):
            return await inter.response.send_message("❌ Нельзя добавить бота в ЧС.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        try:
            row = await blacklist_service.add_entry(
                self.user.id,
                str(self.user),
                inter.text_values["static"],
                inter.text_values["full_name"],
                inter.text_values["reason"],
                inter.text_values["evidence"],
                inter.text_values["notes"],
                inter.author.id,
            )
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")
        await inter.edit_original_response(embed=_blacklist_entry_embed(row, "🚫 ДОБАВЛЕН В ЧС"))


class BlacklistUserSelect(disnake.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Выберите пользователя Discord", min_values=1, max_values=1)

    async def callback(self, inter):
        user = self.values[0]
        if user.id == inter.author.id:
            # Самого себя блокировать технически можно, но почти всегда это ошибка интерфейса.
            return await inter.response.send_message("❌ Нельзя добавить самого себя в ЧС через это меню.", ephemeral=True)
        await inter.response.send_modal(BlacklistAddModal(user))


class BlacklistUserSelectView(disnake.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.add_item(BlacklistUserSelect())

    async def interaction_check(self, inter):
        if inter.author.id != self.user_id:
            await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True)
            return False
        if not _is_admin(inter.author):
            await inter.response.send_message("❌ Чёрный список доступен только Admin.", ephemeral=True)
            return False
        return True


class BlacklistManualAddModal(disnake.ui.Modal):
    def __init__(self):
        super().__init__(
            title="🚫 Добавить в ЧС по Discord ID",
            components=[
                disnake.ui.TextInput(label="Discord ID", custom_id="discord_id", required=True, max_length=20),
                disnake.ui.TextInput(label="Discord-тег / имя", custom_id="tag", required=True, max_length=100),
                disnake.ui.TextInput(label="Статик (если известен)", custom_id="static", required=False, max_length=config.MAX_STATIC_ID_LENGTH),
                disnake.ui.TextInput(label="Имя и фамилия", custom_id="full_name", required=False, max_length=100),
                disnake.ui.TextInput(label="Причина", custom_id="reason", required=True, max_length=1000, style=disnake.TextInputStyle.paragraph),
            ],
        )

    async def callback(self, inter):
        if not _is_admin(inter.author):
            return await inter.response.send_message("❌ Чёрный список доступен только Admin.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        try:
            discord_id = int(inter.text_values["discord_id"].strip())
        except ValueError:
            return await inter.edit_original_response(content="❌ Discord ID должен состоять только из цифр.")
        try:
            row = await blacklist_service.add_entry(
                discord_id,
                inter.text_values["tag"],
                inter.text_values["static"],
                inter.text_values["full_name"],
                inter.text_values["reason"],
                None,
                None,
                inter.author.id,
            )
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")
        await inter.edit_original_response(embed=_blacklist_entry_embed(row, "🚫 ДОБАВЛЕН В ЧС"))


class BlacklistDetailsModal(disnake.ui.Modal):
    def __init__(self):
        super().__init__(title="📝 Дополнить запись ЧС",components=[
            disnake.ui.TextInput(label="ID записи ЧС",custom_id="id",required=True,max_length=10),
            disnake.ui.TextInput(label="Доказательство / ссылка",custom_id="evidence",required=False,max_length=1500),
            disnake.ui.TextInput(label="Добавить заметку",custom_id="note",required=False,max_length=1000,style=disnake.TextInputStyle.paragraph),
        ])
    async def callback(self,inter):
        if not _is_admin(inter.author):
            return await inter.response.send_message("❌ Чёрный список доступен только Admin.",ephemeral=True)
        try: entry_id=int(inter.text_values["id"].strip())
        except ValueError: return await inter.response.send_message("❌ ID записи должен быть числом.",ephemeral=True)
        await inter.response.defer(ephemeral=True)
        try:
            row=await blacklist_service.update_entry_details(entry_id,inter.author.id,inter.text_values.get("evidence"),inter.text_values.get("note"))
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")
        await inter.edit_original_response(embed=_blacklist_entry_embed(row,"📝 ЗАПИСЬ ЧС ОБНОВЛЕНА"))


class BlacklistSearchModal(disnake.ui.Modal):
    def __init__(self):
        super().__init__(
            title="🔎 Поиск по ЧС",
            components=[disnake.ui.TextInput(label="Discord ID / статик / тег / имя", custom_id="query", required=True, max_length=100)],
        )

    async def callback(self, inter):
        if not _is_admin(inter.author):
            return await inter.response.send_message("❌ Чёрный список доступен только Admin.", ephemeral=True)
        try:
            rows = await blacklist_service.search_entries(inter.text_values["query"], include_removed=True, limit=20)
        except UserFacingError as exc:
            return await inter.response.send_message(f"❌ {exc}", ephemeral=True)
        if len(rows) == 1:
            return await inter.response.send_message(embed=_blacklist_entry_embed(rows[0]), ephemeral=True)
        await inter.response.send_message(embed=_blacklist_list_embed(rows, "🔎 ПОИСК ПО ЧС", history=True), ephemeral=True)


class BlacklistRemoveModal(disnake.ui.Modal):
    def __init__(self):
        super().__init__(
            title="♻️ Снять с ЧС",
            components=[
                disnake.ui.TextInput(label="ID записи ЧС", custom_id="id", required=True, max_length=10),
                disnake.ui.TextInput(label="Причина снятия", custom_id="reason", required=True, max_length=1000, style=disnake.TextInputStyle.paragraph),
            ],
        )

    async def callback(self, inter):
        if not _is_admin(inter.author):
            return await inter.response.send_message("❌ Снимать с ЧС может только Admin.", ephemeral=True)
        try:
            entry_id = int(inter.text_values["id"])
        except ValueError:
            return await inter.response.send_message("❌ ID записи должен быть числом.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        try:
            row = await blacklist_service.remove_entry(entry_id, inter.author.id, inter.text_values["reason"])
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")
        await inter.edit_original_response(embed=_blacklist_entry_embed(row, "✅ СНЯТ С ЧС"))


class BlacklistMenuView(disnake.ui.View):
    def __init__(self, user_id: int, admin: bool):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.admin = admin

    async def interaction_check(self, inter):
        if inter.author.id != self.user_id:
            await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True)
            return False
        if not _is_admin(inter.author):
            await inter.response.send_message("❌ Чёрный список доступен только Admin.", ephemeral=True)
            return False
        return True

    @disnake.ui.button(label="➕ Добавить", style=disnake.ButtonStyle.danger, row=0)
    async def add(self, button, inter):
        await inter.response.send_message("Выберите пользователя Discord:", view=BlacklistUserSelectView(inter.author.id), ephemeral=True)

    @disnake.ui.button(label="🆔 Добавить по ID", style=disnake.ButtonStyle.secondary, row=0)
    async def add_id(self, button, inter):
        await inter.response.send_modal(BlacklistManualAddModal())

    @disnake.ui.button(label="🔎 Найти", style=disnake.ButtonStyle.primary, row=1)
    async def search(self, button, inter):
        await inter.response.send_modal(BlacklistSearchModal())

    @disnake.ui.button(label="📋 Активный ЧС", style=disnake.ButtonStyle.primary, row=1)
    async def active(self, button, inter):
        rows = await blacklist_service.list_active(25)
        await inter.response.send_message(embed=_blacklist_list_embed(rows, "🚫 АКТИВНЫЙ ЧЁРНЫЙ СПИСОК"), ephemeral=True)

    @disnake.ui.button(label="📜 История", style=disnake.ButtonStyle.secondary, row=1)
    async def history(self, button, inter):
        rows = await blacklist_service.list_history(25)
        await inter.response.send_message(embed=_blacklist_list_embed(rows, "📜 ИСТОРИЯ ЧЁРНОГО СПИСКА", history=True), ephemeral=True)

    @disnake.ui.button(label="📝 Дополнить", style=disnake.ButtonStyle.secondary, row=2)
    async def details(self,button,inter):
        await inter.response.send_modal(BlacklistDetailsModal())

    @disnake.ui.button(label="♻️ Снять с ЧС", style=disnake.ButtonStyle.success, custom_id="panel:blacklist_remove", row=2)
    async def remove(self, button, inter):
        if not _is_admin(inter.author):
            return await inter.response.send_message("❌ Снимать с ЧС может только Admin.", ephemeral=True)
        await inter.response.send_modal(BlacklistRemoveModal())


class DatabaseSearchModal(disnake.ui.Modal):
    def __init__(self): super().__init__(title="Поиск в базе",components=[disnake.ui.TextInput(label="Discord ID / статик / имя",custom_id="query",required=True,max_length=100)])
    async def callback(self,inter):
        if not _is_admin(inter.author):
            return await inter.response.send_message("❌ Доступ только администратору.", ephemeral=True)
        try: rows=await database_service.search_users(inter.text_values['query'])
        except UserFacingError as exc: return await inter.response.send_message(f"❌ {exc}",ephemeral=True)
        em=disnake.Embed(title="🗄️ ПОИСК В БАЗЕ",color=disnake.Color.blurple()); em.description="Ничего не найдено." if not rows else "\n".join(f"<@{r['discord_id']}> • `{r['discord_id']}` • статик **{r['static_id'] or '—'}** • {r['username'] or '—'}" for r in rows[:10]); await inter.response.send_message(embed=em,ephemeral=True)


class FinanceOperationModal(disnake.ui.Modal):
    def __init__(self): super().__init__(title="Финансовая операция",components=[disnake.ui.TextInput(label="ID операции",custom_id="id",required=True,max_length=10)])
    async def callback(self,inter):
        if not _is_admin(inter.author):
            return await inter.response.send_message("❌ Доступ только администратору.", ephemeral=True)
        try: fid=int(inter.text_values['id'])
        except ValueError: return await inter.response.send_message("❌ ID должен быть числом.",ephemeral=True)
        r=await database_service.get_finance_operation(fid)
        if not r: return await inter.response.send_message("❌ Операция не найдена.",ephemeral=True)
        em=disnake.Embed(title=f"💰 ОПЕРАЦИЯ #{fid}",color=disnake.Color.green()); em.add_field(name="Пользователь",value=f"<@{r['user_id']}>"); em.add_field(name="Сумма",value=money(r['amount'])); em.add_field(name="Тип / статус",value=f"{r['type']} / {r['status']}"); em.add_field(name="Причина",value=r['reason'] or '—',inline=False); await inter.response.send_message(embed=em,ephemeral=True)


class DomainRepairView(disnake.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id

    async def interaction_check(self, inter):
        if inter.author.id != self.user_id:
            await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True)
            return False
        if not _is_admin(inter.author):
            await inter.response.send_message("❌ Доступ только администратору.", ephemeral=True)
            return False
        return True

    @disnake.ui.button(label="🧹 Исправить безопасные", style=disnake.ButtonStyle.success)
    async def repair(self, button, inter):
        await inter.response.defer(ephemeral=True)
        fixes = await repair_safe_domain_anomalies(inter.author.id)
        issues = (await get_domain_anomalies(20)) + (await get_blacklist_anomalies(20))
        em = disnake.Embed(
            title="🧹 БЕЗОПАСНОЕ ИСПРАВЛЕНИЕ",
            color=disnake.Color.green() if not issues else disnake.Color.orange(),
        )
        em.description = (
            ("Исправлено:\n" + "\n".join(f"✅ {x}" for x in fixes))
            if fixes else "Безопасных автоисправлений не найдено."
        )
        if issues:
            em.add_field(
                name="Остались аномалии",
                value="\n".join(f"• {x}" for x in issues[:8])[:1000],
                inline=False,
            )
        else:
            em.add_field(name="Результат", value="✅ Аномалий инвайтов/целей/ЧС больше нет.", inline=False)
        await inter.edit_original_response(embed=em, view=None)


class AdminDatabaseMenuView(disnake.ui.View):
    def __init__(self,user_id):
        super().__init__(timeout=300)
        self.user_id=user_id

    async def interaction_check(self,inter):
        if inter.author.id!=self.user_id:
            await inter.response.send_message("❌ Это не ваше меню.",ephemeral=True)
            return False
        if not _is_admin(inter.author):
            await inter.response.send_message("❌ Доступ только администратору.",ephemeral=True)
            return False
        return True

    @disnake.ui.button(label="👤 Карточка пользователя",style=disnake.ButtonStyle.primary,row=0)
    async def user(self,b,i):
        await i.response.send_message("Выберите пользователя:",view=MemberActionView(i.author.id,"database_user"),ephemeral=True)

    @disnake.ui.button(label="🔎 Поиск",style=disnake.ButtonStyle.secondary,row=0)
    async def search(self,b,i):
        await i.response.send_modal(DatabaseSearchModal())

    @disnake.ui.button(label="💰 Финоперация",style=disnake.ButtonStyle.secondary,row=0)
    async def fin(self,b,i):
        await i.response.send_modal(FinanceOperationModal())

    @disnake.ui.button(label="👥 Последние инвайты",style=disnake.ButtonStyle.primary,row=1)
    async def invites(self,b,i):
        rows=await database_service.list_recent_invites(15)
        em=disnake.Embed(title="👥 ПОСЛЕДНИЕ ИНВАЙТЫ",color=disnake.Color.blue())
        em.description="Записей нет." if not rows else "\n".join(
            f"`#{r['id']}` • **{r['static_id'] or '—'}** • `{r['status']}` • рекрутер <@{r['invited_by']}> • target {('<@'+str(r['user_id'])+'>') if r['user_id'] else '—'}"
            for r in rows
        )[:3900]
        await i.response.send_message(embed=em,ephemeral=True)

    @disnake.ui.button(label="🎯 Последние цели",style=disnake.ButtonStyle.primary,row=1)
    async def goals(self,b,i):
        rows=await database_service.list_recent_goals(15)
        em=disnake.Embed(title="🎯 ПОСЛЕДНИЕ ЦЕЛИ",color=disnake.Color.blue())
        em.description="Записей нет." if not rows else "\n".join(
            f"`#{r['id']}` • <@{r['user_id']}> • **{r['type']}** {r['current_value']}/{r['target_value']} • {r['period']} • `{r['status']}`"
            for r in rows
        )[:3900]
        await i.response.send_message(embed=em,ephemeral=True)

    @disnake.ui.button(label="💳 Последние финоперации",style=disnake.ButtonStyle.secondary,row=1)
    async def finances(self,b,i):
        rows=await database_service.list_recent_finances(15)
        em=disnake.Embed(title="💳 ПОСЛЕДНИЕ ФИНОПЕРАЦИИ",color=disnake.Color.gold())
        em.description="Записей нет." if not rows else "\n".join(
            f"`#{r['id']}` • <@{r['user_id']}> • **{money(r['amount'])}** • `{r['type']}/{r['status']}` • {(r['reason'] or '—')[:80]}"
            for r in rows
        )[:3900]
        await i.response.send_message(embed=em,ephemeral=True)

    @disnake.ui.button(label="🩺 Диагностика",style=disnake.ButtonStyle.danger,row=2)
    async def diagnostics(self,b,i):
        await i.response.defer(ephemeral=True)
        groups=[
            ("Смены/отчёты",await get_shift_anomalies(8)),
            ("Инвайты/цели",await get_domain_anomalies(8)),
            ("Чёрный список",await get_blacklist_anomalies(8)),
            ("Финансы",await get_finance_anomalies(limit=8)),
            ("Уведомления",await get_notification_anomalies(8)),
        ]
        issues=[(title,rows) for title,rows in groups if rows]
        em=disnake.Embed(title="🩺 ДИАГНОСТИКА БАЗЫ",color=disnake.Color.green() if not issues else disnake.Color.orange())
        if not issues:
            em.description="✅ Структурных аномалий не найдено."
            return await i.edit_original_response(embed=em,view=DomainRepairView(i.author.id))
        for title,rows in issues:
            em.add_field(name=title,value="\n".join(f"• {x}" for x in rows[:5])[:1000],inline=False)
        em.set_footer(text="Автоисправление меняет только заранее определённые безопасные legacy-аномалии.")
        await i.edit_original_response(embed=em,view=DomainRepairView(i.author.id))


class SeniorMenuView(disnake.ui.View):
    def __init__(self, bot, user_id: int):
        super().__init__(timeout=300); self.bot=bot; self.user_id=user_id
    async def interaction_check(self, inter):
        if inter.author.id != self.user_id: await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True); return False
        if not is_senior_or_admin(inter.author): await inter.response.send_message("❌ Доступ только старшему составу.", ephemeral=True); return False
        return True
    @disnake.ui.button(label="➕ Создать смену",style=disnake.ButtonStyle.green,row=0)
    async def create_shift(self,b,i): await i.response.send_modal(CreateShiftModal())
    @disnake.ui.button(label="👤 Снять со смены",style=disnake.ButtonStyle.secondary,row=0)
    async def remove(self,b,i): await i.response.send_message("Выберите рекрутера:",view=MemberActionView(i.author.id,"remove_shift"),ephemeral=True)
    @disnake.ui.button(label="⛔ Отменить смену",style=disnake.ButtonStyle.danger,row=0)
    async def cancel(self,b,i): await i.response.send_modal(CancelShiftModal())
    @disnake.ui.button(label="📋 Отчёты",style=disnake.ButtonStyle.primary,row=1)
    async def reports(self,b,i): await i.response.send_message("Проверка отчётов:",view=ReportSeniorMenuView(i.author.id),ephemeral=True)
    @disnake.ui.button(label="👥 Инвайты",style=disnake.ButtonStyle.primary,row=1)
    async def invites(self,b,i): await i.response.send_message("Управление инвайтами:",view=SeniorInviteMenuView(i.author.id),ephemeral=True)
    @disnake.ui.button(label="📊 Статистика рекрутера",style=disnake.ButtonStyle.primary,row=1)
    async def stats(self,b,i): await i.response.send_message("Выберите рекрутера:",view=MemberActionView(i.author.id,"stats"),ephemeral=True)
    @disnake.ui.button(label="💰 Финансы рекрутера",style=disnake.ButtonStyle.secondary,row=2)
    async def finances(self,b,i): await i.response.send_message("Выберите рекрутера:",view=MemberActionView(i.author.id,"finance_view"),ephemeral=True)
    @disnake.ui.button(label="💵 Общие финансы",style=disnake.ButtonStyle.secondary,row=2)
    async def general_finance(self,b,i):
        accrued,paid,available=await finance_service.get_general_balance(); count=await db.fetchone("SELECT COUNT(*) AS count FROM users"); em=disnake.Embed(title="💰 ОБЩИЕ ФИНАНСЫ",color=disnake.Color.green()); em.add_field(name="Начислено",value=money(accrued)); em.add_field(name="Выплачено",value=money(paid)); em.add_field(name="К выплате",value=money(available)); em.add_field(name="👥 Профилей",value=str(count["count"] or 0)); await i.response.send_message(embed=em,ephemeral=True)
    @disnake.ui.button(label="🎯 Цели",style=disnake.ButtonStyle.secondary,row=3)
    async def goals(self,b,i): await i.response.send_message("Управление целями:",view=SeniorGoalsMenuView(self.bot,i.author.id),ephemeral=True)
    @disnake.ui.button(label="📝 Заметка",style=disnake.ButtonStyle.secondary,row=3)
    async def note(self,b,i): await i.response.send_message("Выберите рекрутера:",view=MemberActionView(i.author.id,"note"),ephemeral=True)


class AdminMenuView(disnake.ui.View):
    def __init__(self, bot, user_id: int): super().__init__(timeout=300); self.bot=bot; self.user_id=user_id
    async def interaction_check(self, inter):
        if inter.author.id != self.user_id: await inter.response.send_message("❌ Это не ваше меню.", ephemeral=True); return False
        if not _is_admin(inter.author): await inter.response.send_message("❌ Доступ только администратору.", ephemeral=True); return False
        return True
    @disnake.ui.button(label="🕐 Время",style=disnake.ButtonStyle.primary,row=0)
    async def time(self,b,i):
        em=disnake.Embed(title="🕐 ВРЕМЯ БОТА",color=disnake.Color.blue()); em.add_field(name="TIMEZONE",value=f"`{config.TIMEZONE}`",inline=False); em.add_field(name="Время бота",value=local_now().strftime("%d.%m.%Y %H:%M:%S")); em.add_field(name="UTC",value=utc_now().strftime("%d.%m.%Y %H:%M:%S")); em.add_field(name="База",value=f"`{config.DATABASE_PATH}`",inline=False); await i.response.send_message(embed=em,ephemeral=True)
    @disnake.ui.button(label="🔔 Уведомления",style=disnake.ButtonStyle.primary,row=0)
    async def notifications(self,b,i):
        rows=await db.fetchall("SELECT * FROM notifications ORDER BY id DESC LIMIT 20"); em=disnake.Embed(title="🔔 УВЕДОМЛЕНИЯ",color=disnake.Color.blue()); em.description="Записей нет." if not rows else None
        for r in rows[:15]:
            who=f"<@{r['user_id']}> • ID: `{r['user_id']}`" if r["user_id"] else "Система"
            em.add_field(name=f"#{r['id']} | {r['type']} | {r['status']}",value=f"{who} • {format_utc_db(r['updated_at'])} • попыток: {r['attempts']}\n{(r['last_error'] or '—')[:150]}",inline=False)
        await i.response.send_message(embed=em,ephemeral=True)
    @disnake.ui.button(label="📝 Логи",style=disnake.ButtonStyle.secondary,row=0)
    async def logs(self,b,i):
        rows=await db.fetchall("SELECT * FROM logs ORDER BY id DESC LIMIT 20"); em=disnake.Embed(title="📝 ЛОГИ",color=disnake.Color.dark_gray()); em.description="Логов нет." if not rows else None
        for r in rows[:15]: em.add_field(name=f"{format_utc_db(r['created_at'])} | {r['action']}",value=f"{'<@'+str(r['user_id'])+'>' if r['user_id'] else 'Система'}\n{(r['details'] or '—')[:150]}",inline=False)
        await i.response.send_message(embed=em,ephemeral=True)
    @disnake.ui.button(label="📦 Бэкап",style=disnake.ButtonStyle.green,row=1)
    async def backup(self,b,i):
        cog=self.bot.get_cog("Admin"); await i.response.defer(ephemeral=True); path=None
        if cog is None or not hasattr(cog,"_make_backup"):
            return await i.edit_original_response(content="❌ Модуль бэкапа сейчас недоступен. Проверьте 🩺 Здоровье.")
        try:
            path=await cog._make_backup(); await i.edit_original_response(content="📦 Бэкап готов:",file=disnake.File(path))
        except Exception:
            logger.exception("Не удалось создать бэкап из панели")
            await i.edit_original_response(content="❌ Не удалось создать бэкап. Ошибка записана в лог.")
        finally:
            if path and os.path.exists(path): os.remove(path)
    @disnake.ui.button(label="🩺 Здоровье",style=disnake.ButtonStyle.secondary,row=1)
    async def health(self,b,i):
        await i.response.defer(ephemeral=True)
        checks = await run_health_checks(self.bot, i.guild)
        ok_all = all(check.ok for check in checks)
        em = disnake.Embed(
            title="🩺 ЗДОРОВЬЕ БОТА",
            color=disnake.Color.green() if ok_all else disnake.Color.red(),
        )
        for check in checks:
            value = "✅ OK" if check.ok else "❌ Ошибка"
            if check.details:
                value += f"\n{check.details[:900]}"
            em.add_field(name=check.name, value=value, inline=True)
        em.set_footer(text=f"TIMEZONE={config.TIMEZONE} | {local_now().strftime('%d.%m.%Y %H:%M:%S')}")
        await i.edit_original_response(embed=em)
    @disnake.ui.button(label="💰 Финансы",style=disnake.ButtonStyle.primary,row=1)
    async def finance(self,b,i): await i.response.send_message("Административные финансы:",view=AdminFinanceMenuView(self.bot,i.author.id),ephemeral=True)
    @disnake.ui.button(label="🗄️ База",style=disnake.ButtonStyle.primary,row=2)
    async def database(self,b,i): await i.response.send_message("Администрирование базы:",view=AdminDatabaseMenuView(i.author.id),ephemeral=True)
    @disnake.ui.button(label="🚫 Чёрный список",style=disnake.ButtonStyle.danger,row=2)
    async def blacklist(self,b,i):
        await i.response.send_message(
            "Управление чёрным списком. Снятие доступно только Admin.",
            view=BlacklistMenuView(i.author.id, True),
            ephemeral=True,
        )
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
        await inter.response.send_message(
            embed=disnake.Embed(title="👤 ПРОФИЛЬ", description="Мой профиль или просмотр другого рекрутера.", color=disnake.Color.blue()),
            view=ProfileMenuView(inter.author.id),
            ephemeral=True,
        )

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
            view=SeniorMenuView(self.bot, inter.author.id),
            ephemeral=True,
        )

    @disnake.ui.button(label="⚙️ Админ", style=disnake.ButtonStyle.danger, custom_id="panel:admin", row=1)
    async def admin(self, button, inter):
        if not _is_admin(inter.author):
            return await inter.response.send_message("❌ Доступ только администратору.", ephemeral=True)
        await inter.response.send_message(
            embed=disnake.Embed(title="⚙️ АДМИН-ПАНЕЛЬ", description="Диагностика, финансы и база данных.", color=disnake.Color.red()),
            view=AdminMenuView(self.bot, inter.author.id),
            ephemeral=True,
        )

    @disnake.ui.button(label="❓ Помощь", style=disnake.ButtonStyle.secondary, custom_id="panel:help", row=2)
    async def help(self, button, inter):
        if not await self._recruiter_check(inter):
            return
        embed = disnake.Embed(title="❓ КАК РАБОТАТЬ С ПАНЕЛЬЮ", color=disnake.Color.blurple())
        embed.description = (
            "**Смена** — начать, завершить, выйти, расписание, исправление отчёта.\n"
            "**Профиль / Статистика / Финансы / Цели / Инвайты** — личная работа рекрутера.\n"
            "**Руководство** — смены, отчёты, инвайты, статистика, цели и заметки.\n"
            "**Админ** — диагностика, логи, бэкап, деньги и база.\n\n"
            "Slash-команды сохранены как резервный способ, но для обычной работы они не нужны."
        )
        await inter.response.send_message(embed=embed, ephemeral=True)


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

    async def _disable_old_panel_in_shifts_channel(self, guild):
        if config.PANEL_CHANNEL_ID == config.SHIFTS_CHANNEL_ID:
            return
        old_channel = guild.get_channel(config.SHIFTS_CHANNEL_ID)
        if old_channel is None:
            return
        try:
            async for message in old_channel.history(limit=100):
                if message.author.id != self.bot.user.id or not message.embeds:
                    continue
                footer = message.embeds[0].footer.text if message.embeds[0].footer else ""
                if footer not in LEGACY_PANEL_FOOTERS:
                    continue
                moved = disnake.Embed(
                    title="📌 ПАНЕЛЬ ПЕРЕНЕСЕНА",
                    description=f"Актуальная панель управления находится в <#{config.PANEL_CHANNEL_ID}>.",
                    color=disnake.Color.dark_gray(),
                )
                moved.set_footer(text="Recruiter Department • Старая панель отключена")
                await message.edit(embed=moved, view=None)
        except Exception:
            logger.warning("Не удалось отключить старую панель в канале смен", exc_info=True)

    async def ensure_panel(self):
        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            raise RuntimeError("Сервер для панели не найден")
        channel = guild.get_channel(config.PANEL_CHANNEL_ID)
        if channel is None:
            raise RuntimeError(f"PANEL_CHANNEL_ID={config.PANEL_CHANNEL_ID} не найден")

        found = None
        try:
            async for message in channel.pins(limit=100):
                if message.author.id != self.bot.user.id or not message.embeds:
                    continue
                footer = message.embeds[0].footer.text if message.embeds[0].footer else ""
                if footer in LEGACY_PANEL_FOOTERS:
                    found = message
                    break
        except Exception:
            logger.info("Не удалось прочитать закреплённые сообщения панели", exc_info=True)
        if found is None:
            try:
                async for message in channel.history(limit=200):
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
            await self._disable_old_panel_in_shifts_channel(guild)
            logger.info("Панель управления обновлена: message_id=%s", found.id)
            return

        message = await channel.send(embed=_panel_embed(), view=view)
        await self._disable_old_panel_in_shifts_channel(guild)
        logger.info("Панель управления создана: message_id=%s", message.id)
        try:
            await message.pin(reason="Постоянная панель Recruiter Bot")
        except Exception:
            logger.info("Панель не закреплена: у бота нет права Manage Messages или канал не поддерживает pin")


def setup(bot):
    bot.add_view(MainPanelView(bot))
    bot.add_cog(Panel(bot))
