import logging
import sqlite3
from datetime import timedelta

import disnake
from disnake.ext import commands, tasks

import config
from database.db import db, finish_system_marker, notify, reserve_system_marker
from services import shift_service, statistics_service
from utils.formatting import money
from utils.time_utils import local_now, parse_db, to_db, utc_now

logger = logging.getLogger(__name__)


class Tasks(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        for loop in (self.check_shifts, self.check_reports, self.check_suspicious, self.weekly_report):
            loop.add_exception_type(sqlite3.OperationalError)
        self.check_shifts.start()
        self.check_reports.start()
        self.check_suspicious.start()
        self.weekly_report.start()

    def cog_unload(self):
        self.check_shifts.cancel()
        self.check_reports.cancel()
        self.check_suspicious.cancel()
        self.weekly_report.cancel()

    async def _control_once(self, marker_type: str, object_type: str, object_id: int, content: str):
        if not await reserve_system_marker(marker_type, object_type, object_id):
            return False
        channel = self.bot.get_channel(config.CONTROL_CHANNEL_ID)
        if not channel:
            await finish_system_marker(marker_type, object_type, object_id, False, "CONTROL_CHANNEL_ID not found")
            return False
        try:
            await channel.send(content)
        except Exception as exc:
            await finish_system_marker(marker_type, object_type, object_id, False, str(exc))
            logger.exception("Не удалось отправить сообщение контроля")
            return False
        await finish_system_marker(marker_type, object_type, object_id, True)
        return True

    @tasks.loop(minutes=1)
    async def check_shifts(self):
        now = local_now()
        members = await db.fetchall(
            """
            SELECT sm.*, s.scheduled_start, s.scheduled_end
            FROM shift_members sm
            JOIN shifts s ON s.id=sm.shift_id
            WHERE sm.status IN ('booked', 'active')
              AND s.status<>'cancelled'
            """
        )
        for member in members:
            start = parse_db(member["scheduled_start"])
            end = parse_db(member["scheduled_end"])
            if not start or not end:
                logger.error("Некорректное время смены #%s", member["shift_id"])
                continue

            if member["status"] == "booked":
                minutes_until = (start - now).total_seconds() / 60
                if 10 < minutes_until <= 30:
                    embed = disnake.Embed(title="🔔 НАПОМИНАНИЕ О СМЕНЕ", color=disnake.Color.blue())
                    embed.add_field(name="📋 Смена", value=f"#{member['shift_id']}", inline=True)
                    embed.add_field(name="⏰ Время", value=f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}", inline=True)
                    embed.add_field(name="ℹ️", value="До начала осталось не более 30 минут.", inline=False)
                    await notify(self.bot, member["user_id"], "REMINDER_30", "shift", member["shift_id"], embed=embed)
                elif 0 < minutes_until <= 10:
                    embed = disnake.Embed(title="⚠️ СМЕНА СКОРО НАЧНЁТСЯ", color=disnake.Color.orange())
                    embed.add_field(name="📋 Смена", value=f"#{member['shift_id']}", inline=True)
                    embed.add_field(name="⏰ Начало", value=start.strftime("%H:%M"), inline=True)
                    await notify(self.bot, member["user_id"], "REMINDER_10", "shift", member["shift_id"], embed=embed)

                if now >= start + timedelta(minutes=config.LATE_START_WARNING_MINUTES) and now < end:
                    delay = max(0, int((now - start).total_seconds() / 60))
                    embed = disnake.Embed(title="⚠️ ВЫ НЕ НАЧАЛИ СМЕНУ", color=disnake.Color.red())
                    embed.add_field(name="📋 Смена", value=f"#{member['shift_id']}", inline=True)
                    embed.add_field(name="⏱ Прошло", value=f"{delay} мин.", inline=True)
                    await notify(self.bot, member["user_id"], "LATE_START", "shift", member["shift_id"], embed=embed)
                    await self._control_once(
                        "LATE_START_CONTROL",
                        "member",
                        member["id"],
                        f"🟡 <@{member['user_id']}> не начал смену #{member['shift_id']} уже {delay} мин.\n<@&{config.SENIOR_ROLE_ID}>",
                    )

                if now >= end + timedelta(minutes=config.MISS_AFTER_END_MINUTES):
                    missed = await shift_service.mark_missed(member["id"])
                    if missed:
                        embed = disnake.Embed(title="❌ СМЕНА ПРОПУЩЕНА", color=disnake.Color.red())
                        embed.add_field(name="📋 Смена", value=f"#{member['shift_id']}", inline=True)
                        await notify(self.bot, member["user_id"], "SHIFT_MISSED", "shift", member["shift_id"], embed=embed)
                        await self._control_once(
                            "SHIFT_MISSED_CONTROL",
                            "member",
                            member["id"],
                            f"🔴 ПРОПУСК! <@{member['user_id']}> не вышел на смену #{member['shift_id']}.\n<@&{config.SENIOR_ROLE_ID}>",
                        )
                        try:
                            from cogs.shifts import update_shift_message
                            guild = self.bot.get_guild(config.GUILD_ID)
                            if guild:
                                await update_shift_message(guild, member["shift_id"])
                        except Exception:
                            logger.exception("Не удалось обновить сообщение пропущенной смены")

            elif member["status"] == "active" and now >= end + timedelta(minutes=config.REPORT_REMINDER_AFTER_MINUTES):
                embed = disnake.Embed(title="📋 НЕ ЗАБУДЬТЕ ЗАВЕРШИТЬ СМЕНУ", color=disnake.Color.blue())
                embed.add_field(name="📋 Смена", value=f"#{member['shift_id']}", inline=True)
                embed.add_field(name="ℹ️", value="Используйте `/смена завершить` и заполните отчёт.", inline=False)
                await notify(self.bot, member["user_id"], "REPORT_REMINDER", "shift", member["shift_id"], embed=embed)

    @tasks.loop(minutes=5)
    async def check_reports(self):
        threshold = utc_now() - timedelta(minutes=config.REVIEW_REMINDER_AFTER_MINUTES)
        pending = await db.fetchall(
            "SELECT * FROM shift_reports WHERE status='pending' AND created_at<=?",
            (to_db(threshold),),
        )
        for report in pending:
            await self._control_once(
                "REVIEW_REMINDER",
                "shift_report",
                report["id"],
                f"🟡 Отчёт #{report['id']} ожидает проверки.\nРекрутер: <@{report['user_id']}>\n<@&{config.SENIOR_ROLE_ID}>",
            )

    @tasks.loop(minutes=10)
    async def check_suspicious(self):
        members = await db.fetchall(
            """
            SELECT * FROM shift_members
            WHERE status='completed' AND actual_start IS NOT NULL AND actual_end IS NOT NULL
            """
        )
        for member in members:
            start = parse_db(member["actual_start"])
            end = parse_db(member["actual_end"])
            if not start or not end:
                continue
            duration = (end - start).total_seconds() / 60
            if 0 <= duration < config.SUSPICIOUS_SHORT_MINUTES:
                await self._control_once(
                    "SUSPICIOUS_SHORT",
                    "member",
                    member["id"],
                    f"⚠️ Подозрительно короткая смена!\nРекрутер: <@{member['user_id']}>\nСмена: #{member['shift_id']}\nДлительность: {int(duration)} мин.\n<@&{config.SENIOR_ROLE_ID}>",
                )

    @tasks.loop(minutes=1)
    async def weekly_report(self):
        now = local_now()
        this_monday = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        reached_this_sunday = (
            now.weekday() == 6
            and (now.hour, now.minute) >= (config.WEEKLY_REPORT_HOUR, config.WEEKLY_REPORT_MINUTE)
        )
        if reached_this_sunday:
            week_start = this_monday
            week_end = this_monday + timedelta(days=7)
        else:
            # Если бот был выключен в воскресенье, отправляем пропущенный отчёт
            # при следующем запуске в любой день недели.
            week_end = this_monday
            week_start = this_monday - timedelta(days=7)

        marker = int(week_start.strftime("%Y%m%d"))
        if not await reserve_system_marker("WEEKLY_REPORT", "week", marker):
            return

        channel = self.bot.get_channel(config.STATS_CHANNEL_ID)
        if not channel:
            await finish_system_marker("WEEKLY_REPORT", "week", marker, False, "STATS_CHANNEL_ID not found")
            return
        try:
            _, rankings, total, finance = await statistics_service.weekly_summary(week_start, week_end)
            embed = disnake.Embed(
                title="📊 ИТОГИ НЕДЕЛИ",
                description=(
                    f"Период: {week_start.strftime('%d.%m.%Y')} — "
                    f"{(week_end - timedelta(seconds=1)).strftime('%d.%m.%Y')}"
                ),
                color=disnake.Color.gold(),
            )
            embed.add_field(
                name="👥 РЕКРУТИНГ",
                value=(
                    f"Смен: {total['shifts']}\nПринято: {total['accepted']}\n"
                    f"🏠 На особняке: {total['base']}\n👤 Самостоятельно: {total['self']}"
                ),
                inline=False,
            )
            if rankings:
                medals = ["🥇", "🥈", "🥉"]
                text = "\n".join(
                    f"{medals[i]} <@{row['user_id']}> — {row['accepted']}"
                    for i, row in enumerate(rankings[:3])
                )
                embed.add_field(name="🏆 ТОП-3", value=text, inline=False)
            embed.add_field(
                name="💰 ФИНАНСЫ",
                value=(
                    f"Начислено: {money(finance[0])}\n"
                    f"Выплачено: {money(finance[1])}\n"
                    f"К выплате: {money(finance[2])}"
                ),
                inline=False,
            )
            await channel.send(embed=embed)
        except Exception as exc:
            await finish_system_marker("WEEKLY_REPORT", "week", marker, False, str(exc))
            logger.exception("Не удалось сформировать недельный отчёт")
        else:
            await finish_system_marker("WEEKLY_REPORT", "week", marker, True)

    @check_shifts.before_loop
    @check_reports.before_loop
    @check_suspicious.before_loop
    @weekly_report.before_loop
    async def before_tasks(self):
        await self.bot.wait_until_ready()

    async def _loop_error(self, name: str, error: Exception):
        logger.error("Фоновая задача %s упала: %s", name, error, exc_info=(type(error), error, error.__traceback__))

    @check_shifts.error
    async def check_shifts_error(self, error):
        await self._loop_error("check_shifts", error)

    @check_reports.error
    async def check_reports_error(self, error):
        await self._loop_error("check_reports", error)

    @check_suspicious.error
    async def check_suspicious_error(self, error):
        await self._loop_error("check_suspicious", error)

    @weekly_report.error
    async def weekly_report_error(self, error):
        await self._loop_error("weekly_report", error)


def setup(bot):
    bot.add_cog(Tasks(bot))
