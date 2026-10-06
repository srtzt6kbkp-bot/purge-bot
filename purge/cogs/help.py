from __future__ import annotations

import types
import typing
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config

# Cog name -> label shown in the footer ("Module: Security"). Add yours here.
MODULES = {
    "Antinuke": "Security",
    "VoiceMaster": "VoiceMaster",
    "BoosterRoles": "Boosters",
    "Settings": "Config",
    "Help": "Info",
    "ServerSetup": "Setup",
    "Moderation": "Moderation",
    "AutoMod": "AutoMod",
    "BotCustomization": "Bot Customization",
    "Customization": "Customization",
    "AuditLogs": "Audit Logs",
    "RoleAccess": "Topfloor",
    "Giveaways": "Giveaways",
    "Snipe": "Sniping",
    "AutoResponder": "Auto Responder",
}
MODULE_PREFIXES = {
    "BoosterRoles": "br",
}

# What shows under "Information". Exact command names win, then the cog default.
INFO_BY_COMMAND = {
    "voicemaster setup": "\u26A0\uFE0F Requires Manage Server",
    "voicemaster panel": "\u26A0\uFE0F Requires Manage Server",
    "voicemaster claim": "Anyone in a channel whose owner left",
    "boosterrole create": "Server boosters only",
    "boosterrole base": "\u26A0\uFE0F Requires Manage Server",
    "boosterrole filter": "\u26A0\uFE0F Requires Manage Server",
    "prefix set": "\u26A0\uFE0F Requires Manage Server",
    "autoresponder add": "\u26A0\uFE0F Requires Manage Server",
    "autoresponder remove": "\u26A0\uFE0F Requires Manage Server",
    "autoresponder list": "\u26A0\uFE0F Requires Manage Server",
    "autoreact add": "\u26A0\uFE0F Requires Manage Server",
    "autoreact remove": "\u26A0\uFE0F Requires Manage Server",
    "autoreact list": "\u26A0\uFE0F Requires Manage Server",
}
INFO_BY_COG = {
    "Antinuke": "\u26A0\uFE0F Antinuke owner (server owner only)",
    "VoiceMaster": "Voice channel owners",
    "BoosterRoles": "Booster role owners",
    "AutoResponder": "\u26A0\uFE0F Requires Manage Server",
}

EMBED_COLOUR = discord.Colour(0x000000)
OWNER_ONLY_COMMANDS = {
    "botcustom globalgrant",
    "botcustom globalrevoke",
    "botcustom globallist",
}
OWNER_OR_GRANT_COMMANDS = {"botstatus"}
OWNER_OR_GRANT_COMMAND_PREFIXES = {"dm"}
GLOBAL_ACCESS_COMMANDS = {
    "botcustom status",
    "botcustom globalavatar",
    "botcustom bio",
    "invitekey generate",
}


# ----- helpers ---------------------------------------------------------------

def _flatten(command: commands.Command):
    """A command followed by all of its subcommands (alphabetical, depth first)."""
    yield command
    if isinstance(command, commands.Group):
        for sub in sorted(command.commands, key=lambda c: c.name):
            yield from _flatten(sub)


def _example_value(name: str, annotation) -> str:
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return str(typing.get_args(annotation)[0])
    if origin in (typing.Union, getattr(types, "UnionType", typing.Union)):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        return _example_value(name, args[0]) if args else "text"
    if isinstance(annotation, type):
        if issubclass(annotation, (discord.Member, discord.User)):
            return "@user"
        if issubclass(annotation, discord.Role):
            return "@role"
        if issubclass(annotation, discord.abc.GuildChannel):
            return "#channel"
        if issubclass(annotation, discord.Attachment):
            return "(attach a file)"
        if issubclass(annotation, discord.Colour):
            return "#ff66aa"
    hints = {
        "amount": "5", "limit": "5", "seconds": "60",
        "color": "#ff66aa", "colour": "#ff66aa",
        "member": "@user", "user": "@user", "role": "@role", "channel": "#channel",
        "word": "word", "name": "cool name", "new_prefix": "!",
    }
    return hints.get(name, "text")


