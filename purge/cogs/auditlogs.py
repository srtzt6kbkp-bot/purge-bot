from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from core.audit import send_audit_log

log = logging.getLogger("hoodbot.auditlogs")

AUDITED_COMMANDS = {
    "autoresponder add",
    "autoresponder remove",
    "autoreact add",
    "autoreact remove",
    "antinuke disable",
    "antinuke enable",
    "antinuke logs",
    "antinuke punishment",
    "antinuke threshold",
    "antinuke trust",
    "antinuke untrust",
    "antinuke window",
    "automod blockword",
    "automod disable",
    "automod enable",
    "automod logs",
    "automod setup",
    "automod toggle",
    "boosterrole base",
    "boosterrole color",
    "boosterrole create",
    "boosterrole dominant",
    "boosterrole filter",
    "boosterrole icon",
    "boosterrole random",
    "boosterrole remove",
    "boosterrole rename",
    "boosterrole share",
    "botcustom avatar",
    "botcustom banner",
    "botcustom bio",
    "botcustom globalavatar",
    "botcustom globalgrant",
    "botcustom globalrevoke",
    "botcustom nickname",
    "botcustom status",
    "botstatus",
    "custom embed",
    "custom nick",
    "giveaway edit",
    "giveaway end",
    "giveaway reroll",
    "giveaway start",
    "invitekey generate",
    "invitekey redeem",
    "jailsetup",
    "logs clear",
    "logs setup",
    "logs set",
    "nuke",
    "nuke schedule",
    "nuke stop",
    "prefix set",
    "purge",
    "purge bots",
    "purge contains",
    "purge embeds",
    "purge images",
    "purge links",
    "purge user",
    "reason",
    "setup",
    "topfloor grant",
    "topfloor remove",
    "topfloor set",
    "topfloor setup",
    "voicemaster claim",
    "voicemaster hide",
    "voicemaster limit",
    "voicemaster lock",
    "voicemaster permit",
    "voicemaster reject",
    "voicemaster rename",
    "voicemaster reveal",
    "voicemaster setup",
    "voicemaster transfer",
    "voicemaster unlock",
}

LOG_CHANNELS = {
    "general": "bot-logs",
    "moderation": "moderation-logs",
    "automod": "automod-logs",
    "antinuke": "antinuke-logs",
    "role": "role-logs",
}
COMMAND_LOG_TYPES = {
    "antinuke": "antinuke",
    "automod": "automod",
    "topfloor": "role",
    "ban": "moderation",
    "kick": "moderation",
    "timeout": "moderation",
    "untimeout": "moderation",
    "unban": "moderation",
    "warn": "moderation",
    "unwarn": "moderation",
    "clearwarnings": "moderation",
    "jail": "moderation",
    "unjail": "moderation",
    "purge": "moderation",
    "nuke": "moderation",
    "reason": "moderation",
}


