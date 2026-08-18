import disnake
from disnake.ext import commands

from database.db import db, log, notify
from services import goal_service
from utils.checks import is_recruiter, is_senior

TYPE_LABELS = {"люди": "👥 Люди", "смены": "📋 Смены", "часы": "⏱ Часы"}
PERIOD_LABELS = {"день": "за сегодня", "неделя": "за неделю", "месяц": "за месяц"}


class Goals(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(name="цель", description="Управление целями")
    async def goal(self, inter):
        pass

    @goal.sub_command(name="поставить", description="Поставить цель рекрутеру")
    @is_senior()
    async def set_goal(
        self,
        inter,
        пользователь: disnake.Member,
        тип: str = commands.Param(choices=["люди", "смены", "часы"]),
        значение: int = 0,
        период: str = commands.Param(choices=["день", "неделя", "месяц"], default="неделя"),
    ):
        await inter.response.defer(ephemeral=True)
        if значение <= 0:
            return await inter.edit_original_response(content="❌ Значение цели должно быть больше 0.")

        async with db.transaction() as tx:
            await tx.execute(
                "UPDATE goals SET status='deleted' WHERE user_id=? AND type=? AND status='active'",
                (пользователь.id, тип),
            )
            cursor = await tx.execute(
                """
                INSERT INTO goals (user_id, type, target_value, period, created_by)
                VALUES (?, ?, ?, ?, ?)
                """,
                (пользователь.id, тип, значение, период, inter.author.id),
            )
            goal_id = cursor.lastrowid
            await log(
                inter.author.id,
                "GOAL_SET",
                "goal",
                goal_id,
                f"user={пользователь.id}; {тип}={значение}; period={период}",
                tx=tx,
            )

        dm = disnake.Embed(title="🎯 ВАМ ПОСТАВЛЕНА НОВАЯ ЦЕЛЬ", color=disnake.Color.blue())
        dm.add_field(name="Цель", value=f"{TYPE_LABELS[тип]}: {значение}", inline=True)
        dm.add_field(name="Период", value=период, inline=True)
        dm.add_field(name="Прогресс", value=f"0 / {значение}", inline=True)
        await notify(self.bot, пользователь.id, "GOAL_SET", "goal", goal_id, embed=dm)
        await inter.edit_original_response(content=f"🎯 Цель для {пользователь.mention}: {TYPE_LABELS[тип]} — {значение}.")

    async def _build_goals_embed(self, user_id: int, title: str):
        goals = await db.fetchall(
            "SELECT * FROM goals WHERE user_id=? AND status='active' ORDER BY id",
            (user_id,),
        )
        embed = disnake.Embed(title=title, color=disnake.Color.blue())
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
                name=f"{TYPE_LABELS.get(goal['type'], goal['type'])} ({PERIOD_LABELS.get(goal['period'], goal['period'])})",
                value=f"{bar} **{current} / {goal['target_value']}** ({pct}%)",
                inline=False,
            )
        return embed

    @goal.sub_command(name="мои", description="Мои цели")
    @is_recruiter()
    async def my(self, inter):
        await inter.response.defer(ephemeral=True)
        embed = await self._build_goals_embed(inter.author.id, "🎯 МОИ ЦЕЛИ")
        await inter.edit_original_response(embed=embed)

    @goal.sub_command(name="рекрутера", description="Цели рекрутера")
    @is_senior()
    async def user_goals(self, inter, пользователь: disnake.Member):
        await inter.response.defer(ephemeral=True)
        embed = await self._build_goals_embed(пользователь.id, f"🎯 ЦЕЛИ | {пользователь.display_name}")
        await inter.edit_original_response(embed=embed)

    @goal.sub_command(name="удалить", description="Удалить активные цели рекрутера")
    @is_senior()
    async def delete(self, inter, пользователь: disnake.Member):
        await inter.response.defer(ephemeral=True)
        async with db.transaction() as tx:
            goals = await tx.fetchall(
                "SELECT * FROM goals WHERE user_id=? AND status='active'",
                (пользователь.id,),
            )
            if goals:
                await tx.execute(
                    "UPDATE goals SET status='deleted' WHERE user_id=? AND status='active'",
                    (пользователь.id,),
                )
                await log(inter.author.id, "GOAL_DELETE", "user", пользователь.id, None, tx=tx)

        if not goals:
            return await inter.edit_original_response(content="Нет активных целей.")

        dm = disnake.Embed(title="🗑️ ЦЕЛИ УДАЛЕНЫ", color=disnake.Color.red())
        for goal in goals:
            dm.add_field(name=TYPE_LABELS.get(goal["type"], goal["type"]), value=f"Цель: {goal['target_value']}", inline=False)
        marker = max(goal["id"] for goal in goals)
        await notify(self.bot, пользователь.id, "GOAL_DELETED", "goal_batch", marker, embed=dm)
        await inter.edit_original_response(content=f"✅ Цели {пользователь.mention} удалены.")

    @goal.sub_command(name="прогресс", description="Прогресс целей")
    @is_recruiter()
    async def progress(self, inter):
        await inter.response.defer(ephemeral=True)
        embed = await self._build_goals_embed(inter.author.id, "🎯 ПРОГРЕСС ЦЕЛЕЙ")
        await inter.edit_original_response(embed=embed)


def setup(bot):
    bot.add_cog(Goals(bot))
