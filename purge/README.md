# Purge

A modular Discord bot: VoiceMaster, Antinuke, Booster Roles, per-server prefix.
Every command works as a prefix command (`,vm lock`) and as a slash command (`/voicemaster lock`).

## Layout

```
hoodbot/
├── main.py              entry point
├── config.py            reads .env
├── core/
│   ├── bot.py           intents, DB connect, auto cog loader, error handler
│   └── database.py      SQLite wrapper + the schema for every module
└── cogs/                one file per module; drop a new file in and it loads
    ├── voicemaster.py
    ├── antinuke.py
    ├── boosterroles.py
    └── settings.py
```

## Run it (Python 3.10+)

1. https://discord.com/developers/applications -> your app -> Bot:
   turn ON **Server Members Intent** and **Message Content Intent**, copy the token.
2. `pip install -r requirements.txt`
3. `cp .env.example .env` and paste the token.
4. First run: set `SYNC_ON_START=true` once so slash commands register, then set it back to false.
5. `python main.py`

## Invite permissions

Manage Roles, Manage Channels, Move Members, Ban Members, Kick Members,
View Audit Log, Send Messages, Embed Links.

**Drag the bot's role near the top of your role list.** Antinuke can't ban/strip anyone
whose top role sits above the bot's, and booster roles can't be positioned above it.

## Adding a module

Create `cogs/yourthing.py`:

```python
from discord.ext import commands

class YourThing(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

async def setup(bot):
    await bot.add_cog(YourThing(bot))
```

Add any new tables to `SCHEMA` in `core/database.py`. Restart and it's live.

## Guided server setup

When invited, the bot posts a Quick Start panel in a channel where it can send
embeds. Server administrators can reopen it with `,setup`. Use the individual
buttons to configure private logs, AutoMod, VoiceMaster, jail, or Antinuke.
“Set It All Up” asks for confirmation first: it creates a private `#bot-logs`,
sets up the available modules, adds a Jailed role denied access to existing
channels, and enables Antinuke with kick punishment while trusting current
administrators. Review the warnings before confirming. The bot needs the
corresponding Discord permissions; setup reports any steps it cannot complete.

## Scheduled channel nukes

Server administrators can run `,nuke schedule 24h #nuke-logs` in the text channel
they want to schedule. Confirm the prompt to enable it. Before each replacement,
the bot exports the channel history to a `.txt` file in both the old channel and
the configured archive channel. The message history is not copied to the replacement.
Schedules persist across bot restarts; use `,nuke status` to check one or
`,nuke stop` in that channel to cancel it. Intervals are `1h` through `30d`.
The bot keeps the channel intact if it cannot create or upload the transcript.
For slash commands, use `/nuke schedule`; run the immediate confirmed version
with `,nuke` or `/nuke run`.

## Direct messages

Only the bot application owner and users explicitly granted global access with
`botcustom globalgrant` can see or use these commands; server administrators
don't get access automatically. Use `,dm list` to see the DM commands. Send a message
to all human members using `,dm all Your message`, or to members with a role
using `,dm role @role Your message`; the bot shows the recipient count and
message and waits for confirmation before sending. Broadcasts are limited to
one per server per minute. Use `,dm user @member Your message` to DM one member.
These commands are also available as `/dm` slash commands. Members with DMs
disabled may not receive the message.

## Auto-responses and reactions

Server administrators with **Manage Server** can configure per-server substring
rules. Use `,autoresponder add ven {user} is the best owner` to reply when a
message contains `ven`; `{user}` or `{mention}` mentions the person who wrote the
message, and `{username}` inserts their display name. Matches ignore letter
case. If multiple response substrings match, only the longest match replies.
Use `,autoresponder list` and `,autoresponder remove ven` to manage replies.

Use `,autoreact add hi 👋` to react to messages containing `hi` with a waving
emoji. Unicode and custom Discord emojis are supported. All matching reaction
rules are applied. Use `,autoreact list` and `,autoreact remove hi` to manage
them. Both rule types persist across restarts and are also available as slash
commands. The bot needs **Send Messages** to reply and **Add Reactions** plus
access to the chosen emoji to react.

## Restricted role access

Role IDs are configured independently for each server. An administrator runs
`,topfloor setup @role` once to select that server's initial Topfloor role; after
that, only members with the configured role can use the hidden `topfloor` section
or edit its settings. Use `,topfloor config` to inspect the current role mapping,
then `,topfloor set <topfloor|managed|bot-only|authorized-bot> @role` to update
one role. `,topfloor grant @member` grants the configured managed role, and
`,topfloor remove @member @role` removes either protected role.

When a protected role is granted directly in Discord, the managed role is allowed
only when the actor has the configured Topfloor role. The bot-only role is allowed
only when the actor is a bot with the configured authorized-bot role. Unauthorized
grants are removed when the bot receives the corresponding Discord audit-log
event. The bot needs **Manage Roles** and **View Audit Log**, and its role must be
above both protected roles. Set `SYNC_ON_START=true` once to register the slash
commands, then turn it back off.

## Server audit logs

Server administrators with **Manage Server** can run `,logs setup` to create a
private `Purge Logs` category containing `#bot-logs`, `#moderation-logs`,
`#automod-logs`, `#antinuke-logs`, and `#role-logs`. The category is hidden from
normal members and visible to administrators, the configured Topfloor role, and
the bot. The command can be run again to repair/recreate missing channels. Use
`,logs status` to check every log destination, `,logs set #channel` to change the
general log destination, or `,logs clear` to disable logging.

`#bot-logs` records successful security/admin commands that change server state
(including role access, AutoMod/Antinuke settings, and other supported setup
commands), not their arguments or message contents. Moderation cases, Antinuke
alerts, AutoMod events, and protected-role changes are routed to their respective
channels. The bot needs **Manage Channels**, **Send Messages**, and **Embed Links**.

## Antinuke notes

- Only the **server owner** can configure it (so a rogue admin can't turn it off).
- `antinuke trust @user` exempts trusted staff/bots, do this before you do any bulk cleanup.
- Defaults: 3 channel/role deletes or 4 bans/kicks inside 60s triggers the punishment.
