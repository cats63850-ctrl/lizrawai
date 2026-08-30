"""Имитация конкретных участников и мини-игры на корпусе сервера.

Модель строится отдельно для каждого человека — из его собственных сообщений,
поэтому получается узнаваемо. Готовые модели держатся в кэше, чтобы не
пересобирать их на каждую команду.

Приватность здесь та же, что и везде в боте: кто вышел из сбора через
`optout`, того нельзя ни спародировать, ни разыграть в играх.
"""

from __future__ import annotations

import random
from collections import OrderedDict
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

import filters
from markov import MarkovModel

if TYPE_CHECKING:
    from bot import MarkovBot

# Ниже этого числа сообщений пародия получается бессвязной.
MIN_MESSAGES = 50

# Сколько персональных моделей держим в памяти одновременно.
MODEL_CACHE_SIZE = 32

# Сколько секунд даётся на ответ в играх.
GAME_TIMEOUT = 60


def _author_stats_sync(storage, guild_id: int, minimum: int):
    """Кто сколько написал. Идёт по индексу (guild_id, author_id)."""
    cur = storage.conn.execute(
        "SELECT author_id, COUNT(*) AS n FROM messages"
        " WHERE guild_id = ? GROUP BY author_id"
        " HAVING n >= ? ORDER BY n DESC LIMIT 25",
        (guild_id, minimum),
    )
    return [(row["author_id"], row["n"]) for row in cur.fetchall()]


def _user_corpus_sync(storage, guild_id: int, user_id: int) -> list[str]:
    """Сообщения одного человека — только текст.

    Нарочно не берём ``Storage.user_messages``: та подставляет к каждой
    строке дату для выгрузки по ``requestdata``, и модель начинала бы
    выдавать «[2026-01-01] привет».
    """
    cur = storage.conn.execute(
        "SELECT content FROM messages WHERE guild_id = ? AND author_id = ? ORDER BY id",
        (guild_id, user_id),
    )
    return [row["content"] for row in cur.fetchall()]


class GuessView(discord.ui.View):
    """Кнопки с вариантами ответа. Первый правильный ответ закрывает раунд."""

    def __init__(self, options: list[str], correct: int) -> None:
        super().__init__(timeout=GAME_TIMEOUT)
        self.correct = correct
        self.finished = False
        for index, label in enumerate(options):
            button = discord.ui.Button(
                label=label[:80], style=discord.ButtonStyle.secondary
            )
            button.callback = self._make_callback(index)
            self.add_item(button)

    def _make_callback(self, index: int):
        async def callback(interaction: discord.Interaction) -> None:
            if self.finished:
                await interaction.response.send_message(
                    "Раунд уже закончен.", ephemeral=True
                )
                return

            if index == self.correct:
                self.finished = True
                self._lock()
                await interaction.response.edit_message(view=self)
                await interaction.followup.send(
                    f"{interaction.user.mention} угадал — "
                    f"**{self.children[self.correct].label}**."
                )
            else:
                await interaction.response.send_message("Мимо.", ephemeral=True)

        return callback

    def _lock(self) -> None:
        for child in self.children:
            child.disabled = True

    async def on_timeout(self) -> None:
        self.finished = True
        self._lock()


