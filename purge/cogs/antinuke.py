from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from typing import Literal

import discord
from discord.ext import commands

from core.audit import send_audit_log

log = logging.getLogger("hoodbot.antinuke")

# audit log action -> short key used in commands and the DB
ACTION_KEYS: dict[discord.AuditLogAction, str] = {
    discord.AuditLogAction.channel_delete: "channel_delete",
    discord.AuditLogAction.role_delete: "role_delete",
    discord.AuditLogAction.ban: "ban",
    discord.AuditLogAction.kick: "kick",
}
LIMIT_COLUMNS = {
    "channel_delete": "channel_delete_limit",
    "role_delete": "role_delete_limit",
    "ban": "ban_limit",
    "kick": "kick_limit",
}
SETTABLE = {"enabled", "punishment", "log_channel_id", "window_seconds", *LIMIT_COLUMNS.values()}

# Roles with any of these are what "strip" removes from an offender.
DANGEROUS_PERMS = (
    "administrator", "manage_guild", "manage_roles", "manage_channels",
    "manage_webhooks", "ban_members", "kick_members",
)
PUNISH_COOLDOWN = 30  # seconds, so one burst only gets punished (and logged) once


class Antinuke(commands.Cog):
    """Punishes anyone who deletes/bans/kicks too much, too fast.

    Only the server owner can configure it, so a rogue admin can't switch it off.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._hits: dict[tuple[int, int, str], deque[float]] = defaultdict(deque)
        self._last_punish: dict[tuple[int, int], float] = {}

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if ctx.author.id != ctx.guild.owner_id:
            raise commands.CheckFailure("Only the server owner can use antinuke commands.")
        return True

    # ----- config helpers -----------------------------------------------------

    async def _config(self, guild_id: int):
        return await self.bot.db.fetchone("SELECT * FROM antinuke_config WHERE guild_id = ?", guild_id)

    async def _set(self, guild_id: int, column: str, value) -> None:
        if column not in SETTABLE:
            raise ValueError(f"Unknown antinuke setting: {column}")
        await self.bot.db.execute("INSERT OR IGNORE INTO antinuke_config (guild_id) VALUES (?)", guild_id)
        await self.bot.db.execute(f"UPDATE antinuke_config SET {column} = ? WHERE guild_id = ?", value, guild_id)

    # ----- monitor ------------------------------------------------------------

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        key = ACTION_KEYS.get(entry.action)
        if key is None or entry.user_id is None:
            return

        guild = entry.guild
        actor_id = entry.user_id
        if actor_id in (self.bot.user.id, guild.owner_id):
            return

        cfg = await self._config(guild.id)
        if cfg is None or not cfg["enabled"]:
            return

        trusted = await self.bot.db.fetchone(
            "SELECT 1 FROM antinuke_whitelist WHERE guild_id = ? AND user_id = ?", guild.id, actor_id
        )
        if trusted:
            return

        now = time.monotonic()
        hits = self._hits[(guild.id, actor_id, key)]
        hits.append(now)
        while hits and now - hits[0] > cfg["window_seconds"]:
            hits.popleft()

        count = len(hits)
        if count >= cfg[LIMIT_COLUMNS[key]]:
            hits.clear()
            await self._punish(guild, actor_id, key, count, cfg)

    async def _punish(self, guild: discord.Guild, actor_id: int, key: str, count: int, cfg) -> None:
        now = time.monotonic()
        last = self._last_punish.get((guild.id, actor_id))
        if last is not None and now - last < PUNISH_COOLDOWN:
            return
        self._last_punish[(guild.id, actor_id)] = now

        reason = f"Antinuke: {count}x {key} within {cfg['window_seconds']}s"
        member = guild.get_member(actor_id)
        punishment = cfg["punishment"]

        try:
            if punishment == "ban":
                await guild.ban(discord.Object(id=actor_id), reason=reason)
                outcome = "banned"
            elif punishment == "kick" and member is not None:
                await guild.kick(member, reason=reason)
                outcome = "kicked"
            elif punishment == "strip" and member is not None:
                top = guild.me.top_role
                risky = [
                    r for r in member.roles
                    if not r.is_default() and not r.managed and r < top
                    and any(getattr(r.permissions, perm) for perm in DANGEROUS_PERMS)
                ]
                if risky:
                    await member.remove_roles(*risky, reason=reason)
                outcome = f"stripped {len(risky)} dangerous role(s)"
            else:
                outcome = "no action taken (member not found)"
        except discord.HTTPException as exc:
            outcome = f"FAILED (HTTP {exc.status}) - drag my role higher in the role list"
            log.warning("Antinuke punishment failed in %s: %s", guild.id, exc)

        await self._send_log(guild, cfg, actor_id, key, count, outcome)

    async def _send_log(self, guild: discord.Guild, cfg, actor_id: int, key: str, count: int, outcome: str) -> None:
        embed = discord.Embed(
            title="Antinuke triggered",
            colour=discord.Colour(0x000000),
            description=(
                f"<@{actor_id}> (`{actor_id}`) did **{count}x {key.replace('_', ' ')}** "
                f"within {cfg['window_seconds']}s.\nResult: **{outcome}**"
            ),
        )
        if await send_audit_log(self.bot, guild, embed, log_type="antinuke"):
            return
        channel = guild.get_channel(cfg["log_channel_id"]) if cfg["log_channel_id"] else None
        try:
            if isinstance(channel, discord.abc.Messageable):
                await channel.send(embed=embed)
            elif guild.owner is not None:
                await guild.owner.send(embed=embed)
        except discord.HTTPException:
            log.warning("Could not deliver antinuke log for guild %s", guild.id)

    # ----- commands -----------------------------------------------------------

    @commands.hybrid_group(name="antinuke", aliases=["an"], invoke_without_command=True)
    async def antinuke(self, ctx: commands.Context) -> None:
        """Protect the server from mass deletions and bans."""
        await ctx.send_help(ctx.command)

    @antinuke.command(name="enable")
    async def an_enable(self, ctx: commands.Context) -> None:
        """Turn antinuke on."""
        await self._set(ctx.guild.id, "enabled", 1)
        warning = ""
        if not ctx.guild.me.guild_permissions.view_audit_log:
            warning = "\nWarning: I need the **View Audit Log** permission or I can't see anything."
        await ctx.reply("Antinuke is on." + warning, mention_author=False)

    @antinuke.command(name="disable")
    async def an_disable(self, ctx: commands.Context) -> None:
        """Turn antinuke off."""
        await self._set(ctx.guild.id, "enabled", 0)
        await ctx.reply("Antinuke is off.", mention_author=False)

    @antinuke.command(name="punishment")
    async def an_punishment(self, ctx: commands.Context, kind: Literal["ban", "kick", "strip"]) -> None:
        """What happens to offenders: ban, kick, or strip (remove their dangerous roles)."""
        await self._set(ctx.guild.id, "punishment", kind)
        await ctx.reply(f"Offenders will be handled with: {kind}.", mention_author=False)

    @antinuke.command(name="threshold")
    async def an_threshold(
        self, ctx: commands.Context,
        action: Literal["channel_delete", "role_delete", "ban", "kick"],
        amount: commands.Range[int, 1, 50],
    ) -> None:
        """How many of an action inside the time window triggers a punishment."""
        await self._set(ctx.guild.id, LIMIT_COLUMNS[action], amount)
        await ctx.reply(f"{action.replace('_', ' ')} limit is now {amount}.", mention_author=False)

    @antinuke.command(name="window")
    async def an_window(self, ctx: commands.Context, seconds: commands.Range[int, 10, 600]) -> None:
        """Length of the time window in seconds."""
        await self._set(ctx.guild.id, "window_seconds", seconds)
        await ctx.reply(f"Window is now {seconds}s.", mention_author=False)

    @antinuke.command(name="logs")
    async def an_logs(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Where alerts get posted (falls back to a DM to you)."""
        await self._set(ctx.guild.id, "log_channel_id", channel.id)
        await ctx.reply(f"Alerts will go to {channel.mention}.", mention_author=False)

    @antinuke.command(name="trust")
    async def an_trust(self, ctx: commands.Context, user: discord.User) -> None:
        """Exempt someone (a trusted admin or bot) from antinuke."""
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO antinuke_whitelist (guild_id, user_id) VALUES (?, ?)", ctx.guild.id, user.id
        )
        await ctx.reply(f"{user} is trusted.", mention_author=False)

    @antinuke.command(name="untrust")
    async def an_untrust(self, ctx: commands.Context, user: discord.User) -> None:
        """Remove someone from the trusted list."""
        await self.bot.db.execute(
            "DELETE FROM antinuke_whitelist WHERE guild_id = ? AND user_id = ?", ctx.guild.id, user.id
        )
        await ctx.reply(f"{user} is no longer trusted.", mention_author=False)

    @antinuke.command(name="trusted")
    async def an_trusted(self, ctx: commands.Context) -> None:
        """List trusted users."""
        rows = await self.bot.db.fetchall("SELECT user_id FROM antinuke_whitelist WHERE guild_id = ?", ctx.guild.id)
        text = "\n".join(f"<@{r['user_id']}> (`{r['user_id']}`)" for r in rows) or "Nobody yet."
        await ctx.reply(text, mention_author=False)

    @antinuke.command(name="status")
    async def an_status(self, ctx: commands.Context) -> None:
        """Show the current settings."""
        cfg = await self._config(ctx.guild.id)
        if cfg is None:
            await ctx.reply("Not configured yet. Run `antinuke enable`.", mention_author=False)
            return
        embed = discord.Embed(title="Antinuke", colour=discord.Colour(0x000000))
        embed.add_field(name="Enabled", value="yes" if cfg["enabled"] else "no")
        embed.add_field(name="Punishment", value=cfg["punishment"])
        embed.add_field(name="Window", value=f"{cfg['window_seconds']}s")
        embed.add_field(
            name="Limits",
            value="\n".join(f"{k.replace('_', ' ')}: {cfg[col]}" for k, col in LIMIT_COLUMNS.items()),
        )
        embed.add_field(name="Alerts", value=f"<#{cfg['log_channel_id']}>" if cfg["log_channel_id"] else "DM to owner")
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Antinuke(bot))
