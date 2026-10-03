import logging
from datetime import datetime, timedelta

import disnake
from disnake.ext import commands

import config
from database.db import db, notify
from services.errors import UserFacingError
from services import shift_service
from utils.checks import is_recruiter, is_senior, is_senior_or_admin, is_recruiter_or_higher
from utils.discord_helpers import send_control_warning
from utils.embeds import EmbedGenerator
from utils.time_utils import local_now, parse_db

logger = logging.getLogger(__name__)


def build_shift_view(shift_id: int, can_take: bool = True, can_leave: bool = True):
    view = disnake.ui.View(timeout=None)
    view.add_item(
        disnake.ui.Button(
            label="Взять смену",
            style=disnake.ButtonStyle.green,
            custom_id=f"shift:take:{shift_id}",
            disabled=not can_take,
        )
    )
    view.add_item(
        disnake.ui.Button(
            label="Выйти со смены",
            style=disnake.ButtonStyle.danger,
            custom_id=f"shift:leave:{shift_id}",
            disabled=not can_leave,
        )
    )
    return view


async def publish_shift_message(guild, shift_id: int):
    """Публикует карточку смены и сохраняет её message_id.

    Создание записи в БД и публикация в Discord — две разные системы, поэтому
    вызывающий код обязан обработать исключение и отменить непубликованную смену.
    """
    if guild is None:
        raise RuntimeError("Сервер Discord недоступен")
    channel = guild.get_channel(config.SHIFTS_CHANNEL_ID)
    if not channel:
        raise RuntimeError(f"Канал смен {config.SHIFTS_CHANNEL_ID} не найден")
    shift = await db.fetchone("SELECT * FROM shifts WHERE id=?", (shift_id,))
    if not shift:
        raise RuntimeError(f"Смена #{shift_id} не найдена после создания")
    message = await channel.send(
        content=(f"<@&{config.RECRUITER_ROLE_ID}>" if config.PING_RECRUITERS_ON_SHIFT_CREATE else None),
        embed=EmbedGenerator.create_shift_embed(shift, []),
        view=build_shift_view(shift_id),
        allowed_mentions=disnake.AllowedMentions(roles=True),
    )
    try:
        await shift_service.set_shift_message_id(shift_id, message.id)
    except Exception:
        # Не оставляем внешне рабочую кнопку у карточки, связь с которой не удалось
        # сохранить в БД.
        try:
            await message.edit(view=None)
        except Exception:
            logger.exception("Не удалось отключить карточку смены #%s после ошибки БД", shift_id)
        raise
    return message


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
    return await notify(bot, report["user_id"], "REPORT_APPROVED", "shift_report", report["id"], embed=dm)


async def _notify_report_rejected(bot, report, reason: str):
    dm = disnake.Embed(title="❌ ВАШ ОТЧЁТ ОТКЛОНЁН", color=disnake.Color.red())
    dm.add_field(name="📋 Смена", value=f"#{report['shift_id']}", inline=True)
    dm.add_field(name="📝 Причина", value=reason, inline=False)
    dm.add_field(
        name="ℹ️ Как исправить",
        value=f"Откройте **панель → 🕐 Смена → ♻️ Исправить отчёт**. Резервный способ: `/смена исправить отчёт:{report['id']}`.",
        inline=False,
    )
    return await notify(bot, report["user_id"], "REPORT_REJECTED", "shift_report", report["id"], embed=dm)


