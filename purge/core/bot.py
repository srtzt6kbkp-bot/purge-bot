from __future__ import annotations

import logging
from pathlib import Path

import discord
from discord.ext import commands

import config
from core.database import Database

log = logging.getLogger("hoodbot")

COGS_DIR = Path(__file__).resolve().parent.parent / "cogs"


async def resolve_prefix(bot: "HoodBot", message: discord.Message):
    """Per-server prefix, falling back to the default. Mentioning the bot always works."""
    if message.guild is None:
        prefix = config.DEFAULT_PREFIX
    else:
        prefix = await bot.get_guild_prefix(message.guild.id)
    return commands.when_mentioned_or(prefix)(bot, message)


class HoodBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True          # privileged: enable in the developer portal
        intents.message_content = True  # privileged: enable in the developer portal
        intents.auto_moderation_configuration = True
        intents.auto_moderation_execution = True
        super().__init__(
            command_prefix=resolve_prefix,
            intents=intents,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False),
        )
        self.db = Database(config.DB_PATH)
        self.prefix_cache: dict[int, str] = {}

    # ----- startup / shutdown -------------------------------------------------

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.load_all_cogs()
        if config.SYNC_ON_START:
            synced = await self.tree.sync()
            log.info("Synced %d slash commands", len(synced))

    async def load_all_cogs(self) -> None:
        """Load every cogs/*.py file. Adding a module = dropping a file in cogs/."""
        for path in sorted(COGS_DIR.glob("*.py")):
            if path.stem.startswith("_"):
                continue
            extension = f"cogs.{path.stem}"
            try:
                await self.load_extension(extension)
                log.info("Loaded %s", extension)
            except Exception:
                log.exception("Failed to load %s", extension)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s) in %d servers", self.user, self.user.id, len(self.guilds))

    async def close(self) -> None:
        await self.db.close()
        await super().close()

    # ----- helpers ------------------------------------------------------------

    async def get_guild_prefix(self, guild_id: int) -> str:
        prefix = self.prefix_cache.get(guild_id)
        if prefix is None:
            row = await self.db.fetchone("SELECT prefix FROM guild_settings WHERE guild_id = ?", guild_id)
            prefix = row["prefix"] if row else config.DEFAULT_PREFIX
            self.prefix_cache[guild_id] = prefix
        return prefix

    # ----- errors -------------------------------------------------------------

    async def on_command_error(self, ctx: commands.Context, error: Exception) -> None:
        if ctx.command is not None and ctx.command.has_error_handler():
            return
        if ctx.cog is not None and ctx.cog.has_error_handler():
            return

        error = getattr(error, "original", error)

        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.NoPrivateMessage):
            message = "That only works inside a server."
        elif isinstance(error, commands.MissingPermissions):
            needed = ", ".join(p.replace("_", " ") for p in error.missing_permissions)
            message = f"You need these permissions: {needed}."
        elif isinstance(error, commands.BotMissingPermissions):
            needed = ", ".join(p.replace("_", " ") for p in error.missing_permissions)
            message = f"I'm missing these permissions: {needed}."
        elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            message = f"{error} (see `help {ctx.command.qualified_name}`)" if ctx.command else str(error)
        elif isinstance(error, commands.CheckFailure):
            message = str(error) or "You can't use that command."
        else:
            log.error("Unhandled error in command %s", ctx.command, exc_info=error)
            message = "Something went wrong on my end. It's been logged."

        try:
            await ctx.reply(message, mention_author=False, ephemeral=True)
        except discord.HTTPException:
            pass
