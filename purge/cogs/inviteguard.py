from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time

import discord
from discord.ext import commands

log = logging.getLogger("hoodbot.inviteguard")
KEY_LIFETIME = 24 * 60 * 60
REDEMPTION_GRACE = 3 * 60
LEAVE_RETRY = 30


class InviteGuard(commands.Cog):
    """Require a one-use owner-approved key to keep the bot in a new server."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._application_manager_ids: set[int] = set()
        application = bot.application
        if application is not None:
            self._application_manager_ids.add(application.owner.id)
            if application.team is not None:
                if application.team.owner_id is not None:
                    self._application_manager_ids.add(application.team.owner_id)
                self._application_manager_ids.update(
                    member.id
                    for member in application.team.members
                    if member.membership_state == discord.TeamMembershipState.accepted
                )
        self._pending_tasks: dict[int, asyncio.Task[None]] = {}

    async def _can_generate(self, user_id: int) -> bool:
        if user_id in self._application_manager_ids:
            return True
        row = await self.bot.db.fetchone(
            "SELECT 1 FROM global_customization_access WHERE user_id = ?", user_id
        )
        return row is not None

    async def _respond(self, ctx: commands.Context, message: str, *, ephemeral: bool = True) -> None:
        interaction = ctx.interaction
        if interaction is None:
            await ctx.reply(message, mention_author=False)
        elif interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(message, ephemeral=ephemeral)

    @commands.hybrid_group(name="invitekey", aliases=["botinvite"], invoke_without_command=True)
    async def invitekey(self, ctx: commands.Context) -> None:
        """Generate or redeem an owner-approved bot invite key."""
        await ctx.send_help(ctx.command)

    @invitekey.command(name="generate")
    async def generate(self, ctx: commands.Context) -> None:
        """Generate a private one-use key. Available to app managers and approved users."""
        if not await self._can_generate(ctx.author.id):
            await self._respond(ctx, "Only the bot owner, developer team, or an approved user can generate invite keys.")
            return

        key = secrets.token_urlsafe(32)
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        now = int(time.time())
        expires_at = now + KEY_LIFETIME
        await self.bot.db.execute("DELETE FROM bot_invite_keys WHERE expires_at <= ?", now)
        await self.bot.db.execute(
            "INSERT INTO bot_invite_keys (key_hash, created_by, created_at, expires_at) VALUES (?, ?, ?, ?)",
            key_hash,
            ctx.author.id,
            now,
            expires_at,
        )
        instructions = (
            f"Bot invite key: `{key}`\n"
            f"Expires <t:{expires_at}:R> and can be used once. Invite the bot, then have that server's owner or an administrator run `invitekey redeem {key}` with the bot's prefix within {REDEMPTION_GRACE // 60} minutes."
        )

        if ctx.interaction is not None:
            await self._respond(ctx, instructions)
            return

        try:
            await ctx.author.send(instructions)
        except discord.HTTPException:
            await self.bot.db.execute("DELETE FROM bot_invite_keys WHERE key_hash = ?", key_hash)
            await ctx.reply("I couldn't DM you the key. Enable DMs from this server and generate a new one.", mention_author=False)
            return
        await ctx.reply("I sent the invite key to your DMs. It is private and can only be used once.", mention_author=False)

    @invitekey.command(name="redeem")
    async def redeem(self, ctx: commands.Context, key: str) -> None:
        """Redeem an invite key in the new server. Requires server owner/admin."""
        guild = ctx.guild
        if guild is None:
            await self._respond(ctx, "Redeem the key inside the server where the bot was just invited.")
            return
        is_admin = ctx.author.id == guild.owner_id or (
            isinstance(ctx.author, discord.Member)
            and ctx.author.guild_permissions.administrator
        )
        if not is_admin:
            await self._respond(ctx, "Only this server's owner or an administrator can redeem the invite key.")
            return

        now = int(time.time())
        key_hash = hashlib.sha256(key.strip().encode("utf-8")).hexdigest()
        redeemed = await self.bot.db.redeem_bot_invite_key(key_hash, guild.id, now)
        if not redeemed:
            await self._respond(ctx, "That key is invalid, expired, already used, or this server's redemption window ended.")
            return

        self._cancel_pending_task(guild.id)
        await self._respond(ctx, "Invite approved. The bot is authorized to stay in this server.")

    def _cancel_pending_task(self, guild_id: int) -> None:
        task = self._pending_tasks.pop(guild_id, None)
        if task is not None:
            task.cancel()

    def _schedule_pending_check(self, guild_id: int, expires_at: int) -> None:
        self._cancel_pending_task(guild_id)
        task = asyncio.create_task(self._expire_pending_guild(guild_id, expires_at))
        self._pending_tasks[guild_id] = task

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        authorized = await self.bot.db.fetchone(
            "SELECT 1 FROM bot_authorized_guilds WHERE guild_id = ?", guild.id
        )
        if authorized is not None:
            return

        now = int(time.time())
        expires_at = now + REDEMPTION_GRACE
        try:
            await self.bot.db.execute(
                "INSERT INTO bot_pending_guilds (guild_id, joined_at, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET joined_at = excluded.joined_at, expires_at = excluded.expires_at",
                guild.id,
                now,
                expires_at,
            )
        except Exception:
            log.exception("Could not record pending invite for guild %s; leaving to fail closed", guild.id)
            await guild.leave()
            return

        self._schedule_pending_check(guild.id, expires_at)
        log.warning("Guild %s must redeem an invite key within three minutes", guild.id)
        channel = guild.system_channel
        if channel is not None:
            try:
                await channel.send(
                    "This bot invite is pending approval. A server owner or administrator must redeem an approved key with `invitekey redeem <key>` using the bot's prefix within three minutes, or the bot will leave."
                )
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self._cancel_pending_task(guild.id)
        await self.bot.db.execute("DELETE FROM bot_authorized_guilds WHERE guild_id = ?", guild.id)
        await self.bot.db.execute("DELETE FROM bot_pending_guilds WHERE guild_id = ?", guild.id)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        rows = await self.bot.db.fetchall("SELECT guild_id, expires_at FROM bot_pending_guilds")
        for row in rows:
            guild_id = row["guild_id"]
            if self.bot.get_guild(guild_id) is None:
                await self.bot.db.execute("DELETE FROM bot_pending_guilds WHERE guild_id = ?", guild_id)
                continue
            self._schedule_pending_check(guild_id, row["expires_at"])

    async def _expire_pending_guild(self, guild_id: int, expires_at: int) -> None:
        try:
            await asyncio.sleep(max(0, expires_at - time.time()))
            pending = await self.bot.db.fetchone(
                "SELECT expires_at FROM bot_pending_guilds WHERE guild_id = ?", guild_id
            )
            if pending is None:
                return
            if pending["expires_at"] > int(time.time()):
                self._schedule_pending_check(guild_id, pending["expires_at"])
                return

            authorized = await self.bot.db.fetchone(
                "SELECT 1 FROM bot_authorized_guilds WHERE guild_id = ?", guild_id
            )
            if authorized is not None:
                await self.bot.db.execute("DELETE FROM bot_pending_guilds WHERE guild_id = ?", guild_id)
                return

            guild = self.bot.get_guild(guild_id)
            if guild is not None:
                try:
                    await guild.leave()
                    log.info("Left unapproved guild %s", guild_id)
                except discord.HTTPException:
                    log.exception("Could not leave unapproved guild %s; will retry", guild_id)
                    self._schedule_pending_check(guild_id, int(time.time()) + LEAVE_RETRY)
                    return
            await self.bot.db.execute("DELETE FROM bot_pending_guilds WHERE guild_id = ?", guild_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Invite guard failed for guild %s; scheduling another check", guild_id)
            self._schedule_pending_check(guild_id, int(time.time()) + LEAVE_RETRY)

    def cog_unload(self) -> None:
        for task in self._pending_tasks.values():
            task.cancel()
        self._pending_tasks.clear()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(InviteGuard(bot))