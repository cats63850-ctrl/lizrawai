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

# Не чаще одного ответа в канал за столько секунд, чтобы бот не забивал чат.
REPLY_COOLDOWN = 4.0

# Слова короче этого в затравку не берём — на «а», «ну» модель не зацепится.
MIN_SEED_LEN = 4

# Особый тон для владельца бота: колючая снаружи, но всё равно отвечает.
# Весь смысл цундере в отрицании, поэтому здесь только ворчание и никаких
# нежностей — так смешнее и уместнее.
OWNER_OPENERS = [
    "Ч-чего тебе?",
    "Опять ты. Ну ладно, слушай.",
    "Хмф. Только потому что ты попросил.",
    "Я вообще-то занята была. Но так и быть.",
    "Н-ну хорошо, отвечу.",
    "Только один раз, ясно?",
]

OWNER_CLOSERS = [
    "Н-не подумай, что я старалась.",
    "Не привыкай.",
    "Всё, отстань.",
    "И вообще, я не для тебя это сказала.",
    "Хмф.",
    "Только никому не говори, что я помогала.",
]

# Изредка бот отвечает одной фразой, без генерации вообще.
OWNER_QUIPS = [
    "Б-бака.",
    "Чего смотришь?",
    "Хмф. Занята я.",
    "Сам разбирайся... ладно, шучу. Спрашивай нормально.",
    "Я тебя слышу, не надо повторять.",
]

# Как часто отвечать в этом тоне: обрамление, короткая отговорка, обычный ответ.
OWNER_WRAP_CHANCE = 65
OWNER_QUIP_CHANCE = 15


class Listener(commands.Cog):
    def __init__(self, bot: "MarkovBot") -> None:
        self.bot = bot
        self._last_reply: dict[int, float] = {}

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

        # Обращение к боту обрабатываем до всех проверок сбора: отвечать
        # он должен даже там, где чтение выключено.
        if await self._is_addressed(message):
            await self._reply_to(message, settings)
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
    # ответы на обращения
    # ------------------------------------------------------------------

    async def _is_addressed(self, message: discord.Message) -> bool:
        """Обращаются ли к боту: упоминанием или ответом на его сообщение."""
        me = self.bot.user
        if me is None:
            return False

        # @everyone и @here обращением не считаем, иначе бот влезал бы
        # в каждый массовый пинг.
        mentioned = any(user.id == me.id for user in message.mentions)

        replied = False
        reference = message.reference
        if reference is not None:
            resolved = reference.resolved
            if isinstance(resolved, discord.Message):
                replied = resolved.author.id == me.id
            elif reference.message_id is not None:
                # Сообщение не в кэше — дозапрашиваем, но только если это
                # вообще может быть ответом боту.
                try:
                    original = await message.channel.fetch_message(
                        reference.message_id
                    )
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    return mentioned
                replied = original.author.id == me.id

        if not (mentioned or replied):
            return False

        # Если это команда, пусть отработает команда, а не болталка.
        return not await self._looks_like_command(message)

    async def _looks_like_command(self, message: discord.Message) -> bool:
        content = message.content
        me = self.bot.user

        # Упоминание работает как префикс, поэтому срезаем его.
        if me is not None:
            for form in (f"<@{me.id}>", f"<@!{me.id}>"):
                if content.startswith(form):
                    content = content[len(form):]
                    break

        prefix = await self.bot.prefix_for(message.guild)
        content = content.strip()
        if content.startswith(prefix):
            content = content[len(prefix):]

        first = content.strip().split(" ", 1)[0].lower()
        if not first:
            return False
        return self.bot.get_command(first) is not None

    def _pick_seed(self, content: str, model) -> str | None:
        """Слово из обращения, за которое модель может зацепиться."""
        cleaned = filters.clean_for_learning(
            content, remove_mentions=True, remove_links=True, remove_emoji=True
        )
        words = [w for w in cleaned.split() if len(w) >= MIN_SEED_LEN]
        random.shuffle(words)
        for word in words:
            # find_seed сам разберётся с падежами: «погоде» -> «погода».
            seed = model.find_seed(word)
            if seed and model.generate(seed=seed, max_tokens=8, min_words=1):
                return seed
        return None

    async def _reply_to(self, message: discord.Message, settings) -> None:
        channel = message.channel
        now = message.created_at.timestamp()
        last = self._last_reply.get(channel.id, 0.0)
        if now - last < REPLY_COOLDOWN:
            return

        me = message.guild.me
        if me is None or not channel.permissions_for(me).send_messages:
            return

        is_owner = await self.bot.is_owner(message.author)

        # Иногда владельцу прилетает короткая отговорка вместо генерации.
        if is_owner and random.randint(1, 100) <= OWNER_QUIP_CHANCE:
            self._last_reply[channel.id] = now
            try:
                await message.reply(random.choice(OWNER_QUIPS), mention_author=False)
            except discord.HTTPException:
                pass
            return

        # На вопросы о себе бот отвечает фактами, а не цепью Маркова.
        about = self.bot.get_cog("О боте")
        if about is not None:
            answer = await about.answer_question(
                message.content, message.guild.id
            )
            if answer:
                self._last_reply[channel.id] = now
                try:
                    await message.reply(answer, mention_author=False)
                except discord.HTTPException:
                    pass
                return

        model = await self.bot.get_model(message.guild.id)
        if not model.is_ready:
            prefix = await self.bot.prefix_for(message.guild)
            self._last_reply[channel.id] = now
            try:
                await message.reply(
                    f"Мне пока не на чем говорить. Загрузи историю: `{prefix}import 5000`.",
                    mention_author=False,
                )
            except discord.HTTPException:
                pass
            return

        seed = self._pick_seed(message.content, model)
        text = model.generate(
            seed=seed,
            max_tokens=settings.max_tokens,
            min_words=settings.min_learn_words,
        )
        if not text:
            text = model.generate(
                max_tokens=settings.max_tokens, min_words=settings.min_learn_words
            )
        if not text:
            return

        reply = filters.sanitize_output(text)
        if is_owner and random.randint(1, 100) <= OWNER_WRAP_CHANCE:
            reply = self._owner_tone(reply)

        self._last_reply[channel.id] = now
        try:
            async with channel.typing():
                await message.reply(reply, mention_author=False)
        except discord.HTTPException:
            log.warning("Не смог ответить в канале %s", channel.id)

    @staticmethod
    def _owner_tone(text: str) -> str:
        """Обрамить ответ ворчанием: сверху отговорка, снизу отрицание."""
        opener = random.choice(OWNER_OPENERS)
        closer = random.choice(OWNER_CLOSERS)
        return f"{opener}\n\n{text}\n\n*{closer}*"

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
