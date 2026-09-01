"""Роли по реакциям: поставил эмодзи — получил роль, убрал — потерял.

Работает без привилегированного интента участников. При добавлении
реакции Discord присылает данные участника прямо в событии, а при снятии
не присылает — там участник дозапрашивается по одному вызову API.

Боту нужно право «Управлять ролями», а его собственная роль должна
стоять в списке выше выдаваемой, иначе Discord запретит операцию.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot import MarkovBot

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS rp_reaction_roles (
    guild_id   INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    emoji      TEXT    NOT NULL,
    role_id    INTEGER NOT NULL,
    PRIMARY KEY (guild_id, message_id, emoji)
);
"""


def emoji_key(emoji: discord.PartialEmoji | discord.Emoji | str) -> str:
    """Единый ключ для обычных и серверных эмодзи."""
    if isinstance(emoji, str):
        emoji = discord.PartialEmoji.from_str(emoji)
    if emoji.id:
        return str(emoji.id)
    return emoji.name or ""


def _init_sync(storage) -> None:
    storage.conn.executescript(SCHEMA)
    storage.conn.commit()


def _bind_sync(
    storage, guild_id: int, message_id: int, emoji: str, role_id: int
) -> None:
    storage.conn.execute(
        "INSERT INTO rp_reaction_roles (guild_id, message_id, emoji, role_id)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(guild_id, message_id, emoji) DO UPDATE SET role_id = excluded.role_id",
        (guild_id, message_id, emoji, role_id),
    )
    storage.conn.commit()


def _unbind_sync(storage, guild_id: int, message_id: int, emoji: str) -> bool:
    cur = storage.conn.execute(
        "DELETE FROM rp_reaction_roles"
        " WHERE guild_id = ? AND message_id = ? AND emoji = ?",
        (guild_id, message_id, emoji),
    )
    storage.conn.commit()
    return cur.rowcount > 0


def _lookup_sync(storage, guild_id: int, message_id: int, emoji: str) -> int | None:
    cur = storage.conn.execute(
        "SELECT role_id FROM rp_reaction_roles"
        " WHERE guild_id = ? AND message_id = ? AND emoji = ?",
        (guild_id, message_id, emoji),
    )
    row = cur.fetchone()
    return row["role_id"] if row else None


def _list_sync(storage, guild_id: int) -> list[tuple[int, str, int]]:
    cur = storage.conn.execute(
        "SELECT message_id, emoji, role_id FROM rp_reaction_roles WHERE guild_id = ?",
        (guild_id,),
    )
    return [(r["message_id"], r["emoji"], r["role_id"]) for r in cur.fetchall()]


