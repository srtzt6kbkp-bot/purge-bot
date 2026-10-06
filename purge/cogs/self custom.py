from __future__ import annotations

import logging
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("hoodbot.customization")
MAX_PROFILE_IMAGE_SIZE = 10 * 1024 * 1024
MAX_BIO_LENGTH = 4000
DENIED = "Only the server owner or a server administrator can customize the bot."
GLOBAL_DENIED = "Only the bot application owner or an accepted developer-team member can change global bot settings."
OWNER_MANAGEMENT_DENIED = "Only the bot application owner or developer-team owner can manage global access."


async def _send_interaction(
    interaction: discord.Interaction,
    content: str,
    *,
    ephemeral: bool = True,
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(content, ephemeral=ephemeral)


async def _defer_interaction(interaction: discord.Interaction) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)


class BotCustomizationModal(discord.ui.Modal, title="Customize Bot Status"):
    status_text = discord.ui.TextInput(
        label="Activity Text",
        placeholder="e.g., Protecting your server | !help",
        max_length=100,
        required=True
    )

    def __init__(self, cog: "BotCustomization", activity_type: discord.ActivityType) -> None:
        super().__init__()
        self.cog = cog
        self.activity_type = activity_type

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.update_bot_presence(interaction, self.status_text.value, self.activity_type)


