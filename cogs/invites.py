import disnake
from disnake.ext import commands

import config
from database.db import db, log, notify
from services.errors import UserFacingError
from services import invite_service
from utils.checks import is_recruiter, is_senior, is_senior_or_admin
from utils.formatting import money
from utils.time_utils import local_now


def build_invite_view(invite_id: int):
    view = disnake.ui.View(timeout=None)
    view.add_item(disnake.ui.Button(label="✅ Принять", style=disnake.ButtonStyle.green, custom_id=f"invite:accept:{invite_id}"))
    view.add_item(disnake.ui.Button(label="❌ Отклонить", style=disnake.ButtonStyle.red, custom_id=f"invite:reject:{invite_id}"))
    return view


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
        await inter.response.defer(ephemeral=True)
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

        await inter.edit_original_response(content=f"✅ Отчёт создан. ID: **#{invite_id}**")
        channel = inter.guild.get_channel(config.REPORTS_CHANNEL_ID)
        if channel:
            embed = disnake.Embed(title="👤 НОВЫЙ ИНВАЙТ", color=disnake.Color.blue())
            embed.add_field(name="Приглашённый", value=self.user.mention, inline=True)
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
        try:
            amount = float(inter.text_values["amount"].replace(",", ".").strip())
            invite, _ = await invite_service.approve_invite(self.invite_id, inter.author.id, amount)
        except ValueError:
            return await inter.response.send_message("❌ Введите корректное число.", ephemeral=True)
        except UserFacingError as exc:
            return await inter.response.send_message(f"❌ {exc}", ephemeral=True)

        embed = disnake.Embed(title="✅ ИНВАЙТ ПРИНЯТ", color=disnake.Color.green())
        embed.add_field(name="Статик", value=invite["static_id"], inline=True)
        embed.add_field(name="Проверил", value=inter.author.mention, inline=True)
        if amount > 0:
            embed.add_field(name="💰 Начислено", value=money(amount), inline=True)
        await inter.response.edit_message(embed=embed, view=None)

        dm = disnake.Embed(title="✅ ИНВАЙТ ОДОБРЕН", color=disnake.Color.green())
        dm.add_field(name="Статик", value=invite["static_id"], inline=True)
        dm.add_field(name="Имя", value=invite["full_name"] or "—", inline=True)
        if amount > 0:
            dm.add_field(name="💰 Начислено", value=money(amount), inline=True)
        dm_sent = await notify(
            inter.bot, invite["invited_by"], "INVITE_APPROVED", "invite", self.invite_id, embed=dm
        )
        if not dm_sent:
            await inter.followup.send(
                "⚠️ Инвайт принят, но ЛС рекрутеру не доставлено. Возможно, у пользователя закрыты личные сообщения.",
                ephemeral=True,
            )


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
        try:
            invite = await invite_service.reject_invite(self.invite_id, inter.author.id, inter.text_values["reason"])
        except UserFacingError as exc:
            return await inter.response.send_message(f"❌ {exc}", ephemeral=True)

        embed = disnake.Embed(title="❌ ИНВАЙТ ОТКЛОНЁН", color=disnake.Color.red())
        embed.add_field(name="Статик", value=invite["static_id"], inline=True)
        embed.add_field(name="Причина", value=inter.text_values["reason"], inline=False)
        await inter.response.edit_message(embed=embed, view=None)

        dm = disnake.Embed(title="❌ ИНВАЙТ ОТКЛОНЁН", color=disnake.Color.red())
        dm.add_field(name="Статик", value=invite["static_id"], inline=True)
        dm.add_field(name="Причина", value=inter.text_values["reason"], inline=False)
        dm_sent = await notify(
            inter.bot, invite["invited_by"], "INVITE_REJECTED", "invite", self.invite_id, embed=dm
        )
        if not dm_sent:
            await inter.followup.send(
                "⚠️ Инвайт отклонён, но ЛС рекрутеру не доставлено. Возможно, у пользователя закрыты личные сообщения.",
                ephemeral=True,
            )


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
                value=f"Имя: {inv['full_name'] or '—'}\nДата: {str(inv['created_at'])[:16]}",
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
            embed.add_field(
                name=f"ID: {inv['id']} | {inv['static_id']}",
                value=f"Имя: {inv['full_name'] or '—'}\nРекрутер: <@{inv['invited_by']}>",
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
        await notify(self.bot, invite["invited_by"], "INVITE_APPROVED", "invite", инвайт, embed=dm)
        await inter.edit_original_response(content=f"✅ Инвайт **#{инвайт}** принят.")

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
        await notify(self.bot, invite["invited_by"], "INVITE_REJECTED", "invite", инвайт, embed=dm)
        await inter.edit_original_response(content=f"✅ Инвайт **#{инвайт}** отклонён.")

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
            embed.add_field(
                name=f"✅ {inv['static_id']}",
                value=f"Имя: {inv['full_name'] or '—'}\nРекрутер: <@{inv['invited_by']}>",
                inline=True,
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
        embed.add_field(name="Статус", value={"pending": "🟡 Ожидает", "accepted": "✅ Принят", "rejected": "❌ Отклонён"}.get(inv["status"], inv["status"]), inline=True)
        if inv["reject_reason"]:
            embed.add_field(name="Причина отказа", value=inv["reject_reason"], inline=False)
        if inv["notes"]:
            embed.add_field(name="💬 Заметки", value=inv["notes"][-1000:], inline=False)
        await inter.edit_original_response(embed=embed)

    @invite.sub_command(name="заметка", description="Добавить заметку")
    @is_senior()
    async def note(self, inter, статик: str, заметка: str):
        await inter.response.defer(ephemeral=True)
        inv = await db.fetchone("SELECT * FROM invites WHERE static_id=?", (статик.strip(),))
        if not inv:
            return await inter.edit_original_response(content="❌ Не найдено.")
        note_text = заметка.strip()
        if not note_text:
            return await inter.edit_original_response(content="❌ Заметка не может быть пустой.")
        if len(note_text) > 1000:
            return await inter.edit_original_response(content="❌ Заметка не может быть длиннее 1000 символов.")
        new = f"\n[{local_now().strftime('%d.%m.%Y %H:%M')}] {inter.author.name}: {note_text}"
        await db.execute("UPDATE invites SET notes=COALESCE(notes,'') || ? WHERE id=?", (new, inv["id"]))
        await log(inter.author.id, "INVITE_NOTE", "invite", inv["id"], note_text)
        await inter.edit_original_response(content=f"✅ Заметка добавлена к {inv['static_id']}.")


def setup(bot):
    bot.add_cog(Invites(bot))
