from __future__ import annotations

import logging

import discord
from discord.ext import commands

log = logging.getLogger("hoodbot.autoresponder")
MAX_TRIGGER_LENGTH = 100
MAX_RESPONSE_LENGTH = 1800
MAX_LIST_MESSAGE_LENGTH = 1800


def normalize_trigger(trigger: str) -> str | None:
    trigger = trigger.strip()
    if not trigger or len(trigger) > MAX_TRIGGER_LENGTH:
        return None
    return trigger


class AutoResponder(commands.Cog):
    """Configurable, per-server substring replies and reactions."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.hybrid_group(name="autoresponder", invoke_without_command=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def autoresponder(self, ctx: commands.Context) -> None:
        """Manage automatic replies triggered by message substrings."""
        await ctx.send_help(ctx.command)

    @autoresponder.command(name="add")
    @commands.has_guild_permissions(manage_guild=True)
    async def autoresponder_add(
        self,
        ctx: commands.Context,
        trigger: str,
        *,
        response: str,
    ) -> None:
        """Add or update a reply for a substring; use {user} to mention its author."""
        trigger_text = normalize_trigger(trigger)
        response = response.strip()
        if trigger_text is None:
            await ctx.reply(
                f"Give a substring between 1 and {MAX_TRIGGER_LENGTH} characters.",
                mention_author=False,
                ephemeral=ctx.interaction is not None,
            )
            return
        if not response or len(response) > MAX_RESPONSE_LENGTH:
            await ctx.reply(
                f"Give a response between 1 and {MAX_RESPONSE_LENGTH} characters.",
                mention_author=False,
                ephemeral=ctx.interaction is not None,
            )
            return

        await self.bot.db.execute(
            """
            INSERT INTO autoresponder_rules (guild_id, trigger_key, trigger, response)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (guild_id, trigger_key)
            DO UPDATE SET trigger = excluded.trigger, response = excluded.response
            """,
            ctx.guild.id,
            trigger_text.casefold(),
            trigger_text,
            response,
        )
        await ctx.reply(
            f"Auto-response saved for substring `{trigger_text}`.",
            mention_author=False,
            ephemeral=ctx.interaction is not None,
        )

    @autoresponder.command(name="remove")
    @commands.has_guild_permissions(manage_guild=True)
    async def autoresponder_remove(self, ctx: commands.Context, trigger: str) -> None:
        """Remove an automatic reply for a substring."""
        trigger_text = normalize_trigger(trigger)
        if trigger_text is None:
            await ctx.reply(
                "Give a non-empty substring to remove.",
                mention_author=False,
                ephemeral=ctx.interaction is not None,
            )
            return
        await self.bot.db.execute(
            "DELETE FROM autoresponder_rules WHERE guild_id = ? AND trigger_key = ?",
            ctx.guild.id,
            trigger_text.casefold(),
        )
        await ctx.reply(
            f"Removed the auto-response for `{trigger_text}` (if it existed).",
            mention_author=False,
            ephemeral=ctx.interaction is not None,
        )

    @autoresponder.command(name="list")
    @commands.has_guild_permissions(manage_guild=True)
    async def autoresponder_list(self, ctx: commands.Context) -> None:
        """List this server's automatic replies."""
        rows = await self.bot.db.fetchall(
            "SELECT trigger, response FROM autoresponder_rules WHERE guild_id = ? ORDER BY trigger_key",
            ctx.guild.id,
        )
        if not rows:
            await ctx.reply(
                "No auto-responses are configured.",
                mention_author=False,
                ephemeral=ctx.interaction is not None,
            )
            return
        lines = [f"`{row['trigger']}` → {row['response']}" for row in rows]
        await self._send_rule_lines(ctx, lines)

    @commands.hybrid_group(name="autoreact", invoke_without_command=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def autoreact(self, ctx: commands.Context) -> None:
        """Manage automatic reactions triggered by message substrings."""
        await ctx.send_help(ctx.command)

    @autoreact.command(name="add")
    @commands.has_guild_permissions(manage_guild=True)
    async def autoreact_add(self, ctx: commands.Context, trigger: str, emoji: str) -> None:
        """Add or update a reaction for a substring."""
        trigger_text = normalize_trigger(trigger)
        emoji_text = emoji.strip()
        if trigger_text is None:
            await ctx.reply(
                f"Give a substring between 1 and {MAX_TRIGGER_LENGTH} characters.",
                mention_author=False,
                ephemeral=ctx.interaction is not None,
            )
            return
        if not emoji_text or len(emoji_text) > 100:
            await ctx.reply("Give a valid emoji.", mention_author=False, ephemeral=ctx.interaction is not None)
            return

        await self.bot.db.execute(
            """
            INSERT INTO autoreact_rules (guild_id, trigger_key, trigger, emoji)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (guild_id, trigger_key)
            DO UPDATE SET trigger = excluded.trigger, emoji = excluded.emoji
            """,
            ctx.guild.id,
            trigger_text.casefold(),
            trigger_text,
            emoji_text,
        )
        await ctx.reply(
            f"Auto-reaction {emoji_text} saved for substring `{trigger_text}`.",
            mention_author=False,
            ephemeral=ctx.interaction is not None,
        )

    @autoreact.command(name="remove")
    @commands.has_guild_permissions(manage_guild=True)
    async def autoreact_remove(self, ctx: commands.Context, trigger: str) -> None:
        """Remove an automatic reaction for a substring."""
        trigger_text = normalize_trigger(trigger)
        if trigger_text is None:
            await ctx.reply(
                "Give a non-empty substring to remove.",
                mention_author=False,
                ephemeral=ctx.interaction is not None,
            )
            return
        await self.bot.db.execute(
            "DELETE FROM autoreact_rules WHERE guild_id = ? AND trigger_key = ?",
            ctx.guild.id,
            trigger_text.casefold(),
        )
        await ctx.reply(
            f"Removed the auto-reaction for `{trigger_text}` (if it existed).",
            mention_author=False,
            ephemeral=ctx.interaction is not None,
        )

    @autoreact.command(name="list")
    @commands.has_guild_permissions(manage_guild=True)
    async def autoreact_list(self, ctx: commands.Context) -> None:
        """List this server's automatic reactions."""
        rows = await self.bot.db.fetchall(
            "SELECT trigger, emoji FROM autoreact_rules WHERE guild_id = ? ORDER BY trigger_key",
            ctx.guild.id,
        )
        if not rows:
            await ctx.reply(
                "No auto-reactions are configured.",
                mention_author=False,
                ephemeral=ctx.interaction is not None,
            )
            return
        lines = [f"`{row['trigger']}` → {row['emoji']}" for row in rows]
        await self._send_rule_lines(ctx, lines)

    @staticmethod
    async def _send_rule_lines(ctx: commands.Context, lines: list[str]) -> None:
        chunks: list[str] = []
        chunk = ""
        for line in lines:
            if chunk and len(chunk) + len(line) + 1 > MAX_LIST_MESSAGE_LENGTH:
                chunks.append(chunk)
                chunk = ""
            chunk = f"{chunk}\n{line}".strip()
        if chunk:
            chunks.append(chunk)

        for text in chunks:
            await ctx.send(
                text,
                allowed_mentions=discord.AllowedMentions.none(),
                ephemeral=ctx.interaction is not None,
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or not message.content:
            return

        context = await self.bot.get_context(message)
        if context.valid:
            return

        content = message.content.casefold()
        guild_id = message.guild.id
        responses = await self.bot.db.fetchall(
            "SELECT trigger_key, response FROM autoresponder_rules WHERE guild_id = ?",
            guild_id,
        )
        matching_responses = [
            row for row in responses if row["trigger_key"] in content
        ]
        if matching_responses:
            response = max(matching_responses, key=lambda row: len(row["trigger_key"]))["response"]
            response = (
                response.replace("{user}", message.author.mention)
                .replace("{mention}", message.author.mention)
                .replace("{username}", message.author.display_name)
            )
            try:
                await message.reply(
                    response,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False,
                        roles=False,
                        users=[message.author],
                    ),
                )
            except discord.HTTPException as error:
                log.warning(
                    "Couldn't send auto-response in guild %s for message %s: %s",
                    guild_id,
                    message.id,
                    error,
                )

        reactions = await self.bot.db.fetchall(
            "SELECT trigger_key, emoji FROM autoreact_rules WHERE guild_id = ?",
            guild_id,
        )
        for rule in reactions:
            if rule["trigger_key"] not in content:
                continue
            try:
                await message.add_reaction(rule["emoji"])
            except discord.HTTPException as error:
                log.warning(
                    "Couldn't add auto-reaction in guild %s for message %s: %s",
                    guild_id,
                    message.id,
                    error,
                )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AutoResponder(bot))