class ReactionRoles(commands.Cog, name="Роли по реакциям"):
    def __init__(self, bot: "MarkovBot") -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        storage = self.bot.storage
        await storage._run(_init_sync, storage)

    async def _q(self, fn, *args):
        storage = self.bot.storage
        return await storage._run(fn, storage, *args)

    # ------------------------------------------------------------------
    # проверки перед привязкой
    # ------------------------------------------------------------------

    def _role_problem(self, guild: discord.Guild, role: discord.Role) -> str | None:
        """Понятное объяснение, почему роль выдать не получится."""
        me = guild.me
        if not me.guild_permissions.manage_roles:
            return (
                "У бота нет права «Управлять ролями». Добавь его в настройках "
                "сервера или пересоздай приглашение с этим правом."
            )
        if role >= me.top_role:
            return (
                f"Роль **{role.name}** стоит выше роли бота. "
                "Перетащи роль бота выше неё в настройках ролей."
            )
        if role.managed:
            return f"Роль **{role.name}** управляется интеграцией, её выдать нельзя."
        if role.is_default():
            return "Роль @everyone выдать нельзя."
        return None

    # ------------------------------------------------------------------
    # команды
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="rolepanel",
        aliases=["рольпанель"],
        description="Создать сообщение с реакцией, выдающей роль",
    )
    @app_commands.describe(
        role="Какую роль выдавать",
        emoji="Эмодзи для реакции",
        text="Текст сообщения",
    )
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def rolepanel(
        self,
        ctx: commands.Context,
        role: discord.Role,
        emoji: str,
        *,
        text: str = "Поставь реакцию, чтобы получить роль.",
    ) -> None:
        problem = self._role_problem(ctx.guild, role)
        if problem:
            await ctx.send(problem)
            return

        embed = discord.Embed(
            title="Выдача роли",
            description=f"{text}\n\n{emoji} → **{role.name}**",
            colour=role.colour if role.colour.value else discord.Colour.blurple(),
        )
        embed.set_footer(text="Убрать реакцию — снять роль")

        message = await ctx.channel.send(embed=embed)
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            await message.delete()
            await ctx.send(
                "Не получилось поставить эту реакцию. "
                "Обычные эмодзи работают всегда, серверные — только со своего сервера."
            )
            return

        await self._q(
            _bind_sync, ctx.guild.id, message.id, emoji_key(emoji), role.id
        )
        if ctx.interaction:
            await ctx.send("Панель создана.", ephemeral=True)

    @commands.hybrid_command(
        name="reactrole",
        aliases=["рольреакция"],
        description="Привязать роль к реакции на существующем сообщении",
    )
    @app_commands.describe(
        message_id="ID сообщения", emoji="Эмодзи", role="Какую роль выдавать"
    )
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def reactrole(
        self, ctx: commands.Context, message_id: str, emoji: str, role: discord.Role
    ) -> None:
        if not message_id.isdigit():
            await ctx.send("ID сообщения — это длинное число.")
            return

        problem = self._role_problem(ctx.guild, role)
        if problem:
            await ctx.send(problem)
            return

        try:
            message = await ctx.channel.fetch_message(int(message_id))
        except discord.NotFound:
            await ctx.send(
                "Сообщение не найдено. Команду надо писать в том же канале, "
                "где лежит сообщение."
            )
            return
        except discord.Forbidden:
            await ctx.send("Нет доступа к этому сообщению.")
            return

        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            await ctx.send("Не получилось поставить эту реакцию.")
            return

        await self._q(_bind_sync, ctx.guild.id, message.id, emoji_key(emoji), role.id)
        await ctx.send(f"Готово: {emoji} на этом сообщении выдаёт **{role.name}**.")

    @commands.hybrid_command(
        name="unreactrole",
        aliases=["убратьрольреакцию"],
        description="Убрать привязку роли к реакции",
    )
    @app_commands.describe(message_id="ID сообщения", emoji="Эмодзи")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def unreactrole(
        self, ctx: commands.Context, message_id: str, emoji: str
    ) -> None:
        if not message_id.isdigit():
            await ctx.send("ID сообщения — это длинное число.")
            return

        removed = await self._q(
            _unbind_sync, ctx.guild.id, int(message_id), emoji_key(emoji)
        )
        await ctx.send("Привязка убрана." if removed else "Такой привязки не было.")

    @commands.hybrid_command(
        name="reactroles",
        aliases=["ролиреакции"],
        description="Список привязок ролей к реакциям",
    )
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def reactroles(self, ctx: commands.Context) -> None:
        rows = await self._q(_list_sync, ctx.guild.id)
        if not rows:
            prefix = await self.bot.prefix_for(ctx.guild)
            await ctx.send(f"Привязок нет. Создать: `{prefix}rolepanel @роль 🔔 текст`")
            return

        lines = []
        for message_id, emoji, role_id in rows:
            role = ctx.guild.get_role(role_id)
            name = role.name if role else f"удалённая роль ({role_id})"
            shown = emoji if not emoji.isdigit() else f"<:_:{emoji}>"
            lines.append(f"{shown} → **{name}** · сообщение `{message_id}`")

        embed = discord.Embed(
            title="Роли по реакциям",
            description="\n".join(lines),
            colour=discord.Colour.blurple(),
        )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # события
    # ------------------------------------------------------------------

    async def _resolve(
        self, payload: discord.RawReactionActionEvent
    ) -> tuple[discord.Guild, discord.Member, discord.Role] | None:
        if payload.guild_id is None:
            return None

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return None

        role_id = await self._q(
            _lookup_sync, payload.guild_id, payload.message_id, emoji_key(payload.emoji)
        )
        if role_id is None:
            return None

        role = guild.get_role(role_id)
        if role is None:
            return None

        member = payload.member
        if member is None:
            # При снятии реакции Discord участника не присылает.
            member = guild.get_member(payload.user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(payload.user_id)
                except discord.HTTPException:
                    return None

        if member.bot:
            return None
        return guild, member, role

    @commands.Cog.listener()
    async def on_raw_reaction_add(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        found = await self._resolve(payload)
        if found is None:
            return
        _guild, member, role = found
        if role in member.roles:
            return
        try:
            await member.add_roles(role, reason="Роль по реакции")
        except discord.Forbidden:
            log.warning(
                "не хватает прав выдать роль %s на сервере %s", role.id, payload.guild_id
            )
        except discord.HTTPException:
            log.exception("не удалось выдать роль %s", role.id)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        found = await self._resolve(payload)
        if found is None:
            return
        _guild, member, role = found
        if role not in member.roles:
            return
        try:
            await member.remove_roles(role, reason="Реакция снята")
        except discord.Forbidden:
            log.warning(
                "не хватает прав снять роль %s на сервере %s", role.id, payload.guild_id
            )
        except discord.HTTPException:
            log.exception("не удалось снять роль %s", role.id)


async def setup(bot: "MarkovBot") -> None:
    await bot.add_cog(ReactionRoles(bot))
