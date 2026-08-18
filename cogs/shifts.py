import logging
from datetime import datetime

import disnake
from disnake.ext import commands

import config
from database.db import db, notify
from services.errors import UserFacingError
from services import shift_service
from utils.checks import is_recruiter, is_senior, is_senior_or_admin, is_recruiter_or_higher
from utils.embeds import EmbedGenerator
from utils.time_utils import local_now, parse_db

logger = logging.getLogger(__name__)


def build_shift_view(shift_id: int):
    view = disnake.ui.View(timeout=None)
    view.add_item(
        disnake.ui.Button(
            label="Взять смену",
            style=disnake.ButtonStyle.green,
            custom_id=f"shift:take:{shift_id}",
        )
    )
    return view


def build_report_view(report_id: int):
    view = disnake.ui.View(timeout=None)
    view.add_item(
        disnake.ui.Button(
            label="✅ Одобрить",
            style=disnake.ButtonStyle.green,
            custom_id=f"report:approve:{report_id}",
        )
    )
    view.add_item(
        disnake.ui.Button(
            label="❌ Отклонить",
            style=disnake.ButtonStyle.red,
            custom_id=f"report:reject:{report_id}",
        )
    )
    return view


async def _notify_report_approved(bot, report, reviewer_mention: str):
    dm = disnake.Embed(title="✅ ВАШ ОТЧЁТ ОДОБРЕН", color=disnake.Color.green())
    dm.add_field(name="📋 Смена", value=f"#{report['shift_id']}", inline=True)
    dm.add_field(name="👥 Принято", value=str(report["total_accepted"]), inline=True)
    dm.add_field(name="👤 Проверил", value=reviewer_mention, inline=True)
    await notify(bot, report["user_id"], "REPORT_APPROVED", "shift_report", report["id"], embed=dm)


async def _notify_report_rejected(bot, report, reason: str):
    dm = disnake.Embed(title="❌ ВАШ ОТЧЁТ ОТКЛОНЁН", color=disnake.Color.red())
    dm.add_field(name="📋 Смена", value=f"#{report['shift_id']}", inline=True)
    dm.add_field(name="📝 Причина", value=reason, inline=False)
    dm.add_field(name="ℹ️", value=f"Исправьте его командой `/смена исправить отчёт:{report['id']}`.", inline=False)
    await notify(bot, report["user_id"], "REPORT_REJECTED", "shift_report", report["id"], embed=dm)


