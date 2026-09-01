"""Биржа, гача и подземелья — способы заработка сверх работы.

Экономика общая с ``cogs.family``: те же монеты, тот же кошелёк.
Оттуда же берутся операции со списанием, чтобы не расходились правила
и нельзя было уйти в минус.

Баланс подобран так, чтобы гача была стоком денег, а не станком:
средняя ценность крутки около 2775 при цене 3000.
"""

from __future__ import annotations

import logging
import random
from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.family import (
    _add_balance_sync,
    _human,
    _left,
    _money,
    _now,
    _parse,
    _profile_sync,
    _spend_sync,
)

if TYPE_CHECKING:
    from bot import MarkovBot

log = logging.getLogger(__name__)

SCHEMA = """
-- Биржа общая для всех серверов: рынок один на всех.
CREATE TABLE IF NOT EXISTS rp_stocks (
    symbol TEXT PRIMARY KEY,
    price  INTEGER NOT NULL,
    prev   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rp_holdings (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    symbol   TEXT    NOT NULL,
    qty      INTEGER NOT NULL DEFAULT 0,
    spent    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, symbol)
);

CREATE TABLE IF NOT EXISTS rp_artifacts (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    item     TEXT    NOT NULL,
    qty      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, item)
);

CREATE TABLE IF NOT EXISTS rp_adventure (
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    pity         INTEGER NOT NULL DEFAULT 0,
    rolls        INTEGER NOT NULL DEFAULT 0,
    last_dungeon TEXT,
    PRIMARY KEY (guild_id, user_id)
);
"""

# символ -> (название, стартовая цена, волатильность)
STOCKS: dict[str, tuple[str, int, float]] = {
    "GOLD": ("Золото", 1_000, 0.03),
    "CATS": ("Котики", 250, 0.05),
    "TECH": ("Технологии", 500, 0.09),
    "MEME": ("Мемкоин", 100, 0.18),
    "HYPE": ("Хайп", 50, 0.30),
}

# Комиссия биржи в процентах — с обеих сторон сделки.
TRADE_FEE = 2

# Цена не должна улететь в ноль или в космос.
PRICE_FLOOR = 0.15
PRICE_CEIL = 8.0

# Насколько сильно цену тянет обратно к базовой на каждом тике.
REVERSION = 0.06

PRICE_TICK = 15  # минут между пересчётами

# (название, вес, цена продажи, сила для подземелий)
RARITIES: list[tuple[str, int, int, int]] = [
    ("Обычный", 62, 450, 5),
    ("Редкий", 27, 1_800, 20),
    ("Эпический", 9, 9_000, 80),
    ("Легендарный", 2, 60_000, 300),
]

RARITY_ICONS = ["⚪", "🔵", "🟣", "🟡"]

# ключ -> (название, индекс редкости)
ARTIFACTS: dict[str, tuple[str, int]] = {
    "shard": ("Осколок стекла", 0),
    "bone": ("Старая кость", 0),
    "rope": ("Обрывок верёвки", 0),
    "cup": ("Треснувшая чаша", 0),
    "amulet": ("Амулет из кости", 1),
    "dagger": ("Ржавый кинжал", 1),
    "tome": ("Промокший том", 1),
    "mask": ("Маска шептуна", 2),
    "orb": ("Сфера тумана", 2),
    "heart": ("Сердце бездны", 3),
}

ROLL_COST = 3_000
ROLL_MAX = 10

# Через сколько круток без эпика он выдаётся гарантированно.
PITY_LIMIT = 30

# ключ -> (название, нужная сила, кулдаун, разброс денег, шанс артефакта %)
DUNGEONS: dict[str, tuple[str, int, timedelta, tuple[int, int], int]] = {
    "cellar": ("Подвал", 0, timedelta(hours=2), (200, 600), 12),
    "sewer": ("Канализация", 50, timedelta(hours=3), (600, 1_500), 20),
    "catacombs": ("Катакомбы", 200, timedelta(hours=4), (1_500, 3_500), 30),
    "abyss": ("Бездна", 600, timedelta(hours=6), (4_000, 9_000), 42),
}