def _module_of(command: commands.Command) -> str:
    cog = command.cog_name
    return MODULES.get(cog, cog or "Misc")


def _info_of(command: commands.Command) -> str:
    return (
        INFO_BY_COMMAND.get(command.qualified_name)
        or INFO_BY_COG.get(command.cog_name or "")
        or "No special permissions"
    )


# ----- the menu --------------------------------------------------------------

class ModuleSelect(discord.ui.Select):
    def __init__(self, menu: "HelpMenu") -> None:
        options = [discord.SelectOption(label="Home", value="home", emoji="\U0001F3E0")]
        options.extend(
            discord.SelectOption(
                label=name[:100],
                value=str(index),
                description=f"{len(entries)} commands",
            )
            for index, (name, entries) in enumerate(menu.sections)
        )
        super().__init__(placeholder="Choose a command module", min_values=1, max_values=1, options=options)
        self.menu = menu

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        self.menu.selected = None if value == "home" else int(value)
        await interaction.response.edit_message(embed=self.menu.embed(), view=self.menu)


class HelpMenu(discord.ui.View):
    def __init__(
        self,
        helper: "FancyHelp",
        ctx: commands.Context,
        sections: list[tuple[str, list[commands.Command]]],
    ) -> None:
        super().__init__(timeout=180)
        self.helper = helper
        self.ctx = ctx
        self.sections = sections
        self.selected: int | None = None
        self.message: discord.Message | None = None
        self.add_item(ModuleSelect(self))

    def embed(self) -> discord.Embed:
        if self.selected is None:
            return self.helper.build_home_embed(self.sections)
        name, entries = self.sections[self.selected]
        return self.helper.build_module_embed(name, entries)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "That's not your help menu. Run the help command yourself.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(emoji="\U0001F6AB", style=discord.ButtonStyle.danger)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        self.stop()
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass


# ----- the help command ------------------------------------------------------

