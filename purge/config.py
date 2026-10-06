import os

from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Copy .env.example to .env and paste your bot token in.")

DEFAULT_PREFIX = os.getenv("DEFAULT_PREFIX", ",")
DB_PATH = os.getenv("DB_PATH", "data/bot.db")

TOPFLOOR_ROLE_ID = 1555684138177138810
TOPFLOOR_MANAGED_ROLE_ID = 1555684218854441041
BOT_ONLY_ROLE_ID = 1555684180665307317
AUTHORIZED_BOT_ROLE_ID = 1555705477378080921

# Slash commands only need syncing when you add/change them. Set to true for one
# start, then turn it back off (syncing on every boot gets rate limited).
SYNC_ON_START = os.getenv("SYNC_ON_START", "false").lower() == "true"
