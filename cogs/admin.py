import logging
import os
import tempfile

import disnake
from disnake.ext import commands, tasks

import config
from database.db import db
from services.finance_service import reconcile_all
from utils.checks import is_admin

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
                name=f"{str(row['created_at'])[:16]} | {who}",
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

    @admin.sub_command(name="здоровье", description="Проверить состояние основных компонентов")
    async def health(self, inter):
        await inter.response.defer(ephemeral=True)
        checks = []
        try:
            row = await db.fetchone("SELECT 1 AS ok")
            checks.append(("База данных", bool(row and row["ok"] == 1)))

            quick = await db.fetchone("PRAGMA quick_check")
            checks.append(("SQLite quick_check", bool(quick and quick[0] == "ok")))

            foreign_key_errors = await db.fetchall("PRAGMA foreign_key_check")
            checks.append(("Внешние ключи", len(foreign_key_errors) == 0))

            bad_shifts = await db.fetchone(
                """
                SELECT COUNT(*) AS count FROM shifts
                WHERE slots < 0 OR scheduled_start IS NULL OR scheduled_end IS NULL
                   OR scheduled_end <= scheduled_start
                """
            )
            checks.append(("Данные смен", int(bad_shifts["count"] or 0) == 0))

            bad_members = await db.fetchone(
                """
                SELECT COUNT(*) AS count
                FROM shift_members sm
                LEFT JOIN shifts s ON s.id=sm.shift_id
                WHERE s.id IS NULL
                   OR (sm.status='active' AND sm.actual_start IS NULL)
                   OR (sm.status='completed' AND (sm.actual_end IS NULL OR sm.report_id IS NULL))
                """
            )
            checks.append(("Данные участников", int(bad_members["count"] or 0) == 0))

            bad_reports = await db.fetchone(
                """
                SELECT COUNT(*) AS count
                FROM shift_reports r
                LEFT JOIN shift_members sm ON sm.id=r.member_id
                WHERE sm.id IS NULL OR sm.shift_id<>r.shift_id OR sm.user_id<>r.user_id
                   OR r.total_accepted<0 OR r.came_to_base<0 OR r.found_by_recruiter<0
                   OR r.came_to_base+r.found_by_recruiter>r.total_accepted
                """
            )
            checks.append(("Данные отчётов", int(bad_reports["count"] or 0) == 0))

            bad_finances = await db.fetchone(
                """
                SELECT COUNT(*) AS count
                FROM finances
                WHERE amount <= 0
                   OR type NOT IN ('salary', 'pay')
                   OR status NOT IN ('accrued', 'paid')
                   OR (type='salary' AND status<>'accrued')
                   OR (type='pay' AND status<>'paid')
                """
            )
            checks.append(("Операции финансов", int(bad_finances["count"] or 0) == 0))

            negative_balances = await db.fetchone(
                """
                SELECT COUNT(*) AS count
                FROM (
                    SELECT user_id,
                           COALESCE(SUM(CASE WHEN type='salary' THEN amount ELSE 0 END), 0)
                         - COALESCE(SUM(CASE WHEN type='pay' THEN amount ELSE 0 END), 0) AS balance
                    FROM finances
                    GROUP BY user_id
                ) balances
                WHERE balance < -0.005
                """
            )
            checks.append(("Финансовые остатки", int(negative_balances["count"] or 0) == 0))

            mismatches = await reconcile_all(fix=False)
            checks.append(("Финансовый кеш", len(mismatches) == 0))
        except Exception:
            logger.exception("Ошибка диагностики базы данных")
            checks.append(("База данных", False))

        guild = self.bot.get_guild(config.GUILD_ID)
        checks.append(("Сервер", guild is not None))
        if guild:
            channels = {
                "Канал смен": config.SHIFTS_CHANNEL_ID,
                "Канал отчётов": config.REPORTS_CHANNEL_ID,
                "Канал статистики": config.STATS_CHANNEL_ID,
                "Канал контроля": config.CONTROL_CHANNEL_ID,
                "Канал логов": config.LOGS_CHANNEL_ID,
            }
            for name, channel_id in channels.items():
                checks.append((name, guild.get_channel(channel_id) is not None))

            roles = {
                "Роль рекрутера": config.RECRUITER_ROLE_ID,
                "Роль senior": config.SENIOR_ROLE_ID,
                "Роль admin": config.ADMIN_ROLE_ID,
            }
            for name, role_id in roles.items():
                checks.append((name, guild.get_role(role_id) is not None))

        tasks_cog = self.bot.get_cog("Tasks")
        if tasks_cog:
            for attr, title in (
                ("check_shifts", "Контроль смен"),
                ("check_reports", "Контроль отчётов"),
                ("check_suspicious", "Контроль аномалий"),
                ("weekly_report", "Недельный отчёт"),
            ):
                loop = getattr(tasks_cog, attr, None)
                checks.append((title, bool(loop and loop.is_running())))
        else:
            checks.append(("Tasks Cog", False))

        embed = disnake.Embed(title="🩺 ЗДОРОВЬЕ БОТА", color=disnake.Color.green())
        for name, ok in checks:
            embed.add_field(name=name, value="✅ OK" if ok else "❌ Ошибка", inline=True)
        if not all(ok for _, ok in checks):
            embed.color = disnake.Color.red()
        await inter.edit_original_response(embed=embed)


def setup(bot):
    bot.add_cog(Admin(bot))
