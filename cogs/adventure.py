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

CREATE TABLE IF NOT EXISTS rp_chars (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    char_key TEXT    NOT NULL,
    copies   INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (guild_id, user_id, char_key)
);

-- Надетый артефакт уходит из инвентаря сюда, чтобы силу нельзя было
-- посчитать дважды, а надетое случайно продать.
CREATE TABLE IF NOT EXISTS rp_equip (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    char_key TEXT    NOT NULL,
    slot     TEXT    NOT NULL,
    item     TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id, char_key, slot)
);

CREATE TABLE IF NOT EXISTS rp_adventure (
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    pity         INTEGER NOT NULL DEFAULT 0,
    rolls        INTEGER NOT NULL DEFAULT 0,
    char_pity    INTEGER NOT NULL DEFAULT 0,
    four_pity    INTEGER NOT NULL DEFAULT 0,
    pulls        INTEGER NOT NULL DEFAULT 0,
    last_dungeon TEXT,
    PRIMARY KEY (guild_id, user_id)
);
"""

# символ -> (название, стартовая цена, волатильность)
STOCKS: dict[str, tuple[str, int, float]] = {
    "GOLD": ("Золото", 1_000, 0.03),
    "OIL": ("Нефть", 800, 0.06),
    "TECH": ("Технологии", 500, 0.09),
    "BANK": ("Банк", 600, 0.04),
    "GYM": ("Качалка", 300, 0.10),
    "CATS": ("Котики", 250, 0.05),
    "PIZZA": ("Пиццерия", 150, 0.07),
    "ANIME": ("Аниме", 120, 0.14),
    "MEME": ("Мемкоин", 100, 0.18),
    "HYPE": ("Хайп", 50, 0.30),
    "DOGE": ("Догикоин", 30, 0.35),
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

# Слоты экипировки. Артефакт можно надеть только в свой слот.
SLOTS: list[str] = ["оружие", "броня", "амулет", "кольцо", "реликвия"]

# ключ -> (название, индекс редкости, слот)
# Ключи первых десяти сохранены с прошлой версии, чтобы у людей
# не пропали уже накопленные артефакты.
ARTIFACTS: dict[str, tuple[str, int, str]] = {
    "stick": ("Палка", 0, "оружие"),
    "rope": ("Обрывок верёвки", 0, "броня"),
    "bone": ("Старая кость", 0, "амулет"),
    "cup": ("Треснувшая чаша", 0, "кольцо"),
    "shard": ("Осколок стекла", 0, "реликвия"),
    "dagger": ("Ржавый кинжал", 1, "оружие"),
    "cloak": ("Драный плащ", 1, "броня"),
    "amulet": ("Амулет из кости", 1, "амулет"),
    "ring": ("Тусклое кольцо", 1, "кольцо"),
    "tome": ("Промокший том", 1, "реликвия"),
    "blade": ("Клинок шёпота", 2, "оружие"),
    "mask": ("Маска шептуна", 2, "броня"),
    "pendant": ("Подвеска пустоты", 2, "амулет"),
    "signet": ("Печатка тумана", 2, "кольцо"),
    "orb": ("Сфера тумана", 2, "реликвия"),
    "scythe": ("Коса бездны", 3, "оружие"),
    "aegis": ("Эгида", 3, "броня"),
    "heart": ("Сердце бездны", 3, "амулет"),
    "crown": ("Венец забытого", 3, "кольцо"),
    "core": ("Ядро мира", 3, "реликвия"),
}

# ключ -> (имя, редкость: 1 = 4★, 2 = 5★, стихия)
CHARACTERS: dict[str, tuple[str, int, str]] = {
    "ugolek": ("Уголёк", 1, "огонь"),
    "myata": ("Мята", 1, "вода"),
    "kremen": ("Кремень", 1, "земля"),
    "iskra": ("Искра", 1, "гроза"),
    "moros": ("Морось", 1, "вода"),
    "luch": ("Луч", 1, "свет"),
    "prah": ("Прах", 1, "тьма"),
    "shkval": ("Шквал", 1, "ветер"),
    "zarya": ("Заря", 2, "свет"),
    "vyuga": ("Вьюга", 2, "лёд"),
    "obsidian": ("Обсидиан", 2, "земля"),
    "polyn": ("Полынь", 2, "тьма"),
}

STARS = {1: "🌟🌟🌟🌟", 2: "⭐⭐⭐⭐⭐"}

# Базовая сила: 4★ и 5★.
CHAR_POWER = {1: 100, 2: 250}

# Каждая лишняя копия усиливает персонажа: созвездия.
COPY_BONUS = 0.12
MAX_COPIES = 7

PULL_COST = 2_000
PULL_MAX = 10

# Мягкий гарант: с 65-й крутки шанс растёт, на 80-й пятизвёздочный гарантирован.
# В оригинале 74 и 90, но там и доход другой — на сервере это месяц гринда.
SOFT_PITY = 65
HARD_PITY = 80
BASE_FIVE_RATE = 0.6
PITY_RAMP = 6.0

# Четырёхзвёздочный гарантирован раз в десять круток.
FOUR_PITY = 10
BASE_FOUR_RATE = 5.1

# Сколько персонажей идёт в отряд и считается в силу.
PARTY_SIZE = 4

ROLL_COST = 3_000
ROLL_MAX = 10

# Через сколько круток без эпика он выдаётся гарантированно.
PITY_LIMIT = 30

# ключ -> (название, нужная сила, кулдаун, разброс денег, шанс артефакта %)
DUNGEONS: dict[str, tuple[str, int, timedelta, tuple[int, int], int]] = {
    "cellar": ("Подвал", 0, timedelta(hours=2), (200, 600), 12),
    "sewer": ("Канализация", 200, timedelta(hours=3), (600, 1_500), 20),
    "catacombs": ("Катакомбы", 700, timedelta(hours=4), (1_500, 3_500), 30),
    "abyss": ("Бездна", 1_400, timedelta(hours=6), (4_000, 9_000), 42),
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

    # Если таблица осталась от прошлой версии — дописываем поля молитв.
    columns = {
        row["name"] for row in storage.conn.execute("PRAGMA table_info(rp_adventure)")
    }
    for name in ("char_pity", "four_pity", "pulls"):
        if name not in columns:
            storage.conn.execute(
                f"ALTER TABLE rp_adventure ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
            )
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
        # Перечитываем строку, а не собираем словарь руками: иначе при
        # добавлении новой колонки старый словарь молча теряет поле,
        # и падает первое же обращение новичка.
        row = storage.conn.execute(
            "SELECT * FROM rp_adventure WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
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




def _chars_sync(storage, guild_id: int, user_id: int) -> dict[str, int]:
    cur = storage.conn.execute(
        "SELECT char_key, copies FROM rp_chars WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    return {row["char_key"]: row["copies"] for row in cur.fetchall()}


def _equipped_sync(storage, guild_id: int, user_id: int) -> dict[str, dict[str, str]]:
    cur = storage.conn.execute(
        "SELECT char_key, slot, item FROM rp_equip WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    result: dict[str, dict[str, str]] = {}
    for row in cur.fetchall():
        result.setdefault(row["char_key"], {})[row["slot"]] = row["item"]
    return result


def _give_chars_sync(storage, guild_id: int, user_id: int, keys: list[str]) -> None:
    for key in keys:
        storage.conn.execute(
            "INSERT INTO rp_chars (guild_id, user_id, char_key, copies)"
            " VALUES (?, ?, ?, 1)"
            " ON CONFLICT(guild_id, user_id, char_key)"
            f" DO UPDATE SET copies = MIN({MAX_COPIES}, copies + 1)",
            (guild_id, user_id, key),
        )
    storage.conn.commit()


def _pull_sync(storage, guild_id: int, user_id: int, count: int, cost: int):
    """Молитва на персонажей. Возвращает (успех, выпавшее, счётчики)."""
    if not _spend_sync(storage, guild_id, user_id, cost):
        return False, [], (0, 0)

    row = _adventure_sync(storage, guild_id, user_id)
    five_pity = row["char_pity"]
    four_pity = row["four_pity"]

    by_rarity: dict[int, list[str]] = {}
    for key, (_, rarity, _el) in CHARACTERS.items():
        by_rarity.setdefault(rarity, []).append(key)

    dropped: list[tuple[str, int]] = []
    for _ in range(count):
        five_pity += 1
        four_pity += 1

        # Мягкий гарант: ближе к порогу шанс растёт, на пороге — сто процентов.
        if five_pity >= HARD_PITY:
            rate = 100.0
        elif five_pity >= SOFT_PITY:
            rate = min(100.0, BASE_FIVE_RATE + (five_pity - SOFT_PITY + 1) * PITY_RAMP)
        else:
            rate = BASE_FIVE_RATE

        if random.uniform(0, 100) < rate:
            rarity = 2
            five_pity = 0
            four_pity = 0
        elif four_pity >= FOUR_PITY or random.uniform(0, 100) < BASE_FOUR_RATE:
            rarity = 1
            four_pity = 0
        else:
            rarity = 1  # в этом баннере ниже четырёх звёзд ничего нет
            four_pity = 0

        key = random.choice(by_rarity[rarity])
        dropped.append((key, rarity))

    _give_chars_sync(storage, guild_id, user_id, [k for k, _ in dropped])
    storage.conn.execute(
        "UPDATE rp_adventure SET char_pity = ?, four_pity = ?, pulls = pulls + ?"
        " WHERE guild_id = ? AND user_id = ?",
        (five_pity, four_pity, count, guild_id, user_id),
    )
    storage.conn.commit()
    return True, dropped, (five_pity, four_pity)


def _equip_sync(
    storage, guild_id: int, user_id: int, char_key: str, item: str, slot: str
) -> tuple[bool, str | None]:
    """Надеть артефакт. Возвращает (успех, что было снято из этого слота)."""
    conn = storage.conn

    # Забираем предмет из инвентаря: иначе он остался бы и там, и на персонаже.
    cur = conn.execute(
        "UPDATE rp_artifacts SET qty = qty - 1"
        " WHERE guild_id = ? AND user_id = ? AND item = ? AND qty >= 1",
        (guild_id, user_id, item),
    )
    if cur.rowcount == 0:
        conn.rollback()
        return False, None

    old = conn.execute(
        "SELECT item FROM rp_equip"
        " WHERE guild_id = ? AND user_id = ? AND char_key = ? AND slot = ?",
        (guild_id, user_id, char_key, slot),
    ).fetchone()

    if old is not None:
        # Снятое возвращаем в инвентарь, а не теряем.
        conn.execute(
            "INSERT INTO rp_artifacts (guild_id, user_id, item, qty) VALUES (?, ?, ?, 1)"
            " ON CONFLICT(guild_id, user_id, item) DO UPDATE SET qty = qty + 1",
            (guild_id, user_id, old["item"]),
        )

    conn.execute(
        "INSERT INTO rp_equip (guild_id, user_id, char_key, slot, item)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(guild_id, user_id, char_key, slot) DO UPDATE SET item = excluded.item",
        (guild_id, user_id, char_key, slot, item),
    )
    conn.commit()
    return True, old["item"] if old else None


def _unequip_sync(
    storage, guild_id: int, user_id: int, char_key: str, slot: str
) -> str | None:
    conn = storage.conn
    row = conn.execute(
        "SELECT item FROM rp_equip"
        " WHERE guild_id = ? AND user_id = ? AND char_key = ? AND slot = ?",
        (guild_id, user_id, char_key, slot),
    ).fetchone()
    if row is None:
        return None

    conn.execute(
        "DELETE FROM rp_equip"
        " WHERE guild_id = ? AND user_id = ? AND char_key = ? AND slot = ?",
        (guild_id, user_id, char_key, slot),
    )
    conn.execute(
        "INSERT INTO rp_artifacts (guild_id, user_id, item, qty) VALUES (?, ?, ?, 1)"
        " ON CONFLICT(guild_id, user_id, item) DO UPDATE SET qty = qty + 1",
        (guild_id, user_id, row["item"]),
    )
    conn.commit()
    return row["item"]


def char_power(char_key: str, copies: int, gear: dict[str, str]) -> int:
    """Сила персонажа: база с учётом копий плюс надетые артефакты."""
    _name, rarity, _el = CHARACTERS[char_key]
    base = CHAR_POWER[rarity] * (1 + COPY_BONUS * (copies - 1))
    extra = sum(
        RARITIES[ARTIFACTS[item][1]][3] for item in gear.values() if item in ARTIFACTS
    )
    return int(base + extra)


def party_power(
    chars: dict[str, int], equipped: dict[str, dict[str, str]]
) -> tuple[int, list[tuple[str, int]]]:
    """Сила отряда: четыре сильнейших персонажа."""
    ranked = sorted(
        (
            (key, char_power(key, copies, equipped.get(key, {})))
            for key, copies in chars.items()
            if key in CHARACTERS
        ),
        key=lambda kv: -kv[1],
    )
    party = ranked[:PARTY_SIZE]
    return sum(power for _k, power in party), party


def find_char(query: str) -> str | None:
    """Найти персонажа по имени или ключу, без оглядки на регистр."""
    needle = query.strip().lower()
    if needle in CHARACTERS:
        return needle
    for key, (name, _r, _e) in CHARACTERS.items():
        if name.lower() == needle:
            return key
    return None


def find_artifact(query: str) -> str | None:
    needle = query.strip().lower()
    if needle in ARTIFACTS:
        return needle
    for key, (name, _r, _s) in ARTIFACTS.items():
        if name.lower() == needle:
            return key
    return None


def power_of(artifacts: dict[str, int]) -> int:
    """Сила артефактов, лежащих в инвентаре без дела."""
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

        rows = ["```", f"{'':2}{'БУМАГА':8}{'СЕЙЧАС':>8}{'БАЗА':>8}{'К БАЗЕ':>8}{'ТИК':>7}"]
        for symbol, (title, start, _v) in STOCKS.items():
            price, prev = prices.get(symbol, (start, start))
            tick = (price - prev) * 100 // max(1, prev)
            from_base = (price - start) * 100 // max(1, start)
            mark = "↑" if tick > 0 else ("↓" if tick < 0 else "=")
            rows.append(
                f"{mark} {symbol:8}{price:>8}{start:>8}"
                f"{('+' if from_base > 0 else '') + str(from_base) + '%':>8}"
                f"{('+' if tick > 0 else '') + str(tick) + '%':>7}"
            )
        rows.append("```")

        embed = discord.Embed(
            title="Биржа",
            description="\n".join(rows),
            colour=discord.Colour.dark_green(),
        )
        embed.add_field(
            name="Как читать",
            value=(
                "**База** — цена, вокруг которой бумага колеблется. "
                "Сильно ниже базы — повод присмотреться, сильно выше — повод продать."
            ),
            inline=False,
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
    # персонажи
    # ------------------------------------------------------------------

    async def _power_of_user(self, guild_id: int, user_id: int):
        chars = await self._q(_chars_sync, guild_id, user_id)
        equipped = await self._q(_equipped_sync, guild_id, user_id)
        total, party = party_power(chars, equipped)
        return total, party, chars, equipped

    @commands.hybrid_command(
        name="pull",
        aliases=["молитва", "wish"],
        description="Молитва на персонажей",
    )
    @app_commands.describe(count="Сколько молитв, до 10")
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def pull(self, ctx: commands.Context, count: int = 1) -> None:
        if count < 1 or count > PULL_MAX:
            await ctx.send(f"От 1 до {PULL_MAX} молитв за раз.")
            return

        cost = PULL_COST * count
        await self._q(_profile_sync, ctx.guild.id, ctx.author.id)
        ok, dropped, (five_pity, _four) = await self._q(
            _pull_sync, ctx.guild.id, ctx.author.id, count, cost
        )
        if not ok:
            await ctx.send(f"Не хватает денег: {count} молитв стоят {_money(cost)}.")
            return

        best = max(rarity for _k, rarity in dropped)
        lines = []
        for key, rarity in dropped:
            name, _r, element = CHARACTERS[key]
            lines.append(f"{STARS[rarity]} **{name}** · {element}")

        embed = discord.Embed(
            title=f"Молитва ×{count}",
            description="\n".join(lines),
            colour=discord.Colour.gold() if best == 2 else discord.Colour.blurple(),
        )
        embed.set_footer(
            text=f"Потрачено {cost} · до гаранта на 5★ осталось {HARD_PITY - five_pity}"
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="chars", aliases=["персонажи"], description="Твои персонажи"
    )
    @commands.guild_only()
    async def chars(
        self, ctx: commands.Context, target: discord.Member | None = None
    ) -> None:
        who = target or ctx.author
        total, party, chars, equipped = await self._power_of_user(ctx.guild.id, who.id)
        if not chars:
            prefix = await self.bot.prefix_for(ctx.guild)
            await ctx.send(
                f"У {who.display_name} нет персонажей. Молиться: `{prefix}pull`."
            )
            return

        in_party = {key for key, _p in party}
        ranked = sorted(
            (
                (key, copies, char_power(key, copies, equipped.get(key, {})))
                for key, copies in chars.items()
                if key in CHARACTERS
            ),
            key=lambda kv: -kv[2],
        )

        lines = []
        for key, copies, power in ranked:
            name, rarity, element = CHARACTERS[key]
            mark = "⚔️" if key in in_party else "　"
            cons = f" C{copies - 1}" if copies > 1 else ""
            lines.append(
                f"{mark} {STARS[rarity][:2]} **{name}**{cons} · {element} · сила {power}"
            )

        embed = discord.Embed(
            title=f"Персонажи: {who.display_name}",
            description="\n".join(lines),
            colour=discord.Colour.purple(),
        )
        embed.set_footer(
            text=f"⚔️ — в отряде · сила отряда {total} · всего героев {len(chars)}"
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="char", aliases=["герой"], description="Карточка персонажа и его экипировка"
    )
    @app_commands.describe(name="Имя персонажа")
    @commands.guild_only()
    async def char(self, ctx: commands.Context, *, name: str) -> None:
        key = find_char(name)
        if key is None:
            await ctx.send("Такого персонажа нет.")
            return

        chars = await self._q(_chars_sync, ctx.guild.id, ctx.author.id)
        if key not in chars:
            await ctx.send(f"У тебя нет этого персонажа.")
            return

        equipped = await self._q(_equipped_sync, ctx.guild.id, ctx.author.id)
        gear = equipped.get(key, {})
        title, rarity, element = CHARACTERS[key]
        copies = chars[key]
        power = char_power(key, copies, gear)

        lines = []
        for slot in SLOTS:
            item = gear.get(slot)
            if item and item in ARTIFACTS:
                art_name, art_rarity, _s = ARTIFACTS[item]
                lines.append(
                    f"{RARITY_ICONS[art_rarity]} **{slot}**: {art_name} "
                    f"(+{RARITIES[art_rarity][3]})"
                )
            else:
                lines.append(f"⬜ **{slot}**: пусто")

        prefix = await self.bot.prefix_for(ctx.guild)
        embed = discord.Embed(
            title=f"{STARS[rarity][:2]} {title}",
            description=f"Стихия: {element} · копий: {copies}",
            colour=discord.Colour.gold() if rarity == 2 else discord.Colour.blurple(),
        )
        embed.add_field(name="Экипировка", value="\n".join(lines), inline=False)
        embed.add_field(name="Сила", value=str(power))
        embed.set_footer(text=f"Надеть: {prefix}equip {title} кинжал")
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="equip", aliases=["надеть"], description="Надеть артефакт на персонажа"
    )
    @app_commands.describe(target="Имя персонажа", item="Название или ключ артефакта")
    @commands.guild_only()
    async def equip(self, ctx: commands.Context, target: str, *, item: str) -> None:
        key = find_char(target)
        if key is None:
            await ctx.send("Такого персонажа нет.")
            return

        art = find_artifact(item)
        if art is None:
            await ctx.send("Такого артефакта нет.")
            return

        chars = await self._q(_chars_sync, ctx.guild.id, ctx.author.id)
        if key not in chars:
            await ctx.send("У тебя нет этого персонажа.")
            return

        art_name, art_rarity, slot = ARTIFACTS[art]
        ok, replaced = await self._q(
            _equip_sync, ctx.guild.id, ctx.author.id, key, art, slot
        )
        if not ok:
            await ctx.send(f"«{art_name}» нет у тебя в инвентаре.")
            return

        text = f"На **{CHARACTERS[key][0]}** надет {art_name} в слот «{slot}»."
        if replaced:
            text += f" Снятый {ARTIFACTS[replaced][0]} вернулся в инвентарь."
        await ctx.send(text)

    @commands.hybrid_command(
        name="unequip", aliases=["снять"], description="Снять артефакт с персонажа"
    )
    @app_commands.describe(target="Имя персонажа", slot="Слот: оружие, броня и так далее")
    @commands.guild_only()
    async def unequip(self, ctx: commands.Context, target: str, slot: str) -> None:
        key = find_char(target)
        if key is None:
            await ctx.send("Такого персонажа нет.")
            return

        slot = slot.strip().lower()
        if slot not in SLOTS:
            await ctx.send(f"Слоты: {', '.join(SLOTS)}.")
            return

        removed = await self._q(_unequip_sync, ctx.guild.id, ctx.author.id, key, slot)
        if removed is None:
            await ctx.send("В этом слоте пусто.")
            return
        await ctx.send(
            f"С **{CHARACTERS[key][0]}** снят {ARTIFACTS[removed][0]}, "
            "он вернулся в инвентарь."
        )

    # ------------------------------------------------------------------
    # подземелья
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="dungeon", aliases=["подземелье", "данж"], description="Поход за добычей"
    )
    @app_commands.describe(key="Куда идём, например cellar")
    @commands.guild_only()
    async def dungeon(self, ctx: commands.Context, key: str | None = None) -> None:
        power, party, chars, _eq = await self._power_of_user(
            ctx.guild.id, ctx.author.id
        )

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
            squad = ", ".join(CHARACTERS[k][0] for k, _p in party) or "отряда нет"
            embed.set_footer(
                text=f"Сила отряда: {power} ({squad}) · идти: {prefix}dungeon cellar"
            )
            await ctx.send(embed=embed)
            return

        key = key.lower().strip()
        if key not in DUNGEONS:
            await ctx.send("Такого подземелья нет.")
            return

        title, need, cooldown, money_range, chance = DUNGEONS[key]
        if power < need:
            prefix = await self.bot.prefix_for(ctx.guild)
            hint = (
                f"Молись за персонажей: `{prefix}pull`."
                if not chars
                else f"Надень артефакты на отряд: `{prefix}equip`."
            )
            await ctx.send(f"Сюда нужна сила {need}, у отряда {power}. {hint}")
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
