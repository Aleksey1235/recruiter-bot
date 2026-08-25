import logging

import disnake
from disnake.ext import commands

import config
from database.db import db, log, notify
from services.errors import UserFacingError
from services import blacklist_service, invite_service
from utils.checks import is_recruiter, is_senior, is_senior_or_admin, is_recruiter_or_higher
from utils.formatting import money, normalize_amount
from utils.time_utils import local_now, format_utc_db
from utils.discord_helpers import send_control_warning

logger = logging.getLogger(__name__)


def build_invite_view(invite_id: int, *, accept_disabled: bool = False):
    view = disnake.ui.View(timeout=None)
    view.add_item(
        disnake.ui.Button(
            label="🚫 Принятие заблокировано ЧС" if accept_disabled else "✅ Принять",
            style=disnake.ButtonStyle.secondary if accept_disabled else disnake.ButtonStyle.green,
            custom_id=f"invite:accept:{invite_id}",
            disabled=accept_disabled,
        )
    )
    view.add_item(disnake.ui.Button(label="❌ Отклонить", style=disnake.ButtonStyle.red, custom_id=f"invite:reject:{invite_id}"))
    return view


async def sync_invite_review_message(guild, invite_id: int, approved: bool, reviewer_mention: str, amount: float = 0, reason: str | None = None):
    """Обновляет публичную карточку инвайта после обработки из панели/команды."""
    if guild is None:
        return False
    channel = guild.get_channel(config.REPORTS_CHANNEL_ID)
    if not channel:
        return False

    message = None
    invite = await db.fetchone("SELECT message_id FROM invites WHERE id=?", (invite_id,))
    if invite and invite["message_id"]:
        try:
            message = await channel.fetch_message(invite["message_id"])
        except Exception:
            logger.warning(
                "Не удалось получить message_id=%s для инвайта #%s", invite["message_id"], invite_id
            )

    if message is None:
        try:
            async for candidate in channel.history(limit=200):
                if not candidate.embeds or not getattr(candidate, "components", None):
                    continue
                source = candidate.embeds[0]
                footer = source.footer.text if source.footer else ""
                if footer == f"Инвайт #{invite_id}":
                    message = candidate
                    await invite_service.set_invite_message_id(invite_id, candidate.id)
                    break
        except Exception:
            logger.exception("Не удалось найти старую карточку инвайта #%s", invite_id)

    if message is None:
        return False
    try:
        embed = disnake.Embed(
            title="✅ ИНВАЙТ ПРИНЯТ" if approved else "❌ ИНВАЙТ ОТКЛОНЁН",
            color=disnake.Color.green() if approved else disnake.Color.red(),
        )
        embed.add_field(name="Инвайт", value=f"#{invite_id}", inline=True)
        embed.add_field(name="Проверил", value=reviewer_mention, inline=True)
        if approved and amount > 0:
            embed.add_field(name="💰 Начислено", value=money(amount), inline=True)
        if reason:
            embed.add_field(name="Причина", value=reason, inline=False)
        embed.set_footer(text=f"Инвайт #{invite_id}")
        await message.edit(embed=embed, view=None)
        return True
    except Exception:
        logger.exception("Не удалось синхронизировать карточку инвайта #%s", invite_id)
        return False


def _yes_no(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"да", "yes", "y", "1", "+"}:
        return "yes"
    if normalized in {"нет", "no", "n", "0", "-"}:
        return "no"
    raise UserFacingError(f"Значение {value!r} не распознано. Используйте «да» или «нет».")


