"""Кнопочный интерфейс рекламы, FileUpload и хранение подтверждений в Discord."""
import asyncio
import hashlib
import io
import logging
import sqlite3
from datetime import timedelta, timezone

import disnake
from disnake.ext import commands, tasks

import config
from database.db import db, ensure_user, notify
from services import advertising_service as ads
from services.errors import UserFacingError
from utils.checks import is_recruiter_or_higher, is_senior_or_admin
from utils.time_utils import format_utc_db, parse_db, utc_now

logger = logging.getLogger(__name__)
PERIODS = ("сегодня", "неделя", "месяц", "всё время")


async def add_summary_field(embed, user_id, period="неделя", shift_id=None, *, start=None, end=None):
    data = await ads.summary(user_id, period, shift_id, start=start, end=end)
    value = (f"Зачтено: {data['total']} • без проверки: {data['counted']} • по фото: {data['approved']}\n"
             f"Ожидает проверки: {data['pending']} • отклонено: {data['rejected']}")
    index = next((i for i, field in enumerate(embed.fields) if field.name == "📢 РЕКЛАМА"), None)
    if index is None:
        embed.add_field(name="📢 РЕКЛАМА", value=value, inline=False)
    else:
        embed.set_field_at(index, name="📢 РЕКЛАМА", value=value, inline=False)


def button_view(*buttons):
    view = disnake.ui.View(timeout=None)
    for label, cid, style in buttons:
        view.add_item(disnake.ui.Button(label=label, custom_id=cid, style=style))
    return view


def menu_view(member):
    buttons = [
        ("📢 Подать рекламу", "ads:prepare", disnake.ButtonStyle.green),
        ("🔄 Текущая попытка", "ads:current", disnake.ButtonStyle.primary),
        ("📊 Моя статистика", "ads:stats", disnake.ButtonStyle.primary),
        ("📋 История и результаты", "ads:history:0", disnake.ButtonStyle.secondary),
    ]
    if is_senior_or_admin(member):
        buttons += [
            ("📝 Ожидают проверки", "ads:queue:0", disnake.ButtonStyle.secondary),
            ("🏆 Рейтинг рекламы", "ads:top:1", disnake.ButtonStyle.secondary),
            ("📊 Статистика отдела", "ads:department:1", disnake.ButtonStyle.secondary),
            ("👤 Статистика рекрутера", "ads:choose_user", disnake.ButtonStyle.secondary),
        ]
    return button_view(*buttons)


async def show_advertising_menu(inter):
    if not inter.guild or inter.guild.id != config.GUILD_ID or not is_recruiter_or_higher(inter.author):
        return await inter.response.send_message("❌ Раздел доступен рекрутерам и старшему составу.", ephemeral=True)
    await inter.response.defer(ephemeral=True, with_message=True)
    await ensure_user(inter.author.id, username=inter.author.name)
    await inter.edit_original_response(embed=menu_embed(), view=menu_view(inter.author))


def menu_embed():
    embed = disnake.Embed(title="📢 РЕКЛАМА СЕМЬИ", color=disnake.Color.blue())
    embed.description = (
        f"Семья: **{disnake.utils.escape_markdown(config.ADS_FAMILY_NAME)}**.\n"
        "**До подачи** нажмите «Подать рекламу». Бот заранее сообщит, нужно ли фото.\n"
        "**После выхода объявления** подтвердите публикацию кнопкой или загрузите скриншот.\n"
        f"Проверка: {config.ADS_CHECK_PERCENT}% попыток; интервал публикаций: {config.ADS_INTERVAL_MINUTES} мин.\n\n"
        "Скриншот должен показывать опубликованное объявление, время и ваше игровое имя. "
        "Имя редактора не является именем автора."
    )
    if not config.ADS_REVIEW_CHANNEL_ID:
        embed.add_field(name="Настройка", value="Канал проверки ещё не настроен администратором.", inline=False)
    return embed


