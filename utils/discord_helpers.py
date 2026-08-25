import logging

import disnake

import config

logger = logging.getLogger(__name__)


async def send_control_warning(guild, content: str) -> bool:
    """Best-effort warning to the senior control channel after a partial Discord failure."""
    if guild is None:
        return False
    channel = guild.get_channel(config.CONTROL_CHANNEL_ID)
    if channel is None:
        logger.warning("CONTROL_CHANNEL_ID=%s не найден для системного предупреждения", config.CONTROL_CHANNEL_ID)
        return False
    try:
        await channel.send(content, allowed_mentions=disnake.AllowedMentions(roles=True, users=True))
        return True
    except Exception:
        logger.exception("Не удалось отправить предупреждение в канал контроля")
        return False
