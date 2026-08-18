import disnake
from disnake.ext import commands

from database.db import db, notify
from services.errors import UserFacingError
from services import finance_service
from utils.checks import is_recruiter, is_senior, is_admin
from utils.formatting import money


class Finance(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(name="финансы", description="Управление финансами")
    async def fin(self, inter):
        pass

    @fin.sub_command(name="мои", description="Мои финансы")
    @is_recruiter()
    async def my(self, inter):
        await inter.response.defer(ephemeral=True)
        accrued, paid, available = await finance_service.get_balance(inter.author.id)
        ops = await db.fetchall(
            "SELECT * FROM finances WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (inter.author.id,),
        )
        if not ops and accrued == 0 and paid == 0:
            return await inter.edit_original_response(content="У вас пока нет финансовых операций.")

        embed = disnake.Embed(title="💰 МОИ ФИНАНСЫ", color=disnake.Color.green())
        embed.add_field(name="💵 Начислено", value=money(accrued), inline=True)
        embed.add_field(name="💸 Выплачено", value=money(paid), inline=True)
        embed.add_field(name="📊 К выплате", value=money(available), inline=True)
        if ops:
            lines = []
            for op in ops[:8]:
                if op["type"] == "salary":
                    reason = (op['reason'] or 'Начисление')[:90]
                    lines.append(f"💰 +{money(op['amount'])} — {reason}")
                else:
                    lines.append(f"💸 -{money(op['amount'])} — Выплата")
            embed.add_field(name="📋 Последние операции", value="\n".join(lines), inline=False)
        await inter.edit_original_response(embed=embed)

    @fin.sub_command(name="общие", description="Общие финансы")
    @is_senior()
    async def general(self, inter):
        await inter.response.defer(ephemeral=True)
        accrued, paid, available = await finance_service.get_general_balance()
        count = await db.fetchone("SELECT COUNT(*) AS count FROM users")
        embed = disnake.Embed(title="💰 ОБЩИЕ ФИНАНСЫ", color=disnake.Color.green())
        embed.add_field(name="💵 Начислено", value=money(accrued), inline=True)
        embed.add_field(name="💸 Выплачено", value=money(paid), inline=True)
        embed.add_field(name="📊 К выплате", value=money(available), inline=True)
        embed.add_field(name="👥 Профилей", value=str(count["count"] or 0), inline=True)
        await inter.edit_original_response(embed=embed)

    @fin.sub_command(name="рекрутера", description="Финансы рекрутера")
    @is_senior()
    async def user_fin(self, inter, пользователь: disnake.Member):
        await inter.response.defer(ephemeral=True)
        accrued, paid, available = await finance_service.get_balance(пользователь.id)
        ops = await db.fetchall(
            "SELECT * FROM finances WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (пользователь.id,),
        )
        embed = disnake.Embed(title=f"💰 ФИНАНСЫ | {пользователь.display_name}", color=disnake.Color.green())
        embed.add_field(name="💵 Начислено", value=money(accrued), inline=True)
        embed.add_field(name="💸 Выплачено", value=money(paid), inline=True)
        embed.add_field(name="📊 К выплате", value=money(available), inline=True)
        if ops:
            for op in ops[:8]:
                sign = "+" if op["type"] == "salary" else "-"
                reason = op["reason"] or ("Начисление" if op["type"] == "salary" else "Выплата")
                embed.add_field(
                    name=f"{str(op['created_at'])[:16]} | {'💰' if sign=='+' else '💸'}",
                    value=f"{sign}{money(op['amount'])} — {reason}",
                    inline=False,
                )
        await inter.edit_original_response(embed=embed)

    @fin.sub_command(name="начислить", description="Вручную начислить деньги рекрутеру")
    @is_admin()
    async def accrue(self, inter, пользователь: disnake.Member, сумма: float, причина: str = ""):
        await inter.response.defer(ephemeral=True)
        try:
            fin_id, balance = await finance_service.accrue(
                пользователь.id, пользователь.name, сумма, причина, inter.author.id
            )
        except (UserFacingError, ValueError) as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")

        dm = disnake.Embed(title="💰 ВАМ НАЧИСЛЕНЫ ДЕНЬГИ", color=disnake.Color.green())
        dm.add_field(name="💵 Сумма", value=money(сумма), inline=True)
        if причина.strip():
            dm.add_field(name="📝 Причина", value=причина.strip(), inline=False)
        dm.add_field(name="📊 К выплате", value=money(balance[2]), inline=True)
        await notify(self.bot, пользователь.id, "FIN_ACCRUE", "finance", fin_id, embed=dm)
        await inter.edit_original_response(content=f"✅ Начислено **{money(сумма)}** для {пользователь.mention}.")

    @fin.sub_command(name="выплатить", description="Отметить выплату")
    @is_admin()
    async def pay(self, inter, пользователь: disnake.Member, сумма: float):
        await inter.response.defer(ephemeral=True)
        try:
            fin_id, balance = await finance_service.pay(
                пользователь.id, пользователь.name, сумма, inter.author.id
            )
        except (UserFacingError, ValueError) as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")

        dm = disnake.Embed(title="💸 ЗАРПЛАТА ВЫПЛАЧЕНА", color=disnake.Color.blue())
        dm.add_field(name="💵 Сумма", value=money(сумма), inline=True)
        dm.add_field(name="📊 Остаток", value=money(balance[2]), inline=True)
        await notify(self.bot, пользователь.id, "FIN_PAY", "finance", fin_id, embed=dm)
        await inter.edit_original_response(content=f"✅ Выплачено **{money(сумма)}** для {пользователь.mention}.")

    @fin.sub_command(name="сверить", description="Проверить журнал финансов и кеш балансов")
    @is_admin()
    async def reconcile(self, inter, исправить: bool = False):
        await inter.response.defer(ephemeral=True)
        mismatches = await finance_service.reconcile_all(fix=исправить)
        if not mismatches:
            return await inter.edit_original_response(content="✅ Финансы согласованы: расхождений не найдено.")
        lines = []
        for user_id, ledger, cached in mismatches[:15]:
            lines.append(
                f"<@{user_id}>: журнал {money(ledger[0])}/{money(ledger[1])}, кеш {money(cached[0])}/{money(cached[1])}"
            )
        suffix = "\n✅ Кеш исправлен по журналу." if исправить else "\nЗапустите с `исправить:true`, чтобы синхронизировать кеш."
        await inter.edit_original_response(content="⚠️ Найдены расхождения:\n" + "\n".join(lines) + suffix)


def setup(bot):
    bot.add_cog(Finance(bot))
