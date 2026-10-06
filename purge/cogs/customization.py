from __future__ import annotations

import logging

import discord
from discord.ext import commands

log = logging.getLogger("hoodbot.customization")


class Customization(commands.Cog):
    """A lightweight customization cog that fits the rest of the bot's patterns."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    @commands.hybrid_group(name="custom", aliases=["customize"], invoke_without_command=True)
    async def custom(self, ctx: commands.Context) -> None:
        """Customization helpers for the server."""
        await ctx.send_help(ctx.command)

    @custom.command(name="ping")
    async def custom_ping(self, ctx: commands.Context) -> None:
        """Simple health check for the custom cog."""
        await ctx.reply("Customization cog is online and healthy.", mention_author=False)

    @custom.command(name="embed")
    @commands.has_guild_permissions(administrator=True)
    async def custom_embed(
        self,
        ctx: commands.Context,
        title: str,
        *, body: str,
    ) -> None:
        """Send a clean embed in the current channel."""
        embed = discord.Embed(
            title=title[:256],
            description=body[:4096],
            colour=discord.Colour(0x000000),
        )
        if ctx.author.avatar:
            embed.set_footer(text=f"Posted by {ctx.author.display_name}", icon_url=ctx.author.avatar.url)
        await ctx.send(embed=embed)

    @custom.command(name="nick")
    @commands.has_guild_permissions(manage_nicknames=True)
    async def custom_nick(self, ctx: commands.Context, member: discord.Member, *, new_name: str) -> None:
        """Rename a member to a clean, controlled nickname."""
        if member.id == ctx.guild.owner_id:
            await ctx.reply("I can't change the server owner's nickname.", mention_author=False)
            return
        if ctx.author.id != ctx.guild.owner_id and member.top_role >= ctx.author.top_role:
            await ctx.reply("You can't change a nickname for someone with the same or higher role than you.", mention_author=False)
            return

        try:
            await member.edit(nick=new_name[:32], reason=f"Nick changed by {ctx.author}")
        except discord.HTTPException:
            await ctx.reply("I couldn't change that nickname.", mention_author=False)
            return

        await ctx.reply(f"Updated **{member}**'s nickname.", mention_author=False)

    @custom.command(name="theme")
    @commands.has_guild_permissions(administrator=True)
    async def custom_theme(self, ctx: commands.Context, color: str = "blurple") -> None:
        """Simple example of a themed embed color option for future customization."""
        palette = {
            "blurple": discord.Colour.blurple(),
            "red": discord.Colour.red(),
            "green": discord.Colour.green(),
            "gold": discord.Colour.gold(),
            "black": discord.Colour(0x000000),
        }
        chosen = palette.get(color.lower(), discord.Colour.blurple())
        embed = discord.Embed(
            title="Theme preview",
            description="All bot embeds use the standard black accent.",
            colour=discord.Colour(0x000000),
        )
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Customization(bot))
