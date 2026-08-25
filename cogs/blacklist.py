import disnake
from disnake.ext import commands

from services import blacklist_service
from services.errors import UserFacingError
from utils.checks import is_admin
from utils.time_utils import format_utc_db


def _identity(row) -> str:
    return f"<@{row['discord_id']}> • Discord ID: `{row['discord_id']}`\nТег: **{row['discord_tag']}**"


def _entry_embed(row, title: str | None = None) -> disnake.Embed:
    active = row["status"] == "active"
    embed = disnake.Embed(
        title=title or ("🚫 ЧЁРНЫЙ СПИСОК" if active else "♻️ ИСТОРИЯ ЧС"),
        color=disnake.Color.red() if active else disnake.Color.dark_grey(),
    )
    embed.add_field(name="👤 Discord", value=_identity(row), inline=False)
    embed.add_field(name="🆔 Статик", value=row["static_id"] or "—", inline=True)
    embed.add_field(name="📛 Имя / фамилия", value=row["full_name"] or "—", inline=True)
    embed.add_field(name="📌 Причина", value=row["reason"], inline=False)
    if row["evidence"]:
        embed.add_field(name="🔗 Доказательство", value=row["evidence"][:1000], inline=False)
    if row["notes"]:
        embed.add_field(name="📝 Заметка", value=row["notes"][:1000], inline=False)
    embed.add_field(name="👮 Добавил", value=f"<@{row['created_by']}> • ID: `{row['created_by']}`", inline=True)
    embed.add_field(name="📅 Добавлен", value=format_utc_db(row["created_at"]), inline=True)
    embed.add_field(name="Статус", value="🔴 Активный ЧС" if active else "⚪ Снят с ЧС", inline=True)
    if not active:
        embed.add_field(name="👑 Снял", value=f"<@{row['removed_by']}> • ID: `{row['removed_by']}`", inline=True)
        embed.add_field(name="📅 Снят", value=format_utc_db(row["removed_at"]), inline=True)
        embed.add_field(name="Причина снятия", value=row["remove_reason"] or "—", inline=False)
    embed.set_footer(text=f"Запись ЧС #{row['id']}")
    return embed


def _list_embed(rows, title: str, history: bool = False) -> disnake.Embed:
    embed = disnake.Embed(title=title, color=disnake.Color.dark_red() if not history else disnake.Color.dark_grey())
    if not rows:
        embed.description = "Записей нет."
        return embed
    lines = []
    for row in rows:
        status = "🔴" if row["status"] == "active" else "⚪"
        lines.append(
            f"{status} `#{row['id']}` • **{row['discord_tag']}** • Discord ID: `{row['discord_id']}`\n"
            f"   Статик: **{row['static_id'] or '—'}** • {(row['reason'] or '—')[:120]}"
        )
    embed.description = "\n".join(lines)[:3900]
    return embed


class Blacklist(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(name="чс", description="Чёрный список Recruiter")
    @is_admin()
    async def blacklist(self, inter):
        pass

    @blacklist.sub_command(name="добавить", description="Добавить пользователя в ЧС")
    @is_admin()
    async def add(
        self,
        inter,
        пользователь: disnake.Member,
        причина: str,
        статик: str = "",
        имя_фамилия: str = "",
        доказательство: str = "",
        заметка: str = "",
    ):
        if пользователь.bot:
            return await inter.response.send_message("❌ Нельзя добавить бота в ЧС.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        try:
            row = await blacklist_service.add_entry(
                пользователь.id,
                str(пользователь),
                статик,
                имя_фамилия or пользователь.display_name,
                причина,
                доказательство,
                заметка,
                inter.author.id,
            )
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")
        await inter.edit_original_response(embed=_entry_embed(row, "🚫 ДОБАВЛЕН В ЧС"))

    @blacklist.sub_command(name="добавить_id", description="Добавить в ЧС по Discord ID, если пользователя нет на сервере")
    @is_admin()
    async def add_by_id(
        self,
        inter,
        discord_id: str,
        тег: str,
        причина: str,
        статик: str = "",
        имя_фамилия: str = "",
        доказательство: str = "",
        заметка: str = "",
    ):
        await inter.response.defer(ephemeral=True)
        try:
            parsed_id = int(discord_id.strip())
        except ValueError:
            return await inter.edit_original_response(content="❌ Discord ID должен состоять только из цифр.")
        try:
            row = await blacklist_service.add_entry(
                parsed_id, тег, статик, имя_фамилия, причина, доказательство, заметка, inter.author.id
            )
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")
        await inter.edit_original_response(embed=_entry_embed(row, "🚫 ДОБАВЛЕН В ЧС"))

    @blacklist.sub_command(name="дополнить", description="Добавить доказательство или заметку к записи ЧС")
    @is_admin()
    async def update_details(self, inter, запись: int, доказательство: str = "", заметка: str = ""):
        await inter.response.defer(ephemeral=True)
        try:
            row = await blacklist_service.update_entry_details(запись, inter.author.id, доказательство, заметка)
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")
        await inter.edit_original_response(embed=_entry_embed(row, "📝 ЗАПИСЬ ЧС ОБНОВЛЕНА"))

    @blacklist.sub_command(name="найти", description="Найти запись ЧС по ID, статику, тегу или имени")
    @is_admin()
    async def search(self, inter, запрос: str):
        await inter.response.defer(ephemeral=True)
        try:
            rows = await blacklist_service.search_entries(запрос, include_removed=True, limit=20)
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")
        if len(rows) == 1:
            return await inter.edit_original_response(embed=_entry_embed(rows[0]))
        await inter.edit_original_response(embed=_list_embed(rows, "🔎 ПОИСК ПО ЧС", history=True))

    @blacklist.sub_command(name="список", description="Показать активный ЧС")
    @is_admin()
    async def active_list(self, inter):
        await inter.response.defer(ephemeral=True)
        rows = await blacklist_service.list_active(25)
        await inter.edit_original_response(embed=_list_embed(rows, "🚫 АКТИВНЫЙ ЧЁРНЫЙ СПИСОК"))

    @blacklist.sub_command(name="история", description="Показать историю ЧС")
    @is_admin()
    async def history(self, inter):
        await inter.response.defer(ephemeral=True)
        rows = await blacklist_service.list_history(25)
        await inter.edit_original_response(embed=_list_embed(rows, "📜 ИСТОРИЯ ЧЁРНОГО СПИСКА", history=True))

    @blacklist.sub_command(name="снять", description="Снять человека с ЧС — только Admin")
    @is_admin()
    async def remove(self, inter, запись: int, причина: str):
        await inter.response.defer(ephemeral=True)
        try:
            row = await blacklist_service.remove_entry(запись, inter.author.id, причина)
        except UserFacingError as exc:
            return await inter.edit_original_response(content=f"❌ {exc}")
        await inter.edit_original_response(embed=_entry_embed(row, "✅ СНЯТ С ЧС"))


def setup(bot):
    bot.add_cog(Blacklist(bot))
