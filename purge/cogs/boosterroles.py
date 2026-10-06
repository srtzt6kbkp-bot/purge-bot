from __future__ import annotations

import asyncio
import io
import logging
from typing import Literal, Optional

import discord
from discord.ext import commands

log = logging.getLogger("hoodbot.boosterroles")

MAX_SHARES = 3
MAX_ICON_BYTES = 256 * 1024  # Discord's role icon size limit


class BoosterRoles(commands.Cog):
    """Server boosters get their own custom role they can rename, recolor and share."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # ----- helpers ------------------------------------------------------------

    async def _saved_role(self, guild: discord.Guild, user_id: int) -> discord.Role | None:
        row = await self.bot.db.fetchone(
            "SELECT role_id FROM booster_roles WHERE guild_id = ? AND user_id = ?", guild.id, user_id
        )
        if row is None:
            return None
        role = guild.get_role(row["role_id"])
        if role is None:  # deleted by hand: clear the stale record
            await self.bot.db.execute(
                "DELETE FROM booster_roles WHERE guild_id = ? AND user_id = ?", guild.id, user_id
            )
        return role

    async def _my_role(self, ctx: commands.Context) -> discord.Role | None:
        role = await self._saved_role(ctx.guild, ctx.author.id)
        if role is None:
            await ctx.reply("You don't have a booster role yet. Make one with `boosterrole create <name>`.",
                            mention_author=False)
        return role

    async def _filtered_words(self, guild_id: int) -> list[str]:
        row = await self.bot.db.fetchone("SELECT filtered_words FROM booster_config WHERE guild_id = ?", guild_id)
        return [w for w in row["filtered_words"].split(",") if w] if row else []

    async def _name_allowed(self, ctx: commands.Context, name: str) -> bool:
        lowered = name.lower()
        for word in await self._filtered_words(ctx.guild.id):
            if word in lowered:
                await ctx.reply("That name has a word that isn't allowed here.", mention_author=False)
                return False
        return True

    async def _place_role(self, guild: discord.Guild, role: discord.Role) -> None:
        """Slot new roles right under the configured base role (if there is one)."""
        row = await self.bot.db.fetchone("SELECT base_role_id FROM booster_config WHERE guild_id = ?", guild.id)
        base = guild.get_role(row["base_role_id"]) if row and row["base_role_id"] else None
        if base is None:
            return
        try:
            await role.edit(position=max(base.position - 1, 1), reason="Booster role placement")
        except discord.HTTPException:
            log.warning("Couldn't position booster role in %s - is the base role above my top role?", guild.id)

    async def _delete_booster_role(self, guild: discord.Guild, user_id: int, reason: str) -> None:
        row = await self.bot.db.fetchone(
            "SELECT role_id FROM booster_roles WHERE guild_id = ? AND user_id = ?", guild.id, user_id
        )
        if row is None:
            return
        role = guild.get_role(row["role_id"])
        if role is not None:
            try:
                await role.delete(reason=reason)
            except discord.HTTPException:
                log.warning("Couldn't delete booster role %s in %s", role.id, guild.id)
        await self.bot.db.execute("DELETE FROM booster_roles WHERE guild_id = ? AND user_id = ?", guild.id, user_id)
        await self.bot.db.execute(
            "DELETE FROM booster_role_shares WHERE guild_id = ? AND owner_id = ?", guild.id, user_id
        )

    # ----- lifecycle: roles disappear when the boost does ---------------------

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.premium_since is not None and after.premium_since is None:
            await self._delete_booster_role(after.guild, after.id, "Member stopped boosting")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        await self._delete_booster_role(member.guild, member.id, "Booster left the server")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        await self.bot.db.execute("DELETE FROM booster_roles WHERE guild_id = ? AND role_id = ?", role.guild.id, role.id)

    # ----- booster commands ---------------------------------------------------

    @commands.hybrid_group(name="boosterrole", aliases=["br"], invoke_without_command=True)
    async def boosterrole(self, ctx: commands.Context) -> None:
        """Manage your custom booster role."""
        await ctx.send_help(ctx.command)

    @boosterrole.command(name="create")
    @commands.bot_has_guild_permissions(manage_roles=True)
    async def br_create(self, ctx: commands.Context, *, name: str) -> None:
        """Create your booster role (boosters only)."""
        if ctx.author.premium_since is None:
            await ctx.reply("Only server boosters can make a booster role.", mention_author=False)
            return
        if await self._saved_role(ctx.guild, ctx.author.id):
            await ctx.reply("You already have one. Use `rename` or `color` to change it.", mention_author=False)
            return
        name = name[:100]
        if not await self._name_allowed(ctx, name):
            return

        role = await ctx.guild.create_role(name=name, reason=f"Booster role for {ctx.author}")
        await self._place_role(ctx.guild, role)
        await ctx.author.add_roles(role, reason="Booster role")
        await self.bot.db.execute(
            "INSERT INTO booster_roles (guild_id, user_id, role_id) VALUES (?, ?, ?)",
            ctx.guild.id, ctx.author.id, role.id,
        )
        await ctx.reply(f"Created {role.mention}. Try `boosterrole color #ff66aa`.", mention_author=False)

    @boosterrole.command(name="rename")
    async def br_rename(self, ctx: commands.Context, *, name: str) -> None:
        """Rename your booster role."""
        role = await self._my_role(ctx)
        if role and await self._name_allowed(ctx, name):
            await role.edit(name=name[:100], reason="Booster role rename")
            await ctx.reply("Renamed.", mention_author=False)

    @boosterrole.command(name="color", aliases=["colour"])
    async def br_color(self, ctx: commands.Context, color: discord.Colour) -> None:
        """Set your role color, e.g. #ff66aa or 'red'."""
        role = await self._my_role(ctx)
        if role:
            await role.edit(colour=color, reason="Booster role color")
            await ctx.reply(f"Color set to `{color}`.", mention_author=False)

    @boosterrole.command(name="random")
    async def br_random(self, ctx: commands.Context) -> None:
        """Give your role a random color."""
        role = await self._my_role(ctx)
        if role:
            colour = discord.Colour.random()
            await role.edit(colour=colour, reason="Booster role random color")
            await ctx.reply(f"Color set to `{colour}`.", mention_author=False)

    @boosterrole.command(name="dominant")
    async def br_dominant(self, ctx: commands.Context) -> None:
        """Match your role to the average color of your avatar."""
        role = await self._my_role(ctx)
        if role is None:
            return
        data = await ctx.author.display_avatar.with_format("png").with_size(64).read()

        def average_colour() -> discord.Colour:
            from PIL import Image
            with Image.open(io.BytesIO(data)) as img:
                r, g, b = img.convert("RGB").resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
            return discord.Colour.from_rgb(r, g, b)

        colour = await asyncio.to_thread(average_colour)
        await role.edit(colour=colour, reason="Booster role avatar color")
        await ctx.reply(f"Color set to `{colour}`.", mention_author=False)

    @boosterrole.command(name="icon")
    async def br_icon(self, ctx: commands.Context, image: discord.Attachment) -> None:
        """Set a role icon (needs the server at boost level 2)."""
        role = await self._my_role(ctx)
        if role is None:
            return
        if "ROLE_ICONS" not in ctx.guild.features:
            await ctx.reply("This server hasn't unlocked role icons (boost level 2).", mention_author=False)
            return
        if not (image.content_type or "").startswith("image/") or image.size > MAX_ICON_BYTES:
            await ctx.reply("Attach a PNG or JPEG under 256 KB.", mention_author=False)
            return
        await role.edit(display_icon=await image.read(), reason="Booster role icon")
        await ctx.reply("Icon updated.", mention_author=False)

    @boosterrole.command(name="share")
    async def br_share(self, ctx: commands.Context, member: discord.Member) -> None:
        """Share your role with someone (run again to take it back)."""
        role = await self._my_role(ctx)
        if role is None:
            return
        if member.bot or member.id == ctx.author.id:
            await ctx.reply("Pick someone else.", mention_author=False)
            return

        existing = await self.bot.db.fetchone(
            "SELECT 1 FROM booster_role_shares WHERE guild_id = ? AND owner_id = ? AND member_id = ?",
            ctx.guild.id, ctx.author.id, member.id,
        )
        if existing:
            await member.remove_roles(role, reason="Booster role unshared")
            await self.bot.db.execute(
                "DELETE FROM booster_role_shares WHERE guild_id = ? AND owner_id = ? AND member_id = ?",
                ctx.guild.id, ctx.author.id, member.id,
            )
            await ctx.reply(f"Took your role back from {member.display_name}.", mention_author=False)
            return

        count = await self.bot.db.fetchone(
            "SELECT COUNT(*) AS n FROM booster_role_shares WHERE guild_id = ? AND owner_id = ?",
            ctx.guild.id, ctx.author.id,
        )
        if count["n"] >= MAX_SHARES:
            await ctx.reply(f"You can share with at most {MAX_SHARES} people.", mention_author=False)
            return
        await member.add_roles(role, reason="Booster role shared")
        await self.bot.db.execute(
            "INSERT INTO booster_role_shares (guild_id, owner_id, member_id) VALUES (?, ?, ?)",
            ctx.guild.id, ctx.author.id, member.id,
        )
        await ctx.reply(f"Shared with {member.display_name}.", mention_author=False)

    @boosterrole.command(name="remove")
    async def br_remove(self, ctx: commands.Context) -> None:
        """Delete your booster role."""
        if await self._saved_role(ctx.guild, ctx.author.id) is None:
            await ctx.reply("You don't have one.", mention_author=False)
            return
        await self._delete_booster_role(ctx.guild, ctx.author.id, "Booster removed their role")
        await ctx.reply("Your booster role is gone.", mention_author=False)

    # ----- admin commands -----------------------------------------------------

    @boosterrole.command(name="base")
    @commands.has_guild_permissions(manage_guild=True)
    async def br_base(self, ctx: commands.Context, role: discord.Role) -> None:
        """New booster roles get placed right under this role."""
        await self.bot.db.execute(
            """
            INSERT INTO booster_config (guild_id, base_role_id) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET base_role_id = excluded.base_role_id
            """,
            ctx.guild.id, role.id,
        )
        await ctx.reply(f"Booster roles will sit under {role.mention}.", mention_author=False)

    @boosterrole.command(name="filter")
    @commands.has_guild_permissions(manage_guild=True)
    async def br_filter(
        self, ctx: commands.Context, action: Literal["add", "remove", "list"], word: Optional[str] = None
    ) -> None:
        """Ban words from booster role names."""
        words = await self._filtered_words(ctx.guild.id)
        if action == "list":
            await ctx.reply(", ".join(words) or "No filtered words.", mention_author=False)
            return
        if not word or "," in word:
            await ctx.reply("Give me a single word (no commas).", mention_author=False)
            return
        word = word.lower()
        if action == "add" and word not in words:
            words.append(word)
        elif action == "remove" and word in words:
            words.remove(word)
        await self.bot.db.execute(
            """
            INSERT INTO booster_config (guild_id, filtered_words) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET filtered_words = excluded.filtered_words
            """,
            ctx.guild.id, ",".join(words),
        )
        await ctx.reply(f"Filter updated ({len(words)} word(s)).", mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BoosterRoles(bot))
