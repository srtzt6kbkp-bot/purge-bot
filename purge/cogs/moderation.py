from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord.ext import commands, tasks

from core.audit import send_audit_log
from core.utils import parse_duration

log = logging.getLogger("hoodbot.moderation")

MAX_TIMEOUT = 28 * 24 * 3600
MIN_NUKE_INTERVAL = 60 * 60
MAX_NUKE_INTERVAL = 30 * 24 * 60 * 60
NUKE_RETRY_SECONDS = 5 * 60
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
LINK_RE = re.compile(r"https?://\S+", re.IGNORECASE)

COLOURS = {
    "ban": discord.Colour(0x000000),
    "unban": discord.Colour(0x000000),
    "kick": discord.Colour(0x000000),
    "timeout": discord.Colour(0x000000),
    "untimeout": discord.Colour(0x000000),
    "warn": discord.Colour(0x000000),
    "jail": discord.Colour(0x000000),
    "unjail": discord.Colour(0x000000),
}


def _purge_checks(func):
    """Manage Messages decorators for both the moderator and the bot."""
    func = commands.bot_has_guild_permissions(manage_messages=True)(func)
    func = commands.has_guild_permissions(manage_messages=True)(func)
    return func


class NukeConfirmation(discord.ui.View):
    def __init__(self, user_id: int) -> None:
        super().__init__(timeout=30)
        self.user_id = user_id
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the administrator who ran this command can confirm it.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = True
        button.disabled = True
        await interaction.response.edit_message(content="Nuke confirmed.", embed=None, view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        button.disabled = True
        await interaction.response.edit_message(content="Nuke cancelled. No changes were made.", embed=None, view=None)
        self.stop()


class Moderation(commands.Cog):
    """Bans, kicks, timeouts, warnings, jail, purge and a numbered case log."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._nuke_schedule_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        self.check_nuke_schedules.start()

    def cog_unload(self) -> None:
        self.check_nuke_schedules.cancel()

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # ----- helpers ------------------------------------------------------------

    @staticmethod
    def _reason(reason: Optional[str]) -> str:
        return (reason or "No reason given").strip()[:400]

    def _deny_reason(self, ctx: commands.Context, target: discord.Member) -> str | None:
        """Why this moderator can't act on this member (None = fine)."""
        if target.id == ctx.author.id:
            return "You can't do that to yourself."
        if target.id == ctx.guild.owner_id:
            return "You can't do that to the server owner."
        if target.id == self.bot.user.id:
            return "Nice try."
        if ctx.author.id != ctx.guild.owner_id and target.top_role >= ctx.author.top_role:
            return "Their top role is the same as or higher than yours."
        if target.top_role >= ctx.guild.me.top_role:
            return "Their top role is the same as or higher than mine. Drag my role higher in the role list."
        return None

    async def _dm(self, user: discord.abc.User, guild: discord.Guild, verb: str, reason: str) -> None:
        try:
            embed = discord.Embed(
                title=f"You were {verb} {guild.name}",
                description=f"Reason: {reason}",
                colour=discord.Colour(0x000000),
            )
            await user.send(embed=embed)
        except discord.HTTPException:
            pass  # DMs closed

    async def _reply(self, ctx: commands.Context, text: str, *, title: str = "Moderation") -> None:
        embed = discord.Embed(
            title=title,
            description=text,
            colour=discord.Colour(0x000000),
        )
        await ctx.reply(embed=embed, mention_author=False)

    async def _send(self, ctx: commands.Context, text: str, *, title: str, delete_after: int | None = None) -> None:
        embed = discord.Embed(
            title=title,
            description=text,
            colour=discord.Colour(0x000000),
        )
        await ctx.send(embed=embed, delete_after=delete_after)

    def _case_embed(self, row) -> discord.Embed:
        action = row["action"]
        embed = discord.Embed(
            title=f"Case #{row['case_number']} | {action.replace('_', ' ').title()}",
            colour=COLOURS.get(action, discord.Colour(0x000000)),
            timestamp=datetime.fromtimestamp(row["created_at"], tz=timezone.utc),
        )
        embed.add_field(name="User", value=f"<@{row['user_id']}> (`{row['user_id']}`)")
        embed.add_field(name="Moderator", value=f"<@{row['moderator_id']}>")
        if row["expires_at"]:
            embed.add_field(name="Expires", value=f"<t:{row['expires_at']}:R>")
        embed.add_field(name="Reason", value=row["reason"], inline=False)
        return embed

    async def _case(self, guild: discord.Guild, user, moderator, action: str, reason: str,
                    expires_at: int | None = None) -> int:
        row_id = await self.bot.db.insert(
            """
            INSERT INTO mod_cases (guild_id, case_number, user_id, moderator_id, action, reason, created_at, expires_at)
            VALUES (?, (SELECT COALESCE(MAX(case_number), 0) + 1 FROM mod_cases WHERE guild_id = ?), ?, ?, ?, ?, ?, ?)
            """,
            guild.id, guild.id, user.id, moderator.id, action, reason, int(time.time()), expires_at,
        )
        row = await self.bot.db.fetchone("SELECT * FROM mod_cases WHERE id = ?", row_id)
        embed = self._case_embed(row)
        if await send_audit_log(self.bot, guild, embed, log_type="moderation"):
            return row["case_number"]

        cfg = await self.bot.db.fetchone("SELECT log_channel_id FROM mod_config WHERE guild_id = ?", guild.id)
        if cfg and cfg["log_channel_id"]:
            channel = guild.get_channel(cfg["log_channel_id"])
            if isinstance(channel, discord.abc.Messageable):
                try:
                    await channel.send(embed=embed)
                except discord.HTTPException:
                    log.warning("Couldn't post case %s to the mod log in %s", row["case_number"], guild.id)
        return row["case_number"]

    async def _jail_role(self, guild: discord.Guild) -> discord.Role | None:
        row = await self.bot.db.fetchone("SELECT jail_role_id FROM mod_config WHERE guild_id = ?", guild.id)
        return guild.get_role(row["jail_role_id"]) if row and row["jail_role_id"] else None

    # ----- ban / kick ---------------------------------------------------------

    @commands.hybrid_command(name="ban")
    @commands.has_guild_permissions(ban_members=True)
    @commands.bot_has_guild_permissions(ban_members=True)
    async def ban(self, ctx: commands.Context, user: discord.User, *, reason: Optional[str] = None) -> None:
        """Ban someone, even if they aren't in the server."""
        member = ctx.guild.get_member(user.id)
        if member is not None:
            problem = self._deny_reason(ctx, member)
            if problem:
                await self._reply(ctx, problem, title="Ban failed")
                return
        reason = self._reason(reason)
        await self._dm(user, ctx.guild, "banned from", reason)
        try:
            await ctx.guild.ban(user, reason=f"{ctx.author}: {reason}", delete_message_seconds=0)
        except discord.HTTPException:
            await self._reply(ctx, "I couldn't ban them. Check that my role is above theirs.", title="Ban failed")
            return
        number = await self._case(ctx.guild, user, ctx.author, "ban", reason)
        await ctx.reply(f"\U0001F4A3 Banned **{user}** (case #{number}).", mention_author=False)

    @commands.hybrid_command(name="unban")
    @commands.has_guild_permissions(ban_members=True)
    @commands.bot_has_guild_permissions(ban_members=True)
    async def unban(self, ctx: commands.Context, user: discord.User, *, reason: Optional[str] = None) -> None:
        """Unban someone (use their user ID)."""
        reason = self._reason(reason)
        try:
            await ctx.guild.unban(user, reason=f"{ctx.author}: {reason}")
        except discord.NotFound:
            await self._reply(ctx, "They aren't banned.", title="Unban failed")
            return
        number = await self._case(ctx.guild, user, ctx.author, "unban", reason)
        await self._reply(ctx, f"Unbanned **{user}** (case #{number}).", title="Unbanned")

    @commands.hybrid_command(name="kick")
    @commands.has_guild_permissions(kick_members=True)
    @commands.bot_has_guild_permissions(kick_members=True)
    async def kick(self, ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = None) -> None:
        """Kick a member."""
        problem = self._deny_reason(ctx, member)
        if problem:
            await self._reply(ctx, problem, title="Kick failed")
            return
        reason = self._reason(reason)
        await self._dm(member, ctx.guild, "kicked from", reason)
        try:
            await member.kick(reason=f"{ctx.author}: {reason}")
        except discord.HTTPException:
            await self._reply(ctx, "I couldn't kick them. Check that my role is above theirs.", title="Kick failed")
            return
        number = await self._case(ctx.guild, member, ctx.author, "kick", reason)
        await ctx.reply(f"\U0001F4A3 Kicked **{member}** (case #{number}).", mention_author=False)

    # ----- timeout ------------------------------------------------------------

    @commands.hybrid_command(name="timeout", aliases=["mute"])
    @commands.has_guild_permissions(moderate_members=True)
    @commands.bot_has_guild_permissions(moderate_members=True)
    async def timeout(self, ctx: commands.Context, member: discord.Member, duration: str,
                      *, reason: Optional[str] = None) -> None:
        """Time someone out, e.g. 10s, 10m, 2h or 1d (max 28d)."""
        seconds = parse_duration(duration)
        if seconds is None or not 1 <= seconds <= MAX_TIMEOUT:
            await self._reply(ctx, "Give a duration from 1s to 28d, like `10s`, `10m`, `2h` or `1d`.", title="Invalid duration")
            return
        problem = self._deny_reason(ctx, member)
        if problem:
            await self._reply(ctx, problem, title="Timeout failed")
            return
        reason = self._reason(reason)
        try:
            await member.timeout(timedelta(seconds=seconds), reason=f"{ctx.author}: {reason}")
        except discord.HTTPException as error:
            log.warning("Couldn't timeout member %s in guild %s: %s", member.id, ctx.guild.id, error)
            await self._reply(ctx, "I couldn't time them out (admins can't be timed out).", title="Timeout failed")
            return
        expires = int(time.time()) + seconds
        await self._dm(member, ctx.guild, "timed out in", reason)
        number = await self._case(ctx.guild, member, ctx.author, "timeout", reason, expires)
        await self._reply(ctx, f"Timed out **{member}** until <t:{expires}:R> (case #{number}).", title="Timed out")

    @commands.hybrid_command(name="untimeout", aliases=["unmute"])
    @commands.has_guild_permissions(moderate_members=True)
    @commands.bot_has_guild_permissions(moderate_members=True)
    async def untimeout(self, ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = None) -> None:
        """Remove someone's timeout."""
        reason = self._reason(reason)
        try:
            await member.timeout(None, reason=f"{ctx.author}: {reason}")
        except discord.HTTPException:
            await self._reply(ctx, "I couldn't remove their timeout.", title="Timeout removal failed")
            return
        number = await self._case(ctx.guild, member, ctx.author, "untimeout", reason)
        await self._reply(ctx, f"Removed the timeout on **{member}** (case #{number}).", title="Timeout removed")

    # ----- warnings -----------------------------------------------------------

    @commands.hybrid_command(name="warn")
    @commands.has_guild_permissions(moderate_members=True)
    async def warn(self, ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = None) -> None:
        """Give someone a warning."""
        problem = self._deny_reason(ctx, member)
        if problem:
            await self._reply(ctx, problem, title="Warning failed")
            return
        reason = self._reason(reason)
        number = await self._case(ctx.guild, member, ctx.author, "warn", reason)
        await self._dm(member, ctx.guild, "warned in", reason)
        total = await self.bot.db.fetchone(
            "SELECT COUNT(*) AS n FROM mod_cases WHERE guild_id = ? AND user_id = ? AND action = 'warn'",
            ctx.guild.id, member.id,
        )
        await self._reply(
            ctx,
            f"Warned **{member}** (case #{number}). They now have {total['n']} warning(s).",
            title="Warning issued",
        )

    @commands.hybrid_command(name="warnings")
    @commands.has_guild_permissions(moderate_members=True)
    async def warnings(self, ctx: commands.Context, member: Optional[discord.User] = None) -> None:
        """List active warnings for you or another user, including reasons and moderators."""
        member = member or ctx.author
        rows = await self.bot.db.fetchall(
            "SELECT * FROM mod_cases WHERE guild_id = ? AND user_id = ? AND action = 'warn' "
            "ORDER BY case_number DESC LIMIT 10",
            ctx.guild.id, member.id,
        )
        lines = [
            f"**Case #{row['case_number']}** <t:{row['created_at']}:R> by <@{row['moderator_id']}>\n"
            f"Reason: {row['reason'][:180]}\n"
            f"Remove: `{ctx.clean_prefix}unwarn <@{member.id}> {row['case_number']}`"
            for row in rows
        ]
        embed = discord.Embed(
            title=f"Active warnings for {member}",
            description="\n\n".join(lines) or "No active warnings.",
            colour=discord.Colour(0x000000),
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="unwarn", aliases=["removewarn", "warnremove"])
    @commands.has_guild_permissions(moderate_members=True)
    async def unwarn(
        self,
        ctx: commands.Context,
        user: discord.User,
        warning_number: int,
        *,
        reason: Optional[str] = None,
    ) -> None:
        """Remove one active warning by its case number, retaining an audit trail."""
        warning = await self.bot.db.fetchone(
            "SELECT id FROM mod_cases WHERE guild_id = ? AND user_id = ? "
            "AND case_number = ? AND action = 'warn'",
            ctx.guild.id,
            user.id,
            warning_number,
        )
        if warning is None:
            await self._reply(
                ctx,
                f"No active warning case #{warning_number} was found for {user}.",
                title="Warning not found",
            )
            return

        await self.bot.db.execute(
            "UPDATE mod_cases SET action = 'warn_removed' WHERE id = ?", warning["id"]
        )
        removal_reason = self._reason(reason)
        audit_case = await self._case(
            ctx.guild,
            user,
            ctx.author,
            "unwarn",
            f"Removed warning case #{warning_number}. Reason: {removal_reason}",
        )
        await self._reply(
            ctx,
            f"Removed warning case #{warning_number} from **{user}** (audit case #{audit_case}).",
            title="Warning removed",
        )

    @commands.hybrid_command(name="clearwarnings")
    @commands.has_guild_permissions(moderate_members=True)
    async def clearwarnings(
        self,
        ctx: commands.Context,
        member: discord.Member,
        *,
        reason: Optional[str] = None,
    ) -> None:
        """Clear someone's warnings (the cases stay in the log)."""
        active = await self.bot.db.fetchone(
            "SELECT COUNT(*) AS n FROM mod_cases WHERE guild_id = ? AND user_id = ? AND action = 'warn'",
            ctx.guild.id,
            member.id,
        )
        if active["n"] == 0:
            await self._reply(ctx, f"**{member}** has no active warnings.", title="No active warnings")
            return
        await self.bot.db.execute(
            "UPDATE mod_cases SET action = 'warn_cleared' WHERE guild_id = ? AND user_id = ? AND action = 'warn'",
            ctx.guild.id, member.id,
        )
        audit_case = await self._case(
            ctx.guild,
            member,
            ctx.author,
            "clearwarnings",
            f"Cleared {active['n']} warning(s). Reason: {self._reason(reason)}",
        )
        await self._reply(
            ctx,
            f"Cleared {active['n']} warning(s) for **{member}** (audit case #{audit_case}).",
            title="Warnings cleared",
        )

    # ----- jail ---------------------------------------------------------------

    @commands.hybrid_command(name="jailsetup")
    @commands.has_guild_permissions(manage_guild=True)
    @commands.bot_has_guild_permissions(manage_roles=True, manage_channels=True)
    async def jailsetup(self, ctx: commands.Context) -> None:
        """Create the Jailed role and a jail channel (run once)."""
        await ctx.defer()
        role = await ctx.guild.create_role(name="Jailed", reason="Jail setup")
        jail_channel = await ctx.guild.create_text_channel(
            "jail",
            overwrites={
                ctx.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                ctx.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            },
            reason="Jail setup",
        )
        failed = 0
        for channel in ctx.guild.channels:
            if channel.id == jail_channel.id:
                continue
            try:
                await channel.set_permissions(
                    role, view_channel=False, send_messages=False, connect=False, speak=False, reason="Jail setup"
                )
            except discord.HTTPException:
                failed += 1
        await self.bot.db.execute(
            """
            INSERT INTO mod_config (guild_id, jail_role_id, jail_channel_id) VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                jail_role_id = excluded.jail_role_id, jail_channel_id = excluded.jail_channel_id
            """,
            ctx.guild.id, role.id, jail_channel.id,
        )
        note = f" I couldn't lock {failed} channel(s); check my permissions there." if failed else ""
        await self._reply(ctx, f"Jail is ready: {role.mention} and {jail_channel.mention}.{note}", title="Jail ready")

    @commands.hybrid_command(name="jail")
    @commands.has_guild_permissions(manage_roles=True)
    @commands.bot_has_guild_permissions(manage_roles=True)
    async def jail(self, ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = None) -> None:
        """Take someone's roles and lock them in the jail channel."""
        role = await self._jail_role(ctx.guild)
        if role is None:
            await self._reply(ctx, "Run `jailsetup` first.", title="Jail not configured")
            return
        problem = self._deny_reason(ctx, member)
        if problem:
            await self._reply(ctx, problem, title="Jail failed")
            return
        if await self.bot.db.fetchone(
            "SELECT 1 FROM jailed_members WHERE guild_id = ? AND user_id = ?", ctx.guild.id, member.id
        ):
            await self._reply(ctx, "They're already jailed.", title="Already jailed")
            return

        top = ctx.guild.me.top_role
        removable = [r for r in member.roles if not r.is_default() and not r.managed and r < top]
        keep = [r for r in member.roles if not r.is_default() and r not in removable]
        reason = self._reason(reason)

        await self.bot.db.execute(
            "INSERT INTO jailed_members (guild_id, user_id, saved_roles) VALUES (?, ?, ?)",
            ctx.guild.id, member.id, ",".join(str(r.id) for r in removable),
        )
        try:
            await member.edit(roles=keep + [role], reason=f"{ctx.author}: {reason}")
        except discord.HTTPException:
            await self.bot.db.execute(
                "DELETE FROM jailed_members WHERE guild_id = ? AND user_id = ?", ctx.guild.id, member.id
            )
            await self._reply(ctx, "I couldn't jail them. Check that my role is above theirs.", title="Jail failed")
            return
        await self._dm(member, ctx.guild, "jailed in", reason)
        number = await self._case(ctx.guild, member, ctx.author, "jail", reason)
        await self._reply(ctx, f"Jailed **{member}** (case #{number}).", title="Member jailed")

    @commands.hybrid_command(name="unjail")
    @commands.has_guild_permissions(manage_roles=True)
    @commands.bot_has_guild_permissions(manage_roles=True)
    async def unjail(self, ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = None) -> None:
        """Release someone and give their roles back."""
        row = await self.bot.db.fetchone(
            "SELECT saved_roles FROM jailed_members WHERE guild_id = ? AND user_id = ?", ctx.guild.id, member.id
        )
        if row is None:
            await self._reply(ctx, "They aren't jailed.", title="Not jailed")
            return
        jail_role = await self._jail_role(ctx.guild)
        top = ctx.guild.me.top_role
        restore = [ctx.guild.get_role(int(x)) for x in row["saved_roles"].split(",") if x]
        restore = [r for r in restore if r is not None and r < top]
        current = [r for r in member.roles if not r.is_default() and (jail_role is None or r.id != jail_role.id)]
        merged = {r.id: r for r in current + restore}
        reason = self._reason(reason)
        try:
            await member.edit(roles=list(merged.values()), reason=f"{ctx.author}: {reason}")
        except discord.HTTPException:
            await self._reply(ctx, "I couldn't release them. Check my role position.", title="Unjail failed")
            return
        await self.bot.db.execute(
            "DELETE FROM jailed_members WHERE guild_id = ? AND user_id = ?", ctx.guild.id, member.id
        )
        number = await self._case(ctx.guild, member, ctx.author, "unjail", reason)
        await self._reply(ctx, f"Released **{member}** (case #{number}).", title="Member released")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Leaving and rejoining doesn't get you out of jail."""
        row = await self.bot.db.fetchone(
            "SELECT 1 FROM jailed_members WHERE guild_id = ? AND user_id = ?", member.guild.id, member.id
        )
        if row is None:
            return
        role = await self._jail_role(member.guild)
        if role is not None:
            try:
                await member.add_roles(role, reason="Still jailed")
            except discord.HTTPException:
                log.warning("Couldn't re-jail %s in %s", member.id, member.guild.id)

    # ----- case log -----------------------------------------------------------

    @commands.hybrid_command(name="modlog")
    @commands.has_guild_permissions(manage_guild=True)
    async def modlog(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Choose where every moderation case gets posted."""
        await self.bot.db.execute(
            """
            INSERT INTO mod_config (guild_id, log_channel_id) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET log_channel_id = excluded.log_channel_id
            """,
            ctx.guild.id, channel.id,
        )
        await self._reply(ctx, f"Cases will be posted in {channel.mention}.", title="Moderation log updated")

    @commands.hybrid_command(name="case")
    @commands.has_guild_permissions(moderate_members=True)
    async def case(self, ctx: commands.Context, number: int) -> None:
        """Look up one case by number."""
        row = await self.bot.db.fetchone(
            "SELECT * FROM mod_cases WHERE guild_id = ? AND case_number = ?", ctx.guild.id, number
        )
        if row is None:
            await self._reply(ctx, "No case with that number.", title="Case not found")
            return
        await ctx.reply(embed=self._case_embed(row), mention_author=False)

    @commands.hybrid_command(name="history", aliases=["cases"])
    @commands.has_guild_permissions(moderate_members=True)
    async def history(self, ctx: commands.Context, user: discord.User) -> None:
        """Show a user's last 10 moderation cases."""
        rows = await self.bot.db.fetchall(
            "SELECT * FROM mod_cases WHERE guild_id = ? AND user_id = ? ORDER BY case_number DESC LIMIT 10",
            ctx.guild.id, user.id,
        )
        lines = [
            f"`#{r['case_number']}` **{r['action'].replace('_', ' ')}** <t:{r['created_at']}:R> - {r['reason'][:80]}"
            for r in rows
        ]
        embed = discord.Embed(title=f"Cases for {user}", description="\n".join(lines) or "No cases.",
                      colour=discord.Colour(0x000000))
        await ctx.reply(embed=embed, mention_author=False)

    async def _action_history(
        self,
        ctx: commands.Context,
        action: str,
        user: Optional[discord.User] = None,
    ) -> None:
        if user is None:
            rows = await self.bot.db.fetchall(
                "SELECT * FROM mod_cases WHERE guild_id = ? AND action = ? "
                "ORDER BY case_number DESC LIMIT 10",
                ctx.guild.id, action,
            )
            title = f"Recent {action.title()} History"
        else:
            rows = await self.bot.db.fetchall(
                "SELECT * FROM mod_cases WHERE guild_id = ? AND user_id = ? AND action = ? "
                "ORDER BY case_number DESC LIMIT 10",
                ctx.guild.id, user.id, action,
            )
            title = f"{action.title()} History for {user}"

        lines = [
            f"**Case #{row['case_number']}** <@{row['user_id']}> by <@{row['moderator_id']}> "
            f"<t:{row['created_at']}:R>\nReason: {row['reason'][:120]}"
            for row in rows
        ]
        embed = discord.Embed(
            title=title,
            description="\n\n".join(lines) or f"No {action} cases found.",
            colour=COLOURS[action],
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="banhistory")
    @commands.has_guild_permissions(moderate_members=True)
    async def banhistory(
        self,
        ctx: commands.Context,
        user: Optional[discord.User] = None,
    ) -> None:
        """Show the latest server bans, or the ban history for one user."""
        await self._action_history(ctx, "ban", user)

    @commands.hybrid_command(name="kickhistory")
    @commands.has_guild_permissions(moderate_members=True)
    async def kickhistory(
        self,
        ctx: commands.Context,
        user: Optional[discord.User] = None,
    ) -> None:
        """Show the latest server kicks, or the kick history for one user."""
        await self._action_history(ctx, "kick", user)

    @commands.hybrid_command(name="reason")
    @commands.has_guild_permissions(moderate_members=True)
    async def reason(self, ctx: commands.Context, number: int, *, new_reason: str) -> None:
        """Edit the reason on a case."""
        row = await self.bot.db.fetchone(
            "SELECT 1 FROM mod_cases WHERE guild_id = ? AND case_number = ?", ctx.guild.id, number
        )
        if row is None:
            await self._reply(ctx, "No case with that number.", title="Case not found")
            return
        await self.bot.db.execute(
            "UPDATE mod_cases SET reason = ? WHERE guild_id = ? AND case_number = ?",
            self._reason(new_reason), ctx.guild.id, number,
        )
        await self._reply(ctx, f"Updated case #{number}.", title="Case updated")

    @commands.hybrid_group(name="nuke", invoke_without_command=True, fallback="run")
    @commands.has_guild_permissions(administrator=True)
    @commands.bot_has_guild_permissions(manage_channels=True)
    async def nuke(self, ctx: commands.Context) -> None:
        """Confirm before replacing this text channel with a copy."""
        if not isinstance(ctx.channel, discord.TextChannel):
            await self._reply(ctx, "This command only works in a text channel.", title="Nuke failed")
            return

        channel = ctx.channel
        confirmation = NukeConfirmation(ctx.author.id)
        embed = discord.Embed(
            title="Are you sure you want to do this?",
            description=(
                "This will replace this channel with a copy. **The channel's message history "
                "will not be copied.**"
            ),
            colour=discord.Colour.red(),
        )
        await ctx.reply(
            embed=embed,
            view=confirmation,
            mention_author=False,
            ephemeral=ctx.interaction is not None,
        )
        timed_out = await confirmation.wait()
        if not confirmation.confirmed:
            if timed_out:
                if ctx.interaction is not None:
                    await ctx.send("Nuke confirmation expired. No changes were made.", ephemeral=True)
                else:
                    await self._reply(ctx, "Nuke confirmation expired. No changes were made.", title="Nuke cancelled")
            return

        async with self._nuke_schedule_lock:
            scheduled = await self.bot.db.fetchone(
                "SELECT 1 FROM nuke_schedules WHERE guild_id = ? AND channel_id = ?",
                ctx.guild.id,
                channel.id,
            )
            if scheduled is not None:
                await self._reply(
                    ctx,
                    f"Stop this channel's automatic schedule with `{ctx.clean_prefix}nuke stop` before manually nuking it.",
                    title="Nuke schedule is active",
                )
                return
            await self._replace_channel_now(ctx, channel)

    async def _replace_channel_now(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        try:
            replacement = await channel.clone(reason=f"Channel nuked by {ctx.author}")
        except discord.HTTPException as error:
            log.error("Couldn't clone channel %s in guild %s: %s", channel.id, ctx.guild.id, error)
            message = "I couldn't clone this channel. Check my **Manage Channels** permission."
            if ctx.interaction is not None:
                await ctx.send(message, ephemeral=True)
            else:
                await self._reply(ctx, message, title="Nuke failed")
            return

        try:
            await channel.delete(reason=f"Channel nuked by {ctx.author}")
        except discord.HTTPException as error:
            log.error("Couldn't delete channel %s in guild %s: %s", channel.id, ctx.guild.id, error)
            cleanup_failed = False
            try:
                await replacement.delete(reason="Cleanup after failed channel nuke")
            except discord.HTTPException as cleanup_error:
                cleanup_failed = True
                log.error(
                    "Couldn't clean up replacement channel %s in guild %s: %s",
                    replacement.id,
                    ctx.guild.id,
                    cleanup_error,
                )
            message = "I couldn't delete the original channel."
            if cleanup_failed:
                message += f" The replacement channel {replacement.mention} is still there."
            if ctx.interaction is not None:
                await ctx.send(message, ephemeral=True)
            else:
                await self._reply(ctx, message, title="Nuke failed")
            return

        try:
            await replacement.send("Nuked! 💥")
        except discord.HTTPException as error:
            log.error("Couldn't post nuke confirmation in channel %s: %s", replacement.id, error)
            if ctx.interaction is not None:
                await ctx.send(
                    "The channel was nuked, but I couldn't post the confirmation in the new channel.",
                    ephemeral=True,
                )
            return

        if ctx.interaction is not None:
            await ctx.send("Nuked! 💥", ephemeral=True)

    @nuke.command(name="schedule")
    @commands.has_guild_permissions(administrator=True)
    @commands.bot_has_guild_permissions(
        manage_channels=True,
        view_channel=True,
        read_message_history=True,
        send_messages=True,
        attach_files=True,
    )
    async def nuke_schedule(
        self,
        ctx: commands.Context,
        interval: str,
        archive_channel: discord.TextChannel,
    ) -> None:
        """Schedule this text channel to be archived and replaced repeatedly."""
        if not isinstance(ctx.channel, discord.TextChannel):
            await self._reply(ctx, "Run this command in the text channel you want scheduled.", title="Nuke schedule failed")
            return
        if archive_channel.id == ctx.channel.id:
            await self._reply(ctx, "Choose a different channel for transcript archives.", title="Nuke schedule failed")
            return

        seconds = parse_duration(interval)
        if seconds is None or not MIN_NUKE_INTERVAL <= seconds <= MAX_NUKE_INTERVAL:
            await self._reply(
                ctx,
                "Choose an interval from `1h` to `30d`, such as `24h`.",
                title="Invalid nuke interval",
            )
            return

        bot_member = ctx.guild.me
        if bot_member is None:
            await self._reply(ctx, "I couldn't verify my server permissions.", title="Nuke schedule failed")
            return
        source_permissions = ctx.channel.permissions_for(bot_member)
        archive_permissions = archive_channel.permissions_for(bot_member)
        if not all(
            (
                source_permissions.manage_channels,
                source_permissions.view_channel,
                source_permissions.read_message_history,
                source_permissions.send_messages,
                source_permissions.attach_files,
                archive_permissions.view_channel,
                archive_permissions.send_messages,
                archive_permissions.attach_files,
            )
        ):
            await self._reply(
                ctx,
                "I need Manage Channels, View Channel, Read Message History, Send Messages, and Attach Files "
                "in the scheduled channel, plus View Channel, Send Messages, and Attach Files in the archive channel.",
                title="Nuke schedule permissions",
            )
            return

        confirmation = NukeConfirmation(ctx.author.id)
        embed = discord.Embed(
            title="Confirm automatic channel nukes",
            description=(
                f"Every **{interval}**, I will save this channel's message history as a `.txt` file "
                f"in {archive_channel.mention} and here, then replace this channel. "
                "**Messages will not be copied to the replacement.**"
            ),
            colour=discord.Colour.red(),
        )
        await ctx.reply(
            embed=embed,
            view=confirmation,
            mention_author=False,
            ephemeral=ctx.interaction is not None,
        )
        timed_out = await confirmation.wait()
        if not confirmation.confirmed:
            if timed_out:
                await self._reply(ctx, "Schedule confirmation expired. No schedule was created.", title="Nuke schedule cancelled")
            return

        next_run = int(time.time()) + seconds
        async with self._nuke_schedule_lock:
            await self.bot.db.execute(
                """
                INSERT INTO nuke_schedules (guild_id, channel_id, archive_channel_id, interval_seconds, next_run_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    guild_id = excluded.guild_id,
                    archive_channel_id = excluded.archive_channel_id,
                    interval_seconds = excluded.interval_seconds,
                    next_run_at = excluded.next_run_at
                """,
                ctx.guild.id,
                ctx.channel.id,
                archive_channel.id,
                seconds,
                next_run,
            )
        await self._reply(
            ctx,
            f"Automatic nukes are set for {ctx.channel.mention} every "
            f"**{timedelta(seconds=seconds)}**. "
            f"First run <t:{next_run}:R>. Use `{ctx.clean_prefix}nuke stop` to cancel.",
            title="Nuke schedule created",
        )

    @nuke.command(name="status")
    @commands.has_guild_permissions(administrator=True)
    async def nuke_schedule_status(self, ctx: commands.Context) -> None:
        """Show the automatic nuke schedule for this channel."""
        row = await self.bot.db.fetchone(
            "SELECT * FROM nuke_schedules WHERE guild_id = ? AND channel_id = ?",
            ctx.guild.id,
            ctx.channel.id,
        )
        if row is None:
            await self._reply(ctx, "There is no automatic nuke schedule for this channel.", title="Nuke schedule")
            return
        archive_channel = ctx.guild.get_channel(row["archive_channel_id"])
        await self._reply(
            ctx,
            f"Interval: **{timedelta(seconds=row['interval_seconds'])}**\n"
            f"Transcript channel: {archive_channel.mention if archive_channel else 'Unavailable'}\n"
            f"Next run: <t:{row['next_run_at']}:R>.",
            title="Nuke schedule",
        )

    @nuke.command(name="stop")
    @commands.has_guild_permissions(administrator=True)
    async def nuke_schedule_stop(self, ctx: commands.Context) -> None:
        """Stop automatic nukes for this channel."""
        async with self._nuke_schedule_lock:
            row = await self.bot.db.fetchone(
                "SELECT 1 FROM nuke_schedules WHERE guild_id = ? AND channel_id = ?",
                ctx.guild.id,
                ctx.channel.id,
            )
            if row is not None:
                await self.bot.db.execute(
                    "DELETE FROM nuke_schedules WHERE guild_id = ? AND channel_id = ?",
                    ctx.guild.id,
                    ctx.channel.id,
                )
        if row is None:
            await self._reply(ctx, "There is no automatic nuke schedule for this channel.", title="Nuke schedule")
        else:
            await self._reply(ctx, "Automatic nukes are stopped for this channel.", title="Nuke schedule stopped")

    async def _channel_transcript(self, channel: discord.TextChannel) -> bytes:
        lines = [f"Transcript for #{channel.name} ({channel.id})", ""]
        size = sum(len(line.encode("utf-8")) + 1 for line in lines)
        async for message in channel.history(limit=None, oldest_first=True):
            entry = [
                f"[{message.created_at.isoformat()}] {message.author} ({message.author.id})",
                message.content or "[no text content]",
            ]
            entry.extend(f"Attachment: {attachment.filename} ({attachment.url})" for attachment in message.attachments)
            for embed in message.embeds:
                if embed.title:
                    entry.append(f"Embed title: {embed.title}")
                if embed.description:
                    entry.append(f"Embed: {embed.description}")
            block = "\n".join(entry) + "\n\n"
            encoded = block.encode("utf-8")
            size += len(encoded)
            if size > MAX_TRANSCRIPT_BYTES:
                raise ValueError("Transcript exceeds the bot's safe upload size; channel was not replaced.")
            lines.append(block)
        transcript = "\n".join(lines).encode("utf-8")
        if len(transcript) > MAX_TRANSCRIPT_BYTES:
            raise ValueError("Transcript exceeds the bot's safe upload size; channel was not replaced.")
        return transcript

    @tasks.loop(seconds=30)
    async def check_nuke_schedules(self) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT * FROM nuke_schedules WHERE next_run_at <= ?",
            int(time.time()),
        )
        for row in rows:
            async with self._nuke_schedule_lock:
                try:
                    current = await self.bot.db.fetchone(
                        "SELECT * FROM nuke_schedules WHERE guild_id = ? AND channel_id = ?",
                        row["guild_id"],
                        row["channel_id"],
                    )
                    if current is None or current["next_run_at"] > int(time.time()):
                        continue
                    await self.bot.db.execute(
                        "UPDATE nuke_schedules SET next_run_at = ? WHERE guild_id = ? AND channel_id = ?",
                        int(time.time()) + NUKE_RETRY_SECONDS,
                        current["guild_id"],
                        current["channel_id"],
                    )
                    await self._run_scheduled_nuke(current)
                except Exception:
                    log.exception(
                        "Scheduled nuke failed for guild %s channel %s",
                        row["guild_id"],
                        row["channel_id"],
                    )
                    guild = self.bot.get_guild(row["guild_id"])
                    archive = guild.get_channel(row["archive_channel_id"]) if guild else None
                    if isinstance(archive, discord.TextChannel):
                        try:
                            await archive.send(
                                f"Scheduled nuke failed for channel ID `{row['channel_id']}`. "
                                "The source channel was kept; check the bot's permissions and logs."
                            )
                        except discord.HTTPException:
                            log.exception("Couldn't notify archive channel %s about a failed nuke", archive.id)

    @check_nuke_schedules.before_loop
    async def _wait_for_nuke_scheduler(self) -> None:
        await self.bot.wait_until_ready()

    async def _run_scheduled_nuke(self, schedule) -> None:
        guild = self.bot.get_guild(schedule["guild_id"])
        channel = guild.get_channel(schedule["channel_id"]) if guild else None
        archive = guild.get_channel(schedule["archive_channel_id"]) if guild else None
        if not isinstance(channel, discord.TextChannel) or not isinstance(archive, discord.TextChannel):
            raise RuntimeError("Scheduled channel or transcript channel no longer exists.")
        if channel.id == archive.id:
            raise RuntimeError("Scheduled channel cannot also be its transcript channel.")

        bot_member = guild.me
        if bot_member is None:
            raise RuntimeError("Bot's server member could not be found.")
        source_permissions = channel.permissions_for(bot_member)
        archive_permissions = archive.permissions_for(bot_member)
        if not all(
            (
                source_permissions.manage_channels,
                source_permissions.view_channel,
                source_permissions.read_message_history,
                source_permissions.send_messages,
                source_permissions.attach_files,
                archive_permissions.view_channel,
                archive_permissions.send_messages,
                archive_permissions.attach_files,
            )
        ):
            raise RuntimeError("Bot no longer has the required permissions for this scheduled nuke.")

        transcript = await self._channel_transcript(channel)
        filename = f"{channel.name}-{int(time.time())}.txt"
        await archive.send(
            content=f"Transcript for **#{channel.name}** before its scheduled nuke.",
            file=discord.File(io.BytesIO(transcript), filename=filename),
        )
        await channel.send(
            "Saving this channel's transcript before the scheduled nuke.",
            file=discord.File(io.BytesIO(transcript), filename=filename),
        )

        replacement = await channel.clone(reason="Scheduled channel nuke")
        next_run = int(time.time()) + schedule["interval_seconds"]
        try:
            await self.bot.db.execute(
                "UPDATE nuke_schedules SET channel_id = ?, next_run_at = ? "
                "WHERE guild_id = ? AND channel_id = ?",
                replacement.id,
                next_run,
                schedule["guild_id"],
                channel.id,
            )
        except Exception:
            try:
                await replacement.delete(reason="Cleanup after failed nuke schedule update")
            except discord.HTTPException:
                log.exception("Couldn't clean up replacement channel %s", replacement.id)
            raise

        try:
            await channel.delete(reason="Scheduled channel nuke")
        except discord.HTTPException:
            await self.bot.db.execute(
                "UPDATE nuke_schedules SET channel_id = ?, next_run_at = ? "
                "WHERE guild_id = ? AND channel_id = ?",
                channel.id,
                int(time.time()) + NUKE_RETRY_SECONDS,
                schedule["guild_id"],
                replacement.id,
            )
            try:
                await replacement.delete(reason="Cleanup after failed scheduled nuke")
            except discord.HTTPException:
                log.exception("Couldn't clean up replacement channel %s after delete failure", replacement.id)
            raise

        try:
            await replacement.send("Nuked! 💥")
        except discord.HTTPException:
            log.exception("Channel %s was replaced, but the nuke announcement failed", replacement.id)

    # ----- purge --------------------------------------------------------------

    async def _purge(self, ctx: commands.Context, limit: int, check=None) -> None:
        """Delete messages matching the criteria cleanly across prefix and slash commands."""
        if ctx.interaction is not None:
            if not ctx.interaction.response.is_done():
                await ctx.defer(ephemeral=True)

        try:
            messages = []
            async for message in ctx.channel.history(limit=limit, oldest_first=False):
                if check is None or check(message):
                    messages.append(message)
                    if len(messages) >= limit:
                        break
            if not messages:
                if ctx.interaction is not None:
                    embed = discord.Embed(description="Deleted 0 message(s).", colour=discord.Colour(0x000000))
                    await ctx.followup.send(embed=embed, ephemeral=True)
                else:
                    await self._send(ctx, "Deleted 0 message(s).", title="Purge complete", delete_after=5)
                return
            if len(messages) == 1:
                await messages[0].delete()
                deleted = messages
            else:
                await ctx.channel.delete_messages(messages)
                deleted = messages
        except discord.HTTPException as e:
            log.error("Purge failed in guild %s: %s", ctx.guild.id, e)
            if ctx.interaction is not None:
                embed = discord.Embed(
                    description="Failed to purge messages. Check my **Manage Messages** permission.",
                    colour=discord.Colour(0x000000),
                )
                await ctx.followup.send(embed=embed, ephemeral=True)
            else:
                await self._send(
                    ctx,
                    "Failed to purge messages. Check my **Manage Messages** permission.",
                    title="Purge failed",
                    delete_after=5,
                )
            return

        count = len(deleted)
        if ctx.interaction is not None:
            embed = discord.Embed(
                description=f"Successfully deleted {count} message(s).",
                colour=discord.Colour(0x000000),
            )
            await ctx.followup.send(embed=embed, ephemeral=True)
        else:
            await self._send(ctx, f"Deleted {count} message(s).", title="Purge complete", delete_after=5)

    @commands.group(name="purge", aliases=["clear"], invoke_without_command=True)
    @_purge_checks
    async def purge(self, ctx: commands.Context, *, query: str = "50") -> None:
        """Delete recent messages. Accepts a count, 'all', or a member followed by a count."""
        tokens = query.strip().split()
        target: discord.Member | None = None
        raw = "50"
        if tokens:
            first = tokens[0].lower()
            first_is_count = first == "all" or first.isdecimal()
            if first_is_count:
                raw = first
                if len(tokens) > 1:
                    if len(tokens) > 2:
                        await self._reply(ctx, f"Use `{ctx.clean_prefix}purge @user 12`, `{ctx.clean_prefix}purge 12`, or `{ctx.clean_prefix}purge all`.", title="Purge usage")
                        return
                    try:
                        target = await commands.MemberConverter().convert(ctx, tokens[1])
                    except commands.BadArgument:
                        await self._reply(ctx, f"I couldn't find `{tokens[1]}` in this server.", title="Purge target not found")
                        return
            else:
                try:
                    target = await commands.MemberConverter().convert(ctx, tokens[0])
                except commands.BadArgument:
                    await self._reply(ctx, f"Use a message count, `all`, or a member mention/ID followed by a count. Example: `{ctx.clean_prefix}purge @user 12`.", title="Purge usage")
                    return
                if len(tokens) > 2:
                    await self._reply(ctx, f"Use `{ctx.clean_prefix}purge @user 12` or `{ctx.clean_prefix}purge @user all`.", title="Purge usage")
                    return
                if len(tokens) == 2:
                    raw = tokens[1].lower()

        if raw == "all":
            remaining = None
        else:
            try:
                remaining = int(raw)
            except ValueError:
                await self._reply(ctx, "Use a number like `12` or `all`.", title="Purge usage")
                return
            if remaining <= 0:
                await self._reply(ctx, "Give a number above 0 or use `all`.", title="Purge usage")
                return

        deleted_total = 0
        before = None
        while True:
            if remaining is not None:
                if remaining <= 0:
                    break
                batch = min(remaining, 100) if target is None else 100
            else:
                batch = 100
            try:
                scanned = []
                async for message in ctx.channel.history(limit=batch, oldest_first=False, before=before):
                    scanned.append(message)
                if not scanned:
                    break
                before = scanned[-1]
                messages = [message for message in scanned if target is None or message.author.id == target.id]
                if remaining is not None:
                    messages = messages[:remaining]
                if len(messages) == 1:
                    await messages[0].delete()
                elif len(messages) > 1:
                    await ctx.channel.delete_messages(messages)
                deleted_total += len(messages)
                if remaining is not None:
                    remaining -= len(messages)
                if remaining is not None and remaining <= 0:
                    break
                if len(scanned) < batch:
                    break
            except discord.HTTPException as e:
                log.error("Purge failed in guild %s: %s", ctx.guild.id, e)
                await self._reply(ctx, "Failed to purge messages. Check my **Manage Messages** permission.", title="Purge failed")
                return

        target_text = f" from {target.mention}" if target is not None else ""
        await self._send(ctx, f"Deleted {deleted_total} message(s){target_text}.", title="Purge complete", delete_after=5)

    @purge.command(name="bots")
    @_purge_checks
    async def purge_bots(self, ctx: commands.Context, amount: commands.Range[int, 1, 100] = 50) -> None:
        """Delete bot messages out of the last messages."""
        await self._purge(ctx, amount, lambda m: m.author.bot)

    @purge.command(name="images")
    @_purge_checks
    async def purge_images(self, ctx: commands.Context, amount: commands.Range[int, 1, 100] = 50) -> None:
        """Delete messages with images out of the last messages."""
        def has_image(m: discord.Message) -> bool:
            if any((a.content_type or "").startswith("image/") for a in m.attachments):
                return True
            return any(e.type in ("image", "gifv") for e in m.embeds)
        await self._purge(ctx, amount, has_image)

    @purge.command(name="links")
    @_purge_checks
    async def purge_links(self, ctx: commands.Context, amount: commands.Range[int, 1, 100] = 50) -> None:
        """Delete messages containing links out of the last messages."""
        await self._purge(ctx, amount, lambda m: bool(LINK_RE.search(m.content)))

    @purge.command(name="embeds")
    @_purge_checks
    async def purge_embeds(self, ctx: commands.Context, amount: commands.Range[int, 1, 100] = 50) -> None:
        """Delete messages with embeds out of the last messages."""
        await self._purge(ctx, amount, lambda m: bool(m.embeds))

    @purge.command(name="user")
    @_purge_checks
    async def purge_user(self, ctx: commands.Context, user: discord.User,
                           amount: commands.Range[int, 1, 100] = 50) -> None:
        """Delete one person's messages out of the last messages."""
        await self._purge(ctx, amount, lambda m: m.author.id == user.id)

    @purge.command(name="contains")
    @_purge_checks
    async def purge_contains(self, ctx: commands.Context, text: str,
                               amount: commands.Range[int, 1, 100] = 50) -> None:
        """Delete messages containing some text out of the last messages."""
        needle = text.lower()
        await self._purge(ctx, amount, lambda m: needle in m.content.lower())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))