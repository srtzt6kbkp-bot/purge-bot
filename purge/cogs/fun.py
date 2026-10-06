from __future__ import annotations

from discord.ext import commands


class Fun(commands.Cog):
    """Lighthearted commands."""

    @commands.command(name="vvs")
    async def vvs(self, ctx: commands.Context) -> None:
        """Send a playful message about VVS."""
        await ctx.reply("Yes, VVS is a gay black nigger!", mention_author=False)

    @commands.command(name="lucia")
    async def lucia(self, ctx: commands.Context) -> None:
        """Say Lucia is a ginger."""
        await ctx.reply("Ginger!", mention_author=False)

    @commands.command(name="liv")
    async def liv(self, ctx: commands.Context) -> None:
        """Say you repented in a book for Veil."""
        await ctx.reply("Yes, I repented in a book for Veil.", mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Fun())