# Веса редкостей по подземельям: чем глубже, тем лучше добыча.
DUNGEON_LOOT: dict[str, list[int]] = {
    "cellar": [85, 14, 1, 0],
    "sewer": [65, 30, 5, 0],
    "catacombs": [40, 42, 17, 1],
    "abyss": [15, 45, 35, 5],
}


# ----------------------------------------------------------------------
# запросы
# ----------------------------------------------------------------------


def _init_sync(storage) -> None:
    storage.conn.executescript(SCHEMA)
    for symbol, (_, start, _v) in STOCKS.items():
        storage.conn.execute(
            "INSERT OR IGNORE INTO rp_stocks (symbol, price, prev) VALUES (?, ?, ?)",
            (symbol, start, start),
        )
    storage.conn.commit()


def _prices_sync(storage) -> dict[str, tuple[int, int]]:
    cur = storage.conn.execute("SELECT symbol, price, prev FROM rp_stocks")
    return {row["symbol"]: (row["price"], row["prev"]) for row in cur.fetchall()}


def _tick_sync(storage) -> None:
    """Сдвинуть цены.

    Чистое умножение на (1 + шум) в среднем тянет цену вниз: за сотни
    тиков любая бумага сползает к полу и биржа умирает. Поэтому к шуму
    добавлен возврат к базовой цене — бумага колеблется вокруг своей
    стоимости, а не уходит в ноль навсегда.
    """
    for symbol, (_, start, volatility) in STOCKS.items():
        cur = storage.conn.execute(
            "SELECT price FROM rp_stocks WHERE symbol = ?", (symbol,)
        )
        row = cur.fetchone()
        if row is None:
            continue

        old = row["price"]
        pull = (start - old) * REVERSION
        shock = old * random.gauss(0, volatility)
        moved = old + pull + shock
        moved = max(start * PRICE_FLOOR, min(start * PRICE_CEIL, moved))
        storage.conn.execute(
            "UPDATE rp_stocks SET prev = price, price = ? WHERE symbol = ?",
            (max(1, int(moved)), symbol),
        )
    storage.conn.commit()


def _holdings_sync(storage, guild_id: int, user_id: int) -> dict[str, tuple[int, int]]:
    cur = storage.conn.execute(
        "SELECT symbol, qty, spent FROM rp_holdings"
        " WHERE guild_id = ? AND user_id = ? AND qty > 0",
        (guild_id, user_id),
    )
    return {row["symbol"]: (row["qty"], row["spent"]) for row in cur.fetchall()}


def _buy_stock_sync(
    storage, guild_id: int, user_id: int, symbol: str, qty: int
) -> tuple[bool, int, int]:
    """Купить акции по текущей цене. Возвращает (успех, цена, итог с комиссией)."""
    conn = storage.conn
    cur = conn.execute("SELECT price FROM rp_stocks WHERE symbol = ?", (symbol,))
    row = cur.fetchone()
    if row is None:
        return False, 0, 0

    price = row["price"]
    total = price * qty
    total += total * TRADE_FEE // 100

    if not _spend_sync(storage, guild_id, user_id, total):
        return False, price, total

    conn.execute(
        "INSERT INTO rp_holdings (guild_id, user_id, symbol, qty, spent)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(guild_id, user_id, symbol)"
        " DO UPDATE SET qty = qty + excluded.qty, spent = spent + excluded.spent",
        (guild_id, user_id, symbol, qty, total),
    )
    conn.commit()
    return True, price, total


def _sell_stock_sync(
    storage, guild_id: int, user_id: int, symbol: str, qty: int
) -> tuple[bool, int, int, int]:
    """Продать акции. Возвращает (успех, цена, выручка, вложено в эту долю)."""
    conn = storage.conn
    cur = conn.execute(
        "SELECT qty, spent FROM rp_holdings"
        " WHERE guild_id = ? AND user_id = ? AND symbol = ?",
        (guild_id, user_id, symbol),
    )
    row = cur.fetchone()
    if row is None or row["qty"] < qty:
        return False, 0, 0, 0

    price_row = conn.execute(
        "SELECT price FROM rp_stocks WHERE symbol = ?", (symbol,)
    ).fetchone()
    if price_row is None:
        return False, 0, 0, 0

    price = price_row["price"]
    gross = price * qty
    payout = gross - gross * TRADE_FEE // 100

    # Вложенное списываем пропорционально проданной доле.
    invested = row["spent"] * qty // row["qty"]

    conn.execute(
        "UPDATE rp_holdings SET qty = qty - ?, spent = spent - ?"
        " WHERE guild_id = ? AND user_id = ? AND symbol = ?",
        (qty, invested, guild_id, user_id, symbol),
    )
    conn.execute(
        "UPDATE rp_profiles SET balance = balance + ?"
        " WHERE guild_id = ? AND user_id = ?",
        (payout, guild_id, user_id),
    )
    conn.commit()
    return True, price, payout, invested


