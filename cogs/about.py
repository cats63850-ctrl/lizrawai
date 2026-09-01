"""Факты о самом боте: сколько в нём кода, команд и накоплено сообщений.

Строки считаются по живым файлам на диске, а не зашиты числом — иначе
ответ устареет на следующий же коммит.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from bot import MarkovBot

log = logging.getLogger(__name__)

# Каталоги, которые нельзя считать своим кодом. Установщик кладёт .venv
# внутрь папки бота, и без этого списка в ответ попали бы десятки тысяч
# строк discord.py.
SKIP_DIRS = {".venv", "venv", "__pycache__", ".git", "node_modules", "site-packages"}

# Вопросы, на которые бот отвечает фактами, а не генерацией.
# «скок» и «скоко» — живая речь, без них половина вопросов пролетит мимо.
_HOW_MANY = r"(?:скольк\w*|скок\w*)"

QUESTIONS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(
            rf"{_HOW_MANY}.{{0,20}}(строк|кода)|строк.{{0,10}}кода|lines of code", re.I
        ),
        "code",
    ),
    (re.compile(rf"{_HOW_MANY}.{{0,20}}команд", re.I), "commands"),
    (
        re.compile(rf"{_HOW_MANY}.{{0,25}}(сообщени|запомнил|выучил)", re.I),
        "corpus",
    ),
]


def plural(amount: int, one: str, few: str, many: str) -> str:
    """«1 строка», «2 строки», «5 строк»."""
    if amount % 10 == 1 and amount % 100 != 11:
        word = one
    elif 2 <= amount % 10 <= 4 and not 12 <= amount % 100 <= 14:
        word = few
    else:
        word = many
    return f"{amount} {word}"


def count_own_code(root: Path) -> tuple[int, int, dict[str, int]]:
    """Строки, файлы и разбивка по модулям — по живым файлам на диске."""
    total = 0
    files = 0
    by_file: dict[str, int] = {}

    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            lines = len(path.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            continue
        total += lines
        files += 1
        by_file[str(path.relative_to(root))] = lines

    return total, files, by_file


class About(commands.Cog, name="О боте"):
    def __init__(self, bot: "MarkovBot") -> None:
        self.bot = bot
        # bot.py лежит в корне проекта, коги — на уровень ниже.
        self.root = Path(__file__).resolve().parent.parent

    # ------------------------------------------------------------------

    async def answer_question(
        self, content: str, guild_id: int | None = None
    ) -> str | None:
        """Готовый ответ, если в тексте узнаётся вопрос о самом боте."""
        for pattern, kind in QUESTIONS:
            if not pattern.search(content):
                continue
            if kind == "code":
                total, files, _ = count_own_code(self.root)
                return (
                    f"Во мне {plural(total, 'строка', 'строки', 'строк')} кода "
                    f"в {plural(files, 'файле', 'файлах', 'файлах')}. "
                    "Ни одной нейросети, только статистика."
                )
            if kind == "commands":
                visible = [c for c in self.bot.commands if not c.hidden]
                return (
                    f"У меня {plural(len(visible), 'команда', 'команды', 'команд')}. "
                    "Пиши help, покажу список."
                )
            if kind == "corpus":
                if guild_id is None:
                    return None
                learned = await self.bot.storage.count_messages(guild_id)
                if not learned:
                    return "Я пока ничего не запомнил, корпус пустой."
                return (
                    f"Я выучил {plural(learned, 'сообщение', 'сообщения', 'сообщений')} "
                    "этого сервера."
                )
        return None

    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="code",
        aliases=["строки", "sloc"],
        description="Сколько во мне строк кода",
    )
    async def code(self, ctx: commands.Context) -> None:
        total, files, by_file = count_own_code(self.root)

        top = sorted(by_file.items(), key=lambda kv: -kv[1])[:8]
        listing = "\n".join(f"`{name}` — {lines}" for name, lines in top)

        embed = discord.Embed(
            title="Сколько во мне кода",
            description=f"**{total}** строк в **{files}** файлах",
            colour=discord.Colour.blurple(),
        )
        if listing:
            embed.add_field(name="Самые большие файлы", value=listing, inline=False)

        visible = [c for c in self.bot.commands if not c.hidden]
        extra = [f"Команд: {len(visible)}"]
        if ctx.guild is not None:
            learned = await self.bot.storage.count_messages(ctx.guild.id)
            extra.append(f"Сообщений выучено: {learned}")
        embed.add_field(name="Ещё немного цифр", value=" · ".join(extra), inline=False)
        embed.set_footer(text="Считаю по своим файлам прямо сейчас, не по памяти")
        await ctx.send(embed=embed)


async def setup(bot: "MarkovBot") -> None:
    await bot.add_cog(About(bot))
