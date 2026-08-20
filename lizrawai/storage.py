"""Хранилище на SQLite: корпус сообщений и настройки серверов.

Используется стандартный ``sqlite3``, а блокирующие вызовы уводятся в поток
через ``asyncio.to_thread`` — так у бота остаётся ровно одна внешняя
зависимость (discord.py).
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

# Сколько сообщений максимум поднимаем из базы при сборке модели.
CORPUS_LIMIT = 200_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    author_id  INTEGER NOT NULL,
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    -- ID сообщения в Discord. Нужен, чтобы повторный импорт истории
    -- не задваивал одни и те же сообщения. NULL допустим для старых записей.
    message_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_guild   ON messages(guild_id);
CREATE INDEX IF NOT EXISTS idx_messages_author  ON messages(guild_id, author_id);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(guild_id, created_at);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id         INTEGER PRIMARY KEY,
    prefix           TEXT,
    reading_enabled  INTEGER NOT NULL DEFAULT 0,
    autogen_enabled  INTEGER NOT NULL DEFAULT 0,
    autogen_interval INTEGER NOT NULL DEFAULT 50,
    autogen_random   INTEGER NOT NULL DEFAULT 1,
    remove_mentions  INTEGER NOT NULL DEFAULT 1,
    remove_links     INTEGER NOT NULL DEFAULT 1,
    remove_emoji     INTEGER NOT NULL DEFAULT 0,
    order_n          INTEGER NOT NULL DEFAULT 2,
    max_tokens       INTEGER NOT NULL DEFAULT 60,
    min_learn_words  INTEGER NOT NULL DEFAULT 2
);

CREATE TABLE IF NOT EXISTS ignored_channels (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS opted_out_users (
    user_id INTEGER PRIMARY KEY
);
"""

# Создаётся после миграции: на старой базе колонки message_id ещё нет,
# и индекс по ней в основном скрипте уронил бы весь executescript.
POST_MIGRATION = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_msgid
    ON messages(message_id) WHERE message_id IS NOT NULL;
