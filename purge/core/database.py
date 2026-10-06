from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiosqlite

# Every module's tables live here so the whole schema is in one place.
SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    prefix   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS autoresponder_rules (
    guild_id     INTEGER NOT NULL,
    trigger_key  TEXT NOT NULL,
    trigger      TEXT NOT NULL,
    response     TEXT NOT NULL,
    PRIMARY KEY (guild_id, trigger_key)
);

CREATE TABLE IF NOT EXISTS autoreact_rules (
    guild_id     INTEGER NOT NULL,
    trigger_key  TEXT NOT NULL,
    trigger      TEXT NOT NULL,
    emoji        TEXT NOT NULL,
    PRIMARY KEY (guild_id, trigger_key)
);

CREATE TABLE IF NOT EXISTS role_access_config (
    guild_id              INTEGER PRIMARY KEY,
    topfloor_role_id      INTEGER NOT NULL,
    managed_role_id       INTEGER NOT NULL,
    bot_only_role_id      INTEGER NOT NULL,
    authorized_bot_role_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log_config (
    guild_id       INTEGER PRIMARY KEY,
    log_channel_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log_channels (
    guild_id   INTEGER NOT NULL,
    log_type   TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, log_type)
);

CREATE TABLE IF NOT EXISTS audit_log_setup (
    guild_id   INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS voicemaster_config (
    guild_id       INTEGER PRIMARY KEY,
    hub_channel_id INTEGER NOT NULL,
    category_id    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS voicemaster_channels (
    channel_id INTEGER PRIMARY KEY,
    guild_id   INTEGER NOT NULL,
    owner_id   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS antinuke_config (
    guild_id             INTEGER PRIMARY KEY,
    enabled              INTEGER NOT NULL DEFAULT 0,
    punishment           TEXT    NOT NULL DEFAULT 'ban',
    log_channel_id       INTEGER,
    channel_delete_limit INTEGER NOT NULL DEFAULT 3,
    role_delete_limit    INTEGER NOT NULL DEFAULT 3,
    ban_limit            INTEGER NOT NULL DEFAULT 4,
    kick_limit           INTEGER NOT NULL DEFAULT 4,
    window_seconds       INTEGER NOT NULL DEFAULT 60
);
CREATE TABLE IF NOT EXISTS antinuke_whitelist (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS automod_config (
    guild_id      INTEGER PRIMARY KEY,
    anti_invite   INTEGER NOT NULL DEFAULT 1,
    anti_link     INTEGER NOT NULL DEFAULT 1,
    anti_spam     INTEGER NOT NULL DEFAULT 1,
    anti_mention  INTEGER NOT NULL DEFAULT 1,
    max_mentions  INTEGER NOT NULL DEFAULT 5,
    anti_zalgo    INTEGER NOT NULL DEFAULT 1,
    log_channel_id INTEGER
);
CREATE TABLE IF NOT EXISTS automod_words (
    guild_id INTEGER NOT NULL,
    word     TEXT NOT NULL,
    PRIMARY KEY (guild_id, word)
);

CREATE TABLE IF NOT EXISTS mod_config (
    guild_id       INTEGER PRIMARY KEY,
    log_channel_id INTEGER,
    jail_role_id   INTEGER,
    jail_channel_id INTEGER
);
CREATE TABLE IF NOT EXISTS jailed_members (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    saved_roles TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS nuke_schedules (
    guild_id          INTEGER NOT NULL,
    channel_id        INTEGER PRIMARY KEY,
    archive_channel_id INTEGER NOT NULL,
    interval_seconds  INTEGER NOT NULL,
    next_run_at       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mod_cases (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    case_number  INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    action       TEXT NOT NULL,
    reason       TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER,
    UNIQUE (guild_id, case_number)
);

CREATE TABLE IF NOT EXISTS global_customization_access (
    user_id    INTEGER PRIMARY KEY,
    granted_by INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_invite_keys (
    key_hash   TEXT PRIMARY KEY,
    created_by INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_authorized_guilds (
    guild_id      INTEGER PRIMARY KEY,
    authorized_by INTEGER NOT NULL,
    authorized_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_pending_guilds (
    guild_id   INTEGER PRIMARY KEY,
    joined_at  INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS booster_config (
    guild_id       INTEGER PRIMARY KEY,
    base_role_id   INTEGER,
    filtered_words TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS booster_roles (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    role_id  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS booster_role_shares (
    guild_id  INTEGER NOT NULL,
    owner_id  INTEGER NOT NULL,
    member_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, owner_id, member_id)
);

CREATE TABLE IF NOT EXISTS giveaways (
    message_id INTEGER PRIMARY KEY,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    host_id    INTEGER NOT NULL,
    prize      TEXT NOT NULL,
    winners    INTEGER NOT NULL DEFAULT 1,
    ends_at    INTEGER NOT NULL,
    ended      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS giveaway_entries (
    message_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    PRIMARY KEY (message_id, user_id)
);
"""


class Database:
    """Thin async wrapper around SQLite.

    Fine for a server with thousands of members. If you ever outgrow it, this is the
    only file that has to change: keep execute/fetchone/fetchall and swap the driver
    (e.g. asyncpg for PostgreSQL).
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        async with self._lock:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(self.path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.executescript(SCHEMA)
            await self._conn.commit()

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None

    async def execute(self, sql: str, *params: Any) -> None:
        async with self._lock:
            assert self._conn is not None, "Database.connect() was not called"
            await self._conn.execute(sql, params)
            await self._conn.commit()

    async def insert(self, sql: str, *params: Any) -> int:
        async with self._lock:
            assert self._conn is not None, "Database.connect() was not called"
            async with self._conn.execute(sql, params) as cursor:
                await self._conn.commit()
                return int(cursor.lastrowid)

    async def fetchone(self, sql: str, *params: Any) -> aiosqlite.Row | None:
        async with self._lock:
            assert self._conn is not None, "Database.connect() was not called"
            async with self._conn.execute(sql, params) as cursor:
                return await cursor.fetchone()

    async def fetchall(self, sql: str, *params: Any) -> list[aiosqlite.Row]:
        async with self._lock:
            assert self._conn is not None, "Database.connect() was not called"
            async with self._conn.execute(sql, params) as cursor:
                return list(await cursor.fetchall())

    async def redeem_bot_invite_key(self, key_hash: str, guild_id: int, now: int) -> bool:
        """Atomically consume a valid key and authorize its currently pending guild."""
        async with self._lock:
            assert self._conn is not None, "Database.connect() was not called"
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                async with self._conn.execute(
                    "SELECT created_by FROM bot_invite_keys WHERE key_hash = ? AND expires_at > ?",
                    (key_hash, now),
                ) as cursor:
                    key = await cursor.fetchone()
                async with self._conn.execute(
                    "SELECT 1 FROM bot_pending_guilds WHERE guild_id = ? AND expires_at > ?",
                    (guild_id, now),
                ) as cursor:
                    pending = await cursor.fetchone()
                if key is None or pending is None:
                    await self._conn.rollback()
                    return False

                await self._conn.execute("DELETE FROM bot_invite_keys WHERE key_hash = ?", (key_hash,))
                await self._conn.execute(
                    "INSERT OR REPLACE INTO bot_authorized_guilds (guild_id, authorized_by, authorized_at) "
                    "VALUES (?, ?, ?)",
                    (guild_id, key["created_by"], now),
                )
                await self._conn.execute("DELETE FROM bot_pending_guilds WHERE guild_id = ?", (guild_id,))
                await self._conn.commit()
                return True
            except Exception:
                await self._conn.rollback()
                raise