class LeaveShiftModal(disnake.ui.Modal):
    def __init__(self, shift_id: int):
        self.shift_id = shift_id
        super().__init__(
            title=f"Выйти со смены #{shift_id}",
            custom_id=f"shift_leave_modal:{shift_id}",
            components=[
                disnake.ui.TextInput(
                    label="Причина (необязательно)",
                    custom_id="reason",
                    placeholder="Например: появились срочные дела",
                    required=False,
                    max_length=500,
                    style=disnake.TextInputStyle.paragraph,
                )
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction):
        reason = inter.text_values.get("reason", "").strip() or "Личные обстоятельства"
        try:
            left_shift_id = await shift_service.leave_shift(inter.author.id, self.shift_id, reason)
        except UserFacingError as exc:
            return await inter.response.send_message(f"❌ {exc}", ephemeral=True)
        await inter.response.send_message(
            f"✅ Вы вышли со смены **#{left_shift_id}**. Место снова свободно.",
            ephemeral=True,
        )
        await update_shift_message(inter.guild, left_shift_id)


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
        await inter.response.defer(ephemeral=True, with_message=True)
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

        posted = False
        channel = inter.guild.get_channel(config.REPORTS_CHANNEL_ID)
        if channel:
            try:
                embed = EmbedGenerator.create_report_embed(result.report, result.member, inter.author.mention)
                from cogs.advertising import add_summary_field
                await add_summary_field(embed, inter.author.id, shift_id=result.report["shift_id"])
                message = await channel.send(
                    content=f"<@&{config.SENIOR_ROLE_ID}> <@&{config.ADMIN_ROLE_ID}>",
                    embed=embed,
                    view=build_report_view(result.report["id"]),
                )
                posted = True
                try:
                    await shift_service.set_report_message_id(result.report["id"], message.id)
                except Exception:
                    logger.exception("Карточка отчёта #%s создана, но message_id не сохранён", result.report["id"])
            except Exception:
                logger.exception("Не удалось отправить отчёт #%s в канал отчётов", result.report["id"])
        else:
            logger.error("REPORTS_CHANNEL_ID=%s не найден", config.REPORTS_CHANNEL_ID)

        if posted:
            text = f"✅ Смена **#{self.shift_id}** завершена. Отчёт **#{result.report['id']}** отправлен на проверку."
        else:
            warned = await send_control_warning(
                inter.guild,
                f"⚠️ <@&{config.SENIOR_ROLE_ID}> отчёт **#{result.report['id']}** по смене **#{self.shift_id}** "
                "сохранён в БД, но публичная карточка не была создана. Проверьте отчёт через панель руководства.",
            )
            text = (
                f"⚠️ Смена **#{self.shift_id}** завершена и отчёт **#{result.report['id']}** сохранён в базе, "
                "но отправить карточку в канал отчётов не удалось. "
                + ("Старший состав уведомлён." if warned else "Откройте панель руководства и сообщите старшему составу.")
            )
        await inter.edit_original_response(content=text)
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
        await inter.response.defer(ephemeral=True, with_message=True)
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

        posted = False
        channel = inter.guild.get_channel(config.REPORTS_CHANNEL_ID)
        if channel:
            try:
                embed = EmbedGenerator.create_report_embed(result.report, result.member, inter.author.mention)
                from cogs.advertising import add_summary_field
                await add_summary_field(embed, inter.author.id, shift_id=result.report["shift_id"])
                embed.title = "♻️ ИСПРАВЛЕННЫЙ ОТЧЁТ ПО СМЕНЕ"
                message = await channel.send(
                    content=f"<@&{config.SENIOR_ROLE_ID}> <@&{config.ADMIN_ROLE_ID}>",
                    embed=embed,
                    view=build_report_view(result.report["id"]),
                )
                posted = True
                try:
                    await shift_service.set_report_message_id(result.report["id"], message.id)
                except Exception:
                    logger.exception("Исправленная карточка отчёта #%s создана, но message_id не сохранён", result.report["id"])
            except Exception:
                logger.exception("Не удалось повторно отправить отчёт #%s", self.report_id)
        else:
            logger.error("REPORTS_CHANNEL_ID=%s не найден при повторной отправке отчёта #%s", config.REPORTS_CHANNEL_ID, self.report_id)
        if posted:
            text = f"✅ Отчёт **#{self.report_id}** исправлен и снова отправлен на проверку."
        else:
            warned = await send_control_warning(
                inter.guild,
                f"⚠️ <@&{config.SENIOR_ROLE_ID}> исправленный отчёт **#{self.report_id}** сохранён в БД, "
                "но публичная карточка не была создана. Проверьте отчёт через панель руководства.",
            )
            text = (f"⚠️ Отчёт **#{self.report_id}** исправлен и сохранён в базе, но карточку в канал отчётов "
                    "отправить не удалось. "
                    + ("Старший состав уведомлён." if warned else "Откройте панель руководства и сообщите старшему составу."))
        await inter.edit_original_response(content=text)


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
        await inter.response.defer(ephemeral=True, with_message=True)
        reason = inter.text_values["reason"]
        try:
            report = await shift_service.reject_report(self.report_id, inter.author.id, reason)
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")

        dm_sent = await _notify_report_rejected(inter.bot, report, reason)
        synced = await sync_report_review_message(
            inter.guild, self.report_id, False, inter.author.mention, reason
        )
        warnings = []
        if not synced:
            warnings.append("публичная карточка не обновилась")
        if not dm_sent:
            warnings.append("ЛС рекрутеру не доставлено")
        text = f"✅ Отчёт **#{self.report_id}** отклонён."
        if warnings:
            text += "\n⚠️ " + "; ".join(warnings) + ". Данные в БД сохранены."
        await inter.edit_original_response(content=text)


def _has_private_booking_response(message) -> bool:
    return (getattr(message, "content", "") or "").startswith((
        "❌ ", "✅ Вы записались на смену",
    ))


async def update_shift_message(guild, shift_id: int) -> bool:
    if guild is None:
        return False
    channel = guild.get_channel(config.SHIFTS_CHANNEL_ID)
    if not channel:
        logger.error("SHIFTS_CHANNEL_ID=%s не найден", config.SHIFTS_CHANNEL_ID)
        return False

    shift = await db.fetchone("SELECT * FROM shifts WHERE id=?", (shift_id,))
    if not shift:
        return False
    members = await db.fetchall("SELECT * FROM shift_members WHERE shift_id=? ORDER BY id", (shift_id,))
    embed = EmbedGenerator.create_shift_embed(shift, members)
    # Пока смена не закончена/не отменена и есть свободные места,
    # новые рекрутеры могут записываться даже если другой участник уже active.
    interactive_statuses = ("open", "booked", "active")
    scheduled_start = parse_db(shift["scheduled_start"])
    before_start = scheduled_start is None or local_now() < scheduled_start
    can_take = (shift["slots"] or 0) > 0 and before_start
    can_leave = before_start
    view = (
        build_shift_view(shift_id, can_take=can_take, can_leave=can_leave)
        if shift["status"] in interactive_statuses
        else None
    )

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
            # Remove a leaked v6 booking result, while preserving normal role
            # mentions and leaving the public roster/status update intact.
            repair = {"content": None} if _has_private_booking_response(message) else {}
            await message.edit(embed=embed, view=view, **repair)
            return True
        except Exception:
            logger.exception("Не удалось обновить сообщение смены #%s", shift_id)
            return False
    logger.warning("Не найдено сообщение смены #%s для обновления", shift_id)
    return False


async def sync_report_review_message(guild, report_id: int, approved: bool, reviewer_mention: str, reason: str | None = None):
    """Обновляет публичную карточку отчёта после обработки из панели/команды.

    Для новых отчётов используем сохранённый message_id; сканирование истории остаётся
    только как fallback для карточек, созданных старыми версиями бота.
    """
    if guild is None:
        return False
    channel = guild.get_channel(config.REPORTS_CHANNEL_ID)
    if not channel:
        return False

    message = None
    report = await db.fetchone("SELECT message_id FROM shift_reports WHERE id=?", (report_id,))
    if report and report["message_id"]:
        try:
            message = await channel.fetch_message(report["message_id"])
        except Exception:
            logger.warning("Не удалось получить message_id=%s для отчёта #%s", report["message_id"], report_id)

    if message is None:
        try:
            async for candidate in channel.history(limit=200):
                if not candidate.embeds or not getattr(candidate, "components", None):
                    continue
                source = candidate.embeds[0]
                footer = source.footer.text if source.footer else ""
                if footer == f"Отчёт #{report_id}":
                    message = candidate
                    await shift_service.set_report_message_id(report_id, candidate.id)
                    break
        except Exception:
            logger.exception("Не удалось найти старую карточку отчёта #%s", report_id)

    if message is None:
        return False
    try:
        embed = disnake.Embed(
            title="✅ ОТЧЁТ ОДОБРЕН" if approved else "❌ ОТЧЁТ ОТКЛОНЁН",
            color=disnake.Color.green() if approved else disnake.Color.red(),
        )
        embed.add_field(name="📋 Отчёт", value=f"#{report_id}", inline=True)
        embed.add_field(name="👤 Проверил", value=reviewer_mention, inline=True)
        if reason:
            embed.add_field(name="📝 Причина", value=reason, inline=False)
        from cogs.advertising import add_summary_field
        details = await db.fetchone("SELECT shift_id,user_id FROM shift_reports WHERE id=?", (report_id,))
        if details:
            await add_summary_field(embed, details["user_id"], shift_id=details["shift_id"])
        embed.set_footer(text=f"Отчёт #{report_id}")
        await message.edit(embed=embed, view=None)
        return True
    except Exception:
        logger.exception("Не удалось синхронизировать карточку отчёта #%s", report_id)
        return False


class Shifts(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._booking_cards_restored = False

    @commands.Cog.listener()
    async def on_ready(self):
        if self._booking_cards_restored:
            return
        guild = self.bot.get_guild(config.GUILD_ID)
        channel = guild.get_channel(config.SHIFTS_CHANNEL_ID) if guild else None
        if channel is None:
            return
        try:
            async for message in channel.history(limit=200):
                if message.author.id != self.bot.user.id or not _has_private_booking_response(message):
                    continue
                if not message.embeds or not message.embeds[0].footer:
                    continue
                footer = message.embeds[0].footer.text or ""
                if not footer.startswith("Смена #"):
                    continue
                try:
                    shift_id = int(footer.removeprefix("Смена #"))
                except ValueError:
                    continue
                row = await db.fetchone("SELECT message_id FROM shifts WHERE id=?", (shift_id,))
                if row and row["message_id"] == message.id:
                    if not await update_shift_message(guild, shift_id):
                        raise RuntimeError(f"Не удалось восстановить карточку смены #{shift_id}")
            self._booking_cards_restored = True
        except Exception:
            logger.exception("Не удалось очистить личные ответы на старых карточках смен")

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
            await inter.response.defer(ephemeral=True, with_message=True)
            try:
                await shift_service.take_shift(shift_id, inter.author.id, inter.author.name)
            except UserFacingError as exc:
                return await inter.edit_original_response(content=f"❌ {exc}")
            await inter.edit_original_response(content=f"✅ Вы записались на смену **#{shift_id}**.")
            try:
                await update_shift_message(inter.guild, shift_id)
            except Exception:
                logger.exception("Не удалось обновить карточку смены #%s после записи", shift_id)
                await inter.edit_original_response(
                    content=f"✅ Вы записались на смену **#{shift_id}**. ⚠️ Карточка не обновилась; запись в БД сохранена."
                )
            return

        if custom_id.startswith("shift:leave:"):
            if not is_recruiter_or_higher(inter.author):
                return await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            try:
                shift_id = int(custom_id.rsplit(":", 1)[1])
            except ValueError:
                return await inter.response.send_message("❌ Не удалось определить ID смены.", ephemeral=True)
            return await inter.response.send_modal(LeaveShiftModal(shift_id))

        if custom_id.startswith("report:approve:") or custom_id == "approve_report":
            if not is_senior_or_admin(inter.author):
                return await inter.response.send_message("❌ Только старший состав может проверять отчёты.", ephemeral=True)
            try:
                if custom_id == "approve_report":
                    footer = inter.message.embeds[0].footer.text if inter.message.embeds and inter.message.embeds[0].footer else ""
                    report_id = int(footer.split("#")[-1])
                else:
                    report_id = int(custom_id.rsplit(":", 1)[1])
            except (ValueError, IndexError, AttributeError):
                return await inter.response.send_message("❌ Не удалось определить ID отчёта.", ephemeral=True)
            await inter.response.defer(ephemeral=True, with_message=True)
            try:
                report = await shift_service.approve_report(report_id, inter.author.id)
            except UserFacingError as exc:
                return await inter.edit_original_response(content=f"❌ {exc}")

            dm_sent = await _notify_report_approved(self.bot, report, inter.author.mention)
            synced = await sync_report_review_message(inter.guild, report_id, True, inter.author.mention)
            warnings = []
            if not synced:
                warnings.append("публичная карточка не обновилась")
            if not dm_sent:
                warnings.append("ЛС рекрутеру не доставлено")
            text = f"✅ Отчёт **#{report_id}** одобрен."
            if warnings:
                text += "\n⚠️ " + "; ".join(warnings) + ". Данные в БД сохранены."
            await inter.edit_original_response(content=text)
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
        await inter.response.defer(ephemeral=True, with_message=True)
        try:
            start = datetime.strptime(f"{дата} {начало}", "%d.%m.%Y %H:%M")
            end = datetime.strptime(f"{дата} {конец}", "%d.%m.%Y %H:%M")
            # Если конец по часам раньше/равен началу, считаем, что смена
            # заканчивается на следующие сутки (например 23:50–00:10).
            if end <= start:
                end += timedelta(days=1)
        except ValueError:
            return await inter.edit_original_response(
                content="❌ Формат: `дата: 18.08.2026`, `начало: 18:00`, `конец: 20:00`."
            )

        try:
            shift_id = await shift_service.create_shift(inter.author.id, start, end, места, описание)
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")

        try:
            await publish_shift_message(inter.guild, shift_id)
        except Exception as exc:
            logger.exception("Не удалось опубликовать созданную смену #%s", shift_id)
            try:
                await shift_service.cancel_shift(
                    inter.author.id, shift_id, "Автоотмена: карточку смены не удалось опубликовать"
                )
            except Exception:
                logger.exception("Не удалось автоотменить непубликованную смену #%s", shift_id)
            return await inter.edit_original_response(
                content=(f"❌ Смена **#{shift_id}** не опубликована и автоматически отменена. "
                         "Проверьте канал смен и права бота. Техническая причина записана в лог.")
            )
        await inter.edit_original_response(content=f"✅ Смена **#{shift_id}** создана.")

    @shift.sub_command(name="выйти", description="Отказаться от забронированной смены")
    @is_recruiter()
    async def leave_shift_command(self, inter, смена: int = None, причина: str = "Личные обстоятельства"):
        await inter.response.defer(ephemeral=True, with_message=True)
        try:
            shift_id = await shift_service.leave_shift(inter.author.id, смена, причина)
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")
        await inter.edit_original_response(content=f"✅ Вы вышли со смены **#{shift_id}**. Место снова свободно.")
        await update_shift_message(inter.guild, shift_id)

    @shift.sub_command(name="начать", description="Начать свою ближайшую смену")
    @is_recruiter()
    async def start_shift(self, inter):
        await inter.response.defer(ephemeral=True, with_message=True)
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
        await inter.response.defer(ephemeral=True, with_message=True)
        try:
            report = await shift_service.approve_report(отчёт, inter.author.id)
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")
        dm_sent = await _notify_report_approved(self.bot, report, inter.author.mention)
        synced = await sync_report_review_message(inter.guild, отчёт, True, inter.author.mention)
        warnings = []
        if not synced:
            warnings.append("публичная карточка не обновилась")
        if not dm_sent:
            warnings.append("ЛС рекрутеру не доставлено")
        text = f"✅ Отчёт **#{отчёт}** одобрен."
        if warnings:
            text += "\n⚠️ " + "; ".join(warnings) + ". Данные в БД сохранены."
        await inter.edit_original_response(content=text)

    @shift.sub_command(name="отклонить", description="Отклонить отчёт по ID (резервный способ)")
    @is_senior()
    async def reject_report_command(self, inter, отчёт: int, причина: str):
        await inter.response.defer(ephemeral=True, with_message=True)
        try:
            report = await shift_service.reject_report(отчёт, inter.author.id, причина)
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")
        dm_sent = await _notify_report_rejected(self.bot, report, причина)
        synced = await sync_report_review_message(inter.guild, отчёт, False, inter.author.mention, причина)
        warnings = []
        if not synced:
            warnings.append("публичная карточка не обновилась")
        if not dm_sent:
            warnings.append("ЛС рекрутеру не доставлено")
        text = f"✅ Отчёт **#{отчёт}** отклонён."
        if warnings:
            text += "\n⚠️ " + "; ".join(warnings) + ". Данные в БД сохранены."
        await inter.edit_original_response(content=text)

    @shift.sub_command(name="снять", description="Снять рекрутера со смены")
    @is_senior()
    async def remove_member(
        self,
        inter,
        пользователь: disnake.Member,
        причина: str,
        смена: int = None,
    ):
        await inter.response.defer(ephemeral=True, with_message=True)
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
        await inter.response.defer(ephemeral=True, with_message=True)
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
        await inter.response.defer(ephemeral=True, with_message=True)
        now = local_now()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        shifts = await shift_service.get_schedule(day_start, day_end)

        embed = disnake.Embed(title="📅 РАСПИСАНИЕ СМЕН", color=disnake.Color.blue())
        if not shifts:
            embed.description = "На сегодня смен нет."
        else:
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
            if len(shifts) > 20:
                embed.set_footer(text=f"Показаны первые 20 из {len(shifts)} смен")
        await inter.edit_original_response(embed=embed)


def setup(bot):
    bot.add_cog(Shifts(bot))