class InviteModal(disnake.ui.Modal):
    def __init__(self, user, static_id: str, full_name: str):
        self.user = user
        self.static_id = static_id.strip()
        self.full_name = full_name.strip()
        super().__init__(
            title="📋 Отчёт о приглашённом",
            custom_id=f"invite_create:{user.id}",
            components=[
                disnake.ui.TextInput(label="Заполнил тикет?", placeholder="да / нет", custom_id="ticket", required=True),
                disnake.ui.TextInput(label="Сменил фамилию?", placeholder="да / нет", custom_id="last_name", required=True),
                disnake.ui.TextInput(label="Вступил в организацию?", placeholder="да / нет", custom_id="org", required=True),
                disnake.ui.TextInput(label="Вступил во фракцию?", placeholder="да / нет", custom_id="fraction", required=True),
                disnake.ui.TextInput(label="Прослушал информацию?", placeholder="да / нет", custom_id="info", required=True),
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction):
        if not is_recruiter_or_higher(inter.author):
            return await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        if self.user.id == inter.author.id:
            return await inter.edit_original_response(content="❌ Нельзя создать инвайт на самого себя.")
        if getattr(self.user, "bot", False):
            return await inter.edit_original_response(content="❌ Нельзя создать инвайт на бота.")
        try:
            checklist = {
                "ticket": _yes_no(inter.text_values["ticket"]),
                "last_name": _yes_no(inter.text_values["last_name"]),
                "organization": _yes_no(inter.text_values["org"]),
                "fraction": _yes_no(inter.text_values["fraction"]),
                "info": _yes_no(inter.text_values["info"]),
            }
            invite_id = await invite_service.create_invite(
                self.user.id,
                inter.author.id,
                inter.author.name,
                self.static_id,
                self.full_name,
                checklist,
            )
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")

        posted = False
        channel = inter.guild.get_channel(config.REPORTS_CHANNEL_ID)
        if channel:
            try:
                embed = disnake.Embed(title="👤 НОВЫЙ ИНВАЙТ", color=disnake.Color.blue())
                embed.add_field(name="Приглашённый", value=f"{self.user.mention}\nDiscord ID: `{self.user.id}`", inline=True)
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
        await inter.edit_original_response(content=text)


class ApproveInviteModal(disnake.ui.Modal):
    def __init__(self, invite_id: int):
        self.invite_id = invite_id
        super().__init__(
            title="✅ Подтверждение инвайта",
            custom_id=f"invite_approve:{invite_id}",
            components=[
                disnake.ui.TextInput(
                    label="Сумма начисления",
                    placeholder="0 = без начисления",
                    custom_id="amount",
                    required=True,
                    max_length=12,
                )
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction):
        if not is_senior_or_admin(inter.author):
            return await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        try:
            amount = normalize_amount(inter.text_values["amount"])
            invite, _ = await invite_service.approve_invite(self.invite_id, inter.author.id, amount)
        except (ValueError, UserFacingError) as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")

        dm = disnake.Embed(title="✅ ИНВАЙТ ОДОБРЕН", color=disnake.Color.green())
        dm.add_field(name="Статик", value=invite["static_id"], inline=True)
        dm.add_field(name="Имя", value=invite["full_name"] or "—", inline=True)
        if amount > 0:
            dm.add_field(name="💰 Начислено", value=money(amount), inline=True)
        dm_sent = await notify(
            inter.bot, invite["invited_by"], "INVITE_APPROVED", "invite", self.invite_id, embed=dm
        )
        synced = await sync_invite_review_message(
            inter.guild, self.invite_id, True, inter.author.mention, amount
        )
        warnings = []
        if not synced:
            warnings.append("публичная карточка не обновилась")
        if not dm_sent:
            warnings.append("ЛС рекрутеру не доставлено")
        text = f"✅ Инвайт **#{self.invite_id}** принят."
        if warnings:
            text += "\n⚠️ " + "; ".join(warnings) + ". Данные в БД сохранены."
        await inter.edit_original_response(content=text)


class RejectInviteModal(disnake.ui.Modal):
    def __init__(self, invite_id: int):
        self.invite_id = invite_id
        super().__init__(
            title="❌ Отклонение инвайта",
            custom_id=f"invite_reject:{invite_id}",
            components=[
                disnake.ui.TextInput(
                    label="Причина",
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
        await inter.response.defer(ephemeral=True)
        reason = inter.text_values["reason"]
        try:
            invite = await invite_service.reject_invite(self.invite_id, inter.author.id, reason)
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")

        dm = disnake.Embed(title="❌ ИНВАЙТ ОТКЛОНЁН", color=disnake.Color.red())
        dm.add_field(name="Статик", value=invite["static_id"], inline=True)
        dm.add_field(name="Причина", value=reason, inline=False)
        dm_sent = await notify(
            inter.bot, invite["invited_by"], "INVITE_REJECTED", "invite", self.invite_id, embed=dm
        )
        synced = await sync_invite_review_message(
            inter.guild, self.invite_id, False, inter.author.mention, reason=reason
        )
        warnings = []
        if not synced:
            warnings.append("публичная карточка не обновилась")
        if not dm_sent:
            warnings.append("ЛС рекрутеру не доставлено")
        text = f"✅ Инвайт **#{self.invite_id}** отклонён."
        if warnings:
            text += "\n⚠️ " + "; ".join(warnings) + ". Данные в БД сохранены."
        await inter.edit_original_response(content=text)


class Invites(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_button_click(self, inter: disnake.MessageInteraction):
        custom_id = getattr(inter.component, "custom_id", "") or ""
        async def legacy_invite_id():
            if not inter.message.embeds:
                return None
            embed = inter.message.embeds[0]
            if embed.footer and embed.footer.text and "#" in embed.footer.text:
                try:
                    return int(embed.footer.text.split("#")[-1])
                except ValueError:
                    pass
            static_id = None
            for field in embed.fields:
                if field.name.lower() == "статик":
                    static_id = str(field.value).strip()
                    break
            if not static_id:
                return None
            row = await db.fetchone(
                "SELECT id FROM invites WHERE static_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
                (static_id,),
            )
            return row["id"] if row else None

        if custom_id.startswith("invite:accept:") or custom_id == "invite_accept":
            if not is_senior_or_admin(inter.author):
                return await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            try:
                invite_id = int(custom_id.rsplit(":", 1)[1]) if custom_id != "invite_accept" else await legacy_invite_id()
            except ValueError:
                invite_id = None
            if not invite_id:
                return await inter.response.send_message("❌ Не удалось определить ID инвайта.", ephemeral=True)
            return await inter.response.send_modal(ApproveInviteModal(invite_id))

        if custom_id.startswith("invite:reject:") or custom_id == "invite_reject":
            if not is_senior_or_admin(inter.author):
                return await inter.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            try:
                invite_id = int(custom_id.rsplit(":", 1)[1]) if custom_id != "invite_reject" else await legacy_invite_id()
            except ValueError:
                invite_id = None
            if not invite_id:
                return await inter.response.send_message("❌ Не удалось определить ID инвайта.", ephemeral=True)
            return await inter.response.send_modal(RejectInviteModal(invite_id))

    @commands.slash_command(name="инвайт", description="Учёт приглашённых")
    async def invite(self, inter):
        pass

    @invite.sub_command(name="отчёт", description="Создать отчёт о приглашённом")
    @is_recruiter()
    async def report(self, inter, пользователь: disnake.Member, статик: str, имя_фамилия: str):
        if пользователь.id == inter.author.id:
            return await inter.response.send_message("❌ Нельзя создать инвайт на самого себя.", ephemeral=True)
        if пользователь.bot:
            return await inter.response.send_message("❌ Нельзя создать инвайт на бота.", ephemeral=True)
        await inter.response.send_modal(InviteModal(пользователь, статик, имя_фамилия))

    @invite.sub_command(name="мои", description="Мои инвайты")
    @is_recruiter()
    async def my(self, inter):
        await inter.response.defer(ephemeral=True)
        rows = await db.fetchall(
            "SELECT * FROM invites WHERE invited_by=? ORDER BY created_at DESC LIMIT 20",
            (inter.author.id,),
        )
        embed = disnake.Embed(title="👥 МОИ ИНВАЙТЫ", color=disnake.Color.blue())
        if not rows:
            embed.description = "У вас нет инвайтов."
        for inv in rows:
            status = {"pending": "🟡", "accepted": "✅", "rejected": "❌"}.get(inv["status"], "❓")
            embed.add_field(
                name=f"{status} {inv['static_id']}",
                value=f"Имя: {inv['full_name'] or '—'}\nДата: {format_utc_db(inv['created_at'])}",
                inline=True,
            )
        await inter.edit_original_response(embed=embed)

    @invite.sub_command(name="проверить", description="Инвайты на проверке")
    @is_senior()
    async def check(self, inter):
        await inter.response.defer(ephemeral=True)
        rows = await db.fetchall("SELECT * FROM invites WHERE status='pending' ORDER BY created_at DESC LIMIT 20")
        embed = disnake.Embed(title="📋 НА ПРОВЕРКЕ", color=disnake.Color.yellow())
        if not rows:
            embed.description = "Нет отчётов на проверке."
        for inv in rows:
            target = f"<@{inv['user_id']}> • ID: `{inv['user_id']}`" if inv["user_id"] else "Discord не привязан (legacy-запись)"
            blocked = await blacklist_service.get_active_match(inv["user_id"], inv["static_id"])
            marker = f"\n🚫 Сейчас в ЧС: запись #{blocked['id']}" if blocked else ""
            embed.add_field(
                name=f"ID: {inv['id']} | {inv['static_id']}",
                value=(f"Имя: {inv['full_name'] or '—'}\nDiscord: {target}\n"
                       f"Рекрутер: <@{inv['invited_by']}> • ID: `{inv['invited_by']}`{marker}"),
                inline=False,
            )
        await inter.edit_original_response(embed=embed)

    @invite.sub_command(name="принять", description="Принять инвайт по ID (резервный способ)")
    @is_senior()
    async def approve_by_id(self, inter, инвайт: int, сумма: float = 0):
        await inter.response.defer(ephemeral=True)
        try:
            invite, _ = await invite_service.approve_invite(инвайт, inter.author.id, сумма)
        except (UserFacingError, ValueError) as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")

        dm = disnake.Embed(title="✅ ИНВАЙТ ОДОБРЕН", color=disnake.Color.green())
        dm.add_field(name="Статик", value=invite["static_id"], inline=True)
        dm.add_field(name="Имя", value=invite["full_name"] or "—", inline=True)
        if сумма > 0:
            dm.add_field(name="💰 Начислено", value=money(сумма), inline=True)
        dm_sent = await notify(self.bot, invite["invited_by"], "INVITE_APPROVED", "invite", инвайт, embed=dm)
        synced = await sync_invite_review_message(inter.guild, инвайт, True, inter.author.mention, сумма)
        warnings = []
        if not synced:
            warnings.append("публичная карточка не обновилась")
        if not dm_sent:
            warnings.append("ЛС рекрутеру не доставлено")
        text = f"✅ Инвайт **#{инвайт}** принят."
        if warnings:
            text += "\n⚠️ " + "; ".join(warnings) + ". Данные в БД сохранены."
        await inter.edit_original_response(content=text)

    @invite.sub_command(name="отклонить", description="Отклонить инвайт по ID (резервный способ)")
    @is_senior()
    async def reject_by_id(self, inter, инвайт: int, причина: str):
        await inter.response.defer(ephemeral=True)
        try:
            invite = await invite_service.reject_invite(инвайт, inter.author.id, причина)
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")

        dm = disnake.Embed(title="❌ ИНВАЙТ ОТКЛОНЁН", color=disnake.Color.red())
        dm.add_field(name="Статик", value=invite["static_id"], inline=True)
        dm.add_field(name="Причина", value=причина, inline=False)
        dm_sent = await notify(self.bot, invite["invited_by"], "INVITE_REJECTED", "invite", инвайт, embed=dm)
        synced = await sync_invite_review_message(inter.guild, инвайт, False, inter.author.mention, reason=причина)
        warnings = []
        if not synced:
            warnings.append("публичная карточка не обновилась")
        if not dm_sent:
            warnings.append("ЛС рекрутеру не доставлено")
        text = f"✅ Инвайт **#{инвайт}** отклонён."
        if warnings:
            text += "\n⚠️ " + "; ".join(warnings) + ". Данные в БД сохранены."
        await inter.edit_original_response(content=text)

    @invite.sub_command(name="база", description="База принятых")
    @is_senior()
    async def base(self, inter, статик: str = None):
        await inter.response.defer(ephemeral=True)
        if статик:
            rows = await db.fetchall(
                "SELECT * FROM invites WHERE status='accepted' AND static_id LIKE ? ORDER BY created_at DESC LIMIT 20",
                (f"%{статик.strip()}%",),
            )
        else:
            rows = await db.fetchall("SELECT * FROM invites WHERE status='accepted' ORDER BY created_at DESC LIMIT 20")
        embed = disnake.Embed(title="📋 БАЗА ПРИНЯТЫХ", color=disnake.Color.blue())
        if not rows:
            embed.description = "Ничего не найдено."
        for inv in rows:
            target = f"<@{inv['user_id']}> • ID: `{inv['user_id']}`" if inv["user_id"] else "Discord не привязан"
            embed.add_field(
                name=f"✅ {inv['static_id']}",
                value=(f"Имя: {inv['full_name'] or '—'}\nDiscord: {target}\n"
                       f"Рекрутер: <@{inv['invited_by']}> • ID: `{inv['invited_by']}`"),
                inline=False,
            )
        await inter.edit_original_response(embed=embed)

    @invite.sub_command(name="инфо", description="Информация по статику")
    @is_senior()
    async def info(self, inter, статик: str):
        await inter.response.defer(ephemeral=True)
        inv = await db.fetchone("SELECT * FROM invites WHERE static_id=?", (статик.strip(),))
        if not inv:
            return await inter.edit_original_response(content="❌ Не найдено.")
        embed = disnake.Embed(title=f"👤 {inv['static_id']}", color=disnake.Color.blue())
        target = f"<@{inv['user_id']}> • ID: `{inv['user_id']}`" if inv["user_id"] else "Discord не привязан (legacy-запись)"
        embed.add_field(name="Discord", value=target, inline=False)
        embed.add_field(name="Имя", value=inv["full_name"] or "—", inline=True)
        embed.add_field(name="Рекрутер", value=f"<@{inv['invited_by']}> • ID: `{inv['invited_by']}`", inline=True)
        checklist = (
            f"🎫 Тикет: {'✅' if inv['ticket']=='yes' else '❌'}\n"
            f"📝 Фамилия: {'✅' if inv['last_name_changed']=='yes' else '❌'}\n"
            f"🏢 Организация: {'✅' if inv['organization']=='yes' else '❌'}\n"
            f"⚔️ Фракция: {'✅' if inv['fraction']=='yes' else '❌'}\n"
            f"📢 Инфо: {'✅' if inv['info']=='yes' else '❌'}"
        )
        embed.add_field(name="📋 Чек-лист", value=checklist, inline=False)
        embed.add_field(name="Статус", value={"pending": "🟡 Ожидает", "accepted": "✅ Принят", "rejected": "❌ Отклонён"}.get(inv["status"], inv["status"]), inline=True)
        if inv["reject_reason"]:
            embed.add_field(name="Причина отказа", value=inv["reject_reason"], inline=False)
        if inv["notes"]:
            embed.add_field(name="💬 Заметки", value=inv["notes"][-1000:], inline=False)
        blocked = await blacklist_service.get_active_match(inv["user_id"], inv["static_id"])
        if blocked:
            embed.add_field(
                name="🚫 Чёрный список",
                value=(f"Запись **#{blocked['id']}** • {blacklist_service.identity_text(blocked)}\n"
                       f"Причина: {(blocked['reason'] or '—')[:700]}"),
                inline=False,
            )
        await inter.edit_original_response(embed=embed)

    @invite.sub_command(name="заметка", description="Добавить заметку")
    @is_senior()
    async def note(self, inter, статик: str, заметка: str):
        await inter.response.defer(ephemeral=True)
        inv = await db.fetchone("SELECT * FROM invites WHERE static_id=?", (статик.strip(),))
        if not inv:
            return await inter.edit_original_response(content="❌ Не найдено.")
        try:
            await invite_service.add_invite_note(inv["id"], заметка, inter.author.id, inter.author.name)
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")
        await inter.edit_original_response(content=f"✅ Заметка добавлена к {inv['static_id']}.")


def setup(bot):
    bot.add_cog(Invites(bot))
