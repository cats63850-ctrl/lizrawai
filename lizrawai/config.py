"""Конфигурация из переменных окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass

try:  # python-dotenv не обязателен — просто удобен при локальном запуске
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


@dataclass(frozen=True)
class Config:
    token: str
    default_prefix: str = "g."
    database: str = "markovbot.db"
    dev_guild_id: int | None = None

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise SystemExit(
                "Не задан DISCORD_TOKEN.\n"
                "Скопируй .env.example в .env и впиши туда токен бота."
            )

        raw_guild = os.getenv("DEV_GUILD_ID", "").strip()
        dev_guild_id = int(raw_guild) if raw_guild.isdigit() else None

        return cls(
            token=token,
            default_prefix=os.getenv("DEFAULT_PREFIX", "g.").strip() or "g.",
            database=os.getenv("DATABASE_PATH", "markovbot.db").strip(),
            dev_guild_id=dev_guild_id,
        )
