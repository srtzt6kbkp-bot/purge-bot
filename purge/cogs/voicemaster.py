from __future__ import annotations

import logging

import discord
from discord.ext import commands

log = logging.getLogger("hoodbot.voicemaster")


async def _say(interaction: discord.Interaction, text: str) -> None:
    """Reply privately, whether or not we've already acknowledged the click."""
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


class RenameModal(discord.ui.Modal, title="Rename your channel"):
    new_name = discord.ui.TextInput(label="New name", max_length=100, placeholder="e.g. late night grind")

    def __init__(self, cog: "VoiceMaster") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        channel = await self.cog.panel_owned(interaction)
        if channel is None:
            return
        await interaction.response.defer(ephemeral=True)
        await channel.edit(name=str(self.new_name.value)[:100], reason="VoiceMaster rename")
        await _say(interaction, "Renamed.")


class LimitModal(discord.ui.Modal, title="Set a member limit"):
    amount = discord.ui.TextInput(label="Limit (0 = unlimited, max 99)", max_length=2, placeholder="0")

    def __init__(self, cog: "VoiceMaster") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        channel = await self.cog.panel_owned(interaction)
        if channel is None:
            return
        try:
            value = int(str(self.amount.value).strip())
        except ValueError:
            value = -1
        if not 0 <= value <= 99:
            await _say(interaction, "Enter a whole number from 0 to 99.")
            return
        await interaction.response.defer(ephemeral=True)
        await channel.edit(user_limit=value, reason="VoiceMaster limit")
        await _say(interaction, "Limit removed." if value == 0 else f"Limit set to {value}.")


