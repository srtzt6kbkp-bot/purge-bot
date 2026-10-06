from __future__ import annotations

import logging
import random
import time

import discord
from discord.ext import commands, tasks

from core.utils import parse_duration, parse_id

log = logging.getLogger("hoodbot.giveaways")

MIN_SECONDS = 10
MAX_SECONDS = 30 * 24 * 3600
BLACK = discord.Colour(0x000000)


class GiveawayView(discord.ui.View):
    """Buttons for entering and host-only giveaway management."""

    def __init__(self, cog: "Giveaways", host_id: int) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.host_id = host_id

    @discord.ui.button(label="Enter", emoji="\U0001F389", style=discord.ButtonStyle.primary, custom_id="gw:enter")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        db = self.cog.bot.db
        message_id = interaction.message.id
        row = await db.fetchone("SELECT ended FROM giveaways WHERE message_id = ?", message_id)
        if row is None or row["ended"]:
            await interaction.response.send_message("This giveaway has ended.", ephemeral=True)
            return

        entered = await db.fetchone(
            "SELECT 1 FROM giveaway_entries WHERE message_id = ? AND user_id = ?", message_id, interaction.user.id
        )
        if entered:
            await db.execute(
                "DELETE FROM giveaway_entries WHERE message_id = ? AND user_id = ?", message_id, interaction.user.id
            )
            text = "You left the giveaway."
        else:
            await db.execute(
                "INSERT INTO giveaway_entries (message_id, user_id) VALUES (?, ?)", message_id, interaction.user.id
            )
            text = "You're in. Good luck!"
        count = await db.fetchone("SELECT COUNT(*) AS n FROM giveaway_entries WHERE message_id = ?", message_id)
        await interaction.response.send_message(f"{text} ({count['n']} entries)", ephemeral=True)

    @discord.ui.button(label="Participants", emoji="\U0001F465", style=discord.ButtonStyle.secondary, custom_id="gw:participants")
    async def participants(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.host_id:
            await interaction.response.send_message("Only the giveaway host can check the participant list.", ephemeral=True)
            return
        text = await self.cog._participants_text(interaction.guild, interaction.message.id)
        await interaction.response.send_message(text, ephemeral=True)


class Giveaways(commands.Cog):
    """Button-based giveaways that end on their own."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.check_giveaways.start()

    def cog_unload(self) -> None:
        self.check_giveaways.cancel()

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    # ----- internals ----------------------------------------------------------

    @tasks.loop(seconds=15)
    async def check_giveaways(self) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT * FROM giveaways WHERE ended = 0 AND ends_at <= ?", int(time.time())
        )
        for row in rows:
            try:
                await self._finish(row)
            except Exception:
                log.exception("Failed to end giveaway %s", row["message_id"])

    @check_giveaways.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    @staticmethod
    def _running_embed(prize: str, winners: int, ends_at: int, host_id: int) -> discord.Embed:
        return discord.Embed(
            title=prize,
            colour=BLACK,
            description=(
                "Press the button to enter.\n"
                f"Ends: <t:{ends_at}:R> (<t:{ends_at}:f>)\n"
                f"Hosted by: <@{host_id}>\n"
                f"Winners: **{winners}**"
            ),
        )

    async def _participants_text(self, guild: discord.Guild | None, message_id: int) -> str:
        if guild is None:
            return "I couldn't find the guild for this giveaway."
        rows = await self.bot.db.fetchall(
            "SELECT user_id FROM giveaway_entries WHERE message_id = ? ORDER BY user_id",
            message_id,
        )
        if not rows:
            return "No one has entered this giveaway yet."
        members = [guild.get_member(r["user_id"]) for r in rows]
        members = [m for m in members if m is not None]
        if not members:
            return "No valid members are in the participant list right now."
        lines = "\n".join(f"- {m.mention}" for m in members)
        return f"Participants for this giveaway:\n{lines}"

    async def _pick(self, guild: discord.Guild, message_id: int, count: int) -> list[discord.Member]:
        rows = await self.bot.db.fetchall("SELECT user_id FROM giveaway_entries WHERE message_id = ?", message_id)
        eligible = []
        for r in rows:
            member = guild.get_member(r["user_id"])
            if member is not None and not member.bot:
                eligible.append(member)
        return random.sample(eligible, min(count, len(eligible)))

    async def _finish(self, row) -> None:
        # Mark it first so a slow finish can't run twice.
        await self.bot.db.execute("UPDATE giveaways SET ended = 1 WHERE message_id = ?", row["message_id"])
        guild = self.bot.get_guild(row["guild_id"])
        channel = guild.get_channel(row["channel_id"]) if guild else None
        if guild is None or not isinstance(channel, discord.abc.Messageable):
            return

        winners = await self._pick(guild, row["message_id"], row["winners"])
        mentions = ", ".join(w.mention for w in winners)
        embed = discord.Embed(
            title=row["prize"],
            colour=BLACK,
            description=(
                "Giveaway ended.\n"
                f"Winner(s): {mentions or 'nobody (no valid entries)'}\n"
                f"Hosted by: <@{row['host_id']}>"
            ),
        )
        message = None
        try:
            message = await channel.fetch_message(row["message_id"])
            await message.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass
        if winners:
            try:
                await channel.send(f"Congratulations {mentions}! You won **{row['prize']}**.", reference=message)
            except discord.HTTPException:
                pass

    # ----- commands -----------------------------------------------------------

    @commands.hybrid_group(name="giveaway", aliases=["gw"], invoke_without_command=True)
    async def giveaway(self, ctx: commands.Context) -> None:
        """Run giveaways."""
        await ctx.send_help(ctx.command)

    @giveaway.command(name="start")
    @commands.has_guild_permissions(manage_guild=True)
    async def gw_start(self, ctx: commands.Context, duration: str, winners: commands.Range[int, 1, 20],
                       *, prize: str) -> None:
        """Start a giveaway: duration (like 1h or 2d), winner count, then the prize."""
        seconds = parse_duration(duration)
        if seconds is None or not MIN_SECONDS <= seconds <= MAX_SECONDS:
            await ctx.reply("Give a duration from 10s to 30d, like `30m`, `1h` or `2d`.", mention_author=False)
            return
        prize = prize.strip()[:200]
        ends_at = int(time.time()) + seconds
        view = GiveawayView(self, ctx.author.id)
        self.bot.add_view(view)
        message = await ctx.channel.send(
            embed=self._running_embed(prize, winners, ends_at, ctx.author.id), view=view
        )
        await self.bot.db.execute(
            """
            INSERT INTO giveaways (message_id, guild_id, channel_id, host_id, prize, winners, ends_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            message.id, ctx.guild.id, ctx.channel.id, ctx.author.id, prize, winners, ends_at,
        )
        await ctx.reply("Giveaway started.", mention_author=False, ephemeral=True)

    @giveaway.command(name="participants")
    @commands.has_guild_permissions(manage_guild=True)
    async def gw_participants(self, ctx: commands.Context, message: str) -> None:
        """Privately show the host who has entered a giveaway."""
        message_id = parse_id(message)
        row = await self.bot.db.fetchone(
            "SELECT host_id FROM giveaways WHERE message_id = ? AND guild_id = ? AND ended = 0",
            message_id or 0, ctx.guild.id,
        )
        if row is None:
            await ctx.reply("I can't find a running giveaway with that ID.", mention_author=False, ephemeral=True)
            return
        if ctx.author.id != row["host_id"]:
            await ctx.reply("Only the giveaway host can view the participant list.", mention_author=False, ephemeral=True)
            return
        text = await self._participants_text(ctx.guild, message_id or 0)
        await ctx.reply(text, mention_author=False, ephemeral=True)

    @giveaway.command(name="edit")
    @commands.has_guild_permissions(manage_guild=True)
    async def gw_edit(
        self,
        ctx: commands.Context,
        message: str,
        duration: str | None = None,
        winners: commands.Range[int, 1, 20] | None = None,
        *, prize: str | None = None,
    ) -> None:
        """Edit the duration, winner count, or prize of a running giveaway."""
        message_id = parse_id(message)
        row = await self.bot.db.fetchone(
            "SELECT * FROM giveaways WHERE message_id = ? AND guild_id = ? AND ended = 0",
            message_id or 0, ctx.guild.id,
        )
        if row is None:
            await ctx.reply("I can't find a running giveaway with that ID.", mention_author=False, ephemeral=True)
            return
        if ctx.author.id != row["host_id"]:
            await ctx.reply("Only the giveaway host can edit it.", mention_author=False, ephemeral=True)
            return

        if duration is None and winners is None and prize is None:
            await ctx.reply("Tell me what to change: duration, winners, or prize.", mention_author=False, ephemeral=True)
            return

        new_prize = row["prize"]
        new_winners = row["winners"]
        new_ends_at = row["ends_at"]

        if prize is not None:
            new_prize = prize.strip()[:200]
        if winners is not None:
            new_winners = winners
        if duration is not None:
            seconds = parse_duration(duration)
            if seconds is None or not MIN_SECONDS <= seconds <= MAX_SECONDS:
                await ctx.reply("Give a duration from 10s to 30d, like `30m`, `1h` or `2d`.", mention_author=False, ephemeral=True)
                return
            new_ends_at = int(time.time()) + seconds

        sql = "UPDATE giveaways SET prize = ?, winners = ?, ends_at = ? WHERE message_id = ?"
        await self.bot.db.execute(sql, new_prize, new_winners, new_ends_at, row["message_id"])

        channel = ctx.guild.get_channel(row["channel_id"])
        if isinstance(channel, discord.abc.Messageable):
            try:
                giveaway_message = await channel.fetch_message(row["message_id"])
                view = GiveawayView(self, row["host_id"])
                self.bot.add_view(view)
                await giveaway_message.edit(
                    embed=self._running_embed(new_prize, new_winners, new_ends_at, row["host_id"]),
                    view=view,
                )
            except discord.HTTPException:
                pass

        await ctx.reply(
            f"Updated the giveaway: prize `{new_prize}` | winners `{new_winners}` | ends <t:{new_ends_at}:R>.",
            mention_author=False,
            ephemeral=True,
        )

    @giveaway.command(name="end")
    @commands.has_guild_permissions(manage_guild=True)
    async def gw_end(self, ctx: commands.Context, message: str) -> None:
        """End a giveaway right now (give the message ID or link)."""
        message_id = parse_id(message)
        row = await self.bot.db.fetchone(
            "SELECT * FROM giveaways WHERE message_id = ? AND guild_id = ? AND ended = 0",
            message_id or 0, ctx.guild.id,
        )
        if row is None:
            await ctx.reply("I can't find a running giveaway with that ID.", mention_author=False)
            return
        await self._finish(row)
        await ctx.reply("Ended.", mention_author=False, ephemeral=True)

    @giveaway.command(name="reroll")
    @commands.has_guild_permissions(manage_guild=True)
    async def gw_reroll(self, ctx: commands.Context, message: str,
                        count: commands.Range[int, 1, 20] = 1) -> None:
        """Pick new winner(s) for a finished giveaway."""
        message_id = parse_id(message)
        row = await self.bot.db.fetchone(
            "SELECT * FROM giveaways WHERE message_id = ? AND guild_id = ? AND ended = 1",
            message_id or 0, ctx.guild.id,
        )
        if row is None:
            await ctx.reply("I can't find a finished giveaway with that ID.", mention_author=False)
            return
        winners = await self._pick(ctx.guild, row["message_id"], count)
        if not winners:
            await ctx.reply("There are no valid entries to pick from.", mention_author=False)
            return
        mentions = ", ".join(w.mention for w in winners)
        await ctx.reply(f"New winner(s): {mentions}. Congratulations, you won **{row['prize']}**!",
                        mention_author=False)

    @giveaway.command(name="list")
    @commands.has_guild_permissions(manage_guild=True)
    async def gw_list(self, ctx: commands.Context) -> None:
        """Show the giveaways that are still running."""
        rows = await self.bot.db.fetchall(
            "SELECT * FROM giveaways WHERE guild_id = ? AND ended = 0 ORDER BY ends_at", ctx.guild.id
        )
        lines = [
            f"**{r['prize']}** ends <t:{r['ends_at']}:R> - "
            f"[jump](https://discord.com/channels/{r['guild_id']}/{r['channel_id']}/{r['message_id']})"
            for r in rows
        ]
        embed = discord.Embed(title="Running giveaways", description="\n".join(lines) or "None right now.",
                              colour=BLACK)
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Giveaways(bot))