class BotCustomizationView(discord.ui.View):
    def __init__(self, cog: "BotCustomization") -> None:
        super().__init__(timeout=180)
        self.cog = cog

    @discord.ui.button(label="Set Playing", style=discord.ButtonStyle.blurple, emoji="🎮")
    async def set_playing(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.cog.check_permissions(interaction):
            return
        await interaction.response.send_modal(BotCustomizationModal(self.cog, discord.ActivityType.playing))

    @discord.ui.button(label="Set Watching", style=discord.ButtonStyle.blurple, emoji="👀")
    async def set_watching(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.cog.check_permissions(interaction):
            return
        await interaction.response.send_modal(BotCustomizationModal(self.cog, discord.ActivityType.watching))

    @discord.ui.button(label="Set Listening", style=discord.ButtonStyle.blurple, emoji="🎧")
    async def set_listening(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.cog.check_permissions(interaction):
            return
        await interaction.response.send_modal(BotCustomizationModal(self.cog, discord.ActivityType.listening))

    @discord.ui.button(label="Reset Status", style=discord.ButtonStyle.red, emoji="🔄")
    async def reset_status(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.cog.check_permissions(interaction):
            return
        await _defer_interaction(interaction)
        await self.cog.bot.change_presence(activity=None)
        await _send_interaction(interaction, "Bot activity status has been reset.")


class BotCustomization(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._application_manager_ids: set[int] = set()
        self._application_owner_ids: set[int] = set()
        application = bot.application
        if application is not None:
            self._application_manager_ids.add(application.owner.id)
            self._application_owner_ids.add(application.owner.id)
            if application.team is not None:
                if application.team.owner_id is not None:
                    self._application_manager_ids.add(application.team.owner_id)
                    self._application_owner_ids.add(application.team.owner_id)
                self._application_manager_ids.update(
                    member.id
                    for member in application.team.members
                    if member.membership_state == discord.TeamMembershipState.accepted
                )

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    async def check_permissions(self, interaction: discord.Interaction) -> bool:
        """Allow application managers and users explicitly approved by the owner."""
        if await self._has_global_access(interaction.user.id):
            return True
        await _send_interaction(interaction, GLOBAL_DENIED)
        return False

    def _is_server_admin(self, ctx: commands.Context) -> bool:
        guild = ctx.guild
        return guild is not None and (
            ctx.author.id == guild.owner_id
            or isinstance(ctx.author, discord.Member)
            and ctx.author.guild_permissions.administrator
        )

    async def _has_global_access(self, user_id: int) -> bool:
        if user_id in self._application_manager_ids:
            return True
        row = await self.bot.db.fetchone(
            "SELECT 1 FROM global_customization_access WHERE user_id = ?", user_id
        )
        return row is not None

    async def check_context_permissions(self, ctx: commands.Context) -> bool:
        """Allow server admins or globally approved users to open the panel."""
        if self._is_server_admin(ctx) or await self._has_global_access(ctx.author.id):
            return True
        if ctx.interaction is not None:
            await _send_interaction(ctx.interaction, DENIED)
        else:
            await ctx.reply(DENIED, mention_author=False)
        return False

    async def check_server_context_permissions(self, ctx: commands.Context) -> bool:
        if self._is_server_admin(ctx):
            return True
        if ctx.interaction is not None:
            await _send_interaction(ctx.interaction, DENIED)
        else:
            await ctx.reply(DENIED, mention_author=False)
        return False

    async def check_application_context_permissions(self, ctx: commands.Context) -> bool:
        if await self._has_global_access(ctx.author.id):
            return True
        if ctx.interaction is not None:
            await _send_interaction(ctx.interaction, GLOBAL_DENIED)
        else:
            await ctx.reply(GLOBAL_DENIED, mention_author=False)
        return False

    async def check_status_context_permissions(self, ctx: commands.Context) -> bool:
        if ctx.author.id in self._application_owner_ids:
            return True
        row = await self.bot.db.fetchone(
            "SELECT 1 FROM global_customization_access WHERE user_id = ?", ctx.author.id
        )
        if row is not None:
            return True
        message = "Only the bot owner or a user explicitly approved for global settings can change online status."
        if ctx.interaction is not None:
            await _send_interaction(ctx.interaction, message)
        else:
            await ctx.reply(message, mention_author=False)
        return False

    async def check_application_owner_context_permissions(self, ctx: commands.Context) -> bool:
        if ctx.author.id in self._application_owner_ids:
            return True
        if ctx.interaction is not None:
            await _send_interaction(ctx.interaction, OWNER_MANAGEMENT_DENIED)
        else:
            await ctx.reply(OWNER_MANAGEMENT_DENIED, mention_author=False)
        return False

    async def send_context(
        self,
        ctx: commands.Context,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
    ) -> None:
        kwargs = {}
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view

        if ctx.interaction is None:
            await ctx.send(**kwargs)
        elif ctx.interaction.response.is_done():
            await ctx.interaction.followup.send(ephemeral=True, **kwargs)
        else:
            await ctx.interaction.response.send_message(ephemeral=True, **kwargs)

    async def defer_context(self, ctx: commands.Context) -> None:
        if ctx.interaction is not None:
            await _defer_interaction(ctx.interaction)

    async def update_profile_image(
        self,
        ctx: commands.Context,
        image: discord.Attachment | None,
        *,
        image_kind: str,
        global_scope: bool = False,
    ) -> None:
        if global_scope:
            if not await self.check_application_context_permissions(ctx):
                return
        elif not await self.check_server_context_permissions(ctx):
            return
        if image is None:
            await self.send_context(
                ctx,
                f"Attach an image to this command message, or choose one for the slash command's {image_kind} upload option.",
            )
            return
        if not image.content_type or not image.content_type.startswith("image/"):
            await self.send_context(ctx, "Please upload a valid image file.")
            return
        if image.size > MAX_PROFILE_IMAGE_SIZE:
            await self.send_context(ctx, "The image must be 10 MiB or smaller.")
            return

        if global_scope:
            profile = self.bot.user
        else:
            profile = ctx.guild.me if ctx.guild is not None else None
        if profile is None:
            await self.send_context(ctx, "I could not find the bot profile to update.")
            return

        await self.defer_context(ctx)
        try:
            image_bytes = await image.read()
            if global_scope:
                await profile.edit(**{image_kind: image_bytes})
                scope = "globally"
            else:
                await profile.edit(**{image_kind: image_bytes}, reason=f"Bot {image_kind} changed by {ctx.author}")
                scope = "for this server only"
            await self.send_context(ctx, f"Updated the bot's {image_kind} {scope}.")
        except discord.HTTPException as exc:
            log.warning("Could not update bot %s (global=%s): %s", image_kind, global_scope, exc)
            scope = "global" if global_scope else "server-specific"
            await self.send_context(ctx, f"Discord could not update the {scope} {image_kind}. Check the image format and try again.")

    async def update_bot_presence(
        self,
        interaction: discord.Interaction,
        text: str,
        activity_type: discord.ActivityType,
    ) -> None:
        if not await self.check_permissions(interaction):
            return

        await _defer_interaction(interaction)
        activity = discord.Activity(type=activity_type, name=text)
        await self.bot.change_presence(activity=activity)
        await _send_interaction(
            interaction,
            f"Updated the bot-wide activity to **{activity_type.name.title()} {text}**.",
        )

    @commands.hybrid_command(name="botstatus")
    @app_commands.describe(status="Set the bot's visible status")
    async def set_visibility(self, ctx: commands.Context, status: Literal["online", "offline"]) -> None:
        """Set the bot online or make it appear offline without stopping it."""
        if not await self.check_status_context_permissions(ctx):
            return

        await self.defer_context(ctx)
        discord_status = discord.Status.invisible if status == "offline" else discord.Status.online
        await self.bot.change_presence(activity=self.bot.activity, status=discord_status)
        if status == "offline":
            message = "The bot now appears offline, but remains running and can still receive commands."
        else:
            message = "The bot is online again."
        await self.send_context(ctx, message)

    @commands.hybrid_group(name="botcustom", aliases=["bconfig"], invoke_without_command=True)
    async def botcustom(self, ctx: commands.Context) -> None:
        """Open the interactive bot customization panel."""
        if not await self.check_context_permissions(ctx):
            return
        has_global_access = await self._has_global_access(ctx.author.id)
        is_server_admin = self._is_server_admin(ctx)
        description = []
        if is_server_admin:
            description.append("Server nickname, avatar, and banner are server-specific.")
        if has_global_access:
            description.append("Activity, bio, and global avatar affect the bot everywhere.")
        embed = discord.Embed(
            title="Bot Customization",
            description=" ".join(description),
            colour=discord.Colour(0x000000),
        )
        if is_server_admin:
            embed.add_field(name="Server profile", value="`botcustom avatar`, `botcustom banner`, `botcustom nickname`.", inline=False)
        if has_global_access:
            embed.add_field(
                name="Global profile",
                value="Activity buttons, `botcustom bio`, `botcustom globalavatar`, and `botstatus online/offline`.",
                inline=False,
            )
        embed.set_footer(text="Global controls are shown only to approved users.")
        view = BotCustomizationView(self) if has_global_access else None
        await self.send_context(ctx, embed=embed, view=view)

    @botcustom.command(name="status")
    async def botcustom_status(self, ctx: commands.Context) -> None:
        """Opens the status customization dashboard."""
        if not await self.check_application_context_permissions(ctx):
            return
        embed = discord.Embed(
            title="Bot Activity",
            description="Choose an activity type and enter the text to show. This changes the bot's activity in every server.",
            colour=discord.Colour(0x000000),
        )
        await self.send_context(ctx, embed=embed, view=BotCustomizationView(self))

    @botcustom.command(name="avatar")
    @app_commands.describe(image="Optional image upload for the bot's avatar in this server")
    async def botcustom_avatar(
        self,
        ctx: commands.Context,
        image: discord.Attachment | None = None,
    ) -> None:
        """Upload an image to change the bot's avatar in this server only."""
        await self.update_profile_image(ctx, image, image_kind="avatar")

    @botcustom.command(name="globalavatar")
    @app_commands.describe(image="Optional image upload for the bot's global avatar")
    async def botcustom_globalavatar(
        self,
        ctx: commands.Context,
        image: discord.Attachment | None = None,
    ) -> None:
        """Upload an image to change the bot's global avatar."""
        await self.update_profile_image(ctx, image, image_kind="avatar", global_scope=True)

    @botcustom.command(name="banner")
    @app_commands.describe(image="Optional image upload for the bot's banner in this server")
    async def botcustom_banner(
        self,
        ctx: commands.Context,
        image: discord.Attachment | None = None,
    ) -> None:
        """Upload an image to change the bot's banner in this server only."""
        await self.update_profile_image(ctx, image, image_kind="banner")

    @botcustom.command(name="bio")
    @app_commands.describe(bio="The bot application's public profile description, or 'reset' to clear it")
    async def botcustom_bio(self, ctx: commands.Context, *, bio: str) -> None:
        """Set the public bot profile bio through its application description."""
        if not await self.check_application_context_permissions(ctx):
            return

        bio = bio.strip()
        description = None if bio.lower() in {"reset", "none"} else bio
        if description is not None and (not description or len(description) > MAX_BIO_LENGTH):
            await self.send_context(ctx, f"The bio must contain 1 to {MAX_BIO_LENGTH} characters. Use `reset` to clear it.")
            return

        await self.defer_context(ctx)
        try:
            application = await self.bot.application_info()
            await application.edit(description=description)
        except discord.HTTPException as exc:
            log.warning("Could not update bot application description: %s", exc)
            await self.send_context(ctx, "Discord could not update the bot bio. Check application permissions and try again.")
            return

        result = "cleared" if description is None else "updated"
        await self.send_context(ctx, f"The bot's global profile bio was {result}.")

    @botcustom.command(name="globalgrant")
    @app_commands.describe(user="User to approve for global bot settings")
    async def botcustom_globalgrant(self, ctx: commands.Context, user: discord.User) -> None:
        """Approve a user to change global bot settings. Application owner only."""
        if not await self.check_application_owner_context_permissions(ctx):
            return
        if user.id in self._application_manager_ids:
            await self.send_context(ctx, f"{user} already has global access through the application team.")
            return
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO global_customization_access (user_id, granted_by) VALUES (?, ?)",
            user.id,
            ctx.author.id,
        )
        await self.send_context(ctx, f"Approved {user} for global bot settings.")

    @botcustom.command(name="globalrevoke")
    @app_commands.describe(user="User to remove from global bot settings")
    async def botcustom_globalrevoke(self, ctx: commands.Context, user: discord.User) -> None:
        """Remove a user's explicit global bot settings approval. Application owner only."""
        if not await self.check_application_owner_context_permissions(ctx):
            return
        if user.id in self._application_manager_ids:
            await self.send_context(ctx, "Application owners and team members keep access through Discord's team settings.")
            return
        await self.bot.db.execute(
            "DELETE FROM global_customization_access WHERE user_id = ?", user.id
        )
        await self.send_context(ctx, f"Removed {user}'s global bot settings approval.")

    @botcustom.command(name="globallist")
    async def botcustom_globallist(self, ctx: commands.Context) -> None:
        """List explicitly approved global settings users. Application owner only."""
        if not await self.check_application_owner_context_permissions(ctx):
            return
        rows = await self.bot.db.fetchall(
            "SELECT user_id FROM global_customization_access ORDER BY user_id"
        )
        approved = "\n".join(f"<@{row['user_id']}> (`{row['user_id']}`)" for row in rows)
        await self.send_context(ctx, "Approved global settings users:\n" + (approved or "None."))

    @botcustom.command(name="nickname")
    @app_commands.describe(name="The new server-specific nickname for the bot")
    async def botcustom_nickname(self, ctx: commands.Context, *, name: str) -> None:
        """Changes the bot's nickname specifically within this server."""
        if not await self.check_server_context_permissions(ctx):
            return

        name = name.strip()
        if name.lower() in {"reset", "none"}:
            nickname = None
        elif not name or len(name) > 32:
            await self.send_context(ctx, "A nickname must contain 1 to 32 characters. Use `reset` to clear it.")
            return
        else:
            nickname = name

        try:
            if ctx.guild is None or ctx.guild.me is None:
                await self.send_context(ctx, "I could not find my member record in this server.")
                return
            await self.defer_context(ctx)
            await ctx.guild.me.edit(nick=nickname, reason=f"Bot nickname changed by {ctx.author}")
        except discord.Forbidden:
            await self.send_context(ctx, "I cannot change my nickname here. Check my role position and permissions.")
            return
        except discord.HTTPException as exc:
            log.warning("Could not change bot nickname in guild %s: %s", ctx.guild.id, exc)
            await self.send_context(ctx, "Discord could not update my nickname. Try again shortly.")
            return

        result = "cleared" if nickname is None else f"set to **{nickname}**"
        await self.send_context(ctx, f"The bot's nickname in this server was {result}.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BotCustomization(bot))