def attempt_embed(row):
    embed = disnake.Embed(title=f"📢 РЕКЛАМА #{row['id']}", color=disnake.Color.orange())
    embed.add_field(name="Статус", value=ads.STATUS_LABELS[row["status"]], inline=False)
    embed.add_field(name="Рекрутер / игровое имя", value=disnake.utils.escape_markdown(row["discord_name"]), inline=False)
    embed.add_field(name="Смена", value=f"#{row['shift_id']}" if row["shift_id"] else "—")
    embed.add_field(name="Начало попытки", value=format_utc_db(row["prepared_at"]))
    if row["status"] == "prepared":
        embed.description = (
            "📷 **Нужно фото.** При выходе объявления сделайте скриншот, затем нажмите «Прикрепить фото».\n"
            "Отмена не сбрасывает требование фото."
            if row["requires_proof"] else
            "Фото не требуется. После выхода объявления нажмите «Опубликовано». Подача заявки ещё не является публикацией."
        )
    elif row["status"] == "uploading":
        embed.description = "Передача подтверждения выполняется или восстанавливается. Повторно загружать фото пока не нужно."
    elif row["status"] == "pending":
        embed.description = "Фото принято и ждёт проверки. В зачтённое количество эта запись пока не входит."
    if row["reject_reason"]:
        embed.add_field(name="Причина отклонения", value=disnake.utils.escape_markdown(row["reject_reason"]), inline=False)
    return embed


def attempt_view(row):
    buttons = []
    if row["status"] == "prepared":
        if row["requires_proof"]:
            buttons.append(("📷 Прикрепить фото", f"ads:upload:{row['id']}", disnake.ButtonStyle.primary))
        else:
            buttons.append(("✅ Опубликовано", f"ads:confirm:{row['id']}", disnake.ButtonStyle.green))
        buttons.append(("Отменить попытку", f"ads:cancel:{row['id']}", disnake.ButtonStyle.danger))
    if row["status"] == "counted":
        buttons.append(("Отменить ошибочную отметку", f"ads:cancel:{row['id']}", disnake.ButtonStyle.danger))
    buttons.append(("📢 Меню рекламы", "ads:menu", disnake.ButtonStyle.secondary))
    return button_view(*buttons)


def upload_modal(attempt_id, user_id):
    return disnake.ui.Modal(
        title="Подтверждение публикации", custom_id=f"ads:upload:{attempt_id}:{user_id}",
        components=[disnake.ui.Label(
            "Скриншот опубликованного объявления",
            disnake.ui.FileUpload(custom_id="proof", min_values=1, max_values=1, required=True),
            description="PNG / JPEG / WebP, до 8 МБ. Должны быть видны время, автор и реклама семьи.",
        )],
    )


def reason_modal(action, attempt_id, user_id):
    return disnake.ui.Modal(
        title="Причина отклонения" if action == "reject" else "Причина отмены",
        custom_id=f"ads:{action}:{attempt_id}:{user_id}",
        components=[disnake.ui.Label("Причина", disnake.ui.TextInput(custom_id="reason", style=disnake.TextInputStyle.paragraph,
                                                                    min_length=1, max_length=500, required=True))],
    )


async def open_modal(inter, modal):
    # Обработка идёт через постоянный on_modal_submit, без callback-кеша:
    # формы остаются обработаемыми после перезапуска процесса.
    await inter.response.send_modal(title=modal.title, custom_id=modal.custom_id, components=modal.components)


def proof_marker(row):
    return f"Реклама #{row['id']} • proof:{row['upload_token']}"


def review_embed(row):
    embed = disnake.Embed(title=f"📷 ПРОВЕРКА РЕКЛАМЫ #{row['id']}", color=disnake.Color.orange())
    embed.add_field(name="Рекрутер / игровое имя", value=f"<@{row['user_id']}>\n{disnake.utils.escape_markdown(row['discord_name'])}", inline=False)
    embed.add_field(name="Семья", value=disnake.utils.escape_markdown(row["family_name"]))
    embed.add_field(name="Смена", value=f"#{row['shift_id']}" if row["shift_id"] else "—")
    embed.add_field(name="Подготовлено", value=format_utc_db(row["prepared_at"]))
    embed.add_field(name="Фото отправлено", value=format_utc_db(row["uploading_at"]))
    embed.add_field(name="Статус", value=ads.STATUS_LABELS[row["status"]], inline=False)
    if row["reviewed_by"]:
        embed.add_field(name="Проверил", value=f"<@{row['reviewed_by']}> • {format_utc_db(row['reviewed_at'])}", inline=False)
    if row["reject_reason"]:
        embed.add_field(name="Причина", value=disnake.utils.escape_markdown(row["reject_reason"]), inline=False)
    if row["proof_filename"]:
        embed.set_image(url=f"attachment://{row['proof_filename']}")
    embed.set_footer(text=proof_marker(row))
    return embed


