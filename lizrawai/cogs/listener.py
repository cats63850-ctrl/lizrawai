"""Сбор сообщений в корпус и автоматическая генерация."""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

import filters

if TYPE_CHECKING:
    from bot import MarkovBot

log = logging.getLogger("markovbot.listener")


class Listener(commands.Cog):
    def __init__(self, bot: "MarkovBot") -> None:
        self.bot = bot

    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Это дополнительный слушатель, а не переопределение on_message,
        # поэтому обработка команд ботом продолжает работать сама.
        if message.author.bot or message.guild is None:
            return
        if message.webhook_id is not None:
            return
        if not message.content:
            return

        settings = await self.bot.storage.get_settings(message.guild.id)
        if not settings.reading_enabled:
            return
        if self.bot.storage.is_opted_out(message.author.id):
            return
        if message.channel.id in await self.bot.storage.ignored_channels(message.guild.id):
            return

        prefix = settings.prefix or self.bot.config.default_prefix
        # Команды — свои и чужих ботов — в корпус не пускаем.
        if filters.looks_like_command(message.content, (prefix, "/", "!")):
            return

        cleaned = filters.clean_for_learning(
            message.content,
            remove_mentions=settings.remove_mentions,
            remove_links=settings.remove_links,
            remove_emoji=settings.remove_emoji,
        )
        if filters.word_count(cleaned) < settings.min_learn_words:
            return

        # Модель поднимаем до записи в базу: иначе при холодном старте
        # свежее сообщение попало бы в корпус и выучилось бы дважды.
        model = await self.bot.get_model(message.guild.id)
        await self.bot.storage.add_message(
            message.guild.id,
            message.channel.id,
            message.author.id,
            cleaned,
            message_id=message.id,
        )
        # Дообучаем живую модель, чтобы не пересобирать её из базы каждый раз.
        model.train(cleaned)

        if settings.autogen_enabled:
            await self._maybe_autogen(message, settings, model)

    # ------------------------------------------------------------------

    def _next_target(self, settings) -> int:
        interval = max(3, settings.autogen_interval)
        if not settings.autogen_random:
            return interval
        return random.randint(max(3, interval // 2), int(interval * 1.5))

    async def _maybe_autogen(self, message: discord.Message, settings, model) -> None:
        channel = message.channel
        counters = self.bot.autogen_counters
        targets = self.bot.autogen_targets

        counters[channel.id] += 1
        target = targets.get(channel.id)
        if target is None:
            target = targets[channel.id] = self._next_target(settings)

        if counters[channel.id] < target:
            return

        counters[channel.id] = 0
        targets[channel.id] = self._next_target(settings)

        me = message.guild.me
        if me is None or not channel.permissions_for(me).send_messages:
            return

        text = model.generate(
            max_tokens=settings.max_tokens,
            min_words=settings.min_learn_words,
        )
        if not text:
            return

        try:
            await channel.send(filters.sanitize_output(text))
        except discord.HTTPException:
            log.warning("Не смог отправить автоген в канал %s", channel.id)


async def setup(bot: "MarkovBot") -> None:
    await bot.add_cog(Listener(bot))
