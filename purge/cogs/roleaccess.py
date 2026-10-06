from __future__ import annotations

import logging
import time
from typing import Literal

import discord
from discord.ext import commands

import config
from core.audit import send_audit_log

log = logging.getLogger("hoodbot.roleaccess")

AUTHORIZED_ASSIGNMENT_TTL = 30
ROLE_CONFIG_FIELDS = {
    "topfloor": "topfloor_role_id",
    "managed": "managed_role_id",
    "bot-only": "bot_only_role_id",
    "authorized-bot": "authorized_bot_role_id",
}
DEFAULT_ROLE_IDS = {
    "topfloor_role_id": config.TOPFLOOR_ROLE_ID,
    "managed_role_id": config.TOPFLOOR_MANAGED_ROLE_ID,
    "bot_only_role_id": config.BOT_ONLY_ROLE_ID,
    "authorized_bot_role_id": config.AUTHORIZED_BOT_ROLE_ID,
}


class RoleAccess(commands.Cog):
    """Enforces the restricted role grants and exposes Topfloor-only controls."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._authorized_assignments: dict[tuple[int, int, int], float] = {}

    async def _role_ids(self, guild_id: int) -> dict[str, int]:
        row = await self.bot.db.fetchone(
            "SELECT topfloor_role_id, managed_role_id, bot_only_role_id, authorized_bot_role_id "
            "FROM role_access_config WHERE guild_id = ?",
            guild_id,
        )
        if row is None:
            return DEFAULT_ROLE_IDS.copy()
        return {field: int(row[field]) for field in DEFAULT_ROLE_IDS}

    async def _can_bootstrap(self, ctx: commands.Context) -> bool:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            return False
        if not ctx.author.guild_permissions.administrator:
            return False
        row = await self.bot.db.fetchone(
            "SELECT topfloor_role_id FROM role_access_config WHERE guild_id = ?",
            ctx.guild.id,
        )
        return row is None or ctx.guild.get_role(row["topfloor_role_id"]) is None

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if ctx.command is not None and ctx.command.qualified_name == "topfloor setup":
            if await self._can_bootstrap(ctx):
                return True
            raise commands.CheckFailure(
                "Initial setup is available only to an administrator before a Topfloor role is configured."
            )

        role_ids = await self._role_ids(ctx.guild.id)
        if not isinstance(ctx.author, discord.Member) or not any(
            role.id == role_ids["topfloor_role_id"] for role in ctx.author.roles
        ):
            raise commands.CheckFailure("Only members with the Topfloor role can use this section.")
        return True

    @commands.hybrid_group(name="topfloor", invoke_without_command=True)
    async def topfloor(self, ctx: commands.Context) -> None:
        """Manage the restricted Topfloor roles."""
        await ctx.send_help(ctx.command)

    @topfloor.command(name="setup")
    async def topfloor_setup(self, ctx: commands.Context, role: discord.Role) -> None:
        """Select the Topfloor role the first time this server is configured."""
        if not await self._can_bootstrap(ctx):
            await ctx.reply("Setup is already complete. Use `topfloor config` to update the roles.", mention_author=False)
            return
        if role not in ctx.author.roles:
            await ctx.reply(
                "Assign the selected Topfloor role to yourself first so you can manage the section after setup.",
                mention_author=False,
            )
            return

        await self.bot.db.execute(
            "INSERT OR REPLACE INTO role_access_config "
            "(guild_id, topfloor_role_id, managed_role_id, bot_only_role_id, authorized_bot_role_id) "
            "VALUES (?, ?, ?, ?, ?)",
            ctx.guild.id,
            role.id,
            config.TOPFLOOR_MANAGED_ROLE_ID,
            config.BOT_ONLY_ROLE_ID,
            config.AUTHORIZED_BOT_ROLE_ID,
        )
        await ctx.reply(
            f"Topfloor setup complete. {role.mention} can now use the restricted role section "
            "and configure this server's role IDs.",
            mention_author=False,
        )

    @topfloor.command(name="config")
    async def topfloor_config(self, ctx: commands.Context) -> None:
        """Show this server's role IDs and how to change them."""
        role_ids = await self._role_ids(ctx.guild.id)
        labels = {
            "topfloor_role_id": "Topfloor access role",
            "managed_role_id": "Topfloor-grantable role",
            "bot_only_role_id": "Bot-only grant role",
            "authorized_bot_role_id": "Authorized bot role",
        }
        details = "\n".join(
            f"**{label}:** <@&{role_ids[field]}> (`{role_ids[field]}`)"
            for field, label in labels.items()
        )
        embed = discord.Embed(
            title="Server role access",
            description=(
                f"{details}\n\n"
                "Change one with `topfloor set <topfloor|managed|bot-only|authorized-bot> @role`."
            ),
            colour=discord.Colour(0x000000),
        )
        await ctx.reply(embed=embed, mention_author=False)

    @topfloor.command(name="set")
    async def topfloor_set(
        self,
        ctx: commands.Context,
        setting: Literal["topfloor", "managed", "bot-only", "authorized-bot"],
        role: discord.Role,
    ) -> None:
        """Change one of this server's protected role assignments."""
        field = ROLE_CONFIG_FIELDS[setting]
        if field == "topfloor_role_id" and role not in ctx.author.roles:
            await ctx.reply(
                "You need the new Topfloor role yourself before changing the access role, to avoid locking out this section.",
                mention_author=False,
            )
            return
        role_ids = await self._role_ids(ctx.guild.id)
        role_ids[field] = role.id
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO role_access_config "
            "(guild_id, topfloor_role_id, managed_role_id, bot_only_role_id, authorized_bot_role_id) "
            "VALUES (?, ?, ?, ?, ?)",
            ctx.guild.id,
            role_ids["topfloor_role_id"],
            role_ids["managed_role_id"],
            role_ids["bot_only_role_id"],
            role_ids["authorized_bot_role_id"],
        )
        await ctx.reply(f"Updated **{setting}** to {role.mention}.", mention_author=False)

    @topfloor.command(name="grant")
    @commands.bot_has_guild_permissions(manage_roles=True)
    async def topfloor_grant(self, ctx: commands.Context, member: discord.Member) -> None:
        """Give the Topfloor-managed role to a member."""
        role_ids = await self._role_ids(ctx.guild.id)
        role = ctx.guild.get_role(role_ids["managed_role_id"])
        if role is None:
            await ctx.reply("The configured Topfloor-managed role was not found in this server.", mention_author=False)
            return
        if role in member.roles:
            await ctx.reply(f"{member.mention} already has {role.mention}.", mention_author=False)
            return

        key = (ctx.guild.id, member.id, role.id)
        self._authorized_assignments[key] = time.monotonic() + AUTHORIZED_ASSIGNMENT_TTL
        try:
            await member.add_roles(role, reason=f"Granted by Topfloor member {ctx.author}")
        except discord.HTTPException as exc:
            self._authorized_assignments.pop(key, None)
            log.warning("Could not grant protected role %s to %s in %s: %s", role.id, member.id, ctx.guild.id, exc)
            await ctx.reply("I couldn't grant that role. Check my Manage Roles permission and role position.", mention_author=False)
            return

        await ctx.reply(f"Granted {role.mention} to {member.mention}.", mention_author=False)

    @topfloor.command(name="remove")
    @commands.bot_has_guild_permissions(manage_roles=True)
    async def topfloor_remove(self, ctx: commands.Context, member: discord.Member, role: discord.Role) -> None:
        """Remove either protected role from a member."""
        role_ids = await self._role_ids(ctx.guild.id)
        if role.id not in {role_ids["managed_role_id"], role_ids["bot_only_role_id"]}:
            await ctx.reply("You can only remove one of the two protected roles with this command.", mention_author=False)
            return
        if role not in member.roles:
            await ctx.reply(f"{member.mention} does not have {role.mention}.", mention_author=False)
            return

        try:
            await member.remove_roles(role, reason=f"Removed by Topfloor member {ctx.author}")
        except discord.HTTPException as exc:
            log.warning("Could not remove protected role %s from %s in %s: %s", role.id, member.id, ctx.guild.id, exc)
            await ctx.reply("I couldn't remove that role. Check my Manage Roles permission and role position.", mention_author=False)
            return

        await ctx.reply(f"Removed {role.mention} from {member.mention}.", mention_author=False)

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        if entry.action is not discord.AuditLogAction.member_role_update:
            return

        added_roles = getattr(entry.after, "roles", ())
        if not added_roles:
            return

        guild = entry.guild
        role_ids = await self._role_ids(guild.id)
        protected_ids = {role_ids["managed_role_id"], role_ids["bot_only_role_id"]}
        protected = [role for role in added_roles if role.id in protected_ids]
        if not protected:
            return

        target_id = getattr(entry.target, "id", None)
        if target_id is None:
            log.warning("Could not identify target for protected-role audit entry %s in guild %s", entry.id, guild.id)
            return

        actor_id = entry.user_id
        actor = guild.get_member(actor_id) if actor_id is not None else None
        if actor is None and actor_id is not None:
            try:
                actor = await guild.fetch_member(actor_id)
            except discord.NotFound:
                actor = None
            except discord.HTTPException as exc:
                log.warning("Could not verify role-change actor %s in guild %s: %s", actor_id, guild.id, exc)

        for role in protected:
            key = (guild.id, target_id, role.id)
            expires_at = self._authorized_assignments.get(key, 0)
            bot_user = self.bot.user
            if bot_user is not None and actor_id == bot_user.id and time.monotonic() < expires_at:
                self._authorized_assignments.pop(key, None)
                continue

            allowed = False
            if role.id == role_ids["managed_role_id"]:
                allowed = actor is not None and any(
                    member_role.id == role_ids["topfloor_role_id"] for member_role in actor.roles
                )
            elif role.id == role_ids["bot_only_role_id"]:
                allowed = actor is not None and actor.bot and any(
                    member_role.id == role_ids["authorized_bot_role_id"] for member_role in actor.roles
                )

            if allowed:
                actor_label = f"<@{actor_id}> (`{actor_id}`)" if actor_id else "Unknown actor"
                embed = discord.Embed(
                    title="Protected role granted",
                    description=(
                        f"**Role:** {role.mention} (`{role.id}`)\n"
                        f"**Member:** <@{target_id}> (`{target_id}`)\n"
                        f"**Granted by:** {actor_label}"
                    ),
                    colour=discord.Colour(0x000000),
                    timestamp=discord.utils.utcnow(),
                )
                await send_audit_log(self.bot, guild, embed, log_type="role")
                continue

            target = guild.get_member(target_id)
            if target is None:
                try:
                    target = await guild.fetch_member(target_id)
                except discord.NotFound:
                    log.warning(
                        "Could not find target %s after unauthorized protected-role grant in guild %s",
                        target_id,
                        guild.id,
                    )
                    continue
                except discord.HTTPException as exc:
                    log.error(
                        "Could not fetch target %s to remove protected role %s in guild %s: %s",
                        target_id,
                        role.id,
                        guild.id,
                        exc,
                    )
                    continue
            try:
                await target.remove_roles(
                    role,
                    reason=f"Unauthorized assignment by {actor_id or 'unknown actor'}",
                )
                log.warning(
                    "Removed protected role %s from %s in guild %s after unauthorized assignment by %s",
                    role.id,
                    target_id,
                    guild.id,
                    actor_id or "unknown actor",
                )
                actor_label = f"<@{actor_id}> (`{actor_id}`)" if actor_id else "Unknown actor"
                embed = discord.Embed(
                    title="Unauthorized protected role removed",
                    description=(
                        f"**Role:** {role.mention} (`{role.id}`)\n"
                        f"**Member:** {target.mention} (`{target.id}`)\n"
                        f"**Granted by:** {actor_label}"
                    ),
                    colour=discord.Colour.red(),
                    timestamp=discord.utils.utcnow(),
                )
                await send_audit_log(self.bot, guild, embed, log_type="role")
            except discord.HTTPException as exc:
                log.error(
                    "Failed to remove protected role %s from %s in guild %s after unauthorized assignment: %s",
                    role.id,
                    target_id,
                    guild.id,
                    exc,
                )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RoleAccess(bot))
