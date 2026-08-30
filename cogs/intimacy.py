from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

# Импортируем твою готовую вьюшку для кнопок согласия
from cogs.family import ConsentView

class Intimacy(commands.Cog, name="Близкие отношения"):
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="выебать",  # Замени на нужное название команды
        description="Предложить отрахать во всех щели по абоюдному согласию"
    )
    @app_commands.describe(target="С кем взаимодействовать")
    @commands.guild_only()
    @commands.cooldown(1, 60, commands.BucketType.user)
    async def action(self, ctx: commands.Context, target: discord.Member) -> None:
        # Базовые проверки на ботов и самого себя
        if target.bot or target == ctx.author:
            await ctx.send("С этим участником так нельзя.")
            return

        # Запрос согласия через интерактивные кнопки
        view = ConsentView(target.id, timeout=120)
        await ctx.send(
            f"{target.mention}, {ctx.author.display_name} предлагает тебе уединиться. "
            "Что скажешь?",
            view=view,
        )
        await view.wait()

        # Обработка результатов нажатия
        if view.result is None:
            await ctx.send("Ответа не последовало, время вышло.")
            return
        if not view.result:
            await ctx.send(f"{target.display_name} отказал(а).")
            return

        # Успешный исход (здесь можно добавить начисление баффов, трату денег или запись в БД)
        await ctx.send(
            f"💞 **{ctx.author.display_name}** и **{target.display_name}** провели время вместе."
        )

async def setup(bot) -> None:
    await bot.add_cog(Intimacy(bot))