class TransferView(discord.ui.View):
    """Shown privately after pressing Transfer: pick who gets the channel."""

    def __init__(self, cog: "VoiceMaster") -> None:
        super().__init__(timeout=60)
        self.cog = cog

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Pick the new owner", min_values=1, max_values=1)
    async def pick(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        channel = await self.cog.panel_owned(interaction)
        if channel is None:
            return
        target = select.values[0]
        if not isinstance(target, discord.Member) or target.bot or target.id == interaction.user.id:
            await _say(interaction, "Pick another real person who's in your channel.")
            return
        if target.voice is None or target.voice.channel != channel:
            await _say(interaction, "They need to be in your channel first.")
            return
        await interaction.response.defer(ephemeral=True)
        await self.cog._set_owner(channel, target, interaction.user)
        await _say(interaction, f"{target.display_name} owns this channel now.")
        self.stop()


class DisconnectView(discord.ui.View):
    def __init__(self, cog: "VoiceMaster") -> None:
        super().__init__(timeout=60)
        self.cog = cog

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose a member to disconnect", min_values=1, max_values=1)
    async def pick(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        channel = await self.cog.panel_owned(interaction)
        if channel is None:
            return
        member = select.values[0]
        if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel != channel:
            await _say(interaction, "Choose someone who is currently in your voice channel.")
            return
        if member.id == interaction.user.id:
            await _say(interaction, "Use Discord's leave button to disconnect yourself.")
            return
        try:
            await member.move_to(None, reason="Disconnected by VoiceMaster channel owner")
        except discord.HTTPException:
            await _say(interaction, "I couldn't disconnect them. Check my Move Members permission.")
            return
        await _say(interaction, f"Disconnected {member.display_name} from your channel.")
        self.stop()


class ActivityView(discord.ui.View):
    ACTIVITIES = {
        "Watch Together": 880218394199220334,
        "Poker Night": 755827207812677713,
        "Chess in the Park": 832012774040141894,
    }

    def __init__(self, cog: "VoiceMaster") -> None:
        super().__init__(timeout=60)
        self.cog = cog

    @discord.ui.select(
        placeholder="Choose a Discord activity",
        min_values=1,
        max_values=1,
        options=[discord.SelectOption(label=name, value=str(app_id)) for name, app_id in ACTIVITIES.items()],
    )
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        channel = await self.cog.panel_owned(interaction)
        if channel is None:
            return
        try:
            invite = await channel.create_invite(
                target_type=discord.InviteTarget.embedded_application,
                target_application_id=int(select.values[0]),
                max_age=300,
                reason="VoiceMaster activity invite",
            )
        except discord.HTTPException:
            await _say(interaction, "Discord couldn't start that activity here. Check the bot's Create Invite permission.")
            return
        await _say(interaction, f"Activity invite for **{channel.name}**: {invite.url}")
        self.stop()


class ControlPanel(discord.ui.View):
    """The button interface. Persistent: keeps working after bot restarts."""

    def __init__(self, cog: "VoiceMaster") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    async def _perm(self, interaction: discord.Interaction, done: str, **perms) -> None:
        channel = await self.cog.panel_owned(interaction)
        if channel is None:
            return
        await interaction.response.defer(ephemeral=True)
        await channel.set_permissions(interaction.guild.default_role, reason="VoiceMaster panel", **perms)
        await _say(interaction, done)

    async def _nudge(self, interaction: discord.Interaction, delta: int) -> None:
        channel = await self.cog.panel_owned(interaction)
        if channel is None:
            return
        current = channel.user_limit
        if current == 0:
            if delta < 0:
                await _say(interaction, "The channel is already unlimited.")
                return
            new = 1
        else:
            new = current + delta
        if not 0 <= new <= 99:
            await _say(interaction, "The limit has to stay between 0 and 99.")
            return
        await interaction.response.defer(ephemeral=True)
        await channel.edit(user_limit=new, reason="VoiceMaster limit")
        await _say(interaction, "The channel is unlimited." if new == 0 else f"Limit is now {new}.")

    # ----- row 1: access and claim -------------------------------------------

    @discord.ui.button(emoji="\U0001F512", style=discord.ButtonStyle.secondary, custom_id="vm:lock", row=0)
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._perm(interaction, "Channel locked.", connect=False)

    @discord.ui.button(emoji="\U0001F513", style=discord.ButtonStyle.secondary, custom_id="vm:unlock", row=0)
    async def unlock(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._perm(interaction, "Channel unlocked.", connect=None)

    @discord.ui.button(emoji="\U0001F47B", style=discord.ButtonStyle.secondary, custom_id="vm:hide", row=0)
    async def hide(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._perm(interaction, "Channel hidden.", view_channel=False)

    @discord.ui.button(emoji="\U0001F441", style=discord.ButtonStyle.secondary, custom_id="vm:reveal", row=0)
    async def reveal(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._perm(interaction, "Channel visible again.", view_channel=None)

    @discord.ui.button(emoji="\U0001F3A4", style=discord.ButtonStyle.success, custom_id="vm:claim", row=0)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        voice = getattr(interaction.user, "voice", None)
        if voice is None or voice.channel is None:
            await _say(interaction, "Join the channel you want to claim first.")
            return
        channel = voice.channel
        row = await self.cog.bot.db.fetchone(
            "SELECT owner_id FROM voicemaster_channels WHERE channel_id = ?", channel.id
        )
        if row is None:
            await _say(interaction, "That isn't a VoiceMaster channel.")
            return
        if row["owner_id"] == interaction.user.id:
            await _say(interaction, "You already own this channel.")
            return
        old_owner = interaction.guild.get_member(row["owner_id"])
        if old_owner and old_owner.voice and old_owner.voice.channel == channel:
            await _say(interaction, "The owner is still in the channel.")
            return
        await interaction.response.defer(ephemeral=True)
        await self.cog._set_owner(channel, interaction.user, old_owner)
        await _say(interaction, "You own this channel now.")

    # ----- row 2: tools and limit --------------------------------------------

    @discord.ui.button(emoji="\U0001F50C", style=discord.ButtonStyle.secondary, custom_id="vm:disconnect", row=1)
    async def disconnect(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self.cog.panel_owned(interaction) is None:
            return
        await interaction.response.send_message("Choose a member to disconnect:", view=DisconnectView(self.cog), ephemeral=True)

    @discord.ui.button(emoji="\U0001F3AE", style=discord.ButtonStyle.secondary, custom_id="vm:activity", row=1)
    async def activity(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self.cog.panel_owned(interaction) is None:
            return
        await interaction.response.send_message("Choose an activity:", view=ActivityView(self.cog), ephemeral=True)

    @discord.ui.button(emoji="\u2139\uFE0F", style=discord.ButtonStyle.secondary, custom_id="vm:info", row=1)
    async def info(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        channel = await self.cog.panel_owned(interaction)
        if channel is None:
            return
        embed = discord.Embed(
            title=channel.name,
            colour=discord.Colour(0x000000),
            description=(
                f"**Owner:** {interaction.user.mention}\n"
                f"**Members:** {len(channel.members)}\n"
                f"**User limit:** {channel.user_limit or 'Unlimited'}\n"
                f"**Bitrate:** {channel.bitrate // 1000} kbps"
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(emoji="\u2795", style=discord.ButtonStyle.secondary, custom_id="vm:more", row=1)
    async def more(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._nudge(interaction, 1)

    @discord.ui.button(emoji="\u2796", style=discord.ButtonStyle.secondary, custom_id="vm:less", row=1)
    async def less(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._nudge(interaction, -1)


class VoiceMaster(commands.Cog):
    """Join-to-create voice channels. Whoever joins the hub gets their own channel."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._creating: set[int] = set()  # members mid-creation, stops double channels

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    async def cog_load(self) -> None:
        # Registers the buttons so old panels keep working after a bot restart.
        self.bot.add_view(ControlPanel(self))

    # ----- helpers ------------------------------------------------------------

    async def panel_owned(self, interaction: discord.Interaction) -> discord.VoiceChannel | None:
        """The VoiceMaster channel the clicker owns, or None (after telling them why)."""
        voice = getattr(interaction.user, "voice", None)
        if voice is None or voice.channel is None:
            await _say(interaction, "Join your voice channel first.")
            return None
        row = await self.bot.db.fetchone(
            "SELECT owner_id FROM voicemaster_channels WHERE channel_id = ?", voice.channel.id
        )
        if row is None:
            await _say(interaction, "That isn't a VoiceMaster channel.")
            return None
        if row["owner_id"] != interaction.user.id:
            await _say(interaction, "Only the channel owner can use that. If the owner left, press Claim.")
            return None
        return voice.channel

    def _panel_embed(self, guild: discord.Guild) -> discord.Embed:
        embed = discord.Embed(
            title="VoiceMaster Interface",
            colour=discord.Colour(0x000000),
            description=(
                "Use the buttons below to control your voice channel.\n\n"
                "**Button Usage**\n"
                "🔒 — **Lock** the voice channel\n"
                "🔓 — **Unlock** the voice channel\n"
                "👻 — **Ghost** the voice channel\n"
                "👁️ — **Reveal** the voice channel\n"
                "🎤 — **Claim** the voice channel\n"
                "🔌 — **Disconnect** a member\n"
                "🎮 — **Start** an activity\n"
                "ℹ️ — **View** channel information\n"
                "➕ — **Increase** the user limit\n"
                "➖ — **Decrease** the user limit"
            ),
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        return embed

    async def _forget(self, channel_id: int) -> None:
        await self.bot.db.execute("DELETE FROM voicemaster_channels WHERE channel_id = ?", channel_id)

    async def _delete_channel(self, channel: discord.abc.GuildChannel, reason: str = "VoiceMaster cleanup") -> None:
        try:
            await channel.delete(reason=reason)
        except (discord.NotFound, discord.Forbidden):
            pass
        await self._forget(channel.id)

    async def _current(self, ctx: commands.Context):
        """The VoiceMaster channel the author is sitting in, plus its DB row."""
        voice = getattr(ctx.author, "voice", None)
        if voice is None or voice.channel is None:
            await ctx.reply("Join your voice channel first.", mention_author=False)
            return None, None
        row = await self.bot.db.fetchone(
            "SELECT owner_id FROM voicemaster_channels WHERE channel_id = ?", voice.channel.id
        )
        if row is None:
            await ctx.reply("That isn't a VoiceMaster channel.", mention_author=False)
            return None, None
        return voice.channel, row

    async def _owned(self, ctx: commands.Context) -> discord.VoiceChannel | None:
        channel, row = await self._current(ctx)
        if channel is None:
            return None
        if row["owner_id"] != ctx.author.id:
            await ctx.reply(
                "Only the channel owner can do that. If the owner left, use the `claim` command.",
                mention_author=False,
            )
            return None
        return channel

    async def _set_owner(self, channel: discord.VoiceChannel, new: discord.Member, old: discord.Member | None) -> None:
        await self.bot.db.execute(
            "UPDATE voicemaster_channels SET owner_id = ? WHERE channel_id = ?", new.id, channel.id
        )
        await channel.set_permissions(new, view_channel=True, connect=True, reason="VoiceMaster owner change")
        if old is not None:
            await channel.set_permissions(old, overwrite=None, reason="VoiceMaster owner change")

    # ----- lifecycle ----------------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """After a restart, drop tracked channels that vanished or sit empty."""
        rows = await self.bot.db.fetchall("SELECT channel_id FROM voicemaster_channels")
        for row in rows:
            channel = self.bot.get_channel(row["channel_id"])
            if channel is None:
                await self._forget(row["channel_id"])
            elif isinstance(channel, discord.VoiceChannel) and not channel.members:
                await self._delete_channel(channel)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        if before.channel == after.channel:
            return
        if after.channel is not None:
            await self._handle_join(member, after.channel)
        if before.channel is not None:
            await self._handle_leave(before.channel)

    async def _handle_join(self, member: discord.Member, channel: discord.abc.GuildChannel) -> None:
        config_row = await self.bot.db.fetchone(
            "SELECT hub_channel_id, category_id FROM voicemaster_config WHERE guild_id = ?", member.guild.id
        )
        if config_row is None or channel.id != config_row["hub_channel_id"]:
            return
        if member.id in self._creating:
            return

        self._creating.add(member.id)
        try:
            category = member.guild.get_channel(config_row["category_id"])
            if not isinstance(category, discord.CategoryChannel):
                category = channel.category

            overwrites = dict(category.overwrites) if category else {}
            overwrites[member] = discord.PermissionOverwrite(view_channel=True, connect=True)

            new_channel = await member.guild.create_voice_channel(
                name=f"{member.display_name}'s channel"[:100],
                category=category,
                overwrites=overwrites,
                reason="VoiceMaster channel created",
            )
            await self.bot.db.execute(
                "INSERT INTO voicemaster_channels (channel_id, guild_id, owner_id) VALUES (?, ?, ?)",
                new_channel.id, member.guild.id, member.id,
            )
            try:
                await member.move_to(new_channel, reason="VoiceMaster")
            except discord.HTTPException:
                # They left voice before the move (or I can't move them): don't leave an orphan.
                await self._delete_channel(new_channel)
        except discord.HTTPException:
            log.exception("Could not create a VoiceMaster channel in guild %s", member.guild.id)
        finally:
            self._creating.discard(member.id)

    async def _handle_leave(self, channel: discord.abc.GuildChannel) -> None:
        row = await self.bot.db.fetchone(
            "SELECT 1 FROM voicemaster_channels WHERE channel_id = ?", channel.id
        )
        if row is not None and isinstance(channel, discord.VoiceChannel) and not channel.members:
            await self._delete_channel(channel, reason="VoiceMaster channel empty")

    # ----- commands -----------------------------------------------------------

    @commands.hybrid_group(name="voicemaster", aliases=["vm"], invoke_without_command=True)
    async def voicemaster(self, ctx: commands.Context) -> None:
        """Control your temporary voice channel."""
        await ctx.send_help(ctx.command)

    @voicemaster.command(name="setup")
    @commands.has_guild_permissions(manage_guild=True)
    @commands.bot_has_guild_permissions(manage_channels=True, move_members=True)
    async def vm_setup(self, ctx: commands.Context) -> None:
        """Create the Join-to-Create hub."""
        category = await ctx.guild.create_category("Voice Master", reason="VoiceMaster setup")
        hub = await ctx.guild.create_voice_channel("Join to Create", category=category, reason="VoiceMaster setup")
        interface = await ctx.guild.create_text_channel(
            "interface",
            category=category,
            overwrites={
                ctx.guild.default_role: discord.PermissionOverwrite(send_messages=False),
                ctx.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True),
            },
            reason="VoiceMaster setup",
        )
        await interface.send(embed=self._panel_embed(ctx.guild), view=ControlPanel(self))
        await self.bot.db.execute(
            """
            INSERT INTO voicemaster_config (guild_id, hub_channel_id, category_id) VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                hub_channel_id = excluded.hub_channel_id,
                category_id    = excluded.category_id
            """,
            ctx.guild.id, hub.id, category.id,
        )
        await ctx.reply(
            f"All set. Join {hub.mention} to get your own channel. Controls are in {interface.mention}.",
            mention_author=False,
        )

    @voicemaster.command(name="panel")
    @commands.has_guild_permissions(manage_guild=True)
    async def vm_panel(self, ctx: commands.Context) -> None:
        """Post the control panel buttons in this channel."""
        await ctx.channel.send(embed=self._panel_embed(ctx.guild), view=ControlPanel(self))
        await ctx.reply("Panel posted.", mention_author=False, ephemeral=True)

    @voicemaster.command(name="lock")
    async def vm_lock(self, ctx: commands.Context) -> None:
        """Stop new people from joining your channel."""
        channel = await self._owned(ctx)
        if channel:
            await channel.set_permissions(ctx.guild.default_role, connect=False, reason="VoiceMaster lock")
            await ctx.reply("Channel locked.", mention_author=False)

    @voicemaster.command(name="unlock")
    async def vm_unlock(self, ctx: commands.Context) -> None:
        """Let people join your channel again."""
        channel = await self._owned(ctx)
        if channel:
            await channel.set_permissions(ctx.guild.default_role, connect=None, reason="VoiceMaster unlock")
            await ctx.reply("Channel unlocked.", mention_author=False)

    @voicemaster.command(name="hide")
    async def vm_hide(self, ctx: commands.Context) -> None:
        """Hide your channel from the channel list."""
        channel = await self._owned(ctx)
        if channel:
            await channel.set_permissions(ctx.guild.default_role, view_channel=False, reason="VoiceMaster hide")
            await ctx.reply("Channel hidden.", mention_author=False)

    @voicemaster.command(name="reveal")
    async def vm_reveal(self, ctx: commands.Context) -> None:
        """Make your channel visible again."""
        channel = await self._owned(ctx)
        if channel:
            await channel.set_permissions(ctx.guild.default_role, view_channel=None, reason="VoiceMaster reveal")
            await ctx.reply("Channel visible.", mention_author=False)

    @voicemaster.command(name="limit")
    async def vm_limit(self, ctx: commands.Context, amount: commands.Range[int, 0, 99]) -> None:
        """Set a user limit (0 = unlimited)."""
        channel = await self._owned(ctx)
        if channel:
            await channel.edit(user_limit=amount, reason="VoiceMaster limit")
            await ctx.reply("Limit removed." if amount == 0 else f"Limit set to {amount}.", mention_author=False)

    @voicemaster.command(name="rename")
    async def vm_rename(self, ctx: commands.Context, *, name: str) -> None:
        """Rename your channel."""
        channel = await self._owned(ctx)
        if channel:
            await channel.edit(name=name[:100], reason="VoiceMaster rename")
            await ctx.reply("Renamed.", mention_author=False)

    @voicemaster.command(name="permit")
    async def vm_permit(self, ctx: commands.Context, member: discord.Member) -> None:
        """Let someone in, even when the channel is locked."""
        channel = await self._owned(ctx)
        if channel:
            await channel.set_permissions(member, view_channel=True, connect=True, reason="VoiceMaster permit")
            await ctx.reply(f"{member.display_name} can join now.", mention_author=False)

    @voicemaster.command(name="reject")
    async def vm_reject(self, ctx: commands.Context, member: discord.Member) -> None:
        """Kick someone out and block them from rejoining."""
        channel = await self._owned(ctx)
        if channel is None:
            return
        if member.id == ctx.author.id:
            await ctx.reply("You can't reject yourself.", mention_author=False)
            return
        await channel.set_permissions(member, connect=False, reason="VoiceMaster reject")
        if member.voice and member.voice.channel == channel:
            await member.move_to(None, reason="VoiceMaster reject")
        await ctx.reply(f"{member.display_name} is blocked from this channel.", mention_author=False)

    @voicemaster.command(name="claim")
    async def vm_claim(self, ctx: commands.Context) -> None:
        """Take over a channel whose owner has left."""
        channel, row = await self._current(ctx)
        if channel is None:
            return
        if row["owner_id"] == ctx.author.id:
            await ctx.reply("You already own this channel.", mention_author=False)
            return
        old_owner = ctx.guild.get_member(row["owner_id"])
        if old_owner and old_owner.voice and old_owner.voice.channel == channel:
            await ctx.reply("The owner is still in the channel.", mention_author=False)
            return
        await self._set_owner(channel, ctx.author, old_owner)
        await ctx.reply("You own this channel now.", mention_author=False)

    @voicemaster.command(name="transfer")
    async def vm_transfer(self, ctx: commands.Context, member: discord.Member) -> None:
        """Hand your channel to someone who's in it."""
        channel = await self._owned(ctx)
        if channel is None:
            return
        if member.bot or member.id == ctx.author.id:
            await ctx.reply("Pick another real person.", mention_author=False)
            return
        if member.voice is None or member.voice.channel != channel:
            await ctx.reply("They need to be in your channel first.", mention_author=False)
            return
        await self._set_owner(channel, member, ctx.author)
        await ctx.reply(f"{member.display_name} owns this channel now.", mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceMaster(bot))
