from __future__ import annotations

from discord.ext import commands


class Settings(commands.Cog):
    """Per-server bot settings."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    @commands.hybrid_group(name="prefix", invoke_without_command=True, fallback="show")
    async def prefix(self, ctx: commands.Context) -> None:
        """Show this server's prefix."""
        current = await self.bot.get_guild_prefix(ctx.guild.id)
        await ctx.reply(f"The prefix here is `{current}`.", mention_author=False)

    @prefix.command(name="set")
    @commands.has_guild_permissions(manage_guild=True)
    async def prefix_set(self, ctx: commands.Context, new_prefix: str) -> None:
        """Change this server's prefix."""
        if len(new_prefix) > 5 or new_prefix.isspace():
            await ctx.reply("Keep the prefix to 5 characters or fewer, no spaces.", mention_author=False)
            return
        await self.bot.db.execute(
            """
            INSERT INTO guild_settings (guild_id, prefix) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET prefix = excluded.prefix
            """,
            ctx.guild.id, new_prefix,
        )
        self.bot.prefix_cache[ctx.guild.id] = new_prefix
        await ctx.reply(f"Prefix is now `{new_prefix}`.", mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Settings(bot))
