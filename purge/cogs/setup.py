from __future__ import annotations

import logging

import discord
from discord.ext import commands

from cogs.voicemaster import ControlPanel, VoiceMaster

log = logging.getLogger("hoodbot.setup")


class SetupFailure(RuntimeError):
    pass


class ConfirmSetupStep(discord.ui.View):
    def __init__(self, cog: "ServerSetup", step: str, user_id: int, guild_id: int) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.step = step
        self.user_id = user_id
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (
            interaction.user.id != self.user_id
            or interaction.guild_id != self.guild_id
            or not isinstance(interaction.user, discord.Member)
            or not interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "Only the administrator who started this setup can confirm it.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Confirm setup", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        button.disabled = True
        await interaction.response.edit_message(content="Setting up...", embed=None, view=None)
        if interaction.guild is None:
            result = "The server could not be identified."
        else:
            try:
                result = await self.cog.run_step(interaction.guild, self.step)
            except Exception:
                log.exception("Setup step %s failed in guild %s", self.step, self.guild_id)
                result = f"{self.step.title()}: failed unexpectedly; the error was logged."
        await interaction.followup.send(result, ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        button.disabled = True
        await interaction.response.edit_message(content="Setup cancelled. No changes were made.", embed=None, view=None)
        self.stop()


class ConfirmFullSetup(discord.ui.View):
    def __init__(self, cog: "ServerSetup", user_id: int, guild_id: int) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.user_id = user_id
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (
            interaction.user.id != self.user_id
            or interaction.guild_id != self.guild_id
            or not isinstance(interaction.user, discord.Member)
            or not interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "Only the administrator who started setup can confirm it.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Confirm full setup", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        button.disabled = True
        await interaction.response.edit_message(content="Setting up the server...", embed=None, view=None)
        results = await self.cog.setup_everything(interaction.guild)
        await interaction.followup.send(embed=self.cog.results_embed(results), ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        button.disabled = True
        await interaction.response.edit_message(content="Setup cancelled. No changes were made.", embed=None, view=None)
        self.stop()


class SetupPanel(discord.ui.View):
    def __init__(self, cog: "ServerSetup") -> None:
        super().__init__(timeout=900)
        self.cog = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (
            not isinstance(interaction.user, discord.Member)
            or not interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "Only a server administrator can use the setup panel.",
                ephemeral=True,
            )
            return False
        return True

    async def _run_step(self, interaction: discord.Interaction, step: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.cog.run_step(interaction.guild, step)
        except Exception:
            log.exception("Setup step %s failed in guild %s", step, interaction.guild_id)
            result = f"{step}: failed unexpectedly; the error was logged."
        await interaction.followup.send(result, ephemeral=True)

    @discord.ui.button(label="Create log channels", style=discord.ButtonStyle.secondary, row=0)
    async def logs(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        await self._run_step(interaction, "logs")

    @discord.ui.button(label="Set up AutoMod", style=discord.ButtonStyle.secondary, row=0)
    async def automod(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        await self._run_step(interaction, "automod")

    @discord.ui.button(label="Set up VoiceMaster", style=discord.ButtonStyle.secondary, row=0)
    async def voicemaster(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        await self._run_step(interaction, "voicemaster")

    @discord.ui.button(label="Set up Jail", style=discord.ButtonStyle.secondary, row=1)
    async def jail(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        embed = discord.Embed(
            title="Confirm Jail setup",
            description="This creates a Jailed role and denies that role access to existing channels.",
            colour=discord.Colour.orange(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=ConfirmSetupStep(self.cog, "jail", interaction.user.id, interaction.guild_id),
            ephemeral=True,
        )

    @discord.ui.button(label="Set up Antinuke", style=discord.ButtonStyle.secondary, row=1)
    async def antinuke(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        embed = discord.Embed(
            title="Confirm Antinuke setup",
            description=(
                "This enables Antinuke with **kick** punishment after its configured thresholds "
                "are reached. Current administrators will be trusted."
            ),
            colour=discord.Colour.orange(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=ConfirmSetupStep(self.cog, "antinuke", interaction.user.id, interaction.guild_id),
            ephemeral=True,
        )

    @discord.ui.button(label="Set It All Up", style=discord.ButtonStyle.success, row=1)
    async def setup_all(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _ = button
        embed = discord.Embed(
            title="Confirm full server setup",
            description=(
                "This will create a private `Purge Logs` category with `#bot-logs`, "
                "`#moderation-logs`, `#automod-logs`, `#antinuke-logs`, and `#role-logs`; "
                "set up AutoMod and VoiceMaster, "
                "create a Jailed role that cannot view existing channels, and enable Antinuke "
                "with **kick** punishment (not ban). Existing administrators will be trusted by Antinuke. "
                "**Review these changes before confirming.**"
            ),
            colour=discord.Colour.orange(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=ConfirmFullSetup(self.cog, interaction.user.id, interaction.guild_id),
            ephemeral=True,
        )


class ServerSetup(commands.Cog):
    """Welcome and guided setup for new servers."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if isinstance(ctx.author, discord.Member) and ctx.author.guild_permissions.administrator:
            return True
        await ctx.reply("Only a server administrator can open the setup panel.", mention_author=False)
        return False

    @commands.hybrid_command(name="setup")
    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    async def setup_panel(self, ctx: commands.Context) -> None:
        """Show the guided server setup panel."""
        await ctx.send(embed=self.welcome_embed(ctx.guild, ctx.clean_prefix), view=SetupPanel(self))

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        channel = guild.system_channel
        bot_member = guild.me
        if bot_member is None:
            log.warning("Couldn't find the bot member for setup welcome in guild %s", guild.id)
            return
        if channel is None or not all(
            (
                channel.permissions_for(bot_member).send_messages,
                channel.permissions_for(bot_member).embed_links,
            )
        ):
            channel = next(
                (
                    candidate
                    for candidate in guild.text_channels
                    if candidate.permissions_for(bot_member).send_messages
                    and candidate.permissions_for(bot_member).embed_links
                ),
                None,
            )
        if channel is None:
            log.warning("No channel available for setup welcome in guild %s", guild.id)
            return

        try:
            prefix = await self.bot.get_guild_prefix(guild.id)
            await channel.send(embed=self.welcome_embed(guild, prefix), view=SetupPanel(self))
        except discord.HTTPException:
            log.exception("Couldn't post setup welcome in guild %s", guild.id)

    @staticmethod
    def welcome_embed(guild: discord.Guild, prefix: str) -> discord.Embed:
        embed = discord.Embed(
            title=f"Thanks for adding {guild.me.name if guild.me else 'Purge'}!",
            description=(
                "Purge includes server moderation, AutoMod, VoiceMaster, and more. "
                f"The default prefix is `{prefix}`; server administrators can change it with "
                f"`{prefix}prefix set <prefix>`.\n\n"
                "**Quick Start Guide**\n"
                f"`{prefix}jailsetup` — create a jail role and channel (the role is hidden from existing channels)\n"
                f"`{prefix}voicemaster setup` — create join-to-create voice channels\n"
                f"`{prefix}automod setup` — sync the configured AutoMod rules\n"
                f"`{prefix}antinuke enable` — enable antinuke protection"
            ),
            colour=discord.Colour.blurple(),
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text=f"Use {prefix}setup to reopen this panel.")
        return embed

    @staticmethod
    def results_embed(results: list[str]) -> discord.Embed:
        has_failure = any("failed" in result.lower() or "couldn't" in result.lower() for result in results)
        return discord.Embed(
            title="Server setup results",
            description="\n".join(results)[:4096],
            colour=discord.Colour.orange() if has_failure else discord.Colour.green(),
        )

    async def run_step(self, guild: discord.Guild, step: str) -> str:
        handlers = {
            "logs": self.setup_logs,
            "automod": self.setup_automod,
            "voicemaster": self.setup_voicemaster,
            "jail": self.setup_jail,
            "antinuke": self.setup_antinuke,
        }
        handler = handlers.get(step)
        if handler is None:
            raise ValueError(f"Unknown setup step: {step}")
        try:
            return await handler(guild)
        except SetupFailure as error:
            return f"{step.title()}: {error}"
        except (discord.Forbidden, discord.HTTPException) as error:
            log.exception("Setup step %s failed in guild %s", step, guild.id)
            return f"{step.title()}: failed because Discord rejected an action (HTTP {error.status})."

    async def setup_everything(self, guild: discord.Guild) -> list[str]:
        results = []
        for step in ("logs", "automod", "voicemaster", "jail", "antinuke"):
            try:
                results.append(await self.run_step(guild, step))
            except Exception:
                log.exception("Setup step %s failed in guild %s", step, guild.id)
                results.append(f"{step.title()}: failed unexpectedly; the error was logged.")
        return results

    async def setup_logs(self, guild: discord.Guild) -> str:
        audit_logs = self.bot.get_cog("AuditLogs")
        if audit_logs is None:
            raise RuntimeError("AuditLogs cog is not loaded.")
        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_channels:
            raise SetupFailure("Manage Channels permission is required to create private log channels.")
        return await audit_logs.setup_log_channels(guild)

    async def setup_automod(self, guild: discord.Guild) -> str:
        automod = self.bot.get_cog("AutoMod")
        if automod is None:
            raise RuntimeError("AutoMod cog is not loaded.")
        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_guild:
            raise SetupFailure("Manage Server permission is required to create native AutoMod rules.")
        await automod.get_guild_config(guild.id)
        count, skipped = await automod.sync_native_rules(guild)
        result = f"AutoMod: synced {count} native rule(s)."
        if skipped:
            result += " Skipped: " + ", ".join(skipped) + "."
        return result

    async def setup_voicemaster(self, guild: discord.Guild) -> str:
        existing = await self.bot.db.fetchone(
            "SELECT hub_channel_id, category_id FROM voicemaster_config WHERE guild_id = ?",
            guild.id,
        )
        if existing is not None:
            hub = guild.get_channel(existing["hub_channel_id"])
            category = guild.get_channel(existing["category_id"])
            if isinstance(hub, discord.VoiceChannel) and isinstance(category, discord.CategoryChannel):
                return f"VoiceMaster: already configured with {hub.mention}."

        bot_member = guild.me
        if bot_member is None or not (
            bot_member.guild_permissions.manage_channels and bot_member.guild_permissions.move_members
        ):
            raise SetupFailure("Manage Channels and Move Members permissions are required.")

        category = None
        created: list[discord.abc.GuildChannel] = []
        try:
            category = await guild.create_category("Voice Master", reason="Purge guided setup")
            created.append(category)
            hub = await guild.create_voice_channel(
                "Join to Create",
                category=category,
                reason="Purge guided setup",
            )
            created.append(hub)
            interface = await guild.create_text_channel(
                "interface",
                category=category,
                overwrites={
                    guild.default_role: discord.PermissionOverwrite(send_messages=False),
                    bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True),
                },
                reason="Purge guided setup",
            )
            created.append(interface)
            voice_master = self.bot.get_cog("VoiceMaster")
            if not isinstance(voice_master, VoiceMaster):
                raise RuntimeError("VoiceMaster cog is not loaded.")
            await interface.send(embed=voice_master._panel_embed(guild), view=ControlPanel(voice_master))
            await self.bot.db.execute(
                "INSERT INTO voicemaster_config (guild_id, hub_channel_id, category_id) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET "
                "hub_channel_id = excluded.hub_channel_id, category_id = excluded.category_id",
                guild.id,
                hub.id,
                category.id,
            )
        except Exception:
            for channel in reversed(created):
                try:
                    await channel.delete(reason="Cleanup after incomplete Purge VoiceMaster setup")
                except discord.HTTPException:
                    log.exception("Couldn't clean up channel %s after VoiceMaster setup failed", channel.id)
            raise
        return f"VoiceMaster: created {hub.mention}; controls are in {interface.mention}."

    async def setup_jail(self, guild: discord.Guild) -> str:
        config = await self.bot.db.fetchone(
            "SELECT jail_role_id, jail_channel_id FROM mod_config WHERE guild_id = ?",
            guild.id,
        )
        role = guild.get_role(config["jail_role_id"]) if config and config["jail_role_id"] else None
        channel = guild.get_channel(config["jail_channel_id"]) if config and config["jail_channel_id"] else None
        if role is not None and isinstance(channel, discord.TextChannel):
            return f"Jail: already configured with {role.mention} and {channel.mention}."

        bot_member = guild.me
        if bot_member is None or not (
            bot_member.guild_permissions.manage_roles and bot_member.guild_permissions.manage_channels
        ):
            raise SetupFailure("Manage Roles and Manage Channels permissions are required.")

        created_role = role is None
        created_channel = not isinstance(channel, discord.TextChannel)
        failed_channels: list[str] = []
        try:
            if role is None:
                role = await guild.create_role(name="Jailed", reason="Purge guided setup")
            if not isinstance(channel, discord.TextChannel):
                channel = await guild.create_text_channel(
                    "jail",
                    overwrites={
                        guild.default_role: discord.PermissionOverwrite(view_channel=False),
                        role: discord.PermissionOverwrite(
                            view_channel=True,
                            send_messages=True,
                            read_message_history=True,
                        ),
                        bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                    },
                    reason="Purge guided setup",
                )
            for guild_channel in guild.channels:
                if guild_channel.id == channel.id:
                    continue
                try:
                    await guild_channel.set_permissions(
                        role,
                        view_channel=False,
                        send_messages=False,
                        connect=False,
                        speak=False,
                        reason="Purge guided setup",
                    )
                except discord.HTTPException:
                    failed_channels.append(guild_channel.name)
            await self.bot.db.execute(
                "INSERT INTO mod_config (guild_id, jail_role_id, jail_channel_id) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET "
                "jail_role_id = excluded.jail_role_id, jail_channel_id = excluded.jail_channel_id",
                guild.id,
                role.id,
                channel.id,
            )
        except Exception:
            if created_channel and isinstance(channel, discord.TextChannel):
                try:
                    await channel.delete(reason="Cleanup after incomplete jail setup")
                except discord.HTTPException:
                    log.exception("Couldn't clean up jail channel %s", channel.id)
            if created_role and role is not None:
                try:
                    await role.delete(reason="Cleanup after incomplete jail setup")
                except discord.HTTPException:
                    log.exception("Couldn't clean up Jailed role %s", role.id)
            raise

        result = f"Jail: created {role.mention} and {channel.mention}; that role is hidden from existing channels."
        if failed_channels:
            result += f" I couldn't update {len(failed_channels)} channel permission(s)."
        return result

    async def setup_antinuke(self, guild: discord.Guild) -> str:
        bot_member = guild.me
        if bot_member is None or not (
            bot_member.guild_permissions.view_audit_log and bot_member.guild_permissions.kick_members
        ):
            raise SetupFailure("View Audit Log and Kick Members permissions are required.")

        await self.bot.db.execute(
            "INSERT OR IGNORE INTO antinuke_config (guild_id) VALUES (?)",
            guild.id,
        )
        await self.bot.db.execute(
            "UPDATE antinuke_config SET enabled = 0, punishment = 'kick' WHERE guild_id = ?",
            guild.id,
        )
        try:
            async for member in guild.fetch_members(limit=None):
                if member.guild_permissions.administrator or member.id == guild.owner_id:
                    await self.bot.db.execute(
                        "INSERT OR IGNORE INTO antinuke_whitelist (guild_id, user_id) VALUES (?, ?)",
                        guild.id,
                        member.id,
                    )
        except discord.HTTPException as error:
            log.exception("Couldn't fetch administrators to trust for Antinuke in guild %s", guild.id)
            raise SetupFailure("Couldn't load administrators to trust; Antinuke was left disabled.") from error
        await self.bot.db.execute(
            "UPDATE antinuke_config SET enabled = 1 WHERE guild_id = ?",
            guild.id,
        )
        return "Antinuke: enabled with kick punishment; current administrators were added to its trusted list."

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerSetup(bot))
