"""Импорт истории канала в корпус.

Живой сбор наполняется медленно: чтобы бот стал интересным, нужны тысячи
сообщений. Эта команда за пару минут затягивает то, что уже написано.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

import filters

if TYPE_CHECKING:
    from bot import MarkovBot

log = logging.getLogger("markovbot.importer")

# Копим строки перед вставкой: одна транзакция на пачку вместо тысяч мелких.
BATCH_SIZE = 500
# Как часто обновлять сообщение с прогрессом.
PROGRESS_EVERY = 1000
MAX_LIMIT = 50_000


class Importer(commands.Cog, name="Импорт"):
    def __init__(self, bot: "MarkovBot") -> None:
        self.bot = bot
        # Один импорт на сервер за раз, иначе прогресс превратится в кашу.
        self._running: set[int] = set()

    @commands.hybrid_command(
        name="import",
        description="Затянуть историю канала в корпус",
    )
    @app_commands.describe(
        limit="Сколько сообщений просмотреть, до 50000",
        channel="Канал, по умолчанию текущий",
    )
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def import_history(
        self,
        ctx: commands.Context,
        limit: int = 5000,
        channel: discord.TextChannel | None = None,
    ) -> None:
        guild = ctx.guild
        target = channel or ctx.channel
        limit = max(100, min(MAX_LIMIT, limit))

        if guild.id in self._running:
            await ctx.send("Импорт уже идёт, дождись его окончания.")
            return

        perms = target.permissions_for(guild.me)
        if not (perms.view_channel and perms.read_message_history):
            await ctx.send(
                f"Нет доступа к истории {target.mention}. Нужны права "
                "«Просматривать каналы» и «Читать историю сообщений»."
            )
            return

        settings = await self.bot.storage.get_settings(guild.id)
        prefix = settings.prefix or self.bot.config.default_prefix

        await ctx.defer()
        status = await ctx.send(
            f"Читаю историю {target.mention}, до {limit} сообщений. "
            "Это может занять пару минут."
        )

        self._running.add(guild.id)
        try:
            scanned, kept = await self._pull(
                target, guild, limit, settings, prefix, status
            )
        except discord.Forbidden:
            await status.edit(content=f"Discord не отдал историю {target.mention}.")
            return
        except discord.HTTPException as exc:
            log.exception("Импорт упал")
            await status.edit(content=f"Импорт оборвался: {exc}")
            return
        finally:
            self._running.discard(guild.id)

        # Модель пересоберётся из обновлённого корпуса при следующем обращении.
        self.bot.drop_model(guild.id)
        total = await self.bot.storage.count_messages(guild.id)

        await status.edit(
            content=(
                f"Готово. Просмотрено {scanned}, добавлено {kept}. "
                f"Теперь в базе {total} сообщений.\n"
                f"Пробуй `{prefix}generate`. "
                f"Другой канал: `{prefix}import {limit} #канал`"
            )
        )

    # ------------------------------------------------------------------

    async def _pull(self, target, guild, limit, settings, prefix, status):
        batch: list[tuple] = []
        scanned = 0
        kept = 0

        async for message in target.history(limit=limit):
            scanned += 1

            if self._skip(message, settings, prefix):
                continue

            cleaned = filters.clean_for_learning(
                message.content,
                remove_mentions=settings.remove_mentions,
                remove_links=settings.remove_links,
                remove_emoji=settings.remove_emoji,
            )
            if filters.word_count(cleaned) < settings.min_learn_words:
                continue

            batch.append(
                (
                    guild.id,
                    target.id,
                    message.author.id,
                    cleaned,
                    message.created_at.isoformat(),
                    message.id,
                )
            )

            if len(batch) >= BATCH_SIZE:
                kept += await self.bot.storage.add_messages_bulk(batch)
                batch.clear()

            if scanned % PROGRESS_EVERY == 0:
                await status.edit(
                    content=f"Просмотрено {scanned}, взято {kept + len(batch)}…"
                )

        kept += await self.bot.storage.add_messages_bulk(batch)
        return scanned, kept

    def _skip(self, message: discord.Message, settings, prefix: str) -> bool:
        if message.author.bot or message.webhook_id is not None:
            return True
        if not message.content:
            return True
        if self.bot.storage.is_opted_out(message.author.id):
            return True
        if filters.looks_like_command(message.content, (prefix, "/", "!")):
            return True
        return False


async def setup(bot: "MarkovBot") -> None:
    await bot.add_cog(Importer(bot))
