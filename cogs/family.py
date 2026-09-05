"""Семейно-экономическая система: брак, дети, развод, имущество, деньги, работа.

Всё живёт в тех же таблицах SQLite, что и корпус бота, но в отдельных
таблицах с префиксом ``rp_``. Схема создаётся при загрузке кога, поэтому
трогать ``storage.py`` не нужно.

Любое действие, затрагивающее другого человека (свадьба, усыновление),
требует его явного согласия кнопкой. Никого нельзя женить или усыновить
против воли — иначе это превращается в инструмент травли.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

if TYPE_CHECKING:
    from bot import MarkovBot

log = logging.getLogger(__name__)

CURRENCY = "💰"

SCHEMA = """
CREATE TABLE IF NOT EXISTS rp_profiles (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    balance    INTEGER NOT NULL DEFAULT 0,
    job        TEXT,
    last_work  TEXT,
    last_daily TEXT,
    PRIMARY KEY (guild_id, user_id)
);

-- Брак хранится одной строкой на пару: a_id всегда меньше b_id,
-- чтобы пара не могла задвоиться.
CREATE TABLE IF NOT EXISTS rp_marriages (
    guild_id     INTEGER NOT NULL,
    a_id         INTEGER NOT NULL,
    b_id         INTEGER NOT NULL,
    since        TEXT    NOT NULL,
    affection    INTEGER NOT NULL DEFAULT 0,
    joint        INTEGER NOT NULL DEFAULT 0,
    last_touch   TEXT,
    last_anniv   INTEGER NOT NULL DEFAULT 0,
    home         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, a_id, b_id)
);

-- Кулдауны на знаки внимания: по строке на человека и действие.
CREATE TABLE IF NOT EXISTS rp_actions (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    action   TEXT    NOT NULL,
    last     TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id, action)
);

CREATE TABLE IF NOT EXISTS rp_family (
    guild_id  INTEGER NOT NULL,
    parent_id INTEGER NOT NULL,
    child_id  INTEGER NOT NULL,
    since     TEXT    NOT NULL,
    PRIMARY KEY (guild_id, parent_id, child_id)
);

