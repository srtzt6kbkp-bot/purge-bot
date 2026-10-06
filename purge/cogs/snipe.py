from __future__ import annotations

from collections import defaultdict, deque

import discord
from discord.ext import commands

KEEP = 10  # per channel, per kind. Lives in memory only, wiped on restart.


class Snipe(commands.Cog):
    """See the last deleted message, edit, or removed reaction in a channel."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.deleted: dict[int, deque] = defaultdict(lambda: deque(maxlen=KEEP))
        self.edited: dict[int, deque] = defaultdict(lambda: deque(maxlen=KEEP))
        self.reactions: dict[int, deque] = defaultdict(lambda: deque(maxlen=KEEP))

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # ----- recording ----------------------------------------------------------

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        if not message.content and not message.attachments:
            return
        self.deleted[message.channel.id].append({
            "author": message.author,
            "content": message.content,
            "attachments": [a.url for a in message.attachments],
            "at": discord.utils.utcnow(),
        })

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if after.guild is None or after.author.bot or before.content == after.content:
            return
        self.edited[after.channel.id].append({
            "author": after.author,
            "before": before.content,
            "after": after.content,
            "url": after.jump_url,
            "at": discord.utils.utcnow(),
        })

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None:
            return
        user = self.bot.get_user(payload.user_id)
        if user is not None and user.bot:
            return
        self.reactions[payload.channel_id].append({
            "user_id": payload.user_id,
            "emoji": str(payload.emoji),
            "url": f"https://discord.com/channels/{payload.guild_id}/{payload.channel_id}/{payload.message_id}",
            "at": discord.utils.utcnow(),
        })

    # ----- commands -----------------------------------------------------------

    @staticmethod
    def _pick(entries: deque, number: int):
        """number 1 = newest. None if there isn't one."""
        if not 1 <= number <= len(entries):
            return None
        return list(entries)[-number]

    @commands.hybrid_command(name="snipe", aliases=["s"])
    async def snipe(self, ctx: commands.Context, number: commands.Range[int, 1, KEEP] = 1) -> None:
        """Show the last deleted message here (add a number to go further back)."""
        entry = self._pick(self.deleted[ctx.channel.id], number)
        if entry is None:
            await ctx.reply("Nothing to snipe.", mention_author=False)
            return
        author = entry["author"]
        embed = discord.Embed(
            description=entry["content"][:4000] or "*(no text)*",
            colour=discord.Colour(0x000000),
            timestamp=entry["at"],
        )
        embed.set_author(name=str(author), icon_url=author.display_avatar.url)
        if entry["attachments"]:
            embed.add_field(name="Attachments", value="\n".join(entry["attachments"])[:1000], inline=False)
            embed.set_image(url=entry["attachments"][0])
        embed.set_footer(text=f"Deleted \u2022 {number}/{len(self.deleted[ctx.channel.id])}")
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="editsnipe", aliases=["es"])
    async def editsnipe(self, ctx: commands.Context, number: commands.Range[int, 1, KEEP] = 1) -> None:
        """Show the last edited message here (before and after)."""
        entry = self._pick(self.edited[ctx.channel.id], number)
        if entry is None:
            await ctx.reply("Nothing to snipe.", mention_author=False)
            return
        author = entry["author"]
        embed = discord.Embed(colour=discord.Colour(0x000000), timestamp=entry["at"])
        embed.set_author(name=str(author), icon_url=author.display_avatar.url)
        embed.add_field(name="Before", value=entry["before"][:1000] or "*(empty)*", inline=False)
        embed.add_field(name="After", value=entry["after"][:1000] or "*(empty)*", inline=False)
        embed.add_field(name="Message", value=f"[jump]({entry['url']})", inline=False)
        embed.set_footer(text=f"Edited \u2022 {number}/{len(self.edited[ctx.channel.id])}")
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="reactionsnipe", aliases=["rs"])
    async def reactionsnipe(self, ctx: commands.Context, number: commands.Range[int, 1, KEEP] = 1) -> None:
        """Show the last reaction that was removed here."""
        entry = self._pick(self.reactions[ctx.channel.id], number)
        if entry is None:
            await ctx.reply("Nothing to snipe.", mention_author=False)
            return
        embed = discord.Embed(
            description=f"<@{entry['user_id']}> removed {entry['emoji']} from [this message]({entry['url']})",
            colour=discord.Colour(0x000000),
            timestamp=entry["at"],
        )
        embed.set_footer(text=f"Reaction removed \u2022 {number}/{len(self.reactions[ctx.channel.id])}")
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(name="clearsnipe", aliases=["cs"])
    @commands.has_guild_permissions(manage_messages=True)
    async def clearsnipe(self, ctx: commands.Context) -> None:
        """Wipe everything sniped in this channel."""
        for store in (self.deleted, self.edited, self.reactions):
            store.pop(ctx.channel.id, None)
        await ctx.reply("Snipe cleared for this channel.", mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Snipe(bot))
