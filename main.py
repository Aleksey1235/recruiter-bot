import asyncio
import logging
from logging.handlers import RotatingFileHandler

import disnake
from disnake.ext import commands

import config
from database.db import db


def setup_logging():
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler("bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


logger = logging.getLogger(__name__)


class Bot(commands.Bot):
    def __init__(self):
        intents = disnake.Intents.default()
        intents.members = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            test_guilds=[config.GUILD_ID],
        )

    async def on_ready(self):
        logger.info("Бот %s запущен. Cogs: %s", self.user, ", ".join(self.cogs.keys()))
        guild = self.get_guild(config.GUILD_ID)
        if guild is None:
            logger.error("GUILD_ID=%s не найден среди серверов бота", config.GUILD_ID)
            return
        for name, channel_id in {
            "SHIFTS_CHANNEL_ID": config.SHIFTS_CHANNEL_ID,
            "REPORTS_CHANNEL_ID": config.REPORTS_CHANNEL_ID,
            "STATS_CHANNEL_ID": config.STATS_CHANNEL_ID,
            "CONTROL_CHANNEL_ID": config.CONTROL_CHANNEL_ID,
            "LOGS_CHANNEL_ID": config.LOGS_CHANNEL_ID,
        }.items():
            if guild.get_channel(channel_id) is None:
                logger.error("%s=%s не найден на сервере", name, channel_id)

    async def on_slash_command_error(self, inter, error):
        original = getattr(error, "original", error)
        if isinstance(error, commands.CheckFailure) or isinstance(original, commands.CheckFailure):
            message = "❌ Недостаточно прав для этой команды."
        else:
            message = "❌ Произошла внутренняя ошибка. Событие записано в лог."
            logger.error(
                "Ошибка slash-команды %s: %s",
                getattr(getattr(inter, "application_command", None), "name", "unknown"),
                original,
                exc_info=(type(original), original, original.__traceback__),
            )

        try:
            if inter.response.is_done():
                try:
                    await inter.edit_original_response(content=message, embed=None, view=None)
                except Exception:
                    await inter.followup.send(message, ephemeral=True)
            else:
                await inter.response.send_message(message, ephemeral=True)
        except Exception:
            logger.exception("Не удалось сообщить пользователю об ошибке команды")


async def main():
    setup_logging()
    config.validate_config()
    await db.connect()

    bot = Bot()
    cogs = [
        "cogs.shifts",
        "cogs.invites",
        "cogs.statistics",
        "cogs.finance",
        "cogs.goals",
        "cogs.profile",
        "cogs.admin",
        "cogs.database_admin",
        "cogs.tasks",
        "cogs.help",
    ]

    try:
        for cog in cogs:
            bot.load_extension(cog)
            logger.info("Загружен %s", cog)
        await bot.start(config.BOT_TOKEN)
    finally:
        if not bot.is_closed():
            await bot.close()
        await db.close()
        logger.info("Бот и база данных корректно остановлены")


if __name__ == "__main__":
    asyncio.run(main())