class Extras(commands.Cog, name="Пародии и игры"):
    def __init__(self, bot: "MarkovBot") -> None:
        self.bot = bot
        self._user_models: OrderedDict[tuple[int, int], MarkovModel] = OrderedDict()

    # ------------------------------------------------------------------
    # персональные модели
    # ------------------------------------------------------------------

    async def _get_user_model(self, guild_id: int, user_id: int) -> MarkovModel | None:
        key = (guild_id, user_id)
        cached = self._user_models.get(key)
        if cached is not None:
            self._user_models.move_to_end(key)
            return cached

        storage = self.bot.storage
        texts = await storage._run(_user_corpus_sync, storage, guild_id, user_id)
        if len(texts) < MIN_MESSAGES:
            return None

        settings = await storage.get_settings(guild_id)
        model = MarkovModel(order=settings.order_n)
        model.train_many(texts)

        self._user_models[key] = model
        while len(self._user_models) > MODEL_CACHE_SIZE:
            self._user_models.popitem(last=False)
        return model

    async def _eligible_authors(
        self, guild: discord.Guild, minimum: int = MIN_MESSAGES
    ) -> list[tuple[discord.Member, int]]:
        """Участники, которых достаточно в корпусе и которые не отказались."""
        storage = self.bot.storage
        # Идём через общий лок хранилища, чтобы не конкурировать с записью.
        rows = await storage._run(_author_stats_sync, storage, guild.id, minimum)

        result = []
        for author_id, count in rows:
            if storage.is_opted_out(author_id):
                continue
            member = guild.get_member(author_id)
            if member is None or member.bot:
                continue
            result.append((member, count))
        return result

    # ------------------------------------------------------------------
    # имитация
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="imitate",
        aliases=["как", "style"],
        description="Сгенерировать сообщение в стиле конкретного участника",
    )
    @app_commands.describe(
        target="Кого пародировать", seed="Слово, с которого начать (необязательно)"
    )
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def imitate(
        self,
        ctx: commands.Context,
        target: discord.Member,
        *,
        seed: str | None = None,
    ) -> None:
        if target.bot:
            await ctx.send("Ботов пародировать нечего.")
            return

        if self.bot.storage.is_opted_out(target.id):
            await ctx.send(
                f"{target.display_name} отказался от сбора сообщений — "
                "пародировать его нельзя."
            )
            return

        model = await self._get_user_model(ctx.guild.id, target.id)
        if model is None:
            storage = self.bot.storage
            have = len(
                await storage._run(_user_corpus_sync, storage, ctx.guild.id, target.id)
            )
            await ctx.send(
                f"Сообщений {target.display_name} пока мало: {have} из "
                f"{MIN_MESSAGES} нужных. Попробуй `{await self.bot.prefix_for(ctx.guild)}"
                "import`, чтобы затянуть историю канала."
            )
            return

        settings = await self.bot.storage.get_settings(ctx.guild.id)
        text = model.generate(
            seed=seed,
            max_tokens=settings.max_tokens,
            min_words=settings.min_learn_words,
        )
        if not text:
            await ctx.send(
                f"Со словом «{filters.sanitize_output(seed)}» ничего не собралось."
                if seed
                else "Не получилось ничего собрать, попробуй ещё раз."
            )
            return

        embed = discord.Embed(
            description=filters.sanitize_output(text),
            colour=target.colour if target.colour.value else discord.Colour.blurple(),
        )
        embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
        embed.set_footer(text=f"по {model.sample_count} сообщениям")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # игра: угадай, чей стиль
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="whois",
        aliases=["ктоэто"],
        description="Игра: бот пишет в чьём-то стиле, а вы угадываете в чьём",
    )
    @commands.guild_only()
    @commands.cooldown(1, 10, commands.BucketType.channel)
    async def whois(self, ctx: commands.Context) -> None:
        candidates = await self._eligible_authors(ctx.guild)
        if len(candidates) < 4:
            await ctx.send(
                "Для игры нужно минимум четыре человека, у которых накопилось "
                f"по {MIN_MESSAGES} сообщений. Пока таких меньше."
            )
            return

        picked = random.sample(candidates, 4)
        target, _ = picked[0]

        model = await self._get_user_model(ctx.guild.id, target.id)
        if model is None:
            await ctx.send("Не получилось собрать раунд, попробуй ещё раз.")
            return

        text = model.generate(max_tokens=40, min_words=3)
        if not text:
            await ctx.send("Не получилось собрать раунд, попробуй ещё раз.")
            return

        options = [member.display_name for member, _ in picked]
        order = list(range(4))
        random.shuffle(order)
        shuffled = [options[i] for i in order]
        correct = order.index(0)

        embed = discord.Embed(
            title="Кто это сказал?",
            description=filters.sanitize_output(text),
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(text=f"{GAME_TIMEOUT} секунд на ответ")
        await ctx.send(embed=embed, view=GuessView(shuffled, correct))

    # ------------------------------------------------------------------
    # игра: человек или бот
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="turing",
        aliases=["человекилибот"],
        description="Игра: настоящее сообщение из чата или выдумка бота?",
    )
    @commands.guild_only()
    @commands.cooldown(1, 10, commands.BucketType.channel)
    async def turing(self, ctx: commands.Context) -> None:
        model = await self.bot.get_model(ctx.guild.id)
        if not model.is_ready:
            await ctx.send("Корпус пуст — играть не на чем.")
            return

        real = random.choice([True, False])

        if real:
            candidates = await self._eligible_authors(ctx.guild, minimum=10)
            if not candidates:
                await ctx.send("Пока некому играть, корпус слишком маленький.")
                return
            member, _ = random.choice(candidates)
            storage = self.bot.storage
            texts = await storage._run(
                _user_corpus_sync, storage, ctx.guild.id, member.id
            )
            texts = [t for t in texts if len(t.split()) >= 4]
            if not texts:
                await ctx.send("Не нашлось подходящего сообщения, попробуй ещё раз.")
                return
            text = random.choice(texts)
        else:
            text = model.generate(max_tokens=40, min_words=4)
            if not text:
                await ctx.send("Не получилось собрать раунд, попробуй ещё раз.")
                return

        # Автора настоящего сообщения не раскрываем: игра про текст,
        # а не про то, чтобы вытащить чью-то старую фразу под софиты.
        options = ["Настоящее", "Выдумка бота"]
        correct = 0 if real else 1

        embed = discord.Embed(
            title="Человек или бот?",
            description=filters.sanitize_output(text),
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(text=f"{GAME_TIMEOUT} секунд на ответ")
        await ctx.send(embed=embed, view=GuessView(options, correct))


async def setup(bot: "MarkovBot") -> None:
    await bot.add_cog(Extras(bot))
