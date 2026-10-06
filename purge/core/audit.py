from __future__ import annotations

import logging

import discord
from discord.ext import commands

log = logging.getLogger("hoodbot.audit")


async def send_audit_log(
    bot: commands.Bot,
    guild: discord.Guild,
    embed: discord.Embed,
    *,
    log_type: str = "general",
) -> bool:
    """Send to a typed audit channel, falling back to the shared audit channel."""
    row = await bot.db.fetchone(
        "SELECT channel_id FROM audit_log_channels WHERE guild_id = ? AND log_type = ?",
        guild.id,
        log_type,
    )
    if row is not None:
        channel = guild.get_channel(row["channel_id"])
        if isinstance(channel, discord.abc.Messageable):
            try:
                await channel.send(embed=embed)
                return True
            except discord.HTTPException as exc:
                log.warning(
                    "Could not send %s audit log to channel %s in guild %s: %s",
                    log_type,
                    channel.id,
                    guild.id,
                    exc,
                )
        else:
            log.warning(
                "Configured %s audit channel %s is missing in guild %s",
                log_type,
                row["channel_id"],
                guild.id,
            )

    row = await bot.db.fetchone(
        "SELECT log_channel_id FROM audit_log_config WHERE guild_id = ?",
        guild.id,
    )
    if row is None:
        return False

    channel = guild.get_channel(row["log_channel_id"])
    if not isinstance(channel, discord.abc.Messageable):
        log.warning("Configured audit log channel %s is missing in guild %s", row["log_channel_id"], guild.id)
        return False

    try:
        await channel.send(embed=embed)
    except discord.HTTPException as exc:
        log.warning("Could not send audit log to channel %s in guild %s: %s", channel.id, guild.id, exc)
        return False
    return True