def review_view(row):
    view = button_view(
        ("✅ Одобрить", f"ads:approve:{row['id']}", disnake.ButtonStyle.green),
        ("❌ Отклонить", f"ads:reject:{row['id']}", disnake.ButtonStyle.danger),
    )
    for button in view.children:
        button.disabled = row["status"] != "pending"
    return view


def stats_embed(data, title, period):
    embed = disnake.Embed(title=title, description=f"Период: {period}", color=disnake.Color.blue())
    embed.add_field(name="Зачтено", value=str(data["total"]), inline=False)
    embed.add_field(name="Без проверки (со слов рекрутера)", value=str(data["counted"]), inline=False)
    embed.add_field(name="Подтверждено по фото", value=str(data["approved"]), inline=False)
    embed.add_field(name="Ожидает проверки", value=str(data["pending"]))
    embed.add_field(name="Отклонено", value=str(data["rejected"]))
    embed.add_field(name="Отменено", value=str(data["cancelled"]))
    embed.add_field(name="Незавершено", value=str(data["unfinished"]))
    return embed


def periods_view(prefix):
    return button_view(*[
        (period.capitalize(), f"ads:{prefix}:{index}", disnake.ButtonStyle.secondary)
        for index, period in enumerate(PERIODS)
    ], ("📢 Меню", "ads:menu", disnake.ButtonStyle.secondary))