class TakeShiftModal(disnake.ui.Modal):
    def __init__(self, shift_id: int):
        self.shift_id = shift_id
        super().__init__(
            title="Взятие смены",
            custom_id=f"shift_take_modal:{shift_id}",
            components=[
                disnake.ui.TextInput(
                    label="Ваш статик",
                    custom_id="static_id",
                    placeholder="Например: 12345",
                    required=True,
                    max_length=20,
                )
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction):
        await inter.response.defer(ephemeral=True)
        try:
            await shift_service.take_shift(
                self.shift_id,
                inter.author.id,
                inter.author.name,
                inter.text_values["static_id"],
            )
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")

        await inter.edit_original_response(content=f"✅ Вы записались на смену **#{self.shift_id}**.")
        await update_shift_message(inter.guild, self.shift_id)


class FinishShiftModal(disnake.ui.Modal):
    def __init__(self, member_id: int, shift_id: int):
        self.member_id = member_id
        self.shift_id = shift_id
        super().__init__(
            title=f"Отчёт по смене #{shift_id}",
            custom_id=f"shift_finish_modal:{member_id}",
            components=[
                disnake.ui.TextInput(label="Принято всего", custom_id="total", placeholder="0", required=True, max_length=5),
                disnake.ui.TextInput(label="На особняке", custom_id="base", placeholder="0", required=True, max_length=5),
                disnake.ui.TextInput(label="Самостоятельно", custom_id="self", placeholder="0", required=True, max_length=5),
                disnake.ui.TextInput(
                    label="Комментарий",
                    custom_id="comment",
                    required=False,
                    max_length=1000,
                    style=disnake.TextInputStyle.paragraph,
                ),
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction):
        await inter.response.defer(ephemeral=True)
        try:
            total = int(inter.text_values["total"].strip())
            base = int(inter.text_values["base"].strip())
            self_found = int(inter.text_values["self"].strip())
        except ValueError:
            return await inter.edit_original_response(content="❌ Все числовые поля должны содержать целые числа.")

        try:
            result = await shift_service.finish_shift(
                self.member_id,
                inter.author.id,
                total,
                base,
                self_found,
                inter.text_values.get("comment", ""),
            )
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")

        await inter.edit_original_response(
            content=(
                f"✅ Смена **#{self.shift_id}** завершена. Отчёт **#{result.report['id']}** отправлен на проверку."
            )
        )

        channel = inter.guild.get_channel(config.REPORTS_CHANNEL_ID)
        if channel:
            embed = EmbedGenerator.create_report_embed(result.report, result.member, inter.author.mention)
            await channel.send(
                content=f"<@&{config.SENIOR_ROLE_ID}> <@&{config.ADMIN_ROLE_ID}>",
                embed=embed,
                view=build_report_view(result.report["id"]),
            )
        else:
            logger.error("REPORTS_CHANNEL_ID=%s не найден", config.REPORTS_CHANNEL_ID)

        await update_shift_message(inter.guild, self.shift_id)


class ResubmitReportModal(disnake.ui.Modal):
    def __init__(self, report_id: int):
        self.report_id = report_id
        super().__init__(
            title=f"Исправление отчёта #{report_id}",
            custom_id=f"report_resubmit_modal:{report_id}",
            components=[
                disnake.ui.TextInput(label="Принято всего", custom_id="total", placeholder="0", required=True, max_length=5),
                disnake.ui.TextInput(label="На особняке", custom_id="base", placeholder="0", required=True, max_length=5),
                disnake.ui.TextInput(label="Самостоятельно", custom_id="self", placeholder="0", required=True, max_length=5),
                disnake.ui.TextInput(
                    label="Комментарий",
                    custom_id="comment",
                    required=False,
                    max_length=1000,
                    style=disnake.TextInputStyle.paragraph,
                ),
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction):
        await inter.response.defer(ephemeral=True)
        try:
            total = int(inter.text_values["total"].strip())
            base = int(inter.text_values["base"].strip())
            self_found = int(inter.text_values["self"].strip())
        except ValueError:
            return await inter.edit_original_response(content="❌ Все числовые поля должны содержать целые числа.")

        try:
            result = await shift_service.resubmit_report(
                self.report_id,
                inter.author.id,
                total,
                base,
                self_found,
                inter.text_values.get("comment", ""),
            )
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")

        channel = inter.guild.get_channel(config.REPORTS_CHANNEL_ID)
        if channel:
            embed = EmbedGenerator.create_report_embed(result.report, result.member, inter.author.mention)
            embed.title = "♻️ ИСПРАВЛЕННЫЙ ОТЧЁТ ПО СМЕНЕ"
            await channel.send(
                content=f"<@&{config.SENIOR_ROLE_ID}> <@&{config.ADMIN_ROLE_ID}>",
                embed=embed,
                view=build_report_view(result.report["id"]),
            )
        else:
            logger.error("REPORTS_CHANNEL_ID=%s не найден при повторной отправке отчёта #%s", config.REPORTS_CHANNEL_ID, self.report_id)
        await inter.edit_original_response(content=f"✅ Отчёт **#{self.report_id}** исправлен и снова отправлен на проверку.")


class RejectReportModal(disnake.ui.Modal):
    def __init__(self, report_id: int):
        self.report_id = report_id
        super().__init__(
            title="Отклонение отчёта",
            custom_id=f"report_reject_modal:{report_id}",
            components=[
                disnake.ui.TextInput(
                    label="Причина отклонения",
                    custom_id="reason",
                    required=True,
                    max_length=1000,
                    style=disnake.TextInputStyle.paragraph,
                )
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction):
        if not is_senior_or_admin(inter.author):
            return await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
        try:
            report = await shift_service.reject_report(
                self.report_id, inter.author.id, inter.text_values["reason"]
            )
        except UserFacingError as exc:
            return await inter.response.send_message(f"❌ {exc}", ephemeral=True)

        embed = disnake.Embed(title="❌ ОТЧЁТ ОТКЛОНЁН", color=disnake.Color.red())
        embed.add_field(name="📋 Отчёт", value=f"#{self.report_id}", inline=True)
        embed.add_field(name="👤 Проверил", value=inter.author.mention, inline=True)
        embed.add_field(name="📝 Причина", value=inter.text_values["reason"], inline=False)
        await inter.response.edit_message(embed=embed, view=None)

        await _notify_report_rejected(self.bot, report, inter.text_values["reason"])


async def update_shift_message(guild, shift_id: int):
    if guild is None:
        return
    channel = guild.get_channel(config.SHIFTS_CHANNEL_ID)
    if not channel:
        logger.error("SHIFTS_CHANNEL_ID=%s не найден", config.SHIFTS_CHANNEL_ID)
        return

    shift = await db.fetchone("SELECT * FROM shifts WHERE id=?", (shift_id,))
    if not shift:
        return
    members = await db.fetchall("SELECT * FROM shift_members WHERE shift_id=? ORDER BY id", (shift_id,))
    embed = EmbedGenerator.create_shift_embed(shift, members)
    view = build_shift_view(shift_id) if shift["status"] in ("open", "booked") and shift["slots"] > 0 else None

    message = None
    if shift["message_id"]:
        try:
            message = await channel.fetch_message(shift["message_id"])
        except Exception:
            logger.warning("Не удалось получить message_id=%s для смены #%s", shift["message_id"], shift_id)

    if message is None:
        # Одноразовый fallback для старых смен, созданных до появления message_id.
        try:
            async for candidate in channel.history(limit=100):
                if not candidate.embeds or not candidate.embeds[0].footer:
                    continue
                if candidate.embeds[0].footer.text == f"Смена #{shift_id}":
                    message = candidate
                    await shift_service.set_shift_message_id(shift_id, candidate.id)
                    break
        except Exception:
            logger.exception("Ошибка поиска старого сообщения смены #%s", shift_id)

    if message:
        try:
            await message.edit(embed=embed, view=view)
        except Exception:
            logger.exception("Не удалось обновить сообщение смены #%s", shift_id)


class Shifts(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_button_click(self, inter: disnake.MessageInteraction):
        custom_id = getattr(inter.component, "custom_id", "") or ""

        if custom_id.startswith("shift:take:") or custom_id == "take_shift":
            if not is_recruiter_or_higher(inter.author):
                return await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            try:
                if custom_id == "take_shift":
                    footer = inter.message.embeds[0].footer.text if inter.message.embeds and inter.message.embeds[0].footer else ""
                    shift_id = int(footer.split("#")[-1])
                else:
                    shift_id = int(custom_id.rsplit(":", 1)[1])
            except (ValueError, IndexError, AttributeError):
                return await inter.response.send_message("❌ Не удалось определить ID смены.", ephemeral=True)
            return await inter.response.send_modal(TakeShiftModal(shift_id))

        if custom_id.startswith("report:approve:") or custom_id == "approve_report":
            if not is_senior_or_admin(inter.author):
                return await inter.response.send_message("❌ Только старший состав может проверять отчёты.", ephemeral=True)
            try:
                if custom_id == "approve_report":
                    footer = inter.message.embeds[0].footer.text if inter.message.embeds and inter.message.embeds[0].footer else ""
                    report_id = int(footer.split("#")[-1])
                else:
                    report_id = int(custom_id.rsplit(":", 1)[1])
                report = await shift_service.approve_report(report_id, inter.author.id)
            except (ValueError, UserFacingError) as exc:
                return await inter.response.send_message(f"❌ {exc}", ephemeral=True)

            embed = disnake.Embed(title="✅ ОТЧЁТ ОДОБРЕН", color=disnake.Color.green())
            embed.add_field(name="📋 Отчёт", value=f"#{report_id}", inline=True)
            embed.add_field(name="👤 Проверил", value=inter.author.mention, inline=True)
            await inter.response.edit_message(embed=embed, view=None)

            await _notify_report_approved(self.bot, report, inter.author.mention)
            return

        if custom_id.startswith("report:reject:") or custom_id == "reject_report":
            if not is_senior_or_admin(inter.author):
                return await inter.response.send_message("❌ Только старший состав может проверять отчёты.", ephemeral=True)
            try:
                if custom_id == "reject_report":
                    footer = inter.message.embeds[0].footer.text if inter.message.embeds and inter.message.embeds[0].footer else ""
                    report_id = int(footer.split("#")[-1])
                else:
                    report_id = int(custom_id.rsplit(":", 1)[1])
            except (ValueError, IndexError, AttributeError):
                return await inter.response.send_message("❌ Не удалось определить ID отчёта.", ephemeral=True)
            return await inter.response.send_modal(RejectReportModal(report_id))

    @commands.slash_command(name="смена", description="Управление сменами")
    async def shift(self, inter):
        pass

    @shift.sub_command(name="создать", description="Создать новую смену")
    @is_senior()
    async def create_shift(
        self,
        inter,
        дата: str,
        начало: str,
        конец: str,
        места: int = 1,
        описание: str = "",
    ):
        await inter.response.defer(ephemeral=True)
        try:
            start = datetime.strptime(f"{дата} {начало}", "%d.%m.%Y %H:%M")
            end = datetime.strptime(f"{дата} {конец}", "%d.%m.%Y %H:%M")
        except ValueError:
            return await inter.edit_original_response(
                content="❌ Формат: `дата: 18.08.2026`, `начало: 18:00`, `конец: 20:00`."
            )

        try:
            shift_id = await shift_service.create_shift(inter.author.id, start, end, места, описание)
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")

        shift = await db.fetchone("SELECT * FROM shifts WHERE id=?", (shift_id,))
        channel = inter.guild.get_channel(config.SHIFTS_CHANNEL_ID)
        if not channel:
            return await inter.edit_original_response(
                content=f"⚠️ Смена #{shift_id} создана в БД, но канал смен не найден. Проверьте SHIFTS_CHANNEL_ID."
            )

        message = await channel.send(
            content=(f"<@&{config.RECRUITER_ROLE_ID}>" if config.PING_RECRUITERS_ON_SHIFT_CREATE else None),
            embed=EmbedGenerator.create_shift_embed(shift, []),
            view=build_shift_view(shift_id),
            allowed_mentions=disnake.AllowedMentions(roles=True),
        )
        await shift_service.set_shift_message_id(shift_id, message.id)
        await inter.edit_original_response(content=f"✅ Смена **#{shift_id}** создана.")

    @shift.sub_command(name="начать", description="Начать свою ближайшую смену")
    @is_recruiter()
    async def start_shift(self, inter):
        await inter.response.defer(ephemeral=True)
        try:
            member = await shift_service.find_shift_to_start(inter.author.id)
            shift_id = await shift_service.start_shift(member["id"], inter.author.id)
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")

        await inter.edit_original_response(content=f"🟢 Смена **#{shift_id}** начата.")
        await update_shift_message(inter.guild, shift_id)

    @shift.sub_command(name="завершить", description="Завершить активную смену и отправить отчёт")
    @is_recruiter()
    async def finish_shift(self, inter):
        try:
            member = await shift_service.find_active_member(inter.author.id)
        except UserFacingError as exc:
            return await inter.response.send_message(f"❌ {exc}", ephemeral=True)
        await inter.response.send_modal(FinishShiftModal(member["id"], member["shift_id"]))

    @shift.sub_command(name="исправить", description="Исправить отклонённый отчёт и отправить его повторно")
    @is_recruiter()
    async def resubmit(self, inter, отчёт: int = None):
        try:
            report = await shift_service.find_rejected_report(inter.author.id, отчёт)
        except UserFacingError as exc:
            return await inter.response.send_message(f"❌ {exc}", ephemeral=True)
        await inter.response.send_modal(ResubmitReportModal(report["id"]))

    @shift.sub_command(name="одобрить", description="Одобрить отчёт по ID (резервный способ)")
    @is_senior()
    async def approve_report_command(self, inter, отчёт: int):
        await inter.response.defer(ephemeral=True)
        try:
            report = await shift_service.approve_report(отчёт, inter.author.id)
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")
        await _notify_report_approved(self.bot, report, inter.author.mention)
        await inter.edit_original_response(content=f"✅ Отчёт **#{отчёт}** одобрен.")

    @shift.sub_command(name="отклонить", description="Отклонить отчёт по ID (резервный способ)")
    @is_senior()
    async def reject_report_command(self, inter, отчёт: int, причина: str):
        await inter.response.defer(ephemeral=True)
        try:
            report = await shift_service.reject_report(отчёт, inter.author.id, причина)
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")
        await _notify_report_rejected(self.bot, report, причина)
        await inter.edit_original_response(content=f"✅ Отчёт **#{отчёт}** отклонён.")

    @shift.sub_command(name="снять", description="Снять рекрутера со смены")
    @is_senior()
    async def remove_member(
        self,
        inter,
        пользователь: disnake.Member,
        причина: str,
        смена: int = None,
    ):
        await inter.response.defer(ephemeral=True)
        try:
            shift_id = await shift_service.remove_member(
                inter.author.id, пользователь.id, причина, shift_id=смена
            )
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")

        dm = disnake.Embed(title="⚠️ ВЫ СНЯТЫ СО СМЕНЫ", color=disnake.Color.orange())
        dm.add_field(name="📋 Смена", value=f"#{shift_id}", inline=True)
        dm.add_field(name="👤 Снял", value=inter.author.mention, inline=True)
        dm.add_field(name="📝 Причина", value=причина, inline=False)
        await notify(self.bot, пользователь.id, "REMOVED_FROM_SHIFT", "shift", shift_id, embed=dm)

        control = inter.guild.get_channel(config.CONTROL_CHANNEL_ID)
        if control:
            await control.send(
                f"🛠️ {пользователь.mention} снят со смены #{shift_id}.\n"
                f"Снял: {inter.author.mention}\nПричина: {причина}"
            )
        await inter.edit_original_response(content=f"✅ {пользователь.mention} снят со смены **#{shift_id}**.")
        await update_shift_message(inter.guild, shift_id)

    @shift.sub_command(name="отменить", description="Отменить смену целиком")
    @is_senior()
    async def cancel_shift(self, inter, смена: int, причина: str):
        await inter.response.defer(ephemeral=True)
        try:
            members = await shift_service.cancel_shift(inter.author.id, смена, причина)
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")

        for member in members:
            dm = disnake.Embed(title="⚠️ СМЕНА ОТМЕНЕНА", color=disnake.Color.red())
            dm.add_field(name="📋 Смена", value=f"#{смена}", inline=True)
            dm.add_field(name="📝 Причина", value=причина, inline=False)
            await notify(self.bot, member["user_id"], "SHIFT_CANCELLED", "shift", смена, embed=dm)

        await update_shift_message(inter.guild, смена)
        await inter.edit_original_response(content=f"✅ Смена **#{смена}** отменена.")

    @shift.sub_command(name="расписание", description="Показать расписание на сегодня")
    @is_recruiter()
    async def schedule(self, inter):
        await inter.response.defer(ephemeral=True)
        now = local_now()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = now.replace(hour=23, minute=59, second=59, microsecond=0)
        shifts = await shift_service.get_schedule(day_start, day_end)

        embed = disnake.Embed(title="📅 РАСПИСАНИЕ СМЕН", color=disnake.Color.blue())
        if not shifts:
            embed.description = "На сегодня смен нет."
        else:
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
            if len(shifts) > 20:
                embed.set_footer(text=f"Показаны первые 20 из {len(shifts)} смен")
        await inter.edit_original_response(embed=embed)


def setup(bot):
    bot.add_cog(Shifts(bot))