"""


@dataclass
class GuildSettings:
    """Настройки одного сервера. Значения по умолчанию — как в схеме."""

    guild_id: int
    prefix: str | None = None
    reading_enabled: bool = False
    autogen_enabled: bool = False
    autogen_interval: int = 50
    autogen_random: bool = True
    remove_mentions: bool = True
    remove_links: bool = True
    remove_emoji: bool = False
    order_n: int = 2
    max_tokens: int = 60
    min_learn_words: int = 2

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "GuildSettings":
        bool_fields = {
            "reading_enabled",
            "autogen_enabled",
            "autogen_random",
            "remove_mentions",
            "remove_links",
            "remove_emoji",
        }
        data = {}
        for f in fields(cls):
            value = row[f.name]
            data[f.name] = bool(value) if f.name in bool_fields else value
        return cls(**data)


# Поля, которые разрешено менять командами. Защита от опечатки в имени
# колонки, превращающейся в SQL-инъекцию.
EDITABLE = {
    "prefix",
    "reading_enabled",
    "autogen_enabled",
    "autogen_interval",
    "autogen_random",
    "remove_mentions",
    "remove_links",
    "remove_emoji",
    "order_n",
    "max_tokens",
    "min_learn_words",
}


class Storage:
    def __init__(self, path: str | Path = "markovbot.db") -> None:
        self.path = str(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._settings_cache: dict[int, GuildSettings] = {}
        self._ignored_cache: dict[int, set[int]] = {}
        self._opted_out: set[int] = set()

    # ------------------------------------------------------------------
    # служебное
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Storage.setup() не был вызван")
        return self._conn

    def _migrate_sync(self) -> None:
        """Довести старую базу до текущей схемы."""
        columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(messages)")
        }
        if "message_id" not in columns:
            self.conn.execute("ALTER TABLE messages ADD COLUMN message_id INTEGER")
            self.conn.commit()

    async def setup(self) -> None:
        self._conn = await asyncio.to_thread(self._connect)
        await asyncio.to_thread(self.conn.executescript, SCHEMA)
        await asyncio.to_thread(self._migrate_sync)
        await asyncio.to_thread(self.conn.executescript, POST_MIGRATION)
        await asyncio.to_thread(self.conn.commit)

    async def close(self) -> None:
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    async def _run(self, fn, *args):
        """Выполнить блокирующую работу с базой под общим локом."""
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    # ------------------------------------------------------------------
    # настройки
    # ------------------------------------------------------------------

    def _get_settings_sync(self, guild_id: int) -> GuildSettings:
        cur = self.conn.execute(
            "SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)
        )
        row = cur.fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO guild_settings (guild_id) VALUES (?)", (guild_id,)
            )
            self.conn.commit()
            cur = self.conn.execute(
                "SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)
            )
            row = cur.fetchone()
        return GuildSettings.from_row(row)

    async def get_settings(self, guild_id: int) -> GuildSettings:
        cached = self._settings_cache.get(guild_id)
        if cached is not None:
            return cached
        settings = await self._run(self._get_settings_sync, guild_id)
        self._settings_cache[guild_id] = settings
        return settings

    def _update_settings_sync(self, guild_id: int, changes: dict) -> None:
        self._get_settings_sync(guild_id)  # гарантируем, что строка есть
        assignments = ", ".join(f"{key} = ?" for key in changes)
        values = [
            int(v) if isinstance(v, bool) else v for v in changes.values()
        ]
        self.conn.execute(
            f"UPDATE guild_settings SET {assignments} WHERE guild_id = ?",
            (*values, guild_id),
        )
        self.conn.commit()

    async def update_settings(self, guild_id: int, **changes) -> GuildSettings:
        unknown = set(changes) - EDITABLE
        if unknown:
            raise ValueError(f"Неизвестные настройки: {sorted(unknown)}")
        if not changes:
            return await self.get_settings(guild_id)

        await self._run(self._update_settings_sync, guild_id, changes)
        self._settings_cache.pop(guild_id, None)
        return await self.get_settings(guild_id)

    # ------------------------------------------------------------------
    # корпус
    # ------------------------------------------------------------------

    INSERT_SQL = (
        "INSERT OR IGNORE INTO messages"
        " (guild_id, channel_id, author_id, content, created_at, message_id)"
        " VALUES (?, ?, ?, ?, ?, ?)"
    )

    def _add_message_sync(self, args: tuple) -> None:
        self.conn.execute(self.INSERT_SQL, args)
        self.conn.commit()

    async def add_message(
        self,
        guild_id: int,
        channel_id: int,
        author_id: int,
        content: str,
        message_id: int | None = None,
    ) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        await self._run(
            self._add_message_sync,
            (guild_id, channel_id, author_id, content, created_at, message_id),
        )

    def _add_bulk_sync(self, rows: list[tuple]) -> int:
        cur = self.conn.executemany(self.INSERT_SQL, rows)
        self.conn.commit()
        return cur.rowcount

    async def add_messages_bulk(self, rows: list[tuple]) -> int:
        """Пачкой вставить сообщения при импорте истории.

        Каждая строка — ``(guild_id, channel_id, author_id, content,
        created_at, message_id)``. Дубли по ``message_id`` отбрасываются,
        поэтому импорт можно гонять повторно без вреда.
        Возвращает число реально добавленных записей.
        """
        if not rows:
            return 0
        return await self._run(self._add_bulk_sync, rows)

    def _corpus_sync(self, guild_id: int) -> list[str]:
        cur = self.conn.execute(
            "SELECT content FROM messages WHERE guild_id = ?"
            " ORDER BY id DESC LIMIT ?",
            (guild_id, CORPUS_LIMIT),
        )
        return [row["content"] for row in cur.fetchall()]

    async def corpus(self, guild_id: int) -> list[str]:
        return await self._run(self._corpus_sync, guild_id)

    def _count_sync(self, guild_id: int) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE guild_id = ?", (guild_id,)
        )
        return cur.fetchone()["n"]

    async def count_messages(self, guild_id: int) -> int:
        return await self._run(self._count_sync, guild_id)

    def _user_messages_sync(self, guild_id: int, user_id: int) -> list[str]:
        cur = self.conn.execute(
            "SELECT created_at, content FROM messages"
            " WHERE guild_id = ? AND author_id = ? ORDER BY id",
            (guild_id, user_id),
        )
        return [f"[{row['created_at']}] {row['content']}" for row in cur.fetchall()]

    async def user_messages(self, guild_id: int, user_id: int) -> list[str]:
        return await self._run(self._user_messages_sync, guild_id, user_id)

    def _wipe_sync(self, guild_id: int, criteria: dict) -> int:
        where = ["guild_id = ?"]
        params: list = [guild_id]

        if criteria.get("author_id") is not None:
            where.append("author_id = ?")
            params.append(criteria["author_id"])
        if criteria.get("before") is not None:
            where.append("created_at < ?")
            params.append(criteria["before"])
        if criteria.get("pattern"):
            where.append("content LIKE ?")
            params.append(f"%{criteria['pattern']}%")

        cur = self.conn.execute(
            f"DELETE FROM messages WHERE {' AND '.join(where)}", params
        )
        self.conn.commit()
        return cur.rowcount

    async def wipe(
        self,
        guild_id: int,
        *,
        author_id: int | None = None,
        before: str | None = None,
        pattern: str | None = None,
    ) -> int:
        """Удалить сообщения. Без критериев — весь корпус сервера."""
        return await self._run(
            self._wipe_sync,
            guild_id,
            {"author_id": author_id, "before": before, "pattern": pattern},
        )

    # ------------------------------------------------------------------
    # игнорируемые каналы и отказ от сбора
    # ------------------------------------------------------------------

    def _set_ignored_sync(self, guild_id: int, channel_id: int, ignored: bool) -> None:
        if ignored:
            self.conn.execute(
                "INSERT OR IGNORE INTO ignored_channels (guild_id, channel_id)"
                " VALUES (?, ?)",
                (guild_id, channel_id),
            )
        else:
            self.conn.execute(
                "DELETE FROM ignored_channels WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            )
        self.conn.commit()

    async def set_channel_ignored(
        self, guild_id: int, channel_id: int, ignored: bool
    ) -> None:
        await self._run(self._set_ignored_sync, guild_id, channel_id, ignored)
        self._ignored_cache.pop(guild_id, None)

    def _ignored_sync(self, guild_id: int) -> set[int]:
        cur = self.conn.execute(
            "SELECT channel_id FROM ignored_channels WHERE guild_id = ?", (guild_id,)
        )
        return {row["channel_id"] for row in cur.fetchall()}

    async def ignored_channels(self, guild_id: int) -> set[int]:
        cached = self._ignored_cache.get(guild_id)
        if cached is not None:
            return cached
        result = await self._run(self._ignored_sync, guild_id)
        self._ignored_cache[guild_id] = result
        return result

    def _set_opt_out_sync(self, user_id: int, opted_out: bool) -> None:
        if opted_out:
            self.conn.execute(
                "INSERT OR IGNORE INTO opted_out_users (user_id) VALUES (?)",
                (user_id,),
            )
        else:
            self.conn.execute(
                "DELETE FROM opted_out_users WHERE user_id = ?", (user_id,)
            )
        self.conn.commit()

    async def set_opt_out(self, user_id: int, opted_out: bool) -> None:
        await self._run(self._set_opt_out_sync, user_id, opted_out)
        if opted_out:
            self._opted_out.add(user_id)
        else:
            self._opted_out.discard(user_id)

    def _load_opted_out_sync(self) -> set[int]:
        cur = self.conn.execute("SELECT user_id FROM opted_out_users")
        return {row["user_id"] for row in cur.fetchall()}

    async def load_opted_out(self) -> None:
        """Список отказавшихся держим в памяти — он проверяется на каждое сообщение."""
        self._opted_out = await self._run(self._load_opted_out_sync)

    def is_opted_out(self, user_id: int) -> bool:
        return user_id in self._opted_out
