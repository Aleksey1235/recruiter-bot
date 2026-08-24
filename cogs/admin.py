import logging
import os
import tempfile

import disnake
from disnake.ext import commands, tasks

import config
from database.db import db
from services.health_service import run_health_checks
from utils.checks import is_admin
from utils.time_utils import local_now, utc_now, format_utc_db

logger = logging.getLogger(__name__)


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.auto_backup.start()

    def cog_unload(self):
        self.auto_backup.cancel()

    async def _make_backup(self):
        fd, path = tempfile.mkstemp(prefix="recruiter_bot_", suffix=".db")
        os.close(fd)
        try:
            await db.backup_to(path)
            return path
        except Exception:
            if os.path.exists(path):
                os.remove(path)
            raise

    @tasks.loop(hours=24)
    async def auto_backup(self):
        channel = self.bot.get_channel(config.LOGS_CHANNEL_ID)
        if not channel:
            logger.error("Канал логов для автобэкапа не найден")
            return
        path = None
        try:
            path = await self._make_backup()
            await channel.send(content="📦 Автоматический бэкап базы данных", file=disnake.File(path))
        except Exception:
            logger.exception("Ошибка автоматического бэкапа")
        finally:
            if path and os.path.exists(path):
                os.remove(path)

    @auto_backup.before_loop
    async def before_backup(self):
        await self.bot.wait_until_ready()

    @commands.slash_command(name="админ", description="Административные команды")
    @is_admin()
    async def admin(self, inter):
        pass

    @admin.sub_command(name="логи", description="Показать последние логи")
    async def logs(self, inter, количество: int = 20):
        await inter.response.defer(ephemeral=True)
        limit = max(1, min(int(количество), 25))
        rows = await db.fetchall("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,))
        embed = disnake.Embed(title="📝 ЛОГИ", color=disnake.Color.dark_gray())
        if not rows:
            embed.description = "Логов нет."
        for row in rows:
            who = f"<@{row['user_id']}>" if row["user_id"] else "Система"
            details = (row["details"] or "")[:140]
            embed.add_field(
                name=f"{format_utc_db(row['created_at'])} | {who}",
                value=f"**{row['action']}**\n{details or '—'}",
                inline=False,
            )
        await inter.edit_original_response(embed=embed)

    @admin.sub_command(name="бэкап", description="Скачать консистентный бэкап базы")
    async def backup(self, inter):
        await inter.response.defer(ephemeral=True)
        path = None
        try:
            path = await self._make_backup()
            await inter.edit_original_response(content="📦 Бэкап готов:", file=disnake.File(path))
        finally:
            if path and os.path.exists(path):
                os.remove(path)

    @admin.sub_command(name="время", description="Показать время и часовой пояс, которые использует бот")
    async def time_info(self, inter):
        now = local_now()
        utc = utc_now()
        embed = disnake.Embed(title="🕐 ВРЕМЯ БОТА", color=disnake.Color.blue())
        embed.add_field(name="TIMEZONE", value=f"`{config.TIMEZONE}`", inline=False)
        embed.add_field(name="Время бота", value=now.strftime("%d.%m.%Y %H:%M:%S"), inline=True)
        embed.add_field(name="UTC", value=utc.strftime("%d.%m.%Y %H:%M:%S"), inline=True)
        embed.add_field(name="База", value=f"`{config.DATABASE_PATH}`", inline=False)
        await inter.response.send_message(embed=embed, ephemeral=True)

    @admin.sub_command(name="уведомления", description="Показать последние попытки отправки уведомлений")
    async def notifications(self, inter, количество: int = 10):
        await inter.response.defer(ephemeral=True)
        limit = max(1, min(int(количество), 20))
        rows = await db.fetchall(
            "SELECT * FROM notifications ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        embed = disnake.Embed(title="🔔 ДИАГНОСТИКА УВЕДОМЛЕНИЙ", color=disnake.Color.blue())
        if not rows:
            embed.description = "Записей уведомлений пока нет. Напоминания создаются только для рекрутеров, которые записались на смену."
        for row in rows:
            error = (row["last_error"] or "—")[:180]
            embed.add_field(
                name=f"#{row['id']} | {row['type']} | {row['status']}",
                value=(
                    f"Пользователь: <@{row['user_id']}>\n"
                    f"Объект: {row['object_type']} #{row['object_id']}\n"
                    f"Обновлено: {format_utc_db(row['updated_at'])}\n"
                    f"Попыток: {row['attempts']} | Ошибка: {error}"
                ),
                inline=False,
            )
        await inter.edit_original_response(embed=embed)

    @admin.sub_command(name="здоровье", description="Проверить состояние основных компонентов")
    async def health(self, inter):
        await inter.response.defer(ephemeral=True)
        checks = await run_health_checks(self.bot, inter.guild)
        ok_all = all(check.ok for check in checks)
        embed = disnake.Embed(
            title="🩺 ЗДОРОВЬЕ БОТА",
            color=disnake.Color.green() if ok_all else disnake.Color.red(),
        )
        for check in checks:
            value = "✅ OK" if check.ok else "❌ Ошибка"
            if check.details:
                value += f"\n{check.details[:900]}"
            embed.add_field(name=check.name, value=value, inline=True)
        embed.set_footer(
            text=f"TIMEZONE={config.TIMEZONE} | Время бота: {local_now().strftime('%d.%m.%Y %H:%M:%S')}"
        )
        await inter.edit_original_response(embed=embed)



def setup(bot):
    bot.add_cog(Admin(bot))
