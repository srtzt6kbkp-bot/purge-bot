import discord
from discord.ext import commands
from discord import app_commands
from core.audit import send_audit_log
import re
import time
from collections import defaultdict, deque
from datetime import timedelta
import logging

log = logging.getLogger("hoodbot.automod")

AUTOMOD_FEATURES = {
    "anti_invite", "anti_link", "anti_spam", "anti_mention", "anti_zalgo",
}
FEATURE_NAMES = {
    "invites": "anti_invite",
    "invite": "anti_invite",
    "anti_invite": "anti_invite",
    "links": "anti_link",
    "link": "anti_link",
    "anti_link": "anti_link",
    "spam": "anti_spam",
    "velocity": "anti_spam",
    "anti_spam": "anti_spam",
    "mentions": "anti_mention",
    "mention": "anti_mention",
    "anti_mention": "anti_mention",
    "zalgo": "anti_zalgo",
    "anti_zalgo": "anti_zalgo",
}

class AutoMod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Anti-spam message velocity tracking: {guild_id: {user_id: deque([timestamps])}}
        self.message_tracker = defaultdict(lambda: defaultdict(deque))
        # Duplicate message tracker: {guild_id: {user_id: (last_content, count)}}
        self.duplicate_tracker = defaultdict(dict)

    def _native_rule_specs(self, config: dict, words: list[str]):
        trigger_type = discord.AutoModRuleTriggerType
        event_type = discord.AutoModRuleEventType.message_send
        block = lambda text: [discord.AutoModRuleAction(custom_message=text)]
        specs = [
            (
                "anti_invite",
                "Purge Anti Invite Links",
                discord.AutoModTrigger(
                    type=trigger_type.keyword,
                    regex_patterns=[r"(?:discord\.gg|discord(?:app)?\.com/invite)/[A-Za-z0-9-]+"],
                ),
                bool(config["anti_invite"]),
                block("Discord invites are not allowed here."),
            ),
            (
                "anti_link",
                "Purge External Links",
                discord.AutoModTrigger(
                    type=trigger_type.keyword,
                    regex_patterns=[r"https?://[^\s]+"],
                    allow_list=["tenor.com", "giphy.com", "youtube.com", "youtu.be", "spotify.com", "discord.com"],
                ),
                bool(config["anti_link"]),
                block("External links are not allowed here."),
            ),
            (
                "anti_mention",
                "Purge Mention Spam",
                discord.AutoModTrigger(
                    type=trigger_type.mention_spam,
                    mention_limit=min(50, max(1, int(config["max_mentions"]))),
                ),
                bool(config["anti_mention"]),
                block("Please avoid mass mentioning members or roles."),
            ),
            (
                "anti_zalgo",
                "Purge Zalgo Filter",
                discord.AutoModTrigger(
                    type=trigger_type.keyword,
                    regex_patterns=[r"[\u0300-\u036F\u0489]{11,}"],
                ),
                bool(config["anti_zalgo"]),
                block("Distorted Unicode text is not allowed here."),
            ),
        ]
        if words:
            specs.append((
                "blacklist",
                "Purge Blacklisted Words",
                discord.AutoModTrigger(
                    type=trigger_type.keyword,
                    keyword_filter=words[:1000],
                ),
                True,
                block("That word or phrase is not allowed here."),
            ))
        return event_type, specs

    async def sync_native_rules(self, guild: discord.Guild) -> tuple[int, list[str]]:
        """Create or update Purge's native guild rules without making duplicates."""
        config = await self.get_guild_config(guild.id)
        word_rows = await self.bot.db.fetchall(
            "SELECT word FROM automod_words WHERE guild_id = ? ORDER BY word LIMIT 1000", guild.id
        )
        words = [row["word"] for row in word_rows]
        event_type, specs = self._native_rule_specs(config, words)
        try:
            existing_rules = await guild.fetch_automod_rules()
        except discord.NotFound:
            existing_rules = []

        existing = {
            rule.name.removeprefix("Purge ").removeprefix("Hoodbot "): rule
            for rule in existing_rules
            if rule.name.startswith(("Purge ", "Hoodbot "))
        }
        desired_names = {name for _, name, _, _, _ in specs}
        synced = 0
        skipped: list[str] = []
        for feature, name, trigger, enabled, actions in specs:
            rule = existing.get(name.removeprefix("Purge "))
            if rule is None and trigger.type is discord.AutoModRuleTriggerType.mention_spam:
                has_other_mention_rule = any(
                    existing_rule.trigger.type is discord.AutoModRuleTriggerType.mention_spam
                    for existing_rule in existing_rules
                )
                if has_other_mention_rule:
                    skipped.append("mention spam (the server already has its single allowed mention-spam rule)")
                    continue

            try:
                if rule is None:
                    await guild.create_automod_rule(
                        name=name,
                        event_type=event_type,
                        trigger=trigger,
                        actions=actions,
                        enabled=enabled,
                        reason="Configure Purge AutoMod protection",
                    )
                else:
                    await rule.edit(
                        name=name,
                        event_type=event_type,
                        trigger=trigger,
                        actions=actions,
                        enabled=enabled,
                        reason="Sync Purge AutoMod protection",
                    )
                synced += 1
            except discord.HTTPException as exc:
                if "AUTO_MODERATION_MAX_RULES_OF_TYPE_EXCEEDED" not in (exc.text or ""):
                    raise
                skipped.append(f"{feature.replace('_', ' ')} (Discord's rule limit was reached)")
                log.warning("Skipped native AutoMod rule %s in guild %s: Discord's rule limit was reached", name, guild.id)

        for name, rule in existing.items():
            if name == "Blacklisted Words" and "Purge Blacklisted Words" not in desired_names:
                await rule.delete(reason="No Purge blacklisted words remain")

        return synced, skipped

    def clean_text(self, text: str) -> str:
        """Strips zero-width spaces, normalizes leetspeak variants, and flattens spacing to defeat evasion."""
        # Remove zero-width characters and invisible formatting tricks
        invisible_chars = r"[\u200B-\u200D\uFEFF]"
        cleaned = re.sub(invisible_chars, "", text)
        
        # Normalize common leetspeak substitutions
        leetspeak = {'4': 'a', '@': 'a', '3': 'e', '1': 'i', '!': 'i', '0': 'o', '$': 's', '5': 's', '7': 't'}
        for k, v in leetspeak.items():
            cleaned = cleaned.replace(k, v)
            
        return cleaned.lower()

    async def get_guild_config(self, guild_id: int):
        await self.bot.db.execute("INSERT OR IGNORE INTO automod_config (guild_id) VALUES (?)", guild_id)
        row = await self.bot.db.fetchone("SELECT * FROM automod_config WHERE guild_id = ?", guild_id)
        return dict(row)

    async def log_infraction(self, message: discord.Message, reason: str):
        """Dispatches an audit log embed to the configured automod log channel."""
        if message.guild is None:
            return
        config = await self.get_guild_config(message.guild.id)
        log_channel_id = config["log_channel_id"]

        embed = discord.Embed(
            title="AutoMod Triggered",
            colour=discord.Colour(0x000000),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        embed.add_field(name="User", value=f"{message.author.mention} (`{message.author.id}`)", inline=True)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        embed.add_field(name="Trigger Reason", value=f"```fix\n{reason}\n```", inline=False)
        embed.add_field(name="Offending Content", value=f"```text\n{message.content[:900]}\n```", inline=False)
        embed.set_footer(text="Purge Advanced Security Engine")
        if await send_audit_log(self.bot, message.guild, embed, log_type="automod"):
            return

        channel = message.guild.get_channel(log_channel_id) if log_channel_id else None
        if isinstance(channel, discord.abc.Messageable):
            try:
                await channel.send(embed=embed)
            except discord.HTTPException as exc:
                log.warning("Could not deliver AutoMod log in guild %s: %s", message.guild.id, exc)

    @commands.Cog.listener()
    async def on_automod_action(self, execution: discord.AutoModAction) -> None:
        """Report native Discord AutoMod executions to the configured mod channel."""
        guild = self.bot.get_guild(execution.guild_id)
        if guild is None:
            return
        config = await self.get_guild_config(guild.id)
        channel_id = config["log_channel_id"]

        member = guild.get_member(execution.user_id)
        user_label = member.mention if member is not None else f"<@{execution.user_id}>"
        trigger = execution.rule_trigger_type.name.replace("_", " ").title()
        embed = discord.Embed(
            title="Native AutoMod Triggered",
            colour=discord.Colour(0x000000),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="User", value=f"{user_label} (`{execution.user_id}`)", inline=True)
        embed.add_field(name="Channel", value=f"<#{execution.channel_id}>", inline=True)
        embed.add_field(name="Rule", value=f"`{execution.rule_id}` ({trigger})", inline=False)
        if execution.matched_keyword:
            embed.add_field(name="Matched keyword", value=execution.matched_keyword[:256], inline=True)
        if execution.matched_content:
            embed.add_field(name="Matched content", value=execution.matched_content[:1024], inline=False)
        elif execution.content:
            embed.add_field(name="Message", value=execution.content[:1024], inline=False)
        embed.set_footer(text=f"Action: {execution.action.type.name.replace('_', ' ').title()}")
        if await send_audit_log(self.bot, guild, embed, log_type="automod"):
            return
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.abc.Messageable):
            return
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.exception("Could not log native AutoMod execution for guild %s", guild.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        # Bypass check for server owners, administrators, or users with Manage Messages
        if message.author == message.guild.owner or message.author.guild_permissions.manage_messages:
            return

        config = await self.get_guild_config(message.guild.id)
        raw_content = message.content
        normalized = self.clean_text(raw_content)

        # 1. Anti-Invite Filter (Detects standard and obfuscated discord invite links)
        if config["anti_invite"] == 1:
            invite_pattern = r"(discord\.(gg|io|me|li|com\/invite)|discordapp\.com\/invite)\/[a-zA-Z0-9]+"
            if re.search(invite_pattern, normalized):
                try:
                    await message.delete()
                    await message.channel.send(f"{message.author.mention}, ❌ Discord invites are strictly prohibited in this server.", delete_after=5)
                    await self.log_infraction(message, "Discord Invite Link Detection")
                    return
                except Exception:
                    pass

        # 2. Anti-External Links Filter
        if config["anti_link"] == 1:
            url_pattern = r"https?:\/\/(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_\+.~#?&//=]*)"
            # Check if URL exists and is not an allowed domain (e.g. tenor, youtube)
            urls = re.findall(url_pattern, normalized)
            if urls:
                allowed_domains = ["tenor.com", "giphy.com", "youtube.com", "youtu.be", "spotify.com", "discord.com"]
                is_unallowed = any(not any(domain in url for domain in allowed_domains) for url in urls)
                if is_unallowed:
                    try:
                        await message.delete()
                        await message.channel.send(f"{message.author.mention}, ❌ Unauthorized external links are blocked.", delete_after=5)
                        await self.log_infraction(message, "Unauthorized External Link")
                        return
                    except Exception:
                        pass

        # 3. Custom Blacklisted Words Filter
        rows = await self.bot.db.fetchall(
            "SELECT word FROM automod_words WHERE guild_id = ?", message.guild.id
        )
        blacklisted = [row["word"] for row in rows]
        
        if blacklisted:
            for word in blacklisted:
                if word in normalized:
                    try:
                        await message.delete()
                        await message.channel.send(f"{message.author.mention}, ❌ Your message contained a blacklisted term.", delete_after=5)
                        await self.log_infraction(message, f"Blacklisted Keyword Trigger: '{word}'")
                        return
                    except Exception:
                        pass

        # 4. Anti-Mention Spam Filter
        if config["anti_mention"] == 1:
            total_mentions = len(message.mentions) + len(message.role_mentions)
            if total_mentions > config["max_mentions"]:
                try:
                    await message.delete()
                    # Apply an automatic 10-minute timeout for mention raids
                    duration = discord.utils.utcnow() + timedelta(minutes=10)
                    await message.author.timeout(duration, reason="AutoMod: Excessive mention spam")
                    await message.channel.send(f"{message.author.mention}, ❌ You have been timed out for 10 minutes due to excessive mentions.", delete_after=5)
                    await self.log_infraction(message, f"Excessive Mentions ({total_mentions} pings)")
                    return
                except Exception:
                    pass

        # 5. Anti-Zalgo / Unicode Distortion Filter
        if config["anti_zalgo"] == 1:
            zalgo_pattern = r"[\u0300-\u036F\u0489]"
            if len(re.findall(zalgo_pattern, message.content)) > 10:
                try:
                    await message.delete()
                    await message.channel.send(f"{message.author.mention}, ❌ Zalgo / corrupted text distortion is not allowed.", delete_after=5)
                    await self.log_infraction(message, "Zalgo / Text Distortion")
                    return
                except Exception:
                    pass

        # 6. High-Velocity Fast Spam Detection (5 messages within 4 seconds)
        if config["anti_spam"] == 1:
            guild_id = message.guild.id
            user_id = message.author.id
            current_time = time.time()
            
            user_timestamps = self.message_tracker[guild_id][user_id]
            user_timestamps.append(current_time)
            
            # Prune timestamps older than 4 seconds
            while user_timestamps and current_time - user_timestamps[0] > 4.0:
                user_timestamps.popleft()
                
            if len(user_timestamps) >= 5:
                try:
                    await message.delete()
                    duration = discord.utils.utcnow() + timedelta(minutes=5)
                    await message.author.timeout(duration, reason="AutoMod: Chat spam velocity limit exceeded")
                    await message.channel.send(f"{message.author.mention}, ❌ Slow down! You have been muted for spamming chat.", delete_after=5)
                    await self.log_infraction(message, "Message Velocity Spam (5+ messages in 4s)")
                    user_timestamps.clear()
                    return
                except Exception:
                    pass

    @commands.hybrid_group(name="automod", aliases=["am"], invoke_without_command=True)
    @commands.has_permissions(administrator=True)
    async def automod(self, ctx):
        """Manage advanced AutoMod settings and rules."""
        config = await self.get_guild_config(ctx.guild.id)
        prefix = ctx.clean_prefix
        
        embed = discord.Embed(
            title="🛡️ Advanced AutoMod Dashboard",
            description="Native Discord rules handle links, invites, mentions, and keywords; Purge handles message velocity.",
            color=discord.Color(0x000000)
        )
        log_channel = f"<#{config['log_channel_id']}>" if config["log_channel_id"] else "Not set"
        embed.add_field(
            name="Current Module States",
            value="\n".join((
                f"• **Anti-Invite:** `{'Enabled' if config['anti_invite'] else 'Disabled'}` (native)",
                f"• **Anti-Link:** `{'Enabled' if config['anti_link'] else 'Disabled'}` (native)",
                f"• **Anti-Mention:** `{'Enabled' if config['anti_mention'] else 'Disabled'}` (native, max {config['max_mentions']})",
                f"• **Anti-Zalgo:** `{'Enabled' if config['anti_zalgo'] else 'Disabled'}` (native)",
                f"• **Blacklisted words:** `{len(await self.bot.db.fetchall('SELECT word FROM automod_words WHERE guild_id = ?', ctx.guild.id))}` (native + custom)",
                f"• **Message velocity:** `{'Enabled' if config['anti_spam'] else 'Disabled'}` (custom)",
                f"• **Log channel:** {log_channel}",
            )),
            inline=False
        )
        embed.add_field(
            name="Quick Commands",
            value=(
                f"`{prefix}automod setup` - set up native rules\n"
                f"`{prefix}automod enable invites` / `{prefix}automod disable links`\n"
                f"`{prefix}automod enable spam` - custom message velocity\n"
                f"`{prefix}automod blockword phrase` - add a blocked word\n"
                f"`{prefix}automod logs #channel` - choose the report channel"
            ),
            inline=False,
        )
        embed.set_footer(text="Spam velocity is checked locally; setup syncs native Discord rules.")
        
        await ctx.send(embed=embed)

    @automod.command(name="setup", aliases=["sync"])
    @commands.has_guild_permissions(manage_guild=True)
    @commands.bot_has_guild_permissions(manage_guild=True)
    async def automod_setup(self, ctx):
        """Create or update this server's native Discord AutoMod rules."""
        await self.get_guild_config(ctx.guild.id)
        try:
            count, skipped = await self.sync_native_rules(ctx.guild)
        except discord.Forbidden:
            await ctx.reply("I need **Manage Server** to create or edit native AutoMod rules.", mention_author=False)
            return
        except discord.HTTPException as exc:
            log.exception("Could not sync native AutoMod rules for guild %s", ctx.guild.id)
            await ctx.reply(f"AutoMod settings were kept, but Discord rejected a native rule update (HTTP {exc.status}).", mention_author=False)
            return
        message = f"Synced {count} native Discord AutoMod rules. Message velocity remains handled by Purge."
        if skipped:
            message += " Skipped: " + ", ".join(skipped) + "."
        await ctx.reply(message, mention_author=False)

    async def _set_feature(self, ctx, feature: str, state: bool) -> None:
        feature_key = FEATURE_NAMES.get(feature.lower())
        if feature_key not in AUTOMOD_FEATURES:
            await ctx.reply(
                "Choose: `invites`, `links`, `spam`, `mentions`, or `zalgo`.",
                mention_author=False,
            )
            return
        await self.get_guild_config(ctx.guild.id)
        await self.bot.db.execute(
            f"UPDATE automod_config SET {feature_key} = ? WHERE guild_id = ?",
            int(state),
            ctx.guild.id,
        )
        if feature_key != "anti_spam":
            try:
                _, skipped = await self.sync_native_rules(ctx.guild)
            except discord.HTTPException as exc:
                log.exception("Could not sync native AutoMod feature %s in guild %s", feature_key, ctx.guild.id)
                await ctx.reply(
                    f"Saved the setting, but Discord couldn't sync its native rule (HTTP {exc.status}).",
                    mention_author=False,
                )
                return
        display_name = feature_key.removeprefix("anti_").replace("_", " ")
        message = f"AutoMod **{display_name}** is now {'enabled' if state else 'disabled'}."
        if feature_key != "anti_spam" and skipped:
            message += " Discord skipped: " + ", ".join(skipped) + "."
        await ctx.send(message, ephemeral=True)

    @automod.command(name="toggle")
    @commands.has_permissions(administrator=True)
    @app_commands.describe(feature="The automod module to toggle", state="Enable or disable")
    @app_commands.choices(feature=[
        app_commands.Choice(name="Invites", value="invites"),
        app_commands.Choice(name="Links", value="links"),
        app_commands.Choice(name="Message velocity spam", value="spam"),
        app_commands.Choice(name="Mention spam", value="mentions"),
        app_commands.Choice(name="Zalgo text", value="zalgo"),
    ])
    async def automod_toggle(self, ctx, feature: str, state: bool):
        """Enable or disable an AutoMod feature."""
        await self._set_feature(ctx, feature, state)

    @automod.command(name="enable")
    @commands.has_permissions(administrator=True)
    @app_commands.describe(feature="Protection to enable")
    @app_commands.choices(feature=[
        app_commands.Choice(name="Invites", value="invites"),
        app_commands.Choice(name="Links", value="links"),
        app_commands.Choice(name="Message velocity spam", value="spam"),
        app_commands.Choice(name="Mention spam", value="mentions"),
        app_commands.Choice(name="Zalgo text", value="zalgo"),
    ])
    async def automod_enable(self, ctx, feature: str):
        """Enable one AutoMod feature by its short name."""
        await self._set_feature(ctx, feature, True)

    @automod.command(name="disable")
    @commands.has_permissions(administrator=True)
    @app_commands.describe(feature="Protection to disable")
    @app_commands.choices(feature=[
        app_commands.Choice(name="Invites", value="invites"),
        app_commands.Choice(name="Links", value="links"),
        app_commands.Choice(name="Message velocity spam", value="spam"),
        app_commands.Choice(name="Mention spam", value="mentions"),
        app_commands.Choice(name="Zalgo text", value="zalgo"),
    ])
    async def automod_disable(self, ctx, feature: str):
        """Disable one AutoMod feature by its short name."""
        await self._set_feature(ctx, feature, False)

    @automod.command(name="blockword", aliases=["wordadd", "addword"])
    @commands.has_permissions(administrator=True)
    @app_commands.describe(word="The word or phrase to blacklist")
    async def automod_wordadd(self, ctx, *, word: str):
        """Add a custom blacklisted word to the automod database."""
        clean_word = self.clean_text(word)
        if not clean_word or len(clean_word) > 60:
            await ctx.reply("Blacklist entries must contain 1 to 60 characters for native AutoMod.", mention_author=False)
            return
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO automod_words (guild_id, word) VALUES (?, ?)",
            ctx.guild.id,
            clean_word,
        )
        try:
            await self.sync_native_rules(ctx.guild)
        except discord.HTTPException as exc:
            log.exception("Could not sync native AutoMod blacklist in guild %s", ctx.guild.id)
            await ctx.reply(
                f"Saved the word for custom checks, but Discord couldn't sync the native blacklist (HTTP {exc.status}).",
                mention_author=False,
            )
            return
        await ctx.send(f"✅ Added **`{clean_word}`** to the server's blacklisted word database.", ephemeral=True)

    @automod.command(name="logs", aliases=["logchannel", "setlog"])
    @commands.has_permissions(administrator=True)
    @app_commands.describe(channel="The channel where AutoMod violation logs will be sent")
    async def automod_logchannel(self, ctx, channel: discord.TextChannel):
        """Set the log channel for security notifications."""
        await self.bot.db.execute(
            "INSERT INTO automod_config (guild_id, log_channel_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET log_channel_id = excluded.log_channel_id",
            ctx.guild.id,
            channel.id,
        )
        await ctx.send(f"✅ AutoMod audit logs will now be dispatched to {channel.mention}.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(AutoMod(bot))