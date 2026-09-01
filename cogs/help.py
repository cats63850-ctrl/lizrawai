"""Своя справка вместо стандартной.

Стандартная берёт описание из ``help``/``brief``, а у наших команд
заполнено ``description`` — из-за этого список выходил пустым. Здесь
описание берётся из обоих мест, а вывод собирается в эмбеды.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from bot import MarkovBot

# Значок и краткое пояснение для каждой категории.
CATEGORY_LOOK: dict[str, tuple[str, str]] = {
    "Генерация": ("🧠", "Бот пишет сам"),
    "Импорт": ("📥", "Загрузка истории канала"),
    "Настройки": ("⚙️", "Поведение бота и приватность"),
    "Пародии и игры": ("🎭", "Пародии на людей и угадайки"),
    "Семья и экономика": ("💍", "Брак, дети, деньги, работа"),
    "Биржа и подземелья": ("⚔️", "Акции, гача, походы за добычей"),
    "Роли по реакциям": ("🔔", "Выдача ролей за реакцию"),
    "Справка": ("❓", "Эта самая справка"),
}

DEFAULT_LOOK = ("📁", "")

ACCENT = discord.Colour.blurple()


def describe(command: commands.Command) -> str:
    """Короткое описание команды, откуда бы оно ни было задано."""
    if command.description:
        return command.description
    if command.short_doc:
        return command.short_doc
    return "без описания"


class PrettyHelp(commands.HelpCommand):
    def __init__(self) -> None:
        super().__init__(
            command_attrs={
                "help": "Показать список команд",
                "description": "Показать список команд",
            }
        )

    # ------------------------------------------------------------------

    async def send_bot_help(
        self, mapping: Mapping[commands.Cog | None, list[commands.Command]]
    ) -> None:
        prefix = self.context.clean_prefix

        embed = discord.Embed(
            title="Команды бота",
            description=(
                f"`{prefix}help команда` — что делает конкретная команда\n"
                f"`{prefix}help категория` — все команды раздела"
            ),
            colour=ACCENT,
        )

        me = self.context.bot.user
        if me and me.display_avatar:
            embed.set_thumbnail(url=me.display_avatar.url)

        total = 0
        for cog, cog_commands in mapping.items():
            visible = await self.filter_commands(cog_commands, sort=True)
            if not visible:
                continue

            name = cog.qualified_name if cog else "Прочее"
            icon, hint = CATEGORY_LOOK.get(name, DEFAULT_LOOK)
            total += len(visible)

            chips = " ".join(f"`{c.name}`" for c in visible)
            value = f"*{hint}*\n{chips}" if hint else chips
            embed.add_field(name=f"{icon} {name}", value=value, inline=False)

        embed.set_footer(text=f"Всего команд: {total} · префикс {prefix}")
        await self.get_destination().send(embed=embed)

    # ------------------------------------------------------------------

    async def send_cog_help(self, cog: commands.Cog) -> None:
        visible = await self.filter_commands(cog.get_commands(), sort=True)
        if not visible:
            await self.send_error_message("В этом разделе нет доступных команд.")
            return

        name = cog.qualified_name
        icon, hint = CATEGORY_LOOK.get(name, DEFAULT_LOOK)
        prefix = self.context.clean_prefix

        embed = discord.Embed(
            title=f"{icon} {name}", description=hint or None, colour=ACCENT
        )
        for command in visible:
            embed.add_field(
                name=f"{prefix}{command.name} {command.signature}".strip(),
                value=describe(command),
                inline=False,
            )
        embed.set_footer(text=f"{len(visible)} команд · подробнее: {prefix}help команда")
        await self.get_destination().send(embed=embed)

    # ------------------------------------------------------------------

    async def send_command_help(self, command: commands.Command) -> None:
        prefix = self.context.clean_prefix

        embed = discord.Embed(
            title=f"{prefix}{command.qualified_name}",
            description=describe(command),
            colour=ACCENT,
        )
        embed.add_field(
            name="Как писать",
            value=f"`{prefix}{command.qualified_name} {command.signature}`".strip(),
            inline=False,
        )
        if command.aliases:
            embed.add_field(
                name="Другие названия",
                value=" ".join(f"`{a}`" for a in command.aliases),
                inline=False,
            )
        if command.cog:
            icon, _ = CATEGORY_LOOK.get(command.cog.qualified_name, DEFAULT_LOOK)
            embed.add_field(
                name="Раздел", value=f"{icon} {command.cog.qualified_name}", inline=False
            )

        # Показываем, какие права нужны, чтобы не тыкали вслепую.
        needed = [
            check.__qualname__
            for check in command.checks
            if "has_permissions" in check.__qualname__
        ]
        if needed:
            embed.set_footer(text="Нужны права модератора")
        await self.get_destination().send(embed=embed)

    async def send_group_help(self, group: commands.Group) -> None:
        await self.send_command_help(group)

    # ------------------------------------------------------------------

    async def send_error_message(self, error: str) -> None:
        prefix = self.context.clean_prefix
        embed = discord.Embed(
            description=f"{error}\n\nПолный список: `{prefix}help`",
            colour=discord.Colour.red(),
        )
        await self.get_destination().send(embed=embed)

    async def command_not_found(self, string: str) -> str:
        return f"Команды «{string}» нет."

    async def subcommand_not_found(self, command: Any, string: str) -> str:
        return f"У «{command.qualified_name}» нет подкоманды «{string}»."


class Help(commands.Cog, name="Справка"):
    def __init__(self, bot: "MarkovBot") -> None:
        self.bot = bot
        self._previous = bot.help_command
        help_command = PrettyHelp()
        help_command.cog = self
        bot.help_command = help_command

    async def cog_unload(self) -> None:
        self.bot.help_command = self._previous


async def setup(bot: "MarkovBot") -> None:
    await bot.add_cog(Help(bot))
