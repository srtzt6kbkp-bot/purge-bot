from __future__ import annotations

import logging
import time

import discord
from discord.ext import commands

log = logging.getLogger("hoodbot.dm")


class BroadcastConfirmation(discord.ui.View):
    def __init__(self, user_id: int) -> None:
        super().__init__(timeout=60)
        self.user_id = user_id
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the administrator who started this broadcast can confirm it.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Send DMs", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = True
        button.disabled = True
        await interaction.response.edit_message(content="Sending DMs...", embed=None, view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        button.disabled = True
        await interaction.response.edit_message(content="Broadcast cancelled. No DMs were sent.", embed=None, view=None)
        self.stop()


class DM(commands.Cog):
    """Owner- and explicitly granted-user direct message commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._last_broadcast: dict[int, float] = {}

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()

        application = self.bot.application
        if application is None:
            application = await self.bot.application_info()
        if ctx.author.id == application.owner.id:
            return True

        row = await self.bot.db.fetchone(
            "SELECT 1 FROM global_customization_access WHERE user_id = ?",
            ctx.author.id,
        )
        if row is not None:
            return True

        message = "Only the bot owner or a user explicitly granted global access can use these commands."
        if ctx.interaction is not None:
            if not ctx.interaction.response.is_done():
                await ctx.interaction.response.send_message(message, ephemeral=True)
        else:
            await ctx.reply(message, mention_author=False)
        return False

    @commands.hybrid_group(name="dm", invoke_without_command=True)
    @commands.guild_only()
    async def dm(self, ctx: commands.Context) -> None:
        """Show the available DM commands."""
        await self._send_command_list(ctx)

    @dm.command(name="list")
    @commands.guild_only()
    async def dm_list(self, ctx: commands.Context) -> None:
        """List the available DM commands."""
        await self._send_command_list(ctx)

    @staticmethod
    async def _send_command_list(ctx: commands.Context) -> None:
        await DM._reply(
            ctx,
            "`dm all message` — send a confirmed DM to all human server members.\n"
            "`dm role @role message` — send a confirmed DM to human members with a role.\n"
            "`dm user @member message` — DM one member directly.",
            ephemeral=ctx.interaction is not None,
        )

    @dm.command(name="all")
    @commands.guild_only()
    async def dm_all(self, ctx: commands.Context, *, message: str) -> None:
        """Send a confirmed direct message to every human server member."""
        remaining = self._broadcast_cooldown_remaining(ctx.guild.id)
        if remaining > 0:
            await self._reply(
                ctx,
                f"Please wait **{int(remaining) + 1}** seconds before starting another DM broadcast.",
                ephemeral=ctx.interaction is not None,
            )
            return

        message = message.strip()
        if not message:
            await self._reply(ctx, "Write a message to send.", ephemeral=ctx.interaction is not None)
            return
        if len(message) > 3500:
            await self._reply(ctx, "Keep broadcast messages to 3,500 characters or fewer.", ephemeral=ctx.interaction is not None)
            return

        await self._reply(
            ctx,
            "I'm loading the server members to count recipients before asking for confirmation.",
            ephemeral=ctx.interaction is not None,
        )
        try:
            members = [member async for member in ctx.guild.fetch_members(limit=None) if not member.bot]
        except discord.HTTPException as error:
            log.error("Couldn't fetch members for DM-all in guild %s: %s", ctx.guild.id, error)
            await self._reply(
                ctx,
                "I couldn't load the server's members. Check that the Server Members Intent is enabled.",
                ephemeral=ctx.interaction is not None,
            )
            return

        await self._send_broadcast(ctx, members, message, "all human server members")

    @dm.command(name="role")
    @commands.guild_only()
    async def dm_role(self, ctx: commands.Context, role: discord.Role, *, message: str) -> None:
        """Send a confirmed direct message to every human member with a role."""
        remaining = self._broadcast_cooldown_remaining(ctx.guild.id)
        if remaining > 0:
            await self._reply(
                ctx,
                f"Please wait **{int(remaining) + 1}** seconds before starting another DM broadcast.",
                ephemeral=ctx.interaction is not None,
            )
            return

        message = message.strip()
        if not message:
            await self._reply(ctx, "Write a message to send.", ephemeral=ctx.interaction is not None)
            return
        if len(message) > 3500:
            await self._reply(ctx, "Keep broadcast messages to 3,500 characters or fewer.", ephemeral=ctx.interaction is not None)
            return

        await self._reply(
            ctx,
            "I'm loading the server members to count recipients before asking for confirmation.",
            ephemeral=ctx.interaction is not None,
        )
        try:
            members = [
                member
                async for member in ctx.guild.fetch_members(limit=None)
                if not member.bot and any(member_role.id == role.id for member_role in member.roles)
            ]
        except discord.HTTPException as error:
            log.error("Couldn't fetch members for role DM in guild %s: %s", ctx.guild.id, error)
            await self._reply(
                ctx,
                "I couldn't load the server's members. Check that the Server Members Intent is enabled.",
                ephemeral=ctx.interaction is not None,
            )
            return

        if not members:
            await self._reply(
                ctx,
                f"There are no human members with {role.mention}. No DMs were sent.",
                ephemeral=ctx.interaction is not None,
            )
            return

        await self._send_broadcast(ctx, members, message, f"members with {role.mention}")

    def _broadcast_cooldown_remaining(self, guild_id: int) -> float:
        return 60 - (time.monotonic() - self._last_broadcast.get(guild_id, 0))

    async def _send_broadcast(
        self,
        ctx: commands.Context,
        members: list[discord.Member],
        message: str,
        audience: str,
    ) -> None:
        if not members:
            await self._reply(
                ctx,
                f"There are no human members in {audience}. No DMs were sent.",
                ephemeral=ctx.interaction is not None,
            )
            return

        confirmation = BroadcastConfirmation(ctx.author.id)
        embed = discord.Embed(
            title="Confirm DM broadcast",
            description=(
                f"Send this message to **{len(members)}** {audience}?\n\n"
                f"> {message}"
            ),
            colour=discord.Colour.orange(),
        )
        await self._reply(
            ctx,
            "",
            embed=embed,
            view=confirmation,
            ephemeral=ctx.interaction is not None,
        )
        timed_out = await confirmation.wait()
        if not confirmation.confirmed:
            if timed_out:
                await self._reply(
                    ctx,
                    "Broadcast confirmation expired. No DMs were sent.",
                    ephemeral=ctx.interaction is not None,
                )
            return

        remaining = self._broadcast_cooldown_remaining(ctx.guild.id)
        if remaining > 0:
            await self._reply(
                ctx,
                f"Another broadcast has just started. Try again in **{int(remaining) + 1}** seconds.",
                ephemeral=ctx.interaction is not None,
            )
            return

        self._last_broadcast[ctx.guild.id] = time.monotonic()
        dm_embed = discord.Embed(
            title=f"Message from {ctx.guild.name}",
            description=message,
            colour=discord.Colour.blurple(),
        )
        sent = 0
        failed = 0
        for member in members:
            try:
                await member.send(embed=dm_embed, allowed_mentions=discord.AllowedMentions.none())
                sent += 1
            except discord.Forbidden:
                failed += 1
            except discord.HTTPException as error:
                failed += 1
                log.warning("Couldn't send broadcast DM to member %s in guild %s: %s", member.id, ctx.guild.id, error)

        await self._reply(
            ctx,
            f"DM broadcast finished: sent **{sent}**, couldn't deliver **{failed}**.",
            ephemeral=ctx.interaction is not None,
        )

    @dm.command(name="user")
    @commands.guild_only()
    async def dm_user(self, ctx: commands.Context, member: discord.Member, *, message: str) -> None:
        """Send a direct message to one server member."""
        message = message.strip()
        if not message:
            await self._reply(ctx, "Write a message to send.", ephemeral=ctx.interaction is not None)
            return
        if len(message) > 4096:
            await self._reply(ctx, "Keep the message to 4,096 characters or fewer.", ephemeral=ctx.interaction is not None)
            return

        embed = discord.Embed(
            title=f"Message from {ctx.guild.name}",
            description=message,
            colour=discord.Colour.blurple(),
        )
        try:
            await member.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            await self._reply(
                ctx,
                f"I couldn't DM {member.mention}; they may have DMs disabled.",
                ephemeral=ctx.interaction is not None,
            )
            return
        except discord.HTTPException as error:
            log.error("Couldn't DM member %s in guild %s: %s", member.id, ctx.guild.id, error)
            await self._reply(ctx, "Discord couldn't deliver that DM.", ephemeral=ctx.interaction is not None)
            return

        await self._reply(ctx, f"DM sent to {member.mention}.", ephemeral=ctx.interaction is not None)

    @staticmethod
    async def _reply(
        ctx: commands.Context,
        text: str,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        ephemeral: bool = False,
    ) -> None:
        await ctx.send(
            content=text or None,
            embed=embed,
            view=view,
            ephemeral=ephemeral,
            mention_author=False,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DM(bot))