class FancyHelp(commands.HelpCommand):
    def __init__(self) -> None:
        super().__init__(
            command_attrs={"help": "Browse every command. Try: help <command>", "aliases": ["h", "commands"]}
        )

    def build_home_embed(self, sections: list[tuple[str, list[commands.Command]]]) -> discord.Embed:
        ctx = self.context
        bot_user = ctx.bot.user
        total_commands = sum(len(entries) for _, entries in sections)
        application = ctx.bot.application
        description = (
            application.description.strip()
            if application is not None and application.description
            else "Browse commands by module using the menu below."
        )
        embed = discord.Embed(
            title=f"{bot_user.name} Help" if bot_user else "Bot Help",
            description=description,
            colour=EMBED_COLOUR,
        )
        if bot_user is not None:
            embed.set_thumbnail(url=bot_user.display_avatar.url)
            if bot_user.banner is not None:
                embed.set_image(url=bot_user.banner.url)
        embed.add_field(name="Prefix", value=f"`{ctx.clean_prefix}`", inline=True)
        embed.add_field(name="Commands", value=f"`{total_commands:,}`", inline=True)
        embed.add_field(name="Modules", value=f"`{len(sections)}`", inline=True)
        embed.set_footer(text=f"Use {ctx.clean_prefix}help <command> for command details.")
        return embed

    def build_module_embed(self, name: str, entries: list[commands.Command]) -> discord.Embed:
        ctx = self.context
        prefix = ctx.clean_prefix
        lines = []
        for command in entries:
            command_name = command.qualified_name
            short_prefix = MODULE_PREFIXES.get(command.cog_name or "")
            if short_prefix and command_name.startswith("boosterrole"):
                command_name = short_prefix + command_name[len("boosterrole"):]
            params = " ".join(
                f"<{param_name}>" if parameter.required else f"[{param_name}]"
                for param_name, parameter in command.clean_params.items()
            )
            usage = f"{prefix}{command_name} {params}".rstrip()
            description = (command.short_doc or command.help or "No description.").splitlines()[0]
            if len(description) > 100:
                description = description[:97].rstrip() + "..."
            lines.append(f"`{usage}` - {description}")

        embed = discord.Embed(
            title=f"{name} Commands",
            description="\n".join(lines) or "No commands in this module.",
            colour=EMBED_COLOUR,
        )
        embed.set_footer(text=f"Use {prefix}help <command> for details.")
        return embed

    def build_embed(self, command: commands.Command) -> discord.Embed:
        ctx = self.context
        prefix = ctx.clean_prefix
        text = command.help or "No description."
        quoted = "\n".join(f"> {line}" for line in text.strip().splitlines())
        embed = discord.Embed(title=command.qualified_name, description=quoted, colour=EMBED_COLOUR)
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)

        params = command.clean_params
        param_text = (
            ", ".join(f"`{n}`" if p.required else f"`{n}` (optional)" for n, p in params.items()) or "n/a"
        )
        embed.add_field(name="Aliases", value=", ".join(command.aliases) or "n/a", inline=False)
        embed.add_field(name="Parameters", value=param_text, inline=False)
        embed.add_field(name="Information", value=_info_of(command), inline=False)

        name = command.qualified_name
        if isinstance(command, commands.Group) and command.commands:
            subs = " | ".join(sorted(c.name for c in command.commands))
            syntax = f"{prefix}{name} ({subs})"
            example = f"{prefix}{name}"
        else:
            sig = " ".join(f"<{n}>" if p.required else f"[{n}]" for n, p in params.items())
            syntax = f"{prefix}{name} {sig}".rstrip()
            args = " ".join(_example_value(n, p.annotation) for n, p in params.items() if p.required)
            example = f"{prefix}{name} {args}".rstrip()
        embed.add_field(name="Usage", value=f"```\nSyntax: {syntax}\nExample: {example}\n```", inline=False)

        embed.set_footer(text=f"Module: {_module_of(command)}")
        return embed

    async def _show_modules(self, sections: list[tuple[str, list[commands.Command]]]) -> None:
        if not sections:
            await self.get_destination().send("There's nothing to show.")
            return
        menu = HelpMenu(self, self.context, sections)
        menu.message = await self.get_destination().send(embed=menu.embed(), view=menu)

    async def _viewer_access(self) -> tuple[set[int], set[int], bool]:
        application = self.context.bot.application
        owner_ids: set[int] = set()
        manager_ids: set[int] = set()
        if application is not None:
            owner_ids.add(application.owner.id)
            manager_ids.add(application.owner.id)
            if application.team is not None:
                if application.team.owner_id is not None:
                    owner_ids.add(application.team.owner_id)
                    manager_ids.add(application.team.owner_id)
                manager_ids.update(
                    member.id
                    for member in application.team.members
                    if member.membership_state == discord.TeamMembershipState.accepted
                )

        db = getattr(self.context.bot, "db", None)
        granted = False
        self._topfloor_role_id = config.TOPFLOOR_ROLE_ID
        self._topfloor_setup_available = False
        guild = self.context.guild
        if db is not None and guild is not None:
            role_config = await db.fetchone(
                "SELECT topfloor_role_id FROM role_access_config WHERE guild_id = ?",
                guild.id,
            )
            if role_config is not None:
                self._topfloor_role_id = role_config["topfloor_role_id"]
                self._topfloor_setup_available = guild.get_role(self._topfloor_role_id) is None
            elif isinstance(self.context.author, discord.Member):
                self._topfloor_setup_available = self.context.author.guild_permissions.administrator
        if db is not None:
            row = await db.fetchone(
                "SELECT 1 FROM global_customization_access WHERE user_id = ?",
                self.context.author.id,
            )
            granted = row is not None
        return owner_ids, manager_ids, granted

    def _can_view_command(
        self,
        command: commands.Command,
        viewer_id: int,
        owner_ids: set[int],
        manager_ids: set[int],
        granted: bool,
    ) -> bool:
        qualified_name = command.qualified_name
        if qualified_name == "topfloor" or qualified_name.startswith("topfloor "):
            author = self.context.author
            is_admin = (
                isinstance(author, discord.Member)
                and author.guild_permissions.administrator
            )
            if qualified_name == "topfloor setup":
                return self._topfloor_setup_available and is_admin
            has_topfloor = isinstance(author, discord.Member) and any(
                role.id == self._topfloor_role_id for role in author.roles
            )
            if not has_topfloor:
                return False
        if qualified_name in OWNER_ONLY_COMMANDS:
            return viewer_id in owner_ids
        if qualified_name in OWNER_OR_GRANT_COMMANDS:
            return viewer_id in owner_ids or granted
        if any(
            qualified_name == prefix or qualified_name.startswith(f"{prefix} ")
            for prefix in OWNER_OR_GRANT_COMMAND_PREFIXES
        ):
            return viewer_id in owner_ids or granted
        if qualified_name in GLOBAL_ACCESS_COMMANDS:
            return viewer_id in manager_ids or granted
        return True

    def _visible_commands(
        self,
        commands_to_check,
        owner_ids: set[int],
        manager_ids: set[int],
        granted: bool,
    ) -> list[commands.Command]:
        entries: list[commands.Command] = []
        for command in sorted((c for c in commands_to_check if not c.hidden), key=lambda c: c.name):
            entries.extend(
                child
                for child in _flatten(command)
                if not child.hidden
                and self._can_view_command(
                    child,
                    self.context.author.id,
                    owner_ids,
                    manager_ids,
                    granted,
                )
            )
        return entries

    async def send_bot_help(self, mapping) -> None:
        owner_ids, manager_ids, granted = await self._viewer_access()
        sections = []
        for cog, cog_commands in mapping.items():
            entries = self._visible_commands(cog_commands, owner_ids, manager_ids, granted)
            if not entries:
                continue
            name = MODULES.get(cog.qualified_name, cog.qualified_name) if cog else "Other"
            sections.append((name, entries))
        sections.sort(key=lambda section: section[0].casefold())
        await self._show_modules(sections)

    async def send_cog_help(self, cog: commands.Cog) -> None:
        owner_ids, manager_ids, granted = await self._viewer_access()
        name = MODULES.get(cog.qualified_name, cog.qualified_name)
        entries = self._visible_commands(cog.get_commands(), owner_ids, manager_ids, granted)
        await self._show_modules([(name, entries)] if entries else [])

    async def send_group_help(self, group: commands.Group) -> None:
        owner_ids, manager_ids, granted = await self._viewer_access()
        name = group.qualified_name.title()
        entries = [
            command
            for command in _flatten(group)
            if not command.hidden
            and self._can_view_command(command, self.context.author.id, owner_ids, manager_ids, granted)
        ]
        await self._show_modules([(name, entries)] if entries else [])

    async def send_command_help(self, command: commands.Command) -> None:
        owner_ids, manager_ids, granted = await self._viewer_access()
        if not self._can_view_command(command, self.context.author.id, owner_ids, manager_ids, granted):
            await self.get_destination().send("That command is not available to you.")
            return
        await self.get_destination().send(embed=self.build_embed(command))