def _adventure_sync(storage, guild_id: int, user_id: int) -> dict:
    cur = storage.conn.execute(
        "SELECT * FROM rp_adventure WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    row = cur.fetchone()
    if row is None:
        storage.conn.execute(
            "INSERT INTO rp_adventure (guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id),
        )
        storage.conn.commit()
        return {"pity": 0, "rolls": 0, "last_dungeon": None}
    return dict(row)


def _artifacts_sync(storage, guild_id: int, user_id: int) -> dict[str, int]:
    cur = storage.conn.execute(
        "SELECT item, qty FROM rp_artifacts"
        " WHERE guild_id = ? AND user_id = ? AND qty > 0",
        (guild_id, user_id),
    )
    return {row["item"]: row["qty"] for row in cur.fetchall()}


def _give_artifacts_sync(storage, guild_id: int, user_id: int, items: list[str]) -> None:
    for item in items:
        storage.conn.execute(
            "INSERT INTO rp_artifacts (guild_id, user_id, item, qty) VALUES (?, ?, ?, 1)"
            " ON CONFLICT(guild_id, user_id, item) DO UPDATE SET qty = qty + 1",
            (guild_id, user_id, item),
        )
    storage.conn.commit()


def _sell_artifact_sync(
    storage, guild_id: int, user_id: int, item: str, qty: int, payout: int
) -> bool:
    conn = storage.conn
    cur = conn.execute(
        "UPDATE rp_artifacts SET qty = qty - ?"
        " WHERE guild_id = ? AND user_id = ? AND item = ? AND qty >= ?",
        (qty, guild_id, user_id, item, qty),
    )
    if cur.rowcount == 0:
        conn.rollback()
        return False
    conn.execute(
        "UPDATE rp_profiles SET balance = balance + ?"
        " WHERE guild_id = ? AND user_id = ?",
        (payout, guild_id, user_id),
    )
    conn.commit()
    return True


def _roll_sync(storage, guild_id: int, user_id: int, count: int, cost: int):
    """Крутки с гарантией: возвращает (успех, список ключей, новый счётчик)."""
    if not _spend_sync(storage, guild_id, user_id, cost):
        return False, [], 0

    row = _adventure_sync(storage, guild_id, user_id)
    pity = row["pity"]
    weights = [r[1] for r in RARITIES]
    by_rarity: dict[int, list[str]] = {}
    for key, (_, rarity) in ARTIFACTS.items():
        by_rarity.setdefault(rarity, []).append(key)

    dropped = []
    for _ in range(count):
        pity += 1
        if pity >= PITY_LIMIT:
            rarity = 2  # гарантированный эпик
        else:
            rarity = random.choices(range(len(RARITIES)), weights=weights)[0]
        if rarity >= 2:
            pity = 0
        dropped.append(random.choice(by_rarity[rarity]))

    _give_artifacts_sync(storage, guild_id, user_id, dropped)
    storage.conn.execute(
        "UPDATE rp_adventure SET pity = ?, rolls = rolls + ?"
        " WHERE guild_id = ? AND user_id = ?",
        (pity, count, guild_id, user_id),
    )
    storage.conn.commit()
    return True, dropped, pity


def _finish_dungeon_sync(
    storage, guild_id: int, user_id: int, money: int, loot: list[str]
) -> None:
    _add_balance_sync(storage, guild_id, user_id, money)
    if loot:
        _give_artifacts_sync(storage, guild_id, user_id, loot)
    storage.conn.execute(
        "UPDATE rp_adventure SET last_dungeon = ? WHERE guild_id = ? AND user_id = ?",
        (_now().isoformat(), guild_id, user_id),
    )
    storage.conn.commit()


def power_of(artifacts: dict[str, int]) -> int:
    total = 0
    for key, qty in artifacts.items():
        entry = ARTIFACTS.get(key)
        if entry:
            total += RARITIES[entry[1]][3] * qty
    return total


# ----------------------------------------------------------------------


class Adventure(commands.Cog, name="Биржа и подземелья"):
    def __init__(self, bot: "MarkovBot") -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        storage = self.bot.storage
        await storage._run(_init_sync, storage)
        self.price_tick.start()

    async def cog_unload(self) -> None:
        self.price_tick.cancel()

    async def _q(self, fn, *args):
        storage = self.bot.storage
        return await storage._run(fn, storage, *args)

    @tasks.loop(minutes=PRICE_TICK)
    async def price_tick(self) -> None:
        try:
            await self._q(_tick_sync)
        except Exception:
            log.exception("не удалось пересчитать цены на бирже")

    @price_tick.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # биржа
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="stocks", aliases=["акции", "биржа"], description="Курсы акций"
    )
    @commands.guild_only()
    async def stocks(self, ctx: commands.Context) -> None:
        prices = await self._q(_prices_sync)
        prefix = await self.bot.prefix_for(ctx.guild)

        lines = []
        for symbol, (title, _s, _v) in STOCKS.items():
            price, prev = prices.get(symbol, (0, 0))
            change = (price - prev) * 100 // max(1, prev)
            arrow = "📈" if change > 0 else ("📉" if change < 0 else "➖")
            lines.append(
                f"{arrow} **{symbol}** {title} — {price} {'+' if change > 0 else ''}{change}%"
            )

        embed = discord.Embed(
            title="Биржа",
            description="\n".join(lines),
            colour=discord.Colour.dark_green(),
        )
        embed.set_footer(
            text=f"Цены меняются каждые {PRICE_TICK} мин · комиссия {TRADE_FEE}%"
        )
        embed.add_field(
            name="Как торговать",
            value=f"`{prefix}invest MEME 10` · `{prefix}sellstock MEME 10` · `{prefix}portfolio`",
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="invest", aliases=["купитьакции"], description="Купить акции"
    )
    @app_commands.describe(symbol="Символ, например MEME", qty="Сколько штук")
    @commands.guild_only()
    async def invest(self, ctx: commands.Context, symbol: str, qty: int = 1) -> None:
        symbol = symbol.upper().strip()
        if symbol not in STOCKS:
            await ctx.send(f"Нет такой бумаги. Есть: {', '.join(STOCKS)}.")
            return
        if qty <= 0 or qty > 10_000:
            await ctx.send("Количество от 1 до 10 000.")
            return

        await self._q(_profile_sync, ctx.guild.id, ctx.author.id)
        ok, price, total = await self._q(
            _buy_stock_sync, ctx.guild.id, ctx.author.id, symbol, qty
        )
        if not ok:
            await ctx.send(f"Не хватает денег: нужно {_money(total)}.")
            return
        await ctx.send(
            f"Куплено {qty} × **{symbol}** по {price}. Списано {_money(total)} "
            f"(с комиссией {TRADE_FEE}%)."
        )

    @commands.hybrid_command(
        name="sellstock", aliases=["продатьакции"], description="Продать акции"
    )
    @app_commands.describe(symbol="Символ, например MEME", qty="Сколько штук")
    @commands.guild_only()
    async def sellstock(self, ctx: commands.Context, symbol: str, qty: int = 1) -> None:
        symbol = symbol.upper().strip()
        if qty <= 0:
            await ctx.send("Количество должно быть больше нуля.")
            return

        ok, price, payout, invested = await self._q(
            _sell_stock_sync, ctx.guild.id, ctx.author.id, symbol, qty
        )
        if not ok:
            await ctx.send("У тебя нет столько этих акций.")
            return

        diff = payout - invested
        verdict = f"плюс {_money(diff)}" if diff >= 0 else f"минус {_money(-diff)}"
        await ctx.send(
            f"Продано {qty} × **{symbol}** по {price}. "
            f"Получено {_money(payout)} — {verdict}."
        )

    @commands.hybrid_command(
        name="portfolio", aliases=["портфель"], description="Твои акции"
    )
    @commands.guild_only()
    async def portfolio(
        self, ctx: commands.Context, target: discord.Member | None = None
    ) -> None:
        who = target or ctx.author
        holdings = await self._q(_holdings_sync, ctx.guild.id, who.id)
        if not holdings:
            await ctx.send(f"У {who.display_name} нет акций.")
            return

        prices = await self._q(_prices_sync)
        lines = []
        worth = total_spent = 0
        for symbol, (qty, spent) in holdings.items():
            price = prices.get(symbol, (0, 0))[0]
            value = price * qty
            worth += value
            total_spent += spent
            diff = value - spent
            sign = "+" if diff >= 0 else ""
            lines.append(f"**{symbol}** ×{qty} — {value} ({sign}{diff})")

        diff = worth - total_spent
        embed = discord.Embed(
            title=f"Портфель: {who.display_name}",
            description="\n".join(lines),
            colour=discord.Colour.green() if diff >= 0 else discord.Colour.red(),
        )
        embed.set_footer(
            text=f"Стоимость {worth} · вложено {total_spent} · "
            f"{'прибыль' if diff >= 0 else 'убыток'} {abs(diff)}"
        )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # гача
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="roll", aliases=["крутить", "гача"], description="Крутить артефакты"
    )
    @app_commands.describe(count="Сколько круток, до 10")
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def roll(self, ctx: commands.Context, count: int = 1) -> None:
        if count < 1 or count > ROLL_MAX:
            await ctx.send(f"От 1 до {ROLL_MAX} круток за раз.")
            return

        cost = ROLL_COST * count
        await self._q(_profile_sync, ctx.guild.id, ctx.author.id)
        ok, dropped, pity = await self._q(
            _roll_sync, ctx.guild.id, ctx.author.id, count, cost
        )
        if not ok:
            await ctx.send(f"Не хватает денег: {count} круток стоят {_money(cost)}.")
            return

        best = max(ARTIFACTS[k][1] for k in dropped)
        lines = []
        gained = 0
        for key in dropped:
            title, rarity = ARTIFACTS[key]
            gained += RARITIES[rarity][2]
            lines.append(f"{RARITY_ICONS[rarity]} **{title}** — {RARITIES[rarity][0]}")

        embed = discord.Embed(
            title=f"Крутка ×{count}",
            description="\n".join(lines),
            colour=discord.Colour.gold() if best >= 2 else discord.Colour.greyple(),
        )
        embed.set_footer(
            text=f"Потрачено {cost} · добыто на {gained} · "
            f"до гарантии {PITY_LIMIT - pity}"
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="artifacts", aliases=["артефакты"], description="Твои артефакты и сила"
    )
    @commands.guild_only()
    async def artifacts(
        self, ctx: commands.Context, target: discord.Member | None = None
    ) -> None:
        who = target or ctx.author
        items = await self._q(_artifacts_sync, ctx.guild.id, who.id)
        if not items:
            prefix = await self.bot.prefix_for(ctx.guild)
            await ctx.send(
                f"У {who.display_name} нет артефактов. "
                f"Крутить: `{prefix}roll`, искать: `{prefix}dungeon`."
            )
            return

        by_rarity: dict[int, list[str]] = {}
        worth = 0
        for key, qty in sorted(items.items(), key=lambda kv: -ARTIFACTS[kv[0]][1]):
            title, rarity = ARTIFACTS[key]
            worth += RARITIES[rarity][2] * qty
            by_rarity.setdefault(rarity, []).append(f"`{key}` {title} ×{qty}")

        embed = discord.Embed(
            title=f"Артефакты: {who.display_name}", colour=discord.Colour.purple()
        )
        for rarity in sorted(by_rarity, reverse=True):
            embed.add_field(
                name=f"{RARITY_ICONS[rarity]} {RARITIES[rarity][0]}",
                value="\n".join(by_rarity[rarity]),
                inline=False,
            )
        embed.set_footer(
            text=f"Сила {power_of(items)} · всё вместе стоит {worth}"
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="sell", aliases=["продать"], description="Продать артефакт"
    )
    @app_commands.describe(key="Ключ артефакта, например bone", qty="Сколько")
    @commands.guild_only()
    async def sell(self, ctx: commands.Context, key: str, qty: int = 1) -> None:
        key = key.lower().strip()
        if key not in ARTIFACTS:
            await ctx.send("Такого артефакта нет.")
            return
        if qty <= 0:
            await ctx.send("Количество должно быть больше нуля.")
            return

        title, rarity = ARTIFACTS[key]
        payout = RARITIES[rarity][2] * qty
        await self._q(_profile_sync, ctx.guild.id, ctx.author.id)
        ok = await self._q(
            _sell_artifact_sync, ctx.guild.id, ctx.author.id, key, qty, payout
        )
        if not ok:
            await ctx.send(f"У тебя нет столько «{title}».")
            return
        await ctx.send(f"Продано: {title} ×{qty} за {_money(payout)}.")

    # ------------------------------------------------------------------
    # подземелья
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="dungeon", aliases=["подземелье", "данж"], description="Поход за добычей"
    )
    @app_commands.describe(key="Куда идём, например cellar")
    @commands.guild_only()
    async def dungeon(self, ctx: commands.Context, key: str | None = None) -> None:
        items = await self._q(_artifacts_sync, ctx.guild.id, ctx.author.id)
        power = power_of(items)

        if key is None:
            prefix = await self.bot.prefix_for(ctx.guild)
            lines = []
            for dungeon_key, (title, need, cooldown, money, chance) in DUNGEONS.items():
                mark = "✅" if power >= need else "🔒"
                lines.append(
                    f"{mark} `{dungeon_key}` **{title}** — сила от {need}, "
                    f"{money[0]}–{money[1]} монет, артефакт {chance}%, "
                    f"раз в {int(cooldown.total_seconds() // 3600)} ч"
                )
            embed = discord.Embed(
                title="Подземелья",
                description="\n".join(lines),
                colour=discord.Colour.dark_purple(),
            )
            embed.set_footer(text=f"Твоя сила: {power} · идти: {prefix}dungeon cellar")
            await ctx.send(embed=embed)
            return

        key = key.lower().strip()
        if key not in DUNGEONS:
            await ctx.send("Такого подземелья нет.")
            return

        title, need, cooldown, money_range, chance = DUNGEONS[key]
        if power < need:
            await ctx.send(
                f"Сюда нужна сила {need}, у тебя {power}. "
                "Набери артефактов покруче."
            )
            return

        state = await self._q(_adventure_sync, ctx.guild.id, ctx.author.id)
        wait = _left(_parse(state["last_dungeon"]), cooldown)
        if wait is not None:
            await ctx.send(f"Ты ещё не отдохнул. Следующий поход через {_human(wait)}.")
            return

        # Чем больше запас силы над требованием, тем выше шанс успеха.
        surplus = power - need
        success_chance = min(95, 65 + surplus // 8)
        success = random.randint(1, 100) <= success_chance

        loot: list[str] = []
        if success:
            money = random.randint(*money_range)
            if random.randint(1, 100) <= chance:
                weights = DUNGEON_LOOT[key]
                rarity = random.choices(range(len(RARITIES)), weights=weights)[0]
                pool = [k for k, (_, r) in ARTIFACTS.items() if r == rarity]
                loot.append(random.choice(pool))
        else:
            money = money_range[0] // 3

        await self._q(_profile_sync, ctx.guild.id, ctx.author.id)
        await self._q(
            _finish_dungeon_sync, ctx.guild.id, ctx.author.id, money, loot
        )

        embed = discord.Embed(
            title=title,
            colour=discord.Colour.green() if success else discord.Colour.red(),
        )
        embed.description = (
            "Поход удался." if success else "Еле унёс ноги, добычи почти нет."
        )
        embed.add_field(name="Монеты", value=_money(money))
        if loot:
            artifact_title, rarity = ARTIFACTS[loot[0]]
            embed.add_field(
                name="Находка",
                value=f"{RARITY_ICONS[rarity]} {artifact_title}",
                inline=False,
            )
        embed.set_footer(text=f"Сила {power} · шанс успеха был {success_chance}%")
        await ctx.send(embed=embed)


async def setup(bot: "MarkovBot") -> None:
    await bot.add_cog(Adventure(bot))