CREATE TABLE IF NOT EXISTS rp_items (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    item_key TEXT    NOT NULL,
    qty      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (guild_id, user_id, item_key)
);
"""

# ключ: (название, цена)
SHOP: dict[str, tuple[str, int]] = {
    "phone": ("Телефон", 500),
    "bike": ("Велосипед", 1_200),
    "cat": ("Кот", 3_000),
    "car": ("Машина", 15_000),
    "flat": ("Квартира", 60_000),
    "house": ("Дом", 150_000),
    "yacht": ("Яхта", 500_000),
}

DAILY_COOLDOWN = timedelta(hours=24)
DAILY_AMOUNT = 250
START_BALANCE = 100

# Базовый процент баланса, уходящий бывшему при разводе.
DIVORCE_SHARE = 20

MAX_PARENTS = 2

# Уровни отношений: (порог, название, сколько детей можно, надбавка к разводу).
LEVELS: list[tuple[int, str, int, int]] = [
    (0, "Молодожёны", 2, 0),
    (200, "Крепкая пара", 4, 10),
    (500, "Родные души", 6, 20),
    (1000, "Легенда сервера", 10, 30),
]

# Знаки внимания: ключ -> (очки, кулдаун).
TOUCHES: dict[str, tuple[int, timedelta]] = {
    "kiss": (5, timedelta(hours=4)),
    "hug": (3, timedelta(hours=2)),
    "gift": (0, timedelta(hours=12)),
}

# Подарки: ключ -> (название, цена, очки).
GIFTS: dict[str, tuple[str, int, int]] = {
    "flower": ("Цветы", 200, 8),
    "sweets": ("Конфеты", 500, 15),
    "teddy": ("Плюшевый мишка", 1_000, 25),
    "perfume": ("Духи", 3_000, 50),
    "ring": ("Кольцо", 10_000, 120),
}

# Жильё пары: (название, цена улучшения, надбавка к зарплате %,
# сколько детей сверх лимита, множитель проседания отношений).
HOMES: list[tuple[str, int, int, int, float]] = [
    ("Съёмная комната", 0, 0, 0, 1.0),
    ("Однушка", 20_000, 10, 0, 1.0),
    ("Двушка", 60_000, 20, 1, 0.8),
    ("Свой дом", 200_000, 35, 2, 0.6),
    ("Особняк", 600_000, 50, 3, 0.4),
]

# За каждые сутки без знаков внимания отношения проседают.
DECAY_PER_DAY = 4

# Годовщина отмечается каждые 30 дней брака.
ANNIVERSARY_DAYS = 30
ANNIVERSARY_BONUS = 1_000
ANNIVERSARY_AFFECTION = 30


def level_of(affection: int) -> tuple[int, str, int, int]:
    """Текущий уровень отношений по накопленным очкам."""
    current = LEVELS[0]
    for entry in LEVELS:
        if affection >= entry[0]:
            current = entry
    return current


def level_index(affection: int) -> int:
    index = 0
    for i, entry in enumerate(LEVELS):
        if affection >= entry[0]:
            index = i
    return index


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _left(last: datetime | None, cooldown: timedelta) -> timedelta | None:
    """Сколько ещё ждать. None — можно прямо сейчас."""
    if last is None:
        return None
    passed = _now() - last
    if passed >= cooldown:
        return None
    return cooldown - passed


def _human(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин"
    return f"{seconds} сек"


def _money(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + f" {CURRENCY}"


# ----------------------------------------------------------------------
# синхронные запросы к базе (выполняются под общим локом хранилища)
# ----------------------------------------------------------------------


def _init_sync(storage) -> None:
    storage.conn.executescript(SCHEMA)
    # Если таблица браков осталась от прошлой версии — дописываем поля.
    columns = {
        row["name"] for row in storage.conn.execute("PRAGMA table_info(rp_marriages)")
    }
    for name, definition in (
        ("affection", "INTEGER NOT NULL DEFAULT 0"),
        ("joint", "INTEGER NOT NULL DEFAULT 0"),
        ("last_touch", "TEXT"),
        ("last_anniv", "INTEGER NOT NULL DEFAULT 0"),
        ("home", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            storage.conn.execute(
                f"ALTER TABLE rp_marriages ADD COLUMN {name} {definition}"
            )
    storage.conn.commit()


def _relation_sync(storage, guild_id: int, user_id: int) -> dict | None:
    """Строка брака с уже применённым проседанием за простой."""
    cur = storage.conn.execute(
        "SELECT * FROM rp_marriages WHERE guild_id = ? AND (a_id = ? OR b_id = ?)",
        (guild_id, user_id, user_id),
    )
    row = cur.fetchone()
    if row is None:
        return None

    data = dict(row)
    last = _parse(data["last_touch"]) or _parse(data["since"])
    if last is not None:
        idle_days = (_now() - last).days
        if idle_days > 0 and data["affection"] > 0:
            # В хорошем доме отношения остывают медленнее.
            tier = min(data.get("home", 0), len(HOMES) - 1)
            rate = DECAY_PER_DAY * HOMES[tier][4]
            fresh = max(0, data["affection"] - int(idle_days * rate))
            if fresh != data["affection"]:
                storage.conn.execute(
                    "UPDATE rp_marriages SET affection = ?, last_touch = ?"
                    " WHERE guild_id = ? AND a_id = ? AND b_id = ?",
                    (fresh, _now().isoformat(), guild_id, data["a_id"], data["b_id"]),
                )
                storage.conn.commit()
                data["affection"] = fresh
    return data


def _touch_sync(storage, guild_id: int, user_id: int, action: str, points: int) -> None:
    """Отметить знак внимания: очки паре, кулдаун автору."""
    storage.conn.execute(
        "UPDATE rp_marriages SET affection = affection + ?, last_touch = ?"
        " WHERE guild_id = ? AND (a_id = ? OR b_id = ?)",
        (points, _now().isoformat(), guild_id, user_id, user_id),
    )
    storage.conn.execute(
        "INSERT INTO rp_actions (guild_id, user_id, action, last) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(guild_id, user_id, action) DO UPDATE SET last = excluded.last",
        (guild_id, user_id, action, _now().isoformat()),
    )
    storage.conn.commit()


def _action_last_sync(storage, guild_id: int, user_id: int, action: str) -> str | None:
    cur = storage.conn.execute(
        "SELECT last FROM rp_actions WHERE guild_id = ? AND user_id = ? AND action = ?",
        (guild_id, user_id, action),
    )
    row = cur.fetchone()
    return row["last"] if row else None


def _joint_move_sync(
    storage, guild_id: int, user_id: int, amount: int, into_joint: bool
) -> bool:
    """Переложить деньги между личным кошельком и общим бюджетом."""
    conn = storage.conn
    if into_joint:
        cur = conn.execute(
            "UPDATE rp_profiles SET balance = balance - ?"
            " WHERE guild_id = ? AND user_id = ? AND balance >= ?",
            (amount, guild_id, user_id, amount),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return False
        conn.execute(
            "UPDATE rp_marriages SET joint = joint + ?"
            " WHERE guild_id = ? AND (a_id = ? OR b_id = ?)",
            (amount, guild_id, user_id, user_id),
        )
    else:
        cur = conn.execute(
            "UPDATE rp_marriages SET joint = joint - ?"
            " WHERE guild_id = ? AND (a_id = ? OR b_id = ?) AND joint >= ?",
            (amount, guild_id, user_id, user_id, amount),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return False
        conn.execute(
            "UPDATE rp_profiles SET balance = balance + ?"
            " WHERE guild_id = ? AND user_id = ?",
            (amount, guild_id, user_id),
        )
    conn.commit()
    return True


def _joint_buy_sync(
    storage, guild_id: int, pair: tuple[int, int], key: str, price: int
) -> bool:
    """Покупка из общего бюджета: вещь достаётся обоим супругам."""
    conn = storage.conn
    cur = conn.execute(
        "UPDATE rp_marriages SET joint = joint - ?"
        " WHERE guild_id = ? AND a_id = ? AND b_id = ? AND joint >= ?",
        (price, guild_id, pair[0], pair[1], price),
    )
    if cur.rowcount == 0:
        conn.rollback()
        return False
    for user_id in pair:
        conn.execute(
            "INSERT INTO rp_items (guild_id, user_id, item_key, qty) VALUES (?, ?, ?, 1)"
            " ON CONFLICT(guild_id, user_id, item_key) DO UPDATE SET qty = qty + 1",
            (guild_id, user_id, key),
        )
    conn.commit()
    return True


def _upgrade_home_sync(
    storage, guild_id: int, a_id: int, b_id: int, tier: int, price: int
) -> bool:
    """Улучшить жильё за счёт общего бюджета.

    Проверка диапазона здесь, а не только в команде: иначе в базу можно
    записать несуществующий уровень.
    """
    if not 1 <= tier < len(HOMES):
        return False
    conn = storage.conn
    cur = conn.execute(
        "UPDATE rp_marriages SET joint = joint - ?, home = ?"
        " WHERE guild_id = ? AND a_id = ? AND b_id = ? AND joint >= ? AND home = ?",
        (price, tier, guild_id, a_id, b_id, price, tier - 1),
    )
    conn.commit()
    return cur.rowcount > 0


def _sell_home_sync(
    storage, guild_id: int, a_id: int, b_id: int, amount: int
) -> None:
    storage.conn.execute(
        "UPDATE rp_marriages SET home = 0, joint = joint + ?"
        " WHERE guild_id = ? AND a_id = ? AND b_id = ?",
        (amount, guild_id, a_id, b_id),
    )
    storage.conn.commit()


def _couples_sync(storage, guild_id: int, limit: int) -> list[dict]:
    cur = storage.conn.execute(
        "SELECT a_id, b_id, affection, since, home FROM rp_marriages"
        " WHERE guild_id = ? ORDER BY affection DESC, since ASC LIMIT ?",
        (guild_id, limit),
    )
    return [dict(row) for row in cur.fetchall()]


def _due_anniversaries_sync(storage) -> list[dict]:
    """Пары, у которых наступила очередная годовщина."""
    cur = storage.conn.execute("SELECT * FROM rp_marriages")
    due = []
    for row in cur.fetchall():
        since = _parse(row["since"])
        if since is None:
            continue
        milestone = (_now() - since).days // ANNIVERSARY_DAYS
        if milestone > 0 and milestone > row["last_anniv"]:
            data = dict(row)
            data["milestone"] = milestone
            due.append(data)
    return due


def _mark_anniversary_sync(storage, guild_id: int, a_id: int, b_id: int, milestone: int) -> None:
    storage.conn.execute(
        "UPDATE rp_marriages SET last_anniv = ?, joint = joint + ?,"
        " affection = affection + ?"
        " WHERE guild_id = ? AND a_id = ? AND b_id = ?",
        (milestone, ANNIVERSARY_BONUS, ANNIVERSARY_AFFECTION, guild_id, a_id, b_id),
    )
    storage.conn.commit()


def _profile_sync(storage, guild_id: int, user_id: int) -> dict:
    cur = storage.conn.execute(
        "SELECT * FROM rp_profiles WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    row = cur.fetchone()
    if row is None:
        storage.conn.execute(
            "INSERT INTO rp_profiles (guild_id, user_id, balance) VALUES (?, ?, ?)",
            (guild_id, user_id, START_BALANCE),
        )
        storage.conn.commit()
        return {
            "balance": START_BALANCE,
            "job": None,
            "last_work": None,
            "last_daily": None,
        }
    return dict(row)


def _update_profile_sync(storage, guild_id: int, user_id: int, changes: dict) -> None:
    sets = ", ".join(f"{k} = ?" for k in changes)
    storage.conn.execute(
        f"UPDATE rp_profiles SET {sets} WHERE guild_id = ? AND user_id = ?",
        (*changes.values(), guild_id, user_id),
    )
    storage.conn.commit()


def _spend_sync(storage, guild_id: int, user_id: int, amount: int) -> bool:
    """Списать, только если денег хватает. Проверка внутри самого UPDATE:
    иначе две команды подряд успевают пройти проверку до списания."""
    cur = storage.conn.execute(
        "UPDATE rp_profiles SET balance = balance - ?"
        " WHERE guild_id = ? AND user_id = ? AND balance >= ?",
        (amount, guild_id, user_id, amount),
    )
    storage.conn.commit()
    return cur.rowcount > 0


def _transfer_sync(
    storage, guild_id: int, sender: int, receiver: int, amount: int
) -> bool:
    """Перевод одной транзакцией: либо оба изменения, либо ни одного."""
    conn = storage.conn
    cur = conn.execute(
        "UPDATE rp_profiles SET balance = balance - ?"
        " WHERE guild_id = ? AND user_id = ? AND balance >= ?",
        (amount, guild_id, sender, amount),
    )
    if cur.rowcount == 0:
        conn.rollback()
        return False
    conn.execute(
        "INSERT OR IGNORE INTO rp_profiles (guild_id, user_id, balance)"
        " VALUES (?, ?, 0)",
        (guild_id, receiver),
    )
    conn.execute(
        "UPDATE rp_profiles SET balance = balance + ?"
        " WHERE guild_id = ? AND user_id = ?",
        (amount, guild_id, receiver),
    )
    conn.commit()
    return True


def _add_balance_sync(storage, guild_id: int, user_id: int, delta: int) -> None:
    storage.conn.execute(
        "UPDATE rp_profiles SET balance = MAX(0, balance + ?)"
        " WHERE guild_id = ? AND user_id = ?",
        (delta, guild_id, user_id),
    )
    storage.conn.commit()


def _spouse_sync(storage, guild_id: int, user_id: int) -> int | None:
    cur = storage.conn.execute(
        "SELECT a_id, b_id FROM rp_marriages"
        " WHERE guild_id = ? AND (a_id = ? OR b_id = ?)",
        (guild_id, user_id, user_id),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return row["b_id"] if row["a_id"] == user_id else row["a_id"]


def _marry_sync(storage, guild_id: int, one: int, two: int) -> None:
    a, b = sorted((one, two))
    storage.conn.execute(
        "INSERT OR IGNORE INTO rp_marriages (guild_id, a_id, b_id, since)"
        " VALUES (?, ?, ?, ?)",
        (guild_id, a, b, _now().isoformat()),
    )
    storage.conn.commit()


def _divorce_sync(storage, guild_id: int, one: int, two: int) -> None:
    a, b = sorted((one, two))
    storage.conn.execute(
        "DELETE FROM rp_marriages WHERE guild_id = ? AND a_id = ? AND b_id = ?",
        (guild_id, a, b),
    )
    storage.conn.commit()


def _marriage_since_sync(storage, guild_id: int, user_id: int) -> str | None:
    cur = storage.conn.execute(
        "SELECT since FROM rp_marriages WHERE guild_id = ? AND (a_id = ? OR b_id = ?)",
        (guild_id, user_id, user_id),
    )
    row = cur.fetchone()
    return row["since"] if row else None


def _children_sync(storage, guild_id: int, parent_id: int) -> list[int]:
    cur = storage.conn.execute(
        "SELECT child_id FROM rp_family WHERE guild_id = ? AND parent_id = ?",
        (guild_id, parent_id),
    )
    return [row["child_id"] for row in cur.fetchall()]


def _parents_sync(storage, guild_id: int, child_id: int) -> list[int]:
    cur = storage.conn.execute(
        "SELECT parent_id FROM rp_family WHERE guild_id = ? AND child_id = ?",
        (guild_id, child_id),
    )
    return [row["parent_id"] for row in cur.fetchall()]


def _adopt_sync(storage, guild_id: int, parents: list[int], child_id: int) -> None:
    stamp = _now().isoformat()
    storage.conn.executemany(
        "INSERT OR IGNORE INTO rp_family (guild_id, parent_id, child_id, since)"
        " VALUES (?, ?, ?, ?)",
        [(guild_id, parent, child_id, stamp) for parent in parents],
    )
    storage.conn.commit()


def _items_sync(storage, guild_id: int, user_id: int) -> list[tuple[str, int]]:
    cur = storage.conn.execute(
        "SELECT item_key, qty FROM rp_items WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    return [(row["item_key"], row["qty"]) for row in cur.fetchall()]


def _buy_sync(storage, guild_id: int, user_id: int, key: str, price: int) -> bool:
    if not _spend_sync(storage, guild_id, user_id, price):
        return False
    storage.conn.execute(
        "INSERT INTO rp_items (guild_id, user_id, item_key, qty) VALUES (?, ?, ?, 1)"
        " ON CONFLICT(guild_id, user_id, item_key) DO UPDATE SET qty = qty + 1",
        (guild_id, user_id, key),
    )
    storage.conn.commit()
    return True


def _top_sync(storage, guild_id: int, limit: int) -> list[tuple[int, int]]:
    cur = storage.conn.execute(
        "SELECT user_id, balance FROM rp_profiles WHERE guild_id = ?"
        " ORDER BY balance DESC LIMIT ?",
        (guild_id, limit),
    )
    return [(row["user_id"], row["balance"]) for row in cur.fetchall()]


# ----------------------------------------------------------------------
# кнопка согласия
# ----------------------------------------------------------------------


class ConsentView(discord.ui.View):
    """Да/нет, нажать может только тот, кого спрашивают."""

    def __init__(self, target_id: int, timeout: float = 120) -> None:
        super().__init__(timeout=timeout)
        self.target_id = target_id
        self.result: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.target_id:
            await interaction.response.send_message(
                "Это не тебе предложение.", ephemeral=True
            )
            return False
        return True

    def _lock(self) -> None:
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Согласиться", style=discord.ButtonStyle.success)
    async def accept(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.result = True
        self._lock()
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Отказать", style=discord.ButtonStyle.danger)
    async def decline(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.result = False
        self._lock()
        await interaction.response.edit_message(view=self)
        self.stop()


# ----------------------------------------------------------------------


class Family(commands.Cog, name="Семья и экономика"):
    def __init__(self, bot: "MarkovBot") -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        storage = self.bot.storage
        await storage._run(_init_sync, storage)
        self.anniversary_check.start()

    async def cog_unload(self) -> None:
        self.anniversary_check.cancel()

    # ------------------------------------------------------------------

    @tasks.loop(hours=6)
    async def anniversary_check(self) -> None:
        """Раз в несколько часов ищем пары, у которых наступила годовщина."""
        storage = self.bot.storage
        try:
            due = await storage._run(_due_anniversaries_sync, storage)
        except Exception:
            log.exception("не удалось получить список годовщин")
            return

        for row in due:
            # Одна проблемная пара не должна убивать задачу целиком:
            # при необработанной ошибке tasks.loop останавливается навсегда.
            try:
                await self._celebrate(row)
            except Exception:
                log.exception("годовщина: пропускаю пару %s", row["a_id"])

    async def _celebrate(self, row: dict) -> None:
        storage = self.bot.storage
        guild = self.bot.get_guild(row["guild_id"])
        if guild is None:
            return

        channel = guild.system_channel
        if channel is None or not channel.permissions_for(guild.me).send_messages:
            channel = next(
                (
                    c
                    for c in guild.text_channels
                    if c.permissions_for(guild.me).send_messages
                ),
                None,
            )

        # Отмечаем до отправки: если сообщение не уйдёт, поздравление
        # хотя бы не будет повторяться каждые шесть часов.
        await storage._run(
            _mark_anniversary_sync,
            storage,
            row["guild_id"],
            row["a_id"],
            row["b_id"],
            row["milestone"],
        )
        if channel is None:
            return

        months = row["milestone"]
        one = await self._name(guild, row["a_id"])
        two = await self._name(guild, row["b_id"])
        embed = discord.Embed(
            title="Годовщина",
            description=(
                f"**{one}** и **{two}** вместе уже "
                f"{months * ANNIVERSARY_DAYS} дней.\n"
                f"В общий бюджет упало {_money(ANNIVERSARY_BONUS)}."
            ),
            colour=discord.Colour.magenta(),
        )
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    @anniversary_check.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _q(self, fn, *args):
        storage = self.bot.storage
        return await storage._run(fn, storage, *args)

    async def _name(self, guild: discord.Guild, user_id: int) -> str:
        member = guild.get_member(user_id)
        if member is not None:
            return member.display_name
        return f"кто-то ({user_id})"

    # ------------------------------------------------------------------
    # деньги
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="balance", aliases=["баланс", "bal"], description="Показать баланс"
    )
    @commands.guild_only()
    async def balance(
        self, ctx: commands.Context, target: discord.Member | None = None
    ) -> None:
        who = target or ctx.author
        profile = await self._q(_profile_sync, ctx.guild.id, who.id)
        job = await self._job_title(ctx.guild.id, who.id)
        await ctx.send(
            f"**{who.display_name}** — {_money(profile['balance'])}, {job.lower()}."
        )

    @commands.hybrid_command(
        name="daily", aliases=["ежедневка"], description="Забрать ежедневную выплату"
    )
    @commands.guild_only()
    async def daily(self, ctx: commands.Context) -> None:
        profile = await self._q(_profile_sync, ctx.guild.id, ctx.author.id)
        wait = _left(_parse(profile["last_daily"]), DAILY_COOLDOWN)
        if wait is not None:
            await ctx.send(f"Уже забирал. Приходи через {_human(wait)}.")
            return

        await self._q(
            _update_profile_sync,
            ctx.guild.id,
            ctx.author.id,
            {"balance": profile["balance"] + DAILY_AMOUNT, "last_daily": _now().isoformat()},
        )
        await ctx.send(f"Держи {_money(DAILY_AMOUNT)}. Заходи завтра.")

    async def _job_title(self, guild_id: int, user_id: int) -> str:
        """Название работы. Живёт в коге карьеры, поэтому спрашиваем его."""
        career = self.bot.get_cog("Карьера и бизнес")
        if career is None:
            return "без работы"
        return await career.job_title(guild_id, user_id)

    async def _q_relation(self, guild_id: int, user_id: int) -> int:
        """Надбавка к зарплате за жильё, в процентах. Нужна когу карьеры."""
        relation = await self._q(_relation_sync, guild_id, user_id)
        if not relation:
            return 0
        return HOMES[min(relation["home"], len(HOMES) - 1)][2]

    @commands.hybrid_command(
        name="pay", aliases=["перевод"], description="Перевести деньги другому"
    )
    @app_commands.describe(target="Кому", amount="Сколько")
    @commands.guild_only()
    async def pay(
        self, ctx: commands.Context, target: discord.Member, amount: int
    ) -> None:
        if target.bot or target == ctx.author:
            await ctx.send("Так нельзя.")
            return
        if amount <= 0:
            await ctx.send("Сумма должна быть больше нуля.")
            return

        profile = await self._q(_profile_sync, ctx.guild.id, ctx.author.id)
        if profile["balance"] < amount:
            await ctx.send(f"Не хватает. У тебя {_money(profile['balance'])}.")
            return

        await self._q(_profile_sync, ctx.guild.id, target.id)
        if not await self._q(
            _transfer_sync, ctx.guild.id, ctx.author.id, target.id, amount
        ):
            await ctx.send("Не хватило денег.")
            return
        await ctx.send(f"{ctx.author.display_name} → {target.display_name}: {_money(amount)}.")

    @commands.hybrid_command(
        name="top", aliases=["топ"], description="Самые богатые на сервере"
    )
    @commands.guild_only()
    async def top(self, ctx: commands.Context) -> None:
        rows = await self._q(_top_sync, ctx.guild.id, 10)
        if not rows:
            await ctx.send("Пока никто ничего не заработал.")
            return

        lines = []
        for place, (user_id, balance) in enumerate(rows, 1):
            lines.append(f"**{place}.** {await self._name(ctx.guild, user_id)} — {_money(balance)}")
        embed = discord.Embed(
            title="Богатейшие", description="\n".join(lines), colour=discord.Colour.gold()
        )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # имущество
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="shop", aliases=["магазин"], description="Что можно купить")
    @commands.guild_only()
    async def shop(self, ctx: commands.Context) -> None:
        prefix = await self.bot.prefix_for(ctx.guild)
        lines = [f"`{k}` — {title}, {_money(price)}" for k, (title, price) in SHOP.items()]
        lines.append(f"\nКупить: `{prefix}buy car`")
        await ctx.send("\n".join(lines))

    @commands.hybrid_command(name="buy", aliases=["купить"], description="Купить вещь")
    @app_commands.describe(key="Ключ товара, например car")
    @commands.guild_only()
    async def buy(self, ctx: commands.Context, key: str) -> None:
        key = key.lower().strip()
        if key not in SHOP:
            await ctx.send("Такого товара нет.")
            return

        title, price = SHOP[key]
        profile = await self._q(_profile_sync, ctx.guild.id, ctx.author.id)
        if profile["balance"] < price:
            need = price - profile["balance"]
            await ctx.send(f"Не хватает {_money(need)}.")
            return

        if not await self._q(_buy_sync, ctx.guild.id, ctx.author.id, key, price):
            await ctx.send("Не хватило денег — видимо, потратил их только что.")
            return
        await ctx.send(f"{ctx.author.display_name} купил: {title}.")

    @commands.hybrid_command(
        name="inventory", aliases=["имущество", "inv"], description="Что нажито"
    )
    @commands.guild_only()
    async def inventory(
        self, ctx: commands.Context, target: discord.Member | None = None
    ) -> None:
        who = target or ctx.author
        items = await self._q(_items_sync, ctx.guild.id, who.id)
        if not items:
            await ctx.send(f"У {who.display_name} пока ничего нет.")
            return

        lines = []
        total = 0
        for key, qty in items:
            title, price = SHOP.get(key, (key, 0))
            total += price * qty
            lines.append(f"{title} ×{qty}")
        embed = discord.Embed(
            title=f"Имущество: {who.display_name}",
            description="\n".join(lines),
            colour=discord.Colour.dark_teal(),
        )
        embed.set_footer(text=f"На сумму {total} монет")
        await ctx.send(embed=embed)


    # ------------------------------------------------------------------
    # знаки внимания
    # ------------------------------------------------------------------

    async def _touch(
        self, ctx: commands.Context, action: str, verb: str, extra_points: int = 0
    ) -> bool:
        """Общая обвязка для kiss/hug/gift: проверки, кулдаун, начисление."""
        relation = await self._q(_relation_sync, ctx.guild.id, ctx.author.id)
        if relation is None:
            await ctx.send("Ты не в браке.")
            return False

        points, cooldown = TOUCHES[action]
        last = _parse(await self._q(_action_last_sync, ctx.guild.id, ctx.author.id, action))
        wait = _left(last, cooldown)
        if wait is not None:
            await ctx.send(f"Не так часто. Через {_human(wait)}.")
            return False

        await self._q(
            _touch_sync, ctx.guild.id, ctx.author.id, action, points + extra_points
        )
        return True

    @commands.hybrid_command(
        name="kiss", aliases=["поцеловать"], description="Поцеловать супруга"
    )
    @commands.guild_only()
    async def kiss(self, ctx: commands.Context) -> None:
        relation = await self._q(_relation_sync, ctx.guild.id, ctx.author.id)
        if relation is None:
            await ctx.send("Целовать некого — ты не в браке.")
            return

        spouse_id = (
            relation["b_id"] if relation["a_id"] == ctx.author.id else relation["a_id"]
        )
        if not await self._touch(ctx, "kiss", "целует"):
            return
        name = await self._name(ctx.guild, spouse_id)
        await ctx.send(f"💋 {ctx.author.display_name} целует {name}. +{TOUCHES['kiss'][0]} к отношениям.")

    @commands.hybrid_command(
        name="hug", aliases=["обнять"], description="Обнять человека"
    )
    @app_commands.describe(target="Кого обнять")
    @commands.guild_only()
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def hug(self, ctx: commands.Context, target: discord.Member) -> None:
        if target.bot or target == ctx.author:
            await ctx.send("Так нельзя.")
            return

        relation = await self._q(_relation_sync, ctx.guild.id, ctx.author.id)
        spouse_id = None
        if relation:
            spouse_id = (
                relation["b_id"] if relation["a_id"] == ctx.author.id else relation["a_id"]
            )

        # Очки идут только за объятия супруга, остальное — просто по-дружески.
        if target.id == spouse_id:
            if not await self._touch(ctx, "hug", "обнимает"):
                return
            await ctx.send(
                f"🤗 {ctx.author.display_name} обнимает {target.display_name}. "
                f"+{TOUCHES['hug'][0]} к отношениям."
            )
        else:
            await ctx.send(f"🤗 {ctx.author.display_name} обнимает {target.display_name}.")

    @commands.hybrid_command(
        name="gift", aliases=["подарок"], description="Подарить супругу подарок"
    )
    @app_commands.describe(key="Что дарим, например flower")
    @commands.guild_only()
    async def gift(self, ctx: commands.Context, key: str | None = None) -> None:
        if key is None:
            prefix = await self.bot.prefix_for(ctx.guild)
            lines = [
                f"`{k}` — {title}, {_money(price)}, +{points} к отношениям"
                for k, (title, price, points) in GIFTS.items()
            ]
            lines.append(f"\nПодарить: `{prefix}gift flower`")
            await ctx.send("\n".join(lines))
            return

        key = key.lower().strip()
        if key not in GIFTS:
            await ctx.send("Такого подарка нет.")
            return

        relation = await self._q(_relation_sync, ctx.guild.id, ctx.author.id)
        if relation is None:
            await ctx.send("Дарить некому — ты не в браке.")
            return

        title, price, points = GIFTS[key]
        if not await self._q(_spend_sync, ctx.guild.id, ctx.author.id, price):
            await ctx.send(f"Не хватает денег: подарок стоит {_money(price)}.")
            return

        if not await self._touch(ctx, "gift", "дарит", extra_points=points):
            # Кулдаун не пустил — деньги возвращаем.
            await self._q(_add_balance_sync, ctx.guild.id, ctx.author.id, price)
            return

        spouse_id = (
            relation["b_id"] if relation["a_id"] == ctx.author.id else relation["a_id"]
        )
        name = await self._name(ctx.guild, spouse_id)
        await ctx.send(f"🎁 {ctx.author.display_name} дарит {name}: {title}. +{points} к отношениям.")

    # ------------------------------------------------------------------
    # состояние отношений
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="relationship", aliases=["отношения", "rel"], description="Как у вас дела"
    )
    @commands.guild_only()
    async def relationship(
        self, ctx: commands.Context, target: discord.Member | None = None
    ) -> None:
        who = target or ctx.author
        relation = await self._q(_relation_sync, ctx.guild.id, who.id)
        if relation is None:
            await ctx.send(f"{who.display_name} не в браке.")
            return

        spouse_id = relation["b_id"] if relation["a_id"] == who.id else relation["a_id"]
        affection = relation["affection"]
        index = level_index(affection)
        threshold, title, child_limit, penalty = LEVELS[index]

        if index + 1 < len(LEVELS):
            following = LEVELS[index + 1][0]
            done = affection - threshold
            need = following - threshold
            filled = max(0, min(10, done * 10 // max(1, need)))
            bar = "█" * filled + "░" * (10 - filled)
            progress = f"{bar} {affection}/{following}"
        else:
            progress = f"█████████� {affection} — потолок"

        since = _parse(relation["since"])
        days = (_now() - since).days if since else 0
        till = ANNIVERSARY_DAYS - (days % ANNIVERSARY_DAYS)

        embed = discord.Embed(title=title, colour=discord.Colour.magenta())
        embed.add_field(
            name="Пара",
            value=f"{who.display_name} + {await self._name(ctx.guild, spouse_id)}",
            inline=False,
        )
        embed.add_field(name="Уровень", value=progress, inline=False)
        embed.add_field(name="В браке", value=f"{days} дн.")
        embed.add_field(name="До годовщины", value=f"{till} дн.")
        embed.add_field(name="Общий бюджет", value=_money(relation["joint"]))
        embed.add_field(name="Детей можно", value=str(child_limit))
        embed.set_footer(
            text=f"Без внимания отношения проседают на {DECAY_PER_DAY} очка в сутки"
        )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # жильё
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="home", aliases=["хата", "дом"], description="Жильё пары и его улучшение"
    )
    @app_commands.describe(action="upgrade — улучшить за счёт общего бюджета")
    @commands.guild_only()
    async def home(self, ctx: commands.Context, action: str | None = None) -> None:
        relation = await self._q(_relation_sync, ctx.guild.id, ctx.author.id)
        if relation is None:
            await ctx.send("Жильё бывает только у пары.")
            return

        tier = min(relation["home"], len(HOMES) - 1)
        title, _, salary, kids, decay = HOMES[tier]

        if action and action.lower().strip() == "upgrade":
            if tier + 1 >= len(HOMES):
                await ctx.send("Лучше особняка ничего нет.")
                return

            next_title, price, _, _, _ = HOMES[tier + 1]
            ok = await self._q(
                _upgrade_home_sync,
                ctx.guild.id,
                relation["a_id"],
                relation["b_id"],
                tier + 1,
                price,
            )
            if not ok:
                await ctx.send(
                    f"В общем бюджете нужно {_money(price)}, "
                    f"а там {_money(relation['joint'])}."
                )
                return
            await ctx.send(f"🏠 Переехали: **{next_title}**.")
            return

        perks = []
        if salary:
            perks.append(f"зарплата +{salary}%")
        if kids:
            perks.append(f"детей +{kids}")
        if decay < 1:
            perks.append(f"отношения остывают на {int((1 - decay) * 100)}% медленнее")

        embed = discord.Embed(title=title, colour=discord.Colour.dark_teal())
        embed.add_field(
            name="Что даёт", value=", ".join(perks) if perks else "ничего", inline=False
        )
        if tier + 1 < len(HOMES):
            next_title, price, next_salary, next_kids, _ = HOMES[tier + 1]
            prefix = await self.bot.prefix_for(ctx.guild)
            embed.add_field(
                name="Дальше",
                value=(
                    f"{next_title} — {_money(price)} из общего бюджета\n"
                    f"`{prefix}home upgrade`"
                ),
                inline=False,
            )
        embed.set_footer(text=f"В общем бюджете {relation['joint']} монет")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # таблица пар
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="couples", aliases=["пары", "топпар"], description="Лучшие пары сервера"
    )
    @commands.guild_only()
    async def couples(self, ctx: commands.Context) -> None:
        rows = await self._q(_couples_sync, ctx.guild.id, 10)
        if not rows:
            await ctx.send("На сервере пока никто не женат.")
            return

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = []
        for place, row in enumerate(rows, 1):
            one = await self._name(ctx.guild, row["a_id"])
            two = await self._name(ctx.guild, row["b_id"])
            title = LEVELS[level_index(row["affection"])][1]
            since = _parse(row["since"])
            days = (_now() - since).days if since else 0
            home = HOMES[min(row["home"], len(HOMES) - 1)][0]
            lines.append(
                f"{medals.get(place, f'**{place}.**')} **{one}** + **{two}**\n"
                f"　{title} · {row['affection']} очков · {days} дн. · {home}"
            )

        embed = discord.Embed(
            title="Пары сервера",
            description="\n\n".join(lines),
            colour=discord.Colour.magenta(),
        )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # общий бюджет
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="joint", aliases=["бюджет"], description="Общий бюджет пары"
    )
    @app_commands.describe(
        action="in — внести, out — снять, buy — купить", value="Сумма или ключ товара"
    )
    @commands.guild_only()
    async def joint(
        self, ctx: commands.Context, action: str | None = None, value: str | None = None
    ) -> None:
        relation = await self._q(_relation_sync, ctx.guild.id, ctx.author.id)
        if relation is None:
            await ctx.send("Общий бюджет бывает только в браке.")
            return

        prefix = await self.bot.prefix_for(ctx.guild)
        if action is None:
            await ctx.send(
                f"В общем бюджете {_money(relation['joint'])}.\n"
                f"`{prefix}joint in 500` — внести, `{prefix}joint out 500` — снять, "
                f"`{prefix}joint buy house` — купить на двоих."
            )
            return

        action = action.lower().strip()

        if action in ("in", "out"):
            if value is None or not value.strip().isdigit():
                await ctx.send("Нужна сумма числом.")
                return
            amount = int(value)
            if amount <= 0:
                await ctx.send("Сумма должна быть больше нуля.")
                return

            ok = await self._q(
                _joint_move_sync, ctx.guild.id, ctx.author.id, amount, action == "in"
            )
            if not ok:
                await ctx.send("Не хватает денег.")
                return
            where = "в общий бюджет" if action == "in" else "из общего бюджета"
            await ctx.send(f"{ctx.author.display_name} {where}: {_money(amount)}.")
            return

        if action == "buy":
            key = (value or "").lower().strip()
            if key not in SHOP:
                await ctx.send("Такого товара нет.")
                return
            title, price = SHOP[key]
            pair = (relation["a_id"], relation["b_id"])
            if not await self._q(_joint_buy_sync, ctx.guild.id, pair, key, price):
                await ctx.send(
                    f"В общем бюджете не хватает: нужно {_money(price)}."
                )
                return
            await ctx.send(f"Куплено на двоих: {title}.")
            return

        await ctx.send("Не понял. Доступно: `in`, `out`, `buy`.")

    # ------------------------------------------------------------------
    # брак
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="propose", aliases=["свадьба", "marry"], description="Сделать предложение"
    )
    @app_commands.describe(target="Кому делаешь предложение")
    @commands.guild_only()
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def propose(self, ctx: commands.Context, target: discord.Member) -> None:
        if target.bot or target == ctx.author:
            await ctx.send("Так нельзя.")
            return

        if await self._q(_spouse_sync, ctx.guild.id, ctx.author.id):
            await ctx.send("Ты уже в браке. Сначала развод.")
            return
        if await self._q(_spouse_sync, ctx.guild.id, target.id):
            await ctx.send(f"{target.display_name} уже в браке.")
            return

        view = ConsentView(target.id)
        await ctx.send(
            f"{target.mention}, {ctx.author.display_name} делает тебе предложение. "
            "Что скажешь?",
            view=view,
        )
        await view.wait()

        if view.result is None:
            await ctx.send("Ответа не последовало.")
            return
        if not view.result:
            await ctx.send(f"{target.display_name} отказал(а).")
            return

        # Пока думали, любой из двоих мог успеть жениться в другом канале.
        if await self._q(_spouse_sync, ctx.guild.id, ctx.author.id) or await self._q(
            _spouse_sync, ctx.guild.id, target.id
        ):
            await ctx.send("Кто-то успел жениться раньше. Не судьба.")
            return

        await self._q(_profile_sync, ctx.guild.id, target.id)
        await self._q(_marry_sync, ctx.guild.id, ctx.author.id, target.id)
        await ctx.send(
            f"💍 **{ctx.author.display_name}** и **{target.display_name}** теперь женаты."
        )

    @commands.hybrid_command(
        name="divorce", aliases=["развод"], description="Развестись"
    )
    @commands.guild_only()
    async def divorce(self, ctx: commands.Context) -> None:
        spouse_id = await self._q(_spouse_sync, ctx.guild.id, ctx.author.id)
        if spouse_id is None:
            await ctx.send("Ты и так не женат.")
            return

        relation = await self._q(_relation_sync, ctx.guild.id, ctx.author.id)
        index = level_index(relation["affection"]) if relation else 0
        percent = min(50, DIVORCE_SHARE + LEVELS[index][3])

        profile = await self._q(_profile_sync, ctx.guild.id, ctx.author.id)
        share = profile["balance"] * percent // 100

        # Жильё при разводе продаётся, выручка падает в общий бюджет,
        # который тут же делится пополам.
        sold = 0
        if relation and relation["home"] > 0:
            tier = min(relation["home"], len(HOMES) - 1)
            sold = sum(HOMES[i][1] for i in range(1, tier + 1)) // 2
            await self._q(
                _sell_home_sync,
                ctx.guild.id,
                relation["a_id"],
                relation["b_id"],
                sold,
            )
            relation = await self._q(_relation_sync, ctx.guild.id, ctx.author.id)

        # Общий бюджет делится пополам ещё до расторжения брака.
        joint = relation["joint"] if relation else 0
        if joint:
            half = joint // 2
            await self._q(_profile_sync, ctx.guild.id, spouse_id)
            await self._q(_joint_move_sync, ctx.guild.id, ctx.author.id, half, False)
            await self._q(
                _joint_move_sync, ctx.guild.id, spouse_id, joint - half, False
            )

        await self._q(_divorce_sync, ctx.guild.id, ctx.author.id, spouse_id)
        if share:
            await self._q(_profile_sync, ctx.guild.id, spouse_id)
            await self._q(
                _transfer_sync, ctx.guild.id, ctx.author.id, spouse_id, share
            )

        name = await self._name(ctx.guild, spouse_id)
        text = f"💔 **{ctx.author.display_name}** и **{name}** развелись."
        if sold:
            text += f" Жильё продано за {_money(sold)}."
        if joint:
            text += f" Общий бюджет {_money(joint)} поделён пополам."
        if share:
            text += f" Сверху при разделе ушло {_money(share)} ({percent}%)."
        await ctx.send(text)

    @commands.hybrid_command(
        name="adopt", aliases=["усыновить"], description="Взять кого-то в семью"
    )
    @app_commands.describe(target="Кого усыновляешь")
    @commands.guild_only()
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def adopt(self, ctx: commands.Context, target: discord.Member) -> None:
        if target.bot or target == ctx.author:
            await ctx.send("Так нельзя.")
            return

        spouse_id = await self._q(_spouse_sync, ctx.guild.id, ctx.author.id)
        if target.id == spouse_id:
            await ctx.send("Супруга усыновить нельзя.")
            return

        # Сколько детей потянет семья — зависит от уровня отношений.
        relation = await self._q(_relation_sync, ctx.guild.id, ctx.author.id)
        index = level_index(relation["affection"]) if relation else 0
        limit = LEVELS[index][2]
        if relation:
            # Просторное жильё позволяет взять больше детей.
            limit += HOMES[min(relation["home"], len(HOMES) - 1)][3]
        mine = await self._q(_children_sync, ctx.guild.id, ctx.author.id)
        if len(mine) >= limit:
            await ctx.send(
                f"На вашем уровне отношений можно {limit} детей, а уже {len(mine)}. "
                "Укрепляйте отношения."
            )
            return

        parents = await self._q(_parents_sync, ctx.guild.id, target.id)
        if ctx.author.id in parents:
            await ctx.send("Это и так твой ребёнок.")
            return
        if len(parents) >= MAX_PARENTS:
            await ctx.send(f"У {target.display_name} уже есть родители.")
            return

        # Запрещаем закольцовывать древо: нельзя усыновить своего предка.
        if await self._is_ancestor(ctx.guild.id, target.id, ctx.author.id):
            await ctx.send("Нельзя усыновить собственного родителя.")
            return

        view = ConsentView(target.id)
        await ctx.send(
            f"{target.mention}, {ctx.author.display_name} хочет взять тебя в семью. "
            "Согласен?",
            view=view,
        )
        await view.wait()

        if view.result is None:
            await ctx.send("Ответа не последовало.")
            return
        if not view.result:
            await ctx.send(f"{target.display_name} отказался.")
            return

        new_parents = [ctx.author.id]
        # Если есть супруг и у ребёнка остаётся место — записываем обоих.
        if spouse_id and len(parents) + 2 <= MAX_PARENTS:
            new_parents.append(spouse_id)
            await self._q(_profile_sync, ctx.guild.id, spouse_id)

        await self._q(_profile_sync, ctx.guild.id, target.id)
        await self._q(_adopt_sync, ctx.guild.id, new_parents, target.id)

        names = ", ".join([await self._name(ctx.guild, p) for p in new_parents])
        await ctx.send(f"👨‍👩‍👦 {target.display_name} теперь в семье: {names}.")

    async def _is_ancestor(self, guild_id: int, maybe: int, of: int) -> bool:
        """Является ли ``maybe`` предком ``of``. Защита от циклов в древе."""
        seen: set[int] = set()
        queue = [of]
        while queue:
            current = queue.pop()
            if current in seen:
                continue
            seen.add(current)
            for parent in await self._q(_parents_sync, guild_id, current):
                if parent == maybe:
                    return True
                queue.append(parent)
        return False

    @commands.hybrid_command(
        name="family", aliases=["семья"], description="Семейное древо"
    )
    @commands.guild_only()
    async def family(
        self, ctx: commands.Context, target: discord.Member | None = None
    ) -> None:
        who = target or ctx.author
        spouse_id = await self._q(_spouse_sync, ctx.guild.id, who.id)
        parents = await self._q(_parents_sync, ctx.guild.id, who.id)
        children = await self._q(_children_sync, ctx.guild.id, who.id)

        embed = discord.Embed(
            title=f"Семья: {who.display_name}", colour=discord.Colour.magenta()
        )
        if spouse_id:
            since = _parse(await self._q(_marriage_since_sync, ctx.guild.id, who.id))
            days = (_now() - since).days if since else 0
            embed.add_field(
                name="Супруг(а)",
                value=f"{await self._name(ctx.guild, spouse_id)} — {days} дн. в браке",
                inline=False,
            )
        if parents:
            embed.add_field(
                name="Родители",
                value=", ".join([await self._name(ctx.guild, p) for p in parents]),
                inline=False,
            )
        if children:
            embed.add_field(
                name=f"Дети ({len(children)})",
                value=", ".join([await self._name(ctx.guild, c) for c in children]),
                inline=False,
            )
        if not (spouse_id or parents or children):
            embed.description = "Пока один как перст."
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="profile", aliases=["профиль"], description="Карточка участника"
    )
    @commands.guild_only()
    async def profile(
        self, ctx: commands.Context, target: discord.Member | None = None
    ) -> None:
        who = target or ctx.author
        data = await self._q(_profile_sync, ctx.guild.id, who.id)
        spouse_id = await self._q(_spouse_sync, ctx.guild.id, who.id)
        children = await self._q(_children_sync, ctx.guild.id, who.id)
        items = await self._q(_items_sync, ctx.guild.id, who.id)

        worth = sum(SHOP.get(k, (k, 0))[1] * q for k, q in items)
        job = await self._job_title(ctx.guild.id, who.id)

        embed = discord.Embed(title=who.display_name, colour=who.colour)
        embed.set_thumbnail(url=who.display_avatar.url)
        embed.add_field(name="Кошелёк", value=_money(data["balance"]))
        embed.add_field(name="Работа", value=job)
        embed.add_field(name="Имущество", value=_money(worth))
        embed.add_field(
            name="Супруг(а)",
            value=await self._name(ctx.guild, spouse_id) if spouse_id else "нет",
        )
        embed.add_field(name="Дети", value=str(len(children)))
        await ctx.send(embed=embed)


async def setup(bot: "MarkovBot") -> None:
    await bot.add_cog(Family(bot))
