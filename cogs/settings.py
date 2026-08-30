"""Настройки сервера, приватность и мастер первичной настройки."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot import MarkovBot

ON_OFF = Literal["on", "off"]


def yes_no(value: bool) -> str:
    return "включено" if value else "выключено"


def settings_embed(settings, prefix: str, guild_name: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"Настройки — {guild_name}",
        colour=discord.Colour.blurple(),
        description=(
            "Бот учится на сообщениях сервера и собирает из них новые. "
            "Пока чтение выключено, он ничего не запоминает."
        ),
    )
    embed.add_field(name="Чтение сообщений", value=yes_no(settings.reading_enabled))
    embed.add_field(name="Автогенерация", value=yes_no(settings.autogen_enabled))
    embed.add_field(
        name="Интервал автогена",
        value=(
            f"~{settings.autogen_interval} сообщений"
            + (" (со случайным разбросом)" if settings.autogen_random else " (ровно)")
        ),
        inline=False,
    )
    embed.add_field(name="Вырезать упоминания", value=yes_no(settings.remove_mentions))
    embed.add_field(name="Вырезать ссылки", value=yes_no(settings.remove_links))
    embed.add_field(name="Вырезать эмодзи", value=yes_no(settings.remove_emoji))
    embed.add_field(name="Порядок цепи", value=str(settings.order_n))
    embed.add_field(name="Длина ответа", value=f"до {settings.max_tokens} слов")
    embed.add_field(name="Префикс", value=f"`{prefix}`")
    embed.set_footer(text=f"Полный список команд: {prefix}help")
    return embed


class ConfirmView(discord.ui.View):
    """Кнопки да/нет для необратимых действий."""

    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=60)
        self.author_id = author_id
        self.value: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Это не твоя кнопка.", ephemeral=True
            )
            return False
        return True

    def _lock(self) -> None:
        for child in self.children:
            child.disabled = True

    async def on_timeout(self) -> None:
        self._lock()

    @discord.ui.button(label="Да, удалить", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        self._lock()
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        self._lock()
        await interaction.response.edit_message(view=self)
        self.stop()


class WizardView(discord.ui.View):
    """Мастер настройки: переключатели основных опций прямо в сообщении."""

    def __init__(self, cog: "Settings", guild_id: int, author_id: int, prefix: str,
                 guild_name: str) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.author_id = author_id
        self.prefix = prefix
        self.guild_name = guild_name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Мастер запустил другой человек.", ephemeral=True
            )
            return False
        return True

    async def _toggle(self, interaction: discord.Interaction, field: str) -> None:
        settings = await self.cog.bot.storage.get_settings(self.guild_id)
        new_value = not getattr(settings, field)
        settings = await self.cog.bot.storage.update_settings(
            self.guild_id, **{field: new_value}
        )
        await self._refresh(interaction, settings)

    async def _refresh(self, interaction: discord.Interaction, settings) -> None:
        self._sync_labels(settings)
        await interaction.response.edit_message(
            embed=settings_embed(settings, self.prefix, self.guild_name), view=self
        )

    def _sync_labels(self, settings) -> None:
        self.toggle_read.label = (
            "Выключить чтение" if settings.reading_enabled else "Включить чтение"
        )
        self.toggle_read.style = (
            discord.ButtonStyle.secondary
            if settings.reading_enabled
            else discord.ButtonStyle.success
        )
        self.toggle_autogen.label = (
            "Выключить автоген" if settings.autogen_enabled else "Включить автоген"
        )
        self.toggle_autogen.style = (
            discord.ButtonStyle.secondary
            if settings.autogen_enabled
            else discord.ButtonStyle.success
        )

    @discord.ui.button(label="Включить чтение", style=discord.ButtonStyle.success)
    async def toggle_read(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle(interaction, "reading_enabled")

    @discord.ui.button(label="Включить автоген", style=discord.ButtonStyle.success)
    async def toggle_autogen(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle(interaction, "autogen_enabled")

    @discord.ui.button(label="Готово", style=discord.ButtonStyle.primary)
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        settings = await self.cog.bot.storage.get_settings(self.guild_id)
        await interaction.response.edit_message(
            embed=settings_embed(settings, self.prefix, self.guild_name), view=self
        )
        self.stop()


class Settings(commands.Cog, name="Настройки"):
    def __init__(self, bot: "MarkovBot") -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # просмотр и мастер
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="config", description="Показать настройки сервера")
    @commands.guild_only()
    async def config(self, ctx: commands.Context) -> None:
        settings = await self.bot.storage.get_settings(ctx.guild.id)
        prefix = await self.bot.prefix_for(ctx.guild)
        await ctx.send(embed=settings_embed(settings, prefix, ctx.guild.name))

    @commands.hybrid_command(name="wizard", description="Мастер настройки с кнопками")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def wizard(self, ctx: commands.Context) -> None:
        settings = await self.bot.storage.get_settings(ctx.guild.id)
        prefix = await self.bot.prefix_for(ctx.guild)
        view = WizardView(self, ctx.guild.id, ctx.author.id, prefix, ctx.guild.name)
        view._sync_labels(settings)
        await ctx.send(
            embed=settings_embed(settings, prefix, ctx.guild.name), view=view
        )

    # ------------------------------------------------------------------
    # переключатели
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="read", description="Разрешить боту читать сообщения")
    @app_commands.describe(state="on — собирать сообщения, off — перестать")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def read(self, ctx: commands.Context, state: ON_OFF) -> None:
        enabled = state == "on"
        await self.bot.storage.update_settings(ctx.guild.id, reading_enabled=enabled)
        if enabled:
            await ctx.send(
                "Теперь я запоминаю сообщения этого сервера. "
                "Дай накопиться паре сотен — потом будет смешно."
            )
        else:
            await ctx.send("Больше ничего не запоминаю. Накопленное осталось в базе.")

    @commands.hybrid_command(name="autogen", description="Автоматические сообщения в чат")
    @app_commands.describe(state="on — писать самому, off — только по команде")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def autogen(self, ctx: commands.Context, state: ON_OFF) -> None:
        enabled = state == "on"
        settings = await self.bot.storage.update_settings(
            ctx.guild.id, autogen_enabled=enabled
        )
        if enabled:
            await ctx.send(
                f"Буду вставлять свои пять копеек примерно раз в "
                f"{settings.autogen_interval} сообщений."
            )
        else:
            await ctx.send("Молчу, пока не позовут.")

    @commands.hybrid_command(name="interval", description="Как часто срабатывает автоген")
    @app_commands.describe(messages="Раз во сколько сообщений писать, от 3 до 1000")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def interval(self, ctx: commands.Context, messages: int) -> None:
        value = max(3, min(1000, messages))
        await self.bot.storage.update_settings(ctx.guild.id, autogen_interval=value)
        self.bot.autogen_targets.clear()
        await ctx.send(f"Интервал автогена: примерно раз в {value} сообщений.")

    @commands.hybrid_command(name="filters", description="Что вырезать из сообщений")
    @app_commands.describe(
        what="Что настраиваем", state="on — вырезать, off — оставлять"
    )
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def filters_cmd(
        self,
        ctx: commands.Context,
        what: Literal["mentions", "links", "emoji"],
        state: ON_OFF,
    ) -> None:
        field = {"mentions": "remove_mentions", "links": "remove_links",
                 "emoji": "remove_emoji"}[what]
        await self.bot.storage.update_settings(ctx.guild.id, **{field: state == "on"})
        titles = {"mentions": "Упоминания", "links": "Ссылки", "emoji": "Эмодзи"}
        verb = "вырезаю" if state == "on" else "оставляю"
        await ctx.send(
            f"{titles[what]}: {verb}. Уже накопленные сообщения это не меняет."
        )

    @commands.hybrid_command(name="order", description="Порядок цепи Маркова")
    @app_commands.describe(
        value="1 — полный бред, 2 — золотая середина, 3-4 — ближе к цитатам"
    )
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def order(self, ctx: commands.Context, value: int) -> None:
        value = max(1, min(4, value))
        await self.bot.storage.update_settings(ctx.guild.id, order_n=value)
        self.bot.drop_model(ctx.guild.id)  # модель пересоберётся с новым порядком
        await ctx.send(
            f"Порядок цепи: {value}. Модель пересоберётся при следующей генерации."
        )

    @commands.hybrid_command(
        name="minwords", description="Минимум слов в сообщении, чтобы оно училось"
    )
    @app_commands.describe(
        value="От 1 до 10. Меньше — учится больше мусора, больше — корпус чище"
    )
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def minwords(self, ctx: commands.Context, value: int) -> None:
        value = max(1, min(10, value))
        await self.bot.storage.update_settings(ctx.guild.id, min_learn_words=value)
        if value == 1:
            note = (
                "Односложные «да», «лол» и «ок» теперь тоже идут в корпус. "
                "Переходов из них не построить, так что генерации это не поможет, "
                "зато статистика перестанет пугать нулями."
            )
        else:
            note = f"Сообщения короче {value} слов в корпус не попадают."
        await ctx.send(f"Минимум слов: {value}. {note}")

    @commands.hybrid_command(name="prefix", description="Сменить префикс команд")
    @app_commands.describe(new_prefix="Новый префикс, например g. или !")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def prefix(self, ctx: commands.Context, new_prefix: str) -> None:
        new_prefix = new_prefix.strip()
        if not new_prefix or len(new_prefix) > 5:
            await ctx.send("Префикс должен быть от 1 до 5 символов.")
            return
        await self.bot.storage.update_settings(ctx.guild.id, prefix=new_prefix)
        await ctx.send(
            f"Префикс теперь `{new_prefix}`. Упоминание бота работает всегда."
        )

    @commands.hybrid_command(name="ignore", description="Не собирать сообщения из канала")
    @app_commands.describe(channel="Канал, по умолчанию текущий")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def ignore(
        self, ctx: commands.Context, channel: discord.TextChannel | None = None
    ) -> None:
        target = channel or ctx.channel
        await self.bot.storage.set_channel_ignored(ctx.guild.id, target.id, True)
        await ctx.send(f"Канал {target.mention} больше не собирается.")

    @commands.hybrid_command(name="unignore", description="Снова собирать сообщения из канала")
    @app_commands.describe(channel="Канал, по умолчанию текущий")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def unignore(
        self, ctx: commands.Context, channel: discord.TextChannel | None = None
    ) -> None:
        target = channel or ctx.channel
        await self.bot.storage.set_channel_ignored(ctx.guild.id, target.id, False)
        await ctx.send(f"Канал {target.mention} снова собирается.")

    # ------------------------------------------------------------------
    # удаление данных
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="wipe", description="Стереть накопленные сообщения")
    @app_commands.describe(
        pattern="Удалить только содержащие эту подстроку (пусто — стереть всё)"
    )
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def wipe(self, ctx: commands.Context, *, pattern: str | None = None) -> None:
        scope = (
            f"сообщения со словом «{pattern}»" if pattern else "**весь корпус сервера**"
        )
        view = ConfirmView(ctx.author.id)
        message = await ctx.send(f"Точно удалить {scope}? Это необратимо.", view=view)
        await view.wait()

        if not view.value:
            await message.edit(content="Отменено, ничего не удалил.", view=view)
            return

        removed = await self.bot.storage.wipe(ctx.guild.id, pattern=pattern)
        self.bot.drop_model(ctx.guild.id)
        await message.edit(content=f"Удалено сообщений: {removed}.", view=view)

    @commands.hybrid_command(name="forgetme", description="Удалить мои сообщения из корпуса")
    @commands.guild_only()
    async def forgetme(self, ctx: commands.Context) -> None:
        removed = await self.bot.storage.wipe(ctx.guild.id, author_id=ctx.author.id)
        self.bot.drop_model(ctx.guild.id)
        await ctx.send(f"Удалил твоих сообщений: {removed}.", ephemeral=True)

    @commands.hybrid_command(name="optout", description="Не собирать мои сообщения нигде")
    async def optout(self, ctx: commands.Context) -> None:
        await self.bot.storage.set_opt_out(ctx.author.id, True)
        await ctx.send(
            "Больше не запоминаю твои сообщения. "
            "Уже собранные удалит `forgetme`.",
            ephemeral=True,
        )

    @commands.hybrid_command(name="optin", description="Снова собирать мои сообщения")
    async def optin(self, ctx: commands.Context) -> None:
        await self.bot.storage.set_opt_out(ctx.author.id, False)
        await ctx.send("Снова тебя слушаю.", ephemeral=True)

    @commands.hybrid_command(name="requestdata", description="Прислать в ЛС мои собранные сообщения")
    @commands.guild_only()
    @commands.cooldown(1, 60, commands.BucketType.user)
    async def requestdata(self, ctx: commands.Context) -> None:
        lines = await self.bot.storage.user_messages(ctx.guild.id, ctx.author.id)
        if not lines:
            await ctx.send("Про тебя ничего не сохранено.", ephemeral=True)
            return

        buffer = io.BytesIO("\n".join(lines).encode("utf-8"))
        file = discord.File(buffer, filename=f"my_messages_{ctx.guild.id}.txt")
        try:
            await ctx.author.send(
                f"Твои сообщения, собранные на сервере «{ctx.guild.name}»:", file=file
            )
        except discord.Forbidden:
            await ctx.send("Не могу написать в ЛС — открой личные сообщения.", ephemeral=True)
            return
        await ctx.send(f"Отправил в ЛС. Строк: {len(lines)}.", ephemeral=True)


async def setup(bot: "MarkovBot") -> None:
    await bot.add_cog(Settings(bot))
