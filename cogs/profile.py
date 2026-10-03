import disnake
from disnake.ext import commands

from database.db import db
from services.finance_service import get_balance
from services import database_service
from services.errors import UserFacingError
from utils.checks import is_recruiter, is_senior, is_senior_or_admin, is_recruiter_or_higher
from utils.formatting import money


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
        if пользователь is not None and not is_recruiter_or_higher(target):
            return await inter.edit_original_response(content="❌ Выбранный пользователь не является рекрутером или членом старшего состава.")
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
        from cogs.advertising import add_summary_field
        await add_summary_field(embed, target.id, "всё время")

        # Заметки — внутренний инструмент старшего состава, обычным рекрутерам их не показываем.
        if user["notes"] and is_senior_or_admin(inter.author):
            embed.add_field(name="📝 Заметки", value=user["notes"][-1000:], inline=False)
        await inter.edit_original_response(embed=embed)

    @recruiter.sub_command(name="заметка", description="Добавить служебную заметку о рекрутере")
    @is_senior()
    async def note(self, inter, пользователь: disnake.Member, текст: str):
        await inter.response.defer(ephemeral=True)
        if not is_recruiter_or_higher(пользователь):
            return await inter.edit_original_response(content="❌ Выбранный пользователь не является рекрутером или членом старшего состава.")
        try:
            await database_service.add_user_note(
                пользователь.id, пользователь.name, текст, inter.author.id, inter.author.name
            )
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")
        await inter.edit_original_response(content=f"✅ Заметка добавлена для {пользователь.mention}.")


def setup(bot):
    bot.add_cog(Profile(bot))
