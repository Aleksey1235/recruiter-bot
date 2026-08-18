import disnake
from disnake.ext import commands

from services import statistics_service
from utils.checks import is_recruiter, is_senior
from utils.formatting import money

PERIOD_CHOICES = ["всё время", "сегодня", "неделя", "месяц"]


class Statistics(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(name="статистика", description="Статистика рекрутинга")
    async def stats(self, inter):
        pass

    async def _build(self, user_id: int, member, period: str):
        data = await statistics_service.user_statistics(user_id, period)
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
            name="💰 ФИНАНСЫ",
            value=(
                f"Начислено: {money(data['accrued'])}\n"
                f"Выплачено: {money(data['paid'])}\n"
                f"К выплате: {money(data['available'])}"
            ),
            inline=True,
        )
        embed.add_field(name="⭐ РЕЙТИНГ", value=f"Место: #{data['rank']}" if data["rank"] else "Нет места", inline=True)
        return embed

    @stats.sub_command(name="моя", description="Моя статистика")
    @is_recruiter()
    async def my(self, inter, период: str = commands.Param(choices=PERIOD_CHOICES, default="всё время")):
        await inter.response.defer(ephemeral=True)
        await inter.edit_original_response(embed=await self._build(inter.author.id, inter.author, период))

    @stats.sub_command(name="рекрутера", description="Статистика рекрутера")
    @is_senior()
    async def user_stats(
        self,
        inter,
        пользователь: disnake.Member,
        период: str = commands.Param(choices=PERIOD_CHOICES, default="всё время"),
    ):
        await inter.response.defer(ephemeral=True)
        await inter.edit_original_response(embed=await self._build(пользователь.id, пользователь, период))

    @stats.sub_command(name="топ", description="Рейтинг рекрутеров")
    @is_recruiter()
    async def top(
        self,
        inter,
        период: str = commands.Param(choices=["всё время", "неделя", "месяц"], default="всё время"),
    ):
        await inter.response.defer(ephemeral=True)
        rows = await statistics_service.top_statistics(период)
        embed = disnake.Embed(title="🏆 ТОП РЕКРУТЕРОВ", description=f"Период: {период}", color=disnake.Color.gold())
        medals = ["🥇", "🥈", "🥉", "4.", "5."]

        def add_top(title, key, suffix=""):
            ordered = sorted(rows, key=lambda x: x[key], reverse=True)[:5]
            text = "\n".join(f"{medals[i]} <@{row['user_id']}> — {row[key]:.1f}{suffix}" if isinstance(row[key], float) else f"{medals[i]} <@{row['user_id']}> — {row[key]}{suffix}" for i, row in enumerate(ordered))
            embed.add_field(name=title, value=text or "Нет данных", inline=False)

        add_top("👥 ТОП ПО ПРИНЯТЫМ", "accepted", " чел")
        add_top("👤 ТОП ПО САМОСТОЯТЕЛЬНЫМ", "self_found", " чел")

        efficiency = sorted([row for row in rows if row["shifts"] >= 3], key=lambda x: x["avg_per_shift"], reverse=True)[:5]
        text = "\n".join(f"{medals[i]} <@{row['user_id']}> — {row['avg_per_shift']:.1f}/смену" for i, row in enumerate(efficiency))
        embed.add_field(name="📈 ТОП ПО ЭФФЕКТИВНОСТИ", value=text or "Недостаточно данных (минимум 3 одобренных смены)", inline=False)
        add_top("📋 ТОП ПО СМЕНАМ", "shifts", " смен")
        await inter.edit_original_response(embed=embed)

    @stats.sub_command(name="неделя", description="Моя статистика за неделю по дням")
    @is_recruiter()
    async def week(self, inter):
        await inter.response.defer(ephemeral=True)
        days = await statistics_service.current_week_by_day(inter.author.id)
        names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
        embed = disnake.Embed(title="📊 СТАТИСТИКА ЗА НЕДЕЛЮ", color=disnake.Color.blue())
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
        await inter.edit_original_response(embed=embed)


def setup(bot):
    bot.add_cog(Statistics(bot))
