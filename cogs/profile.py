import disnake
from disnake.ext import commands

from database.db import db, ensure_user, log
from services.finance_service import get_balance
from utils.checks import is_recruiter, is_senior, is_senior_or_admin
from utils.formatting import money
from utils.time_utils import local_now


class Profile(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(name="рекрутер", description="Профиль рекрутера")
    async def recruiter(self, inter):
        pass

    @recruiter.sub_command(name="профиль", description="Показать профиль рекрутера")
    @is_recruiter()
    async def profile(self, inter, пользователь: disnake.Member = None):
        await inter.response.defer(ephemeral=True)
        target = пользователь or inter.author
        user = await db.fetchone("SELECT * FROM users WHERE discord_id=?", (target.id,))
        if not user:
            return await inter.edit_original_response(content=f"❌ Профиль {target.display_name} не найден.")

        report_stats = await db.fetchone(
            """
            SELECT COUNT(*) AS shifts, COALESCE(SUM(total_accepted),0) AS accepted
            FROM shift_reports WHERE user_id=? AND status='approved'
            """,
            (target.id,),
        )
        invites = await db.fetchone(
            "SELECT COUNT(*) AS count FROM invites WHERE invited_by=? AND status='accepted'",
            (target.id,),
        )
        accrued, paid, available = await get_balance(target.id)

        embed = disnake.Embed(title=f"👤 ПРОФИЛЬ: {target.display_name}", color=disnake.Color.blue())
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="🆔 Статик", value=user["static_id"] or "—", inline=True)
        embed.add_field(name="📋 Смен", value=str(report_stats["shifts"] or 0), inline=True)
        embed.add_field(name="✅ Принято", value=str(report_stats["accepted"] or 0), inline=True)
        embed.add_field(name="👥 Инвайтов", value=str(invites["count"] or 0), inline=True)
        embed.add_field(name="💰 Начислено", value=money(accrued), inline=True)
        embed.add_field(name="📊 К выплате", value=money(available), inline=True)

        # Заметки — внутренний инструмент старшего состава, обычным рекрутерам их не показываем.
        if user["notes"] and is_senior_or_admin(inter.author):
            embed.add_field(name="📝 Заметки", value=user["notes"][-1000:], inline=False)
        await inter.edit_original_response(embed=embed)

    @recruiter.sub_command(name="заметка", description="Добавить служебную заметку о рекрутере")
    @is_senior()
    async def note(self, inter, пользователь: disnake.Member, текст: str):
        await inter.response.defer(ephemeral=True)
        text = текст.strip()
        if not text:
            return await inter.edit_original_response(content="❌ Заметка не может быть пустой.")
        if len(text) > 1000:
            return await inter.edit_original_response(content="❌ Заметка не может быть длиннее 1000 символов.")
        stamp = local_now().strftime("%d.%m.%Y %H:%M")
        addition = f"\n[{stamp}] {inter.author.name}: {text}"
        async with db.transaction() as tx:
            await ensure_user(пользователь.id, username=пользователь.name, tx=tx)
            await tx.execute(
                "UPDATE users SET notes=COALESCE(notes,'') || ? WHERE discord_id=?",
                (addition, пользователь.id),
            )
            await log(inter.author.id, "NOTE_ADD", "user", пользователь.id, text, tx=tx)
        await inter.edit_original_response(content=f"✅ Заметка добавлена для {пользователь.mention}.")


def setup(bot):
    bot.add_cog(Profile(bot))