class Advertising(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._upload_slots = asyncio.Semaphore(2)
        self._card_lock = asyncio.Lock()
        self._report_lock = asyncio.Lock()
        self.maintenance.add_exception_type(sqlite3.OperationalError)
        self.maintenance.start()

    def cog_unload(self):
        self.maintenance.cancel()

    def channel(self, guild):
        if not config.ADS_REVIEW_CHANNEL_ID:
            raise UserFacingError("Раздел рекламы ещё не настроен администратором.")
        channel = guild.get_channel(config.ADS_REVIEW_CHANNEL_ID)
        if not isinstance(channel, disnake.TextChannel):
            raise UserFacingError("Канал проверки рекламы недоступен. Сообщите администратору.")
        ordinary_channels = {config.SHIFTS_CHANNEL_ID, config.REPORTS_CHANNEL_ID, config.STATS_CHANNEL_ID,
                             config.CONTROL_CHANNEL_ID, config.LOGS_CHANNEL_ID, config.PANEL_CHANNEL_ID}
        if channel.id in ordinary_channels:
            raise UserFacingError("Для рекламы нужен отдельный закрытый канал проверки.")
        recruiter_role = guild.get_role(config.RECRUITER_ROLE_ID)
        if channel.permissions_for(guild.default_role).view_channel or (
            recruiter_role and channel.permissions_for(recruiter_role).view_channel
        ):
            raise UserFacingError("Канал проверки должен быть закрыт от @everyone и обычной роли рекрутеров.")
        perms = channel.permissions_for(guild.me)
        if not all(getattr(perms, name, False) for name in ("view_channel", "send_messages", "embed_links", "attach_files", "read_message_history")):
            raise UserFacingError("Боту не хватает прав в канале проверки рекламы. Сообщите администратору.")
        return channel

    async def _allowed(self, inter, senior=False):
        predicate = is_senior_or_admin if senior else is_recruiter_or_higher
        if not inter.guild or inter.guild.id != config.GUILD_ID or not predicate(inter.author):
            await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return False
        return True

    async def _error(self, inter, exc):
        if isinstance(exc, UserFacingError):
            text = f"❌ {exc}"
        else:
            logger.error("Ошибка рекламы", exc_info=(type(exc), exc, exc.__traceback__))
            text = "❌ Операция не завершена. Откройте текущую попытку; ошибка записана в лог."
        if inter.response.is_done():
            await inter.edit_original_response(content=text, embed=None, view=None)
        else:
            await inter.response.send_message(text, ephemeral=True)

    @commands.Cog.listener()
    async def on_button_click(self, inter):
        cid = getattr(inter.component, "custom_id", "") or ""
        if not cid.startswith("ads:"):
            return
        parts = cid.split(":")
        action = parts[1]
        senior = action in ("approve", "reject", "queue", "top", "department", "choose_user", "userstats")
        if not await self._allowed(inter, senior):
            return
        try:
            if action == "menu":
                return await show_advertising_menu(inter)
            if action in ("upload", "cancel", "reject"):
                attempt_id = int(parts[2])
                row = await ads.get_attempt(attempt_id)
                if not row:
                    raise UserFacingError("Попытка не найдена.")
                if action == "reject":
                    self._check_review_source(inter, row)
                    if row["user_id"] == inter.author.id:
                        raise UserFacingError("Нельзя проверять собственную рекламу.")
                elif row["user_id"] != inter.author.id:
                    raise UserFacingError("Это не ваша попытка.")
                if action == "upload":
                    self.channel(inter.guild)
                    if row["status"] != "prepared" or not row["requires_proof"]:
                        raise UserFacingError("Откройте текущую попытку: загрузка фото сейчас недоступна.")
                    return await open_modal(inter, upload_modal(attempt_id, inter.author.id))
                return await open_modal(inter, reason_modal(action, attempt_id, inter.author.id))
            await inter.response.defer(ephemeral=True, with_message=True)
            if action == "prepare":
                self.channel(inter.guild)
                row = await ads.prepare(inter.author.id, inter.author.name, inter.author.display_name)
                await inter.edit_original_response(embed=attempt_embed(row), view=attempt_view(row))
            elif action == "current":
                row = await ads.current_attempt(inter.author.id)
                if not row:
                    return await inter.edit_original_response(content="Незавершённых попыток нет. Результаты доступны в истории.", view=menu_view(inter.author))
                await inter.edit_original_response(embed=attempt_embed(row), view=attempt_view(row))
            elif action == "confirm":
                self.channel(inter.guild)
                row = await ads.confirm(int(parts[2]), inter.author.id)
                embed = attempt_embed(row)
                shift_data = await ads.summary(inter.author.id, shift_id=row["shift_id"])
                embed.add_field(name="За эту смену", value=f"Зачтено: {shift_data['total']} • ожидает проверки: {shift_data['pending']}", inline=False)
                await inter.edit_original_response(embed=embed, view=attempt_view(row))
            elif action == "stats":
                period = PERIODS[int(parts[2])] if len(parts) > 2 else "неделя"
                data = await ads.summary(inter.author.id, period)
                embed = stats_embed(data, "📊 МОЯ РЕКЛАМА", period)
                member = await db.fetchone("SELECT shift_id FROM shift_members WHERE user_id=? AND status='active' ORDER BY id DESC LIMIT 1", (inter.author.id,))
                if member:
                    shift_data = await ads.summary(inter.author.id, shift_id=member["shift_id"])
                    embed.add_field(name=f"Текущая смена #{member['shift_id']}", value=f"Зачтено: {shift_data['total']} • На проверке: {shift_data['pending']}", inline=False)
                await inter.edit_original_response(embed=embed, view=periods_view("stats"))
            elif action == "history":
                await self._history(inter, int(parts[2]))
            elif action == "queue":
                await self._queue(inter, int(parts[2]))
            elif action == "approve":
                row = await ads.get_attempt(int(parts[2]))
                self._check_review_source(inter, row)
                await self._review(inter, row["id"], True)
            elif action == "top":
                period = PERIODS[int(parts[2])]
                rows = await ads.rankings(period)
                embed = disnake.Embed(title="🏆 РЕЙТИНГ РЕКЛАМЫ", description=f"Период: {period}", color=disnake.Color.gold())
                embed.add_field(name="Зачтённые публикации", value="\n".join(
                    f"{i}. <@{r['user_id']}> — {r['total']} (по фото: {r['approved']})" for i, r in enumerate(rows, 1)
                ) or "Пока нет публикаций.", inline=False)
                await inter.edit_original_response(embed=embed, view=periods_view("top"))
            elif action == "department":
                period = PERIODS[int(parts[2])]
                await inter.edit_original_response(embed=stats_embed(await ads.summary(None, period), "📊 РЕКЛАМА ОТДЕЛА", period), view=periods_view("department"))
            elif action == "choose_user":
                view = disnake.ui.View(timeout=None)
                view.add_item(disnake.ui.UserSelect(custom_id="ads:user_select", placeholder="Выберите рекрутера"))
                await inter.edit_original_response(content="Выберите рекрутера для статистики рекламы:", view=view)
            elif action == "userstats":
                target_id, index = int(parts[2]), int(parts[3])
                await inter.edit_original_response(embed=stats_embed(await ads.summary(target_id, PERIODS[index]), f"📊 РЕКЛАМА • {target_id}", PERIODS[index]), view=periods_view(f"userstats:{target_id}"))
        except Exception as exc:
            await self._error(inter, exc)

    def _check_review_source(self, inter, row):
        if not row or row["proof_channel_id"] != inter.channel_id or row["proof_message_id"] != inter.message.id:
            raise UserFacingError("Используйте кнопки исходной карточки проверки.")

    async def _history(self, inter, before_id):
        rows = await db.fetchall(
            "SELECT * FROM ad_attempts WHERE user_id=? AND (?=0 OR id<?) ORDER BY id DESC LIMIT 11",
            (inter.author.id, before_id, before_id),
        )
        embed = disnake.Embed(title="📋 МОЯ ИСТОРИЯ РЕКЛАМЫ", color=disnake.Color.blue())
        embed.description = "\n".join(
            f"**#{r['id']}** • {format_utc_db(r['prepared_at'])} • {ads.STATUS_LABELS[r['status']]}"
            + (f"\nПричина: {disnake.utils.escape_markdown(r['reject_reason'])}" if r["reject_reason"] else "")
            for r in rows[:10]
        )[:4000] or "Пока нет попыток."
        buttons = [("📢 Меню", "ads:menu", disnake.ButtonStyle.secondary)]
        if rows and not before_id and rows[0]["status"] == "counted":
            buttons.append(("Отменить последнюю отметку", f"ads:cancel:{rows[0]['id']}", disnake.ButtonStyle.danger))
        if len(rows) > 10:
            buttons.append(("Следующие", f"ads:history:{rows[9]['id']}", disnake.ButtonStyle.primary))
        if before_id:
            buttons.append(("Сначала", "ads:history:0", disnake.ButtonStyle.secondary))
        view = button_view(*buttons)
        if rows:
            view.add_item(disnake.ui.StringSelect(custom_id="ads:history_select", placeholder="Открыть запись", options=[
                disnake.SelectOption(label=f"#{r['id']} • {ads.STATUS_LABELS[r['status']]}"[:100], value=str(r["id"])) for r in rows[:10]
            ]))
        await inter.edit_original_response(embed=embed, view=view)

    async def _queue(self, inter, after_id):
        rows = await db.fetchall("SELECT * FROM ad_attempts WHERE status='pending' AND id>? ORDER BY id LIMIT 26", (after_id,))
        embed = disnake.Embed(title="📝 ОЖИДАЮТ ПРОВЕРКИ", color=disnake.Color.orange())
        embed.description = "\n".join(f"**#{r['id']}** • <@{r['user_id']}> • {format_utc_db(r['published_at'])}" for r in rows[:25]) or "Ожидающих проверок на этой странице нет."
        view = button_view(("Сначала", "ads:queue:0", disnake.ButtonStyle.secondary), ("📢 Меню", "ads:menu", disnake.ButtonStyle.secondary))
        if rows:
            view.add_item(disnake.ui.StringSelect(custom_id="ads:queue_select", placeholder="Открыть карточку", options=[
                disnake.SelectOption(label=f"#{r['id']} • {r['discord_name']}"[:100], value=str(r["id"])) for r in rows[:25]
            ]))
        if len(rows) > 25:
            view.add_item(disnake.ui.Button(label="Следующие", custom_id=f"ads:queue:{rows[24]['id']}", style=disnake.ButtonStyle.primary))
        await inter.edit_original_response(embed=embed, view=view)

    @commands.Cog.listener()
    async def on_dropdown(self, inter):
        cid = getattr(inter.component, "custom_id", "") or ""
        if cid not in ("ads:queue_select", "ads:user_select", "ads:history_select"):
            return
        if not await self._allowed(inter, senior=cid != "ads:history_select"):
            return
        await inter.response.defer(ephemeral=True, with_message=True)
        try:
            target_id = int(inter.values[0])
            if cid == "ads:history_select":
                row = await ads.get_attempt(target_id)
                if not row or row["user_id"] != inter.author.id:
                    raise UserFacingError("Это не ваша запись.")
                await inter.edit_original_response(embed=attempt_embed(row), view=attempt_view(row))
                return
            if cid == "ads:user_select":
                await inter.edit_original_response(embed=stats_embed(await ads.summary(target_id), f"📊 РЕКЛАМА • {target_id}", "неделя"), view=periods_view(f"userstats:{target_id}"))
                return
            row = await ads.get_attempt(target_id)
            if not row or not row["proof_message_id"]:
                raise UserFacingError("Карточка не найдена.")
            message = await self._proof_message(row)
            link = f"https://discord.com/channels/{config.GUILD_ID}/{row['proof_channel_id']}/{message.id}"
            view = disnake.ui.View(timeout=None)
            view.add_item(disnake.ui.Button(label="Открыть подтверждение", url=link))
            await inter.edit_original_response(content="Проверьте имя, время и текст объявления. Решение принимается кнопками исходной карточки.", view=view)
        except Exception as exc:
            await self._error(inter, exc)

    @commands.Cog.listener()
    async def on_modal_submit(self, inter):
        cid = inter.custom_id
        if not cid.startswith("ads:"):
            return
        parts = cid.split(":")
        try:
            action, attempt_id, owner_id = parts[1], int(parts[2]), int(parts[3])
        except (ValueError, IndexError):
            return
        if not await self._allowed(inter, senior=action == "reject"):
            return
        await inter.response.defer(ephemeral=True, with_message=True)
        try:
            if inter.author.id != owner_id:
                raise UserFacingError("Это не ваша форма.")
            if action == "cancel":
                await ads.cancel(attempt_id, owner_id, inter.text_values.get("reason", ""))
                await inter.edit_original_response(content="Попытка отменена. Если требовалось фото, следующая публикация также потребует фото.", view=menu_view(inter.author))
            elif action == "reject":
                await self._review(inter, attempt_id, False, inter.text_values.get("reason", ""))
            elif action == "upload":
                files = inter.resolved_values.get("proof", [])
                if len(files) != 1 or not isinstance(files[0], disnake.Attachment):
                    raise UserFacingError("Прикрепите один скриншот.")
                await self._upload(inter, attempt_id, files[0])
        except Exception as exc:
            await self._error(inter, exc)

    async def _upload(self, inter, attempt_id, attachment):
        channel = self.channel(inter.guild)
        if attachment.size > config.ADS_MAX_FILE_BYTES:
            raise UserFacingError("Скриншот превышает 8 МБ. Сохраните его в меньшем размере.")
        async with self._upload_slots:
            data = await asyncio.wait_for(attachment.read(), timeout=30)
            if len(data) > config.ADS_MAX_FILE_BYTES:
                raise UserFacingError("Скриншот превышает 8 МБ.")
            extension = ads.image_extension(data)
            row = await ads.claim_upload(attempt_id, inter.author.id, hashlib.sha256(data).hexdigest(), channel.id, f"ad_{attempt_id}.{extension}")
            token = row["upload_token"]
            try:
                with io.BytesIO(data) as buffer:
                    upload = disnake.File(buffer, filename=row["proof_filename"])
                    try:
                        message = await asyncio.wait_for(channel.send(embed=review_embed(row), view=review_view(row), file=upload,
                                                                      allowed_mentions=disnake.AllowedMentions.none()), timeout=30)
                    finally:
                        upload.close()
            except (disnake.Forbidden, disnake.NotFound):
                await ads.release_upload(attempt_id, token)
                raise UserFacingError("Discord отказал в загрузке. Попытка сохранена; исправьте права канала и повторите.")
            except Exception:
                # Ответ Discord мог потеряться после успешного создания сообщения.
                # Оставляем token для сверки, не разрешаем повторную публикацию.
                logger.exception("Неопределённый результат загрузки рекламы #%s", attempt_id)
                return await inter.edit_original_response(content="Передача фото не подтверждена. Бот сверит канал автоматически; откройте текущую попытку через несколько минут.", view=menu_view(inter.author))
            if not message.attachments:
                raise RuntimeError("Discord вернул сообщение без вложения")
            await ads.complete_upload(attempt_id, token, message.id, message.attachments[0].id)
            try:
                await self._sync_card(attempt_id)
            except Exception:
                logger.exception("Не удалось включить кнопки проверки рекламы #%s", attempt_id)
            await inter.edit_original_response(content="📷 Фото отправлено на проверку. После одобрения публикация войдёт в зачтённое количество.", view=menu_view(inter.author))

    async def _proof_message(self, row):
        guild = self.bot.get_guild(config.GUILD_ID)
        channel = guild.get_channel(row["proof_channel_id"]) if guild else None
        if not channel:
            raise UserFacingError("Канал подтверждения недоступен.")
        message = await channel.fetch_message(row["proof_message_id"])
        if message.author.id != self.bot.user.id:
            raise UserFacingError("Подтверждение не является сообщением бота.")
        return message

    async def _sync_card(self, attempt_id):
        async with self._card_lock:
            await self._sync_card_locked(attempt_id)

    async def _sync_card_locked(self, attempt_id):
        row = await ads.get_attempt(attempt_id)
        message = await self._proof_message(row)
        embed = review_embed(row)
        # Получаем актуальный attachment URL через сообщение, а не старую CDN-ссылку.
        attachment = next((a for a in message.attachments if a.id == row["proof_attachment_id"]), None)
        if attachment:
            embed.set_image(url=attachment.url)
        else:
            embed.set_image(url=None)
            embed.add_field(name="Подтверждение", value="⚠️ Вложение отсутствует. Одобрять без фото нельзя.", inline=False)
        view = review_view(row)
        if not attachment:
            view.children[0].disabled = True
        await message.edit(embed=embed, view=view)
        await db.execute("UPDATE ad_attempts SET card_status=? WHERE id=? AND status=?", (row["status"], row["id"], row["status"]))

    async def _review(self, inter, attempt_id, approved, reason=""):
        row = await ads.get_attempt(attempt_id)
        if not row:
            raise UserFacingError("Проверка не найдена.")
        if approved:
            message = await self._proof_message(row)
            if not any(a.id == row["proof_attachment_id"] for a in message.attachments):
                raise UserFacingError("Фото отсутствует. Одобрить публикацию нельзя.")
        updated = await ads.review(attempt_id, inter.author.id, approved, reason)
        warning = ""
        try:
            await self._sync_card(attempt_id)
        except Exception:
            logger.exception("Не удалось обновить решение по рекламе #%s", attempt_id)
            warning = " Карточка обновится при следующей синхронизации; решение в БД сохранено."
        try:
            await self._sync_shift_report(updated)
            await db.execute("UPDATE ad_attempts SET report_synced_status=? WHERE id=? AND status=?",
                             (updated["status"], updated["id"], updated["status"]))
        except Exception:
            logger.exception("Не удалось обновить рекламу в отчёте смены #%s", updated["shift_id"])
        try:
            await notify(self.bot, updated["user_id"], "AD_REVIEWED", "advertisement", attempt_id,
                         content=f"Реклама #{attempt_id}: {ads.STATUS_LABELS[updated['status']]}." + (f" Причина: {reason}" if reason else ""))
        except Exception:
            logger.exception("Не удалось уведомить о проверке рекламы #%s", attempt_id)
        await inter.edit_original_response(content=f"✅ {ads.STATUS_LABELS[updated['status']]}.{warning}")

    async def _sync_shift_report(self, row):
        async with self._report_lock:
            await self._sync_shift_report_locked(row)

    async def _sync_shift_report_locked(self, row):
        report = await db.fetchone("SELECT message_id FROM shift_reports WHERE shift_id=? AND user_id=?", (row["shift_id"], row["user_id"]))
        guild = self.bot.get_guild(config.GUILD_ID)
        channel = guild.get_channel(config.REPORTS_CHANNEL_ID) if guild else None
        if not report or not report["message_id"] or not channel:
            return
        message = await channel.fetch_message(report["message_id"])
        if message.author.id != self.bot.user.id or not message.embeds:
            return
        embed = message.embeds[0].copy()
        await add_summary_field(embed, row["user_id"], shift_id=row["shift_id"])
        await message.edit(embed=embed)

    @tasks.loop(minutes=5)
    async def maintenance(self):
        if not config.ADS_REVIEW_CHANNEL_ID:
            return
        # Восстанавливаем отправку после перезапуска между Discord send и commit.
        uploading = await db.fetchall("SELECT * FROM ad_attempts WHERE status='uploading'")
        for row in uploading:
            try:
                stamp = parse_db(row["uploading_at"])
                if not stamp or utc_now() - stamp < timedelta(minutes=5):
                    continue
                guild = self.bot.get_guild(config.GUILD_ID)
                channel = guild.get_channel(row["proof_channel_id"]) if guild else None
                if not channel:
                    continue  # Недоступность — не доказательство отсутствия сообщения.
                found = None
                after = (stamp - timedelta(seconds=5)).replace(tzinfo=timezone.utc)
                async for message in channel.history(limit=None, after=after, oldest_first=True):
                    if message.author.id == self.bot.user.id and message.embeds and message.embeds[0].footer.text == proof_marker(row):
                        found = message
                        break
                if found and found.attachments:
                    await ads.complete_upload(row["id"], row["upload_token"], found.id, found.attachments[0].id)
                elif found:
                    await found.delete()
                    await ads.release_upload(row["id"], row["upload_token"])
                elif found is None:
                    await ads.release_upload(row["id"], row["upload_token"])
            except Exception:
                logger.exception("Не удалось восстановить отправку рекламы #%s", row["id"])
        cards = await db.fetchall(
            "SELECT id FROM ad_attempts WHERE proof_message_id IS NOT NULL AND proof_deleted_at IS NULL "
            "AND (card_status IS NULL OR card_status<>status) LIMIT 100"
        )
        for row in cards:
            try:
                await self._sync_card(row["id"])
            except Exception:
                logger.exception("Не удалось синхронизировать карточку рекламы #%s", row["id"])
        reports = await db.fetchall(
            "SELECT * FROM ad_attempts WHERE status IN ('approved','rejected') "
            "AND (report_synced_status IS NULL OR report_synced_status<>status) LIMIT 100"
        )
        for row in reports:
            try:
                await self._sync_shift_report(row)
                await db.execute("UPDATE ad_attempts SET report_synced_status=? WHERE id=? AND status=?",
                                 (row["status"], row["id"], row["status"]))
            except disnake.NotFound:
                await db.execute("UPDATE ad_attempts SET report_synced_status=? WHERE id=?", (row["status"], row["id"]))
            except Exception:
                logger.exception("Не удалось обновить рекламу в отчёте смены #%s", row["shift_id"])
        for row in await ads.cleanup_candidates():
            try:
                message = await self._proof_message(row)
                await message.delete()
            except disnake.NotFound:
                pass  # Уже удалено; статистика сохраняется.
            except Exception:
                logger.exception("Не удалось удалить старое фото рекламы #%s", row["id"])
                continue
            await ads.mark_proof_deleted(row["id"])

    @maintenance.before_loop
    async def before_maintenance(self):
        await self.bot.wait_until_ready()

    @maintenance.error
    async def maintenance_error(self, error):
        logger.error("Фоновая задача рекламы остановилась", exc_info=(type(error), error, error.__traceback__))


def setup(bot):
    bot.add_cog(Advertising(bot))
