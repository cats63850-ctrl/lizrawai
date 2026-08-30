"""Шуточные «модерские» команды: бот делает вид, что мутит и банит.

Ничего не мутит и не банит по-настоящему — рисует правдоподобный эмбед,
а через некоторое время сам раскрывает розыгрыш, чтобы человек не сидел
и не гадал, за что его наказали.

Права нужны настоящие: `fakemute` требует «Тайм-аут участников»,
`fakeban` — «Банить участников». Без них команда не сработает, и рядовой
участник не сможет пугать других от имени модерации.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot import MarkovBot

# Через сколько секунд бот сам признается, что это шутка.
REVEAL_AFTER = 25

DEFAULT_REASON = "Причина не указана"

_DURATION_RE = re.compile(
    r"^(\d{1,4})\s*([сcмmчhдd]|sec|min|hour|day)?[а-яa-z]*$",
    re.IGNORECASE,
)

_UNIT_SECONDS = {
    "с": 1, "c": 1, "sec": 1,
    "м": 60, "m": 60, "min": 60,
    "ч": 3600, "h": 3600, "hour": 3600,
    "д": 86400, "d": 86400, "day": 86400,
}


def plural(amount: int, one: str, few: str, many: str) -> str:
    """«1 час», «2 часа», «5 часов» — иначе фейк палится с первого взгляда."""
    if amount % 10 == 1 and amount % 100 != 11:
        word = one
    elif 2 <= amount % 10 <= 4 and not 12 <= amount % 100 <= 14:
        word = few
    else:
        word = many
    return f"{amount} {word}"


def parse_duration(raw: str | None) -> str:
    """Превратить «10м», «2ч», «30» в человеческую подпись длительности."""
    if not raw:
        return "60 минут"

    match = _DURATION_RE.match(raw.strip())
    if not match:
        return raw.strip()[:32]

    amount = int(match.group(1))
    unit = (match.group(2) or "м").lower()
    seconds = amount * _UNIT_SECONDS.get(unit, 60)

    if seconds < 60:
        return plural(seconds, "секунду", "секунды", "секунд")
    if seconds < 3600:
        return plural(seconds // 60, "минуту", "минуты", "минут")
    if seconds < 86400:
        return plural(seconds // 3600, "час", "часа", "часов")
    return plural(seconds // 86400, "день", "дня", "дней")


class Prank(commands.Cog, name="Приколы"):
    def __init__(self, bot: "MarkovBot") -> None:
        self.bot = bot

    # ------------------------------------------------------------------

    @staticmethod
    def _action_embed(
        *,
        title: str,
        target: discord.Member,
        moderator: discord.Member,
        reason: str,
        duration: str | None = None,
    ) -> discord.Embed:
        embed = discord.Embed(title=title, colour=discord.Colour.dark_red())
        embed.add_field(name="Участник", value=f"{target.mention} (`{target}`)", inline=False)
        if duration:
            embed.add_field(name="Длительность", value=duration)
        embed.add_field(name="Модератор", value=moderator.mention)
        embed.add_field(name="Причина", value=reason, inline=False)
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text=f"ID: {target.id}")
        return embed

    @staticmethod
    def _reveal_embed(target: discord.Member, moderator: discord.Member) -> discord.Embed:
        embed = discord.Embed(
            title="Шутка",
            description=(
                f"{target.mention}, тебя никто не трогал — "
                f"{moderator.mention} просто балуется командой бота."
            ),
            colour=discord.Colour.green(),
        )
        embed.set_footer(text="Никаких реальных действий не совершалось")
        return embed

    async def _stage(
        self,
        ctx: commands.Context,
        target: discord.Member,
        embed: discord.Embed,
    ) -> None:
        """Отправить фейковый эмбед и через паузу заменить его признанием."""
        if target.bot:
            await ctx.send("Ботов разыгрывать бессмысленно, они не обидятся.")
            return
        if target == ctx.author:
            await ctx.send("Себя-то за что?")
            return

        message = await ctx.send(embed=embed)
        await asyncio.sleep(REVEAL_AFTER)
        try:
            await message.edit(embed=self._reveal_embed(target, ctx.author))
        except discord.HTTPException:
            # Сообщение могли удалить — тогда просто молчим.
            pass

    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="fakemute",
        aliases=["fmute"],
        description="Сделать вид, что участник получил тайм-аут (ничего не происходит)",
    )
    @app_commands.describe(
        target="Кого разыграть",
        duration="Например: 10м, 2ч, 1д",
        reason="Причина для правдоподобия",
    )
    @commands.guild_only()
    @commands.has_permissions(moderate_members=True)
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def fakemute(
        self,
        ctx: commands.Context,
        target: discord.Member,
        duration: str | None = None,
        *,
        reason: str = DEFAULT_REASON,
    ) -> None:
        embed = self._action_embed(
            title="Участнику выдан тайм-аут",
            target=target,
            moderator=ctx.author,
            reason=reason,
            duration=parse_duration(duration),
        )
        await self._stage(ctx, target, embed)

    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="fakeban",
        aliases=["fban"],
        description="Сделать вид, что участник забанен (ничего не происходит)",
    )
    @app_commands.describe(target="Кого разыграть", reason="Причина для правдоподобия")
    @commands.guild_only()
    @commands.has_permissions(ban_members=True)
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def fakeban(
        self,
        ctx: commands.Context,
        target: discord.Member,
        *,
        reason: str = DEFAULT_REASON,
    ) -> None:
        embed = self._action_embed(
            title="Участник забанен",
            target=target,
            moderator=ctx.author,
            reason=reason,
        )
        await self._stage(ctx, target, embed)


async def setup(bot: "MarkovBot") -> None:
    await bot.add_cog(Prank(bot))
