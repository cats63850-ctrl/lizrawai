"""Команды генерации текста."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

import filters

if TYPE_CHECKING:
    from bot import MarkovBot

NOT_ENOUGH = (
    "Мне пока не из чего генерировать. Включи чтение канала командой "
    "`{prefix}read on` и дай боту накопить сообщений."
)


class Generation(commands.Cog, name="Генерация"):
    def __init__(self, bot: "MarkovBot") -> None:
        self.bot = bot

    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="generate",
        aliases=["gen", "write"],
        description="Сгенерировать сообщение из того, что писали в чате",
    )
    @app_commands.describe(seed="Слово, с которого начать (необязательно)")
    @commands.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.channel)
    async def generate(self, ctx: commands.Context, *, seed: str | None = None) -> None:
        settings = await self.bot.storage.get_settings(ctx.guild.id)
        model = await self.bot.get_model(ctx.guild.id)

        if not model.is_ready:
            prefix = await self.bot.prefix_for(ctx.guild)
            await ctx.send(NOT_ENOUGH.format(prefix=prefix))
            return

        text = model.generate(
            seed=seed,
            max_tokens=settings.max_tokens,
            min_words=settings.min_learn_words,
        )
        if not text:
            if seed:
                await ctx.send(f"Слова «{filters.sanitize_output(seed)}» в корпусе нет.")
            else:
                await ctx.send("Не получилось ничего собрать, попробуй ещё раз.")
            return

        await ctx.send(filters.sanitize_output(text))

    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="dialog",
        description="Сгенерировать несколько реплик подряд",
    )
    @app_commands.describe(lines="Сколько реплик, от 2 до 8")
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def dialog(self, ctx: commands.Context, lines: int = 4) -> None:
        lines = max(2, min(8, lines))
        settings = await self.bot.storage.get_settings(ctx.guild.id)
        model = await self.bot.get_model(ctx.guild.id)

        if not model.is_ready:
            prefix = await self.bot.prefix_for(ctx.guild)
            await ctx.send(NOT_ENOUGH.format(prefix=prefix))
            return

        replies = model.generate_dialog(
            lines=lines, max_tokens=40, min_words=settings.min_learn_words
        )
        if not replies:
            await ctx.send("Не получилось ничего собрать, попробуй ещё раз.")
            return

        body = "\n".join(f"— {line}" for line in replies)
        await ctx.send(filters.sanitize_output(body))

    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="stats",
        aliases=["botinfo"],
        description="Сколько бот успел выучить на этом сервере",
    )
    @commands.guild_only()
    async def stats(self, ctx: commands.Context) -> None:
        settings = await self.bot.storage.get_settings(ctx.guild.id)
        stored = await self.bot.storage.count_messages(ctx.guild.id)
        model = await self.bot.get_model(ctx.guild.id)

        embed = discord.Embed(title="Статистика", colour=discord.Colour.blurple())
        embed.add_field(name="Сообщений в базе", value=f"{stored:,}".replace(",", " "))
        embed.add_field(name="Состояний в модели", value=f"{model.state_count:,}".replace(",", " "))
        embed.add_field(name="Порядок цепи", value=str(settings.order_n))
        embed.add_field(
            name="Чтение",
            value="включено" if settings.reading_enabled else "выключено",
        )
        embed.add_field(
            name="Автоген",
            value=(
                f"раз в ~{settings.autogen_interval} сообщений"
                if settings.autogen_enabled
                else "выключен"
            ),
        )
        embed.set_footer(text=f"Серверов у бота: {len(self.bot.guilds)}")
        await ctx.send(embed=embed)


async def setup(bot: "MarkovBot") -> None:
    await bot.add_cog(Generation(bot))