class AuditLogs(commands.Cog):
    """Configurable per-server audit destination and state-change command logs."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.hybrid_group(name="logs", invoke_without_command=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def logs(self, ctx: commands.Context) -> None:
        """Configure the server-wide audit log channel."""
        await ctx.send_help(ctx.command)

    async def setup_log_channels(self, guild: discord.Guild) -> str:
        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_channels:
            raise RuntimeError("Manage Channels permission is required to create private log channels.")

        row = await self.bot.db.fetchone(
            "SELECT category_id FROM audit_log_setup WHERE guild_id = ?",
            guild.id,
        )
        category = guild.get_channel(row["category_id"]) if row else None
        if not isinstance(category, discord.CategoryChannel):
            category = (
                discord.utils.get(guild.categories, name="Purge Logs")
                or discord.utils.get(guild.categories, name="Hoodbot Logs")
            )

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                embed_links=True,
                attach_files=True,
            ),
        }
        for role in guild.roles:
            if role.permissions.administrator or role.permissions.manage_guild:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    read_message_history=True,
                )
        role_config = await self.bot.db.fetchone(
            "SELECT topfloor_role_id FROM role_access_config WHERE guild_id = ?",
            guild.id,
        )
        topfloor_role_id = role_config["topfloor_role_id"] if role_config else None
        topfloor_role = guild.get_role(topfloor_role_id) if topfloor_role_id else None
        if topfloor_role is not None:
            overwrites[topfloor_role] = discord.PermissionOverwrite(
                view_channel=True,
                read_message_history=True,
            )

        if category is None:
            category = await guild.create_category(
                "Purge Logs",
                overwrites=overwrites,
                reason="Create private Purge log channels",
            )
        else:
            await category.edit(
                name="Purge Logs",
                overwrites=overwrites,
                reason="Configure private Purge log channels",
            )

        await self.bot.db.execute(
            "INSERT INTO audit_log_setup (guild_id, category_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET category_id = excluded.category_id",
            guild.id,
            category.id,
        )

        channels: dict[str, discord.TextChannel] = {}
        for log_type, name in LOG_CHANNELS.items():
            channel = discord.utils.get(category.text_channels, name=name)
            if channel is None:
                channel = await guild.create_text_channel(
                    name,
                    category=category,
                    reason="Create private Purge log channels",
                )
            if channel.permissions_synced is False:
                await channel.edit(
                    sync_permissions=True,
                    reason="Sync Purge log channel permissions with its private category",
                )
            channels[log_type] = channel
            await self.bot.db.execute(
                "INSERT INTO audit_log_channels (guild_id, log_type, channel_id) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, log_type) DO UPDATE SET channel_id = excluded.channel_id",
                guild.id,
                log_type,
                channel.id,
            )

        await self.bot.db.execute(
            "INSERT INTO audit_log_config (guild_id, log_channel_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET log_channel_id = excluded.log_channel_id",
            guild.id,
            channels["general"].id,
        )
        await self.bot.db.execute(
            "INSERT INTO mod_config (guild_id, log_channel_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET log_channel_id = excluded.log_channel_id",
            guild.id,
            channels["moderation"].id,
        )
        await self.bot.db.execute(
            "INSERT INTO automod_config (guild_id, log_channel_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET log_channel_id = excluded.log_channel_id",
            guild.id,
            channels["automod"].id,
        )
        await self.bot.db.execute(
            "INSERT INTO antinuke_config (guild_id, log_channel_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET log_channel_id = excluded.log_channel_id",
            guild.id,
            channels["antinuke"].id,
        )
        setup_embed = discord.Embed(
            title="Purge log channels configured",
            description=(
                f"Private category: {category.mention}\n"
                + "\n".join(f"**{kind.title()}:** {channel.mention}" for kind, channel in channels.items())
            ),
            colour=discord.Colour(0x000000),
            timestamp=discord.utils.utcnow(),
        )
        await channels["general"].send(embed=setup_embed)
        mentions = ", ".join(channel.mention for channel in channels.values())
        return f"Created/configured private {category.name} category and log channels: {mentions}."

    @logs.command(name="setup")
    @commands.has_guild_permissions(manage_guild=True)
    @commands.bot_has_guild_permissions(manage_channels=True)
    async def logs_setup(self, ctx: commands.Context) -> None:
        """Create and configure all private Purge log channels."""
        await ctx.defer()
        try:
            result = await self.setup_log_channels(ctx.guild)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.exception("Could not create private log channels in guild %s", ctx.guild.id)
            await ctx.send(f"Log setup failed because Discord rejected a channel or permission change: {exc}.")
            return
        await ctx.send(result)

    @logs.command(name="set")
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.describe(channel="Text channel for server audit logs")
    async def logs_set(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Set the server-wide audit log channel."""
        permissions = channel.permissions_for(ctx.guild.me)
        if not permissions.send_messages or not permissions.embed_links:
            await ctx.reply(
                "I need **Send Messages** and **Embed Links** in that channel.",
                mention_author=False,
            )
            return

        await self.bot.db.execute(
            "INSERT INTO audit_log_config (guild_id, log_channel_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET log_channel_id = excluded.log_channel_id",
            ctx.guild.id,
            channel.id,
        )
        await ctx.reply(f"Server audit logs will go to {channel.mention}.", mention_author=False)

    @logs.command(name="status")
    @commands.has_guild_permissions(manage_guild=True)
    async def logs_status(self, ctx: commands.Context) -> None:
        """Show the configured server-wide audit log channel."""
        row = await self.bot.db.fetchone(
            "SELECT log_channel_id FROM audit_log_config WHERE guild_id = ?",
            ctx.guild.id,
        )
        channel = ctx.guild.get_channel(row["log_channel_id"]) if row else None
        channels = await self.bot.db.fetchall(
            "SELECT log_type, channel_id FROM audit_log_channels WHERE guild_id = ? ORDER BY log_type",
            ctx.guild.id,
        )
        if channels:
            details = "\n".join(
                f"**{entry['log_type'].title()}:** "
                f"<#{entry['channel_id']}>" if ctx.guild.get_channel(entry["channel_id"]) else
                f"**{entry['log_type'].title()}:** Missing channel (`{entry['channel_id']}`)"
                for entry in channels
            )
            embed = discord.Embed(
                title="Purge log channel status",
                description=details,
                colour=discord.Colour(0x000000),
            )
            await ctx.reply(embed=embed, mention_author=False)
            return
        message = (
            f"Server audit logs go to {channel.mention}."
            if channel
            else "No server audit log channel is configured."
        )
        await ctx.reply(message, mention_author=False)

    @logs.command(name="clear")
    @commands.has_guild_permissions(manage_guild=True)
    async def logs_clear(self, ctx: commands.Context) -> None:
        """Disable all Purge audit logging for this server."""
        await self.bot.db.execute(
            "DELETE FROM audit_log_channels WHERE guild_id = ?",
            ctx.guild.id,
        )
        await self.bot.db.execute(
            "DELETE FROM audit_log_config WHERE guild_id = ?",
            ctx.guild.id,
        )
        await self.bot.db.execute(
            "UPDATE mod_config SET log_channel_id = NULL WHERE guild_id = ?",
            ctx.guild.id,
        )
        await self.bot.db.execute(
            "UPDATE automod_config SET log_channel_id = NULL WHERE guild_id = ?",
            ctx.guild.id,
        )
        await self.bot.db.execute(
            "UPDATE antinuke_config SET log_channel_id = NULL WHERE guild_id = ?",
            ctx.guild.id,
        )
        await ctx.reply("Purge logging has been disabled. The log channels were left in place.", mention_author=False)

    async def _log_command(
        self,
        guild: discord.Guild | None,
        actor: discord.abc.User,
        command_name: str,
        channel: discord.abc.Messageable | None,
    ) -> None:
        if guild is None or command_name not in AUDITED_COMMANDS:
            return

        channel_label = getattr(channel, "mention", "unknown channel")
        embed = discord.Embed(
            title="Server command audit",
            description=(
                f"**Command:** `{command_name}`\n"
                f"**Actor:** {actor.mention} (`{actor.id}`)\n"
                f"**Location:** {channel_label}"
            ),
            colour=discord.Colour(0x000000),
            timestamp=discord.utils.utcnow(),
        )
        log_type = "general"
        for prefix, channel_type in COMMAND_LOG_TYPES.items():
            if command_name == prefix or command_name.startswith(f"{prefix} "):
                log_type = channel_type
                break
        await send_audit_log(self.bot, guild, embed, log_type=log_type)

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context) -> None:
        if ctx.command is not None:
            await self._log_command(ctx.guild, ctx.author, ctx.command.qualified_name, ctx.channel)

    @commands.Cog.listener()
    async def on_app_command_completion(
        self,
        interaction: discord.Interaction,
        command: app_commands.Command | app_commands.Group,
    ) -> None:
        if interaction.guild is not None:
            await self._log_command(
                interaction.guild,
                interaction.user,
                command.qualified_name,
                interaction.channel,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AuditLogs(bot))
