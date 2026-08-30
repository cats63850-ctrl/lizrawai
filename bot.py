"""Точка входа: сборка бота, кэш моделей, запуск."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

import discord
from discord.ext import commands

from config import Config
from markov import MarkovModel
from storage import Storage

log = logging.getLogger("markovbot")

COGS = ("cogs.listener", "cogs.generation", "cogs.settings", "cogs.importer",
        "cogs.prank", "cogs.extras", "cogs.family", "cogs.intimacy")


class MarkovBot(commands.Bot):
    def __init__(self, config: Config, storage: Storage) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # без этого бот не увидит текст сообщений
        intents.guilds = True

        super().__init__(
            command_prefix=self._resolve_prefix,
            intents=intents,
            case_insensitive=True,  # чтобы «G.config» тоже работало
            # Ни при каких обстоятельствах бот никого не пингует.
            allowed_mentions=discord.AllowedMentions.none(),
            help_command=commands.DefaultHelpCommand(no_category="Команды"),
        )
        self.config = config
        self.storage = storage

        self._models: dict[int, MarkovModel] = {}
        self._model_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.autogen_counters: dict[int, int] = defaultdict(int)
        self.autogen_targets: dict[int, int] = {}

    # ------------------------------------------------------------------
    # префикс
    # ------------------------------------------------------------------

    async def _resolve_prefix(self, bot, message: discord.Message) -> list[str]:
        prefix = self.config.default_prefix
        if message.guild is not None:
            settings = await self.storage.get_settings(message.guild.id)
            prefix = settings.prefix or prefix
        # Упоминание бота работает как префикс всегда — удобно, когда
        # кто-то забыл, какой префикс настроен на сервере.
        return commands.when_mentioned_or(prefix)(bot, message)

    async def prefix_for(self, guild: discord.Guild | None) -> str:
        if guild is None:
            return self.config.default_prefix
        settings = await self.storage.get_settings(guild.id)
        return settings.prefix or self.config.default_prefix

    # ------------------------------------------------------------------
    # модели
    # ------------------------------------------------------------------

    async def get_model(self, guild_id: int) -> MarkovModel:
        """Достать модель сервера, собрав её из базы при первом обращении."""
        model = self._models.get(guild_id)
        if model is not None:
            return model

        async with self._model_locks[guild_id]:
            # Пока ждали лок, модель мог собрать кто-то другой.
            model = self._models.get(guild_id)
            if model is not None:
                return model

            settings = await self.storage.get_settings(guild_id)
            corpus = await self.storage.corpus(guild_id)
            model = await asyncio.to_thread(self._build_model, corpus, settings.order_n)
            self._models[guild_id] = model
            log.info(
                "Модель сервера %s собрана: %d сообщений, %d состояний",
                guild_id,
                model.sample_count,
                model.state_count,
            )
            return model

    @staticmethod
    def _build_model(corpus: list[str], order: int) -> MarkovModel:
        model = MarkovModel(order=order)
        model.train_many(corpus)
        return model

    def drop_model(self, guild_id: int) -> None:
        """Выбросить модель из кэша — пересоберётся при следующем обращении."""
        self._models.pop(guild_id, None)

    # ------------------------------------------------------------------
    # жизненный цикл
    # ------------------------------------------------------------------

    async def setup_hook(self) -> None:
        await self.storage.setup()
        await self.storage.load_opted_out()

        for cog in COGS:
            await self.load_extension(cog)
            log.info("Загружен ког %s", cog)

        if self.config.dev_guild_id:
            # На одном сервере слэш-команды появляются мгновенно,
            # глобальная синхронизация может занимать до часа.
            guild = discord.Object(id=self.config.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Слэш-команды синхронизированы для сервера %s", self.config.dev_guild_id)
        else:
            await self.tree.sync()
            log.info("Слэш-команды синхронизированы глобально")

    async def on_ready(self) -> None:
        log.info("Вошли как %s (id %s), серверов: %d",
                 self.user, self.user.id, len(self.guilds))
        await self.change_presence(
            activity=discord.Game(name=f"{self.config.default_prefix}help")
        )

    async def close(self) -> None:
        await self.storage.close()
        await super().close()

    # ------------------------------------------------------------------
    # ошибки команд
    # ------------------------------------------------------------------

    async def on_command_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("Для этого нужны права «Управление сервером».")
            return
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Эта команда работает только на сервере.")
            return
        if isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument,
                              commands.BadLiteralArgument)):
            prefix = await self.prefix_for(ctx.guild)
            await ctx.send(
                f"Не понял аргументы. Посмотри `{prefix}help {ctx.command}`."
            )
            return
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"Слишком часто. Попробуй через {error.retry_after:.0f} с.")
            return

        log.exception("Ошибка в команде %s", ctx.command, exc_info=error)
        await ctx.send("Что-то сломалось. Подробности в логах бота.")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    config = Config.from_env()
    storage = Storage(config.database)
    bot = MarkovBot(config, storage)

    async with bot:
        await bot.start(config.token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