class Help(commands.Cog):
    """Replaces the default help with the paginated menu."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._original = bot.help_command
        bot.help_command = FancyHelp()
        bot.help_command.cog = self

    @commands.hybrid_command(name="serverinfo", aliases=["si"])
    async def serverinfo(self, ctx: commands.Context) -> None:
        """Show information about this server."""
        guild = ctx.guild
        if guild is None:
            await ctx.reply("This command only works in a server.", mention_author=False)
            return

        embed = discord.Embed(
            title=guild.name,
            colour=EMBED_COLOUR,
        )
        if guild.description:
            embed.description = guild.description[:4096]
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        owner = guild.owner
        owner_name = getattr(owner, "display_name", None) or getattr(owner, "name", None) or str(guild.owner_id)
        embed.set_author(name=f"Owned by {owner_name} — {guild.owner_id}",
                         icon_url=owner.display_avatar.url if owner is not None else None)

        created = int(guild.created_at.timestamp())
        embed.add_field(name="Created", value=f"<t:{created}:F> (<t:{created}:R>)", inline=False)

        stickers = len(guild.stickers)
        emojis = len(guild.emojis)
        roles = max(0, len(guild.roles) - 1)
        total_counts = stickers + emojis + roles
        embed.add_field(
            name=f"Counts ({total_counts})",
            value=f"Stickers: `{stickers}`\nEmojis: `{emojis}`\nRoles: `{roles}`",
            inline=True,
        )

        text_channels = sum(
            isinstance(channel, (discord.TextChannel, discord.ForumChannel))
            for channel in guild.channels
        )
        voice_channels = sum(
            isinstance(channel, (discord.VoiceChannel, discord.StageChannel))
            for channel in guild.channels
        )
        categories = sum(isinstance(channel, discord.CategoryChannel) for channel in guild.channels)
        total_channels = text_channels + voice_channels + categories
        embed.add_field(
            name=f"Channels ({total_channels})",
            value=f"Categories: `{categories}`\nText: `{text_channels}`\nVoice: `{voice_channels}`",
            inline=True,
        )

        member_count = guild.member_count if guild.member_count is not None else len(guild.members)
        boost_count = guild.premium_subscription_count or 0
        embed.add_field(
            name=f"Members ({member_count})",
            value=f"Total: `{member_count}`\nBoosters: `{boost_count}`",
            inline=True,
        )
        embed.add_field(
            name=f"Boosts ({boost_count})",
            value=f"Level: `{guild.premium_tier}`\nBoosts: `{boost_count}`",
            inline=True,
        )

        design_links = []
        for label, asset in (("Icon", guild.icon), ("Banner", guild.banner), ("Splash", guild.splash)):
            design_links.append(f"{label}: [view]({asset.url})" if asset else f"{label}: `none`")
        embed.add_field(name="Design", value="\n".join(design_links), inline=True)

        verification = getattr(guild.verification_level, "name", str(guild.verification_level)).lower()
        mfa_level = getattr(guild.mfa_level, "name", str(guild.mfa_level)).lower()
        vanity = guild.vanity_url_code or "none"
        embed.add_field(
            name="System",
            value=f"Verification: `{verification}`\nMFA level: `{mfa_level}`\nVanity: `{vanity}`",
            inline=True,
        )
        embed.set_footer(text=f"Server ID: {guild.id}")
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="userinfo", aliases=["ui"])
    @app_commands.describe(user="Mention a user or choose them; prefix commands also accept a user ID")
    async def userinfo(self, ctx: commands.Context, user: discord.User | None = None) -> None:
        """Show account and server information for you or another user."""
        if ctx.guild is None:
            await ctx.reply("This command only works in a server.", mention_author=False)
            return

        user = user or ctx.author
        member = ctx.guild.get_member(user.id)
        target: discord.User = member or user
        embed = discord.Embed(
            title=f"User Information: {target}",
            colour=EMBED_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="User ID", value=f"`{target.id}`", inline=True)
        embed.add_field(name="Bot", value="Yes" if target.bot else "No", inline=True)
        embed.add_field(name="Account Created", value=f"<t:{int(target.created_at.timestamp())}:F>", inline=False)

        if member is not None:
            embed.add_field(name="Joined Server", value=f"<t:{int(member.joined_at.timestamp())}:F>" if member.joined_at else "Unknown", inline=False)
            if member.nick:
                embed.add_field(name="Nickname", value=member.nick[:256], inline=True)
            embed.add_field(name="Top Role", value=member.top_role.mention, inline=True)
            roles = [role.mention for role in reversed(member.roles) if not role.is_default()]
            role_text = ", ".join(roles) or "No roles"
            if len(role_text) > 1024:
                role_text = role_text[:1021].rsplit(",", 1)[0] + "..."
            embed.add_field(name=f"Roles ({len(roles)})", value=role_text, inline=False)
        else:
            embed.add_field(name="Server Membership", value="Not currently in this server", inline=False)

        embed.set_footer(text=f"Requested by {ctx.author}")
        await ctx.reply(embed=embed, mention_author=False)

    def cog_unload(self) -> None:
        self.bot.help_command = self._original


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Help(bot))
