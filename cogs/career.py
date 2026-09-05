"""Верхний ярус экономики: образование, карьера, бизнес и вклады.

Курьер с зарплатой в двести монет не имеет смысла, когда на счету
миллионы. Здесь начинается лестница: выучился — устроился лучше,
накопил — открыл дело, разбогател — положил под процент.

Работа и вакансии переехали сюда из ``cogs.family``: там остались
семья, имущество и базовые деньги.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

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


def plural(amount: int, one: str, few: str, many: str) -> str:
    """«1 работник», «2 работника», «5 работников»."""
    if amount % 10 == 1 and amount % 100 != 11:
        word = one
    elif 2 <= amount % 10 <= 4 and not 12 <= amount % 100 <= 14:
        word = few
    else:
        word = many
    return f"{amount} {word}"

SCHEMA = """
CREATE TABLE IF NOT EXISTS rp_career (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    edu         INTEGER NOT NULL DEFAULT 0,
    job         TEXT,
    last_work   TEXT,
    studying    INTEGER,
    study_until TEXT,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS rp_business (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    kind     TEXT    NOT NULL,
    workers  INTEGER NOT NULL DEFAULT 0,
    stock    INTEGER NOT NULL DEFAULT 0,
    auto     INTEGER NOT NULL DEFAULT 0,
    last_run TEXT,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS rp_deposits (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    kind     TEXT    NOT NULL,
    amount   INTEGER NOT NULL,
    last_pay TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id, kind)
);
"""

# (название, цена, сколько учиться)
EDUCATION: list[tuple[str, int, timedelta]] = [
    ("Без образования", 0, timedelta(0)),
    ("Курсы", 50_000, timedelta(hours=2)),
    ("Колледж", 300_000, timedelta(hours=6)),
    ("Университет", 1_500_000, timedelta(hours=12)),
    ("Аспирантура", 8_000_000, timedelta(hours=24)),
]

# ключ -> (название, нужное образование, минимум, максимум за смену)
JOBS: dict[str, tuple[str, int, int, int]] = {
    "courier": ("Курьер", 0, 50, 150),
    "taxi": ("Таксист", 0, 60, 200),
    "miner": ("Шахтёр", 0, 80, 260),
    "streamer": ("Стример", 0, 10, 500),
    "coder": ("Программист", 1, 400, 900),
    "manager": ("Менеджер", 1, 500, 850),
    "smm": ("Маркетолог", 1, 450, 1_000),
    "engineer": ("Инженер", 2, 1_500, 3_000),
    "doctor": ("Врач", 2, 1_800, 3_200),
    "lawyer": ("Юрист", 3, 5_000, 9_000),
    "architect": ("Архитектор", 3, 4_500, 10_000),
    "director": ("Директор", 4, 15_000, 30_000),
}

WORK_COOLDOWN = timedelta(hours=1)

# ключ -> (название, цена, рабочих мест, зарплата за час,
#          цена единицы товара, выручка с единицы)
# Выручка подобрана так, чтобы окупаемость росла вместе с масштабом:
# от 20 дней у ларька до 25 у корпорации. Если сделать наоборот,
# крупный бизнес превращается в разгон — чем богаче, тем быстрее.
BUSINESSES: dict[str, tuple[str, int, int, int, int, int]] = {
    "kiosk": ("Ларёк", 200_000, 3, 40, 120, 300),
    "cafe": ("Кафе", 1_000_000, 8, 150, 400, 810),
    "shop": ("Магазин", 5_000_000, 20, 400, 1_000, 1_900),
    "factory": ("Завод", 20_000_000, 50, 1_200, 3_000, 4_900),
    "corp": ("Корпорация", 100_000_000, 150, 4_000, 10_000, 15_000),
}

# Бизнес отрабатывает не больше суток без присмотра: иначе можно
# уйти на неделю и вернуться к горе денег из ниоткуда.
BUSINESS_MAX_HOURS = 24

# Вклады. Проценты почасовые, потолок выплаты за час — чтобы вклад
# не превращался в станок при большом капитале.
# Процент почасовой, поэтому цифры обманчиво маленькие: 1% в час
# это 27% в сутки, а 10% в час — 240%. При таких ставках вклад
# обгоняет любой бизнес и становится станком, поэтому ставки ниже,
# зато вклад безрисковый и работает без присмотра.
DEPOSITS: dict[str, tuple[str, float, int, int]] = {
    # ключ: (название, процент в час, потолок выплаты за час, минимум вклада)
    "simple": ("Простой вклад", 0.2, 10_000, 1_000),
    "compound": ("Сложный вклад", 0.1, 100_000, 1_000_000),
}

# Сложный вклад капитализируется: проценты падают в тело вклада.
COMPOUND_KINDS = {"compound"}


def _init_sync(storage) -> None:
    storage.conn.executescript(SCHEMA)
    storage.conn.commit()


def _career_sync(storage, guild_id: int, user_id: int) -> dict:
    cur = storage.conn.execute(
        "SELECT * FROM rp_career WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    row = cur.fetchone()
    if row is None:
        storage.conn.execute(
            "INSERT INTO rp_career (guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id),
        )
        storage.conn.commit()
        row = storage.conn.execute(
            "SELECT * FROM rp_career WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
    return dict(row)


def _career_set_sync(storage, guild_id: int, user_id: int, changes: dict) -> None:
    sets = ", ".join(f"{k} = ?" for k in changes)
    storage.conn.execute(
        f"UPDATE rp_career SET {sets} WHERE guild_id = ? AND user_id = ?",
        (*changes.values(), guild_id, user_id),
    )
    storage.conn.commit()


def _business_sync(storage, guild_id: int, user_id: int) -> dict | None:
    cur = storage.conn.execute(
        "SELECT * FROM rp_business WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _open_business_sync(
    storage, guild_id: int, user_id: int, kind: str, price: int
) -> bool:
    if not _spend_sync(storage, guild_id, user_id, price):
        return False
    storage.conn.execute(
        "INSERT INTO rp_business (guild_id, user_id, kind, last_run)"
        " VALUES (?, ?, ?, ?)",
        (guild_id, user_id, kind, _now().isoformat()),
    )
    storage.conn.commit()
    return True


def _business_set_sync(storage, guild_id: int, user_id: int, changes: dict) -> None:
    sets = ", ".join(f"{k} = ?" for k in changes)
    storage.conn.execute(
        f"UPDATE rp_business SET {sets} WHERE guild_id = ? AND user_id = ?",
        (*changes.values(), guild_id, user_id),
    )
    storage.conn.commit()


def _deposits_sync(storage, guild_id: int, user_id: int) -> dict[str, dict]:
    cur = storage.conn.execute(
        "SELECT * FROM rp_deposits WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    return {row["kind"]: dict(row) for row in cur.fetchall()}


def _deposit_put_sync(
    storage, guild_id: int, user_id: int, kind: str, amount: int
) -> bool:
    if not _spend_sync(storage, guild_id, user_id, amount):
        return False
    storage.conn.execute(
        "INSERT INTO rp_deposits (guild_id, user_id, kind, amount, last_pay)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(guild_id, user_id, kind)"
        " DO UPDATE SET amount = amount + excluded.amount",
        (guild_id, user_id, kind, amount, _now().isoformat()),
    )
    storage.conn.commit()
    return True


def _deposit_close_sync(storage, guild_id: int, user_id: int, kind: str) -> int:
    conn = storage.conn
    row = conn.execute(
        "SELECT amount FROM rp_deposits"
        " WHERE guild_id = ? AND user_id = ? AND kind = ?",
        (guild_id, user_id, kind),
    ).fetchone()
    if row is None:
        return 0
    conn.execute(
        "DELETE FROM rp_deposits WHERE guild_id = ? AND user_id = ? AND kind = ?",
        (guild_id, user_id, kind),
    )
    _add_balance_sync(storage, guild_id, user_id, row["amount"])
    conn.commit()
    return row["amount"]


def deposit_income(kind: str, amount: int, hours: int) -> tuple[int, int]:
    """Сколько накапало и каким стало тело вклада.

    Простой процент считается от исходной суммы, сложный
    капитализируется. И тот и другой упираются в потолок за час.
    """
    _name, rate, cap, _minimum = DEPOSITS[kind]
    if hours <= 0:
        return 0, amount

    if kind in COMPOUND_KINDS:
        body = amount
        earned = 0
        for _ in range(hours):
            step = min(cap, int(body * rate / 100))
            earned += step
            body += step
        return earned, body

    per_hour = min(cap, int(amount * rate / 100))
    return per_hour * hours, amount


def _settle_deposits_sync(storage, guild_id: int, user_id: int) -> list[tuple[str, int]]:
    """Начислить проценты по всем вкладам за прошедшие часы."""
    conn = storage.conn
    paid: list[tuple[str, int]] = []

    for kind, row in _deposits_sync(storage, guild_id, user_id).items():
        if kind not in DEPOSITS:
            continue
        last = _parse(row["last_pay"])
        if last is None:
            continue
        hours = int((_now() - last).total_seconds() // 3600)
        if hours <= 0:
            continue

        earned, body = deposit_income(kind, row["amount"], hours)
        if not earned:
            continue

        new_last = (last + timedelta(hours=hours)).isoformat()
        if kind in COMPOUND_KINDS:
            conn.execute(
                "UPDATE rp_deposits SET amount = ?, last_pay = ?"
                " WHERE guild_id = ? AND user_id = ? AND kind = ?",
                (body, new_last, guild_id, user_id, kind),
            )
        else:
            conn.execute(
                "UPDATE rp_deposits SET last_pay = ?"
                " WHERE guild_id = ? AND user_id = ? AND kind = ?",
                (new_last, guild_id, user_id, kind),
            )
            _add_balance_sync(storage, guild_id, user_id, earned)
        paid.append((kind, earned))

    conn.commit()
    return paid


def _run_business_sync(storage, guild_id: int, user_id: int) -> dict:
    """Отработать накопившиеся часы: произвести, продать, заплатить людям."""
    conn = storage.conn
    row = _business_sync(storage, guild_id, user_id)
    if row is None:
        return {"ok": False}

    kind = row["kind"]
    _name, _price, seats, salary, unit_cost, unit_revenue = BUSINESSES[kind]
    last = _parse(row["last_run"]) or _now()
    hours = min(BUSINESS_MAX_HOURS, int((_now() - last).total_seconds() // 3600))
    if hours <= 0:
        return {"ok": False, "hours": 0}

    workers = min(row["workers"], seats)
    stock = row["stock"]
    auto = bool(row["auto"])

    produced = 0
    wages = 0
    bought = 0
    spent_on_stock = 0

    for _ in range(hours):
        if auto and stock < workers:
            need = workers - stock
            price = need * unit_cost
            if _spend_sync(storage, guild_id, user_id, price):
                stock += need
                bought += need
                spent_on_stock += price

        made = min(workers, stock)
        stock -= made
        produced += made
        wages += workers * salary

    revenue = produced * unit_revenue
    profit = revenue - wages

    conn.execute(
        "UPDATE rp_business SET stock = ?, last_run = ?"
        " WHERE guild_id = ? AND user_id = ?",
        (stock, _now().isoformat(), guild_id, user_id),
    )
    conn.commit()

    if profit > 0:
        _add_balance_sync(storage, guild_id, user_id, profit)
    elif profit < 0:
        _spend_sync(storage, guild_id, user_id, -profit)

    return {
        "ok": True,
        "hours": hours,
        "produced": produced,
        "revenue": revenue,
        "wages": wages,
        "profit": profit,
        "stock": stock,
        "bought": bought,
        "spent_on_stock": spent_on_stock,
        "idle": workers - min(workers, produced // max(1, hours)),
    }


class Career(commands.Cog, name="Карьера и бизнес"):
    def __init__(self, bot: "MarkovBot") -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        storage = self.bot.storage
        await storage._run(_init_sync, storage)

    async def _q(self, fn, *args):
        storage = self.bot.storage
        return await storage._run(fn, storage, *args)

    async def job_title(self, guild_id: int, user_id: int) -> str:
        """Название текущей работы — для карточек в коге семьи."""
        career = await self._q(_career_sync, guild_id, user_id)
        key = career["job"]
        return JOBS[key][0] if key in JOBS else "без работы"

    # ------------------------------------------------------------------
    # образование
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="study", aliases=["учиться", "образование"], description="Получить образование"
    )
    @commands.guild_only()
    async def study(self, ctx: commands.Context) -> None:
        career = await self._q(_career_sync, ctx.guild.id, ctx.author.id)
        prefix = await self.bot.prefix_for(ctx.guild)

        # Учёба идёт — проверяем, не пора ли выдать диплом.
        if career["studying"] is not None:
            until = _parse(career["study_until"])
            left = _left(until, timedelta(0)) if until else None
            if until and _now() < until:
                remain = until - _now()
                name = EDUCATION[career["studying"]][0]
                await ctx.send(f"Ты учишься на «{name}». Осталось {_human(remain)}.")
                return

            level = career["studying"]
            await self._q(
                _career_set_sync,
                ctx.guild.id,
                ctx.author.id,
                {"edu": level, "studying": None, "study_until": None},
            )
            await ctx.send(
                f"🎓 Готово: **{EDUCATION[level][0]}**. "
                f"Новые вакансии: `{prefix}job`"
            )
            return

        level = career["edu"]
        if level + 1 >= len(EDUCATION):
            await ctx.send("Ты уже выучился на всё, что есть.")
            return

        name, price, duration = EDUCATION[level + 1]
        if not await self._q(_spend_sync, ctx.guild.id, ctx.author.id, price):
            await ctx.send(f"«{name}» стоит {_money(price)}, столько у тебя нет.")
            return

        until = _now() + duration
        await self._q(
            _career_set_sync,
            ctx.guild.id,
            ctx.author.id,
            {"studying": level + 1, "study_until": until.isoformat()},
        )
        await ctx.send(
            f"Поступил на «{name}». Учиться {_human(duration)}, "
            f"потом снова напиши `{prefix}study`."
        )

    # ------------------------------------------------------------------
    # работа
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="job", aliases=["работа", "вакансии"], description="Вакансии или устроиться"
    )
    @app_commands.describe(key="Ключ вакансии, например coder")
    @commands.guild_only()
    async def job(self, ctx: commands.Context, key: str | None = None) -> None:
        career = await self._q(_career_sync, ctx.guild.id, ctx.author.id)
        edu = career["edu"]
        prefix = await self.bot.prefix_for(ctx.guild)

        if key is None:
            lines = []
            for k, (title, need, low, high) in JOBS.items():
                if need <= edu:
                    lines.append(f"✅ `{k}` {title} — {low}–{high}")
                else:
                    lines.append(f"🔒 `{k}` {title} — нужно: {EDUCATION[need][0]}")

            embed = discord.Embed(
                title="Вакансии",
                description="\n".join(lines),
                colour=discord.Colour.blurple(),
            )
            embed.set_footer(
                text=f"Твоё образование: {EDUCATION[edu][0]} · "
                f"учиться дальше: {prefix}study"
            )
            await ctx.send(embed=embed)
            return

        key = key.lower().strip()
        if key not in JOBS:
            await ctx.send("Такой вакансии нет.")
            return

        title, need, _low, _high = JOBS[key]
        if need > edu:
            await ctx.send(
                f"Для «{title}» нужно образование «{EDUCATION[need][0]}», "
                f"а у тебя «{EDUCATION[edu][0]}». Учись: `{prefix}study`"
            )
            return

        await self._q(_career_set_sync, ctx.guild.id, ctx.author.id, {"job": key})
        await ctx.send(f"Теперь ты {title.lower()}.")

    @commands.hybrid_command(
        name="work", aliases=["работать", "смена"], description="Сходить на смену"
    )
    @commands.guild_only()
    async def work(self, ctx: commands.Context) -> None:
        import random

        career = await self._q(_career_sync, ctx.guild.id, ctx.author.id)
        prefix = await self.bot.prefix_for(ctx.guild)

        if not career["job"]:
            await ctx.send(f"Ты без работы. Вакансии: `{prefix}job`")
            return

        wait = _left(_parse(career["last_work"]), WORK_COOLDOWN)
        if wait is not None:
            await ctx.send(f"Ты только со смены. Следующая через {_human(wait)}.")
            return

        title, _need, low, high = JOBS[career["job"]]
        earned = random.randint(low, high)

        # Надбавка за жильё остаётся, её считает ког семьи.
        family = self.bot.get_cog("Семья и экономика")
        bonus = 0
        if family is not None:
            relation = await family._q_relation(ctx.guild.id, ctx.author.id)
            if relation:
                bonus = earned * relation // 100

        await self._q(_profile_sync, ctx.guild.id, ctx.author.id)
        await self._q(
            _add_balance_sync, ctx.guild.id, ctx.author.id, earned + bonus
        )
        await self._q(
            _career_set_sync,
            ctx.guild.id,
            ctx.author.id,
            {"last_work": _now().isoformat()},
        )

        text = f"Смена окончена. {title} заработал {_money(earned)}."
        if bonus:
            text += f" Надбавка за жильё: {_money(bonus)}."
        await ctx.send(text)

    # ------------------------------------------------------------------
    # бизнес
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="business", aliases=["бизнес", "дело"], description="Своё дело"
    )
    @commands.guild_only()
    async def business(self, ctx: commands.Context) -> None:
        prefix = await self.bot.prefix_for(ctx.guild)
        row = await self._q(_business_sync, ctx.guild.id, ctx.author.id)

        if row is None:
            lines = []
            for k, (name, price, seats, salary, cost, rev) in BUSINESSES.items():
                margin = rev - cost - salary
                lines.append(
                    f"`{k}` **{name}** — {_money(price)}\n"
                    f"　{seats} мест · товар {cost} · выручка {rev} · "
                    f"чистыми {margin}/час с работника"
                )
            embed = discord.Embed(
                title="Открыть своё дело",
                description="\n".join(lines),
                colour=discord.Colour.dark_gold(),
            )
            embed.set_footer(text=f"Открыть: {prefix}open kiosk")
            await ctx.send(embed=embed)
            return

        kind = row["kind"]
        name, price, seats, salary, cost, rev = BUSINESSES[kind]
        last = _parse(row["last_run"])
        idle_hours = int((_now() - last).total_seconds() // 3600) if last else 0

        embed = discord.Embed(title=name, colour=discord.Colour.dark_gold())
        embed.add_field(name="Работники", value=f"{row['workers']} из {seats}")
        embed.add_field(name="Товар на складе", value=str(row["stock"]))
        embed.add_field(
            name="Автозакупка", value="включена" if row["auto"] else "выключена"
        )
        embed.add_field(
            name="Экономика",
            value=f"зарплата {salary}/час · товар {cost} · выручка {rev}",
            inline=False,
        )
        embed.add_field(
            name="Не собрано",
            value=f"{min(idle_hours, BUSINESS_MAX_HOURS)} ч "
            f"(копится максимум {BUSINESS_MAX_HOURS} ч)",
            inline=False,
        )
        embed.set_footer(
            text=f"{prefix}hire 5 · {prefix}supply 100 · {prefix}collect · {prefix}auto"
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="open", aliases=["открыть"], description="Открыть своё дело"
    )
    @app_commands.describe(kind="Что открываем, например kiosk")
    @commands.guild_only()
    async def open_business(self, ctx: commands.Context, kind: str) -> None:
        kind = kind.lower().strip()
        if kind not in BUSINESSES:
            await ctx.send("Такого дела нет.")
            return

        if await self._q(_business_sync, ctx.guild.id, ctx.author.id):
            await ctx.send("У тебя уже есть дело. Одно на человека.")
            return

        name, price, *_rest = BUSINESSES[kind]
        await self._q(_profile_sync, ctx.guild.id, ctx.author.id)
        if not await self._q(
            _open_business_sync, ctx.guild.id, ctx.author.id, kind, price
        ):
            await ctx.send(f"«{name}» стоит {_money(price)}, столько у тебя нет.")
            return

        prefix = await self.bot.prefix_for(ctx.guild)
        await ctx.send(
            f"🏪 Открыто: **{name}**. Теперь найми людей `{prefix}hire 3` "
            f"и закупи товар `{prefix}supply 100`."
        )

    @commands.hybrid_command(
        name="hire", aliases=["нанять"], description="Нанять работников"
    )
    @app_commands.describe(count="Сколько нанять (отрицательное — уволить)")
    @commands.guild_only()
    async def hire(self, ctx: commands.Context, count: int) -> None:
        row = await self._q(_business_sync, ctx.guild.id, ctx.author.id)
        if row is None:
            await ctx.send("У тебя нет своего дела.")
            return

        name, _price, seats, salary, *_rest = BUSINESSES[row["kind"]]
        workers = max(0, min(seats, row["workers"] + count))
        if workers == row["workers"]:
            await ctx.send(f"Ничего не изменилось. Мест всего {seats}.")
            return

        await self._q(
            _business_set_sync, ctx.guild.id, ctx.author.id, {"workers": workers}
        )
        await ctx.send(
            f"Теперь в «{name}» {plural(workers, 'работник', 'работника', 'работников')} "
            f"из {seats}. Каждый берёт {salary} в час."
        )

    @commands.hybrid_command(
        name="supply", aliases=["закупка", "товар"], description="Закупить товар"
    )
    @app_commands.describe(count="Сколько единиц закупить")
    @commands.guild_only()
    async def supply(self, ctx: commands.Context, count: int) -> None:
        if count <= 0:
            await ctx.send("Количество должно быть больше нуля.")
            return

        row = await self._q(_business_sync, ctx.guild.id, ctx.author.id)
        if row is None:
            await ctx.send("У тебя нет своего дела.")
            return

        name, _price, _seats, _salary, unit_cost, _rev = BUSINESSES[row["kind"]]
        total = unit_cost * count
        await self._q(_profile_sync, ctx.guild.id, ctx.author.id)
        if not await self._q(_spend_sync, ctx.guild.id, ctx.author.id, total):
            await ctx.send(f"Закупка стоит {_money(total)}, столько у тебя нет.")
            return

        await self._q(
            _business_set_sync,
            ctx.guild.id,
            ctx.author.id,
            {"stock": row["stock"] + count},
        )
        await ctx.send(
            f"Закуплено {count} ед. за {_money(total)}. "
            f"На складе: {row['stock'] + count}."
        )

    @commands.hybrid_command(
        name="auto", aliases=["автозакупка"], description="Автоматическая закупка товара"
    )
    @commands.guild_only()
    async def auto(self, ctx: commands.Context) -> None:
        row = await self._q(_business_sync, ctx.guild.id, ctx.author.id)
        if row is None:
            await ctx.send("У тебя нет своего дела.")
            return

        new = 0 if row["auto"] else 1
        await self._q(_business_set_sync, ctx.guild.id, ctx.author.id, {"auto": new})
        await ctx.send(
            "Автозакупка включена: товар докупается сам, деньги списываются "
            "с баланса." if new else "Автозакупка выключена."
        )

    @commands.hybrid_command(
        name="collect", aliases=["собрать", "выручка"], description="Забрать выручку"
    )
    @commands.guild_only()
    async def collect(self, ctx: commands.Context) -> None:
        row = await self._q(_business_sync, ctx.guild.id, ctx.author.id)
        if row is None:
            await ctx.send("У тебя нет своего дела.")
            return

        await self._q(_profile_sync, ctx.guild.id, ctx.author.id)
        result = await self._q(_run_business_sync, ctx.guild.id, ctx.author.id)
        if not result.get("hours"):
            await ctx.send("С прошлого раза не прошло и часа.")
            return

        name = BUSINESSES[row["kind"]][0]
        prefix = await self.bot.prefix_for(ctx.guild)

        embed = discord.Embed(
            title=name,
            colour=discord.Colour.green()
            if result["profit"] >= 0
            else discord.Colour.red(),
        )
        embed.add_field(name="Отработано", value=f"{result['hours']} ч")
        embed.add_field(name="Произведено", value=str(result["produced"]))
        embed.add_field(name="Выручка", value=_money(result["revenue"]))
        embed.add_field(name="Зарплаты", value=_money(result["wages"]))
        if result["bought"]:
            embed.add_field(
                name="Автозакупка",
                value=f"{result['bought']} ед. за {_money(result['spent_on_stock'])}",
                inline=False,
            )
        embed.add_field(
            name="Чистыми",
            value=("+" if result["profit"] >= 0 else "") + _money(result["profit"]),
            inline=False,
        )
        if result["stock"] == 0:
            embed.set_footer(
                text=f"Склад пуст, люди простаивают. Закупись: {prefix}supply 100"
            )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # вклады
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="deposit", aliases=["вклад", "банк"], description="Положить деньги под процент"
    )
    @app_commands.describe(kind="simple или compound", amount="Сколько вложить")
    @commands.guild_only()
    async def deposit(
        self, ctx: commands.Context, kind: str | None = None, amount: int | None = None
    ) -> None:
        prefix = await self.bot.prefix_for(ctx.guild)
        await self._q(_profile_sync, ctx.guild.id, ctx.author.id)
        paid = await self._q(_settle_deposits_sync, ctx.guild.id, ctx.author.id)
        current = await self._q(_deposits_sync, ctx.guild.id, ctx.author.id)

        if kind is None:
            lines = []
            for k, (name, rate, cap, minimum) in DEPOSITS.items():
                mine = current.get(k)
                note = "капитализация" if k in COMPOUND_KINDS else "на баланс"
                lines.append(
                    f"`{k}` **{name}** — {rate}% в час, {note}\n"
                    f"　потолок выплаты {cap} в час · от {_money(minimum)}"
                    + (f"\n　у тебя вложено: {_money(mine['amount'])}" if mine else "")
                )
            embed = discord.Embed(
                title="Вклады",
                description="\n".join(lines),
                colour=discord.Colour.dark_teal(),
            )
            if paid:
                got = ", ".join(
                    f"{DEPOSITS[k][0]}: {_money(v)}" for k, v in paid
                )
                embed.add_field(name="Начислено с прошлого раза", value=got, inline=False)
            embed.set_footer(
                text=f"{prefix}deposit simple 50000 · забрать: {prefix}withdraw simple"
            )
            await ctx.send(embed=embed)
            return

        kind = kind.lower().strip()
        if kind not in DEPOSITS:
            await ctx.send("Есть `simple` и `compound`.")
            return
        if amount is None or amount <= 0:
            await ctx.send("Сколько вложить?")
            return

        name, _rate, _cap, minimum = DEPOSITS[kind]
        if amount < minimum:
            await ctx.send(f"«{name}» открывается от {_money(minimum)}.")
            return

        if not await self._q(
            _deposit_put_sync, ctx.guild.id, ctx.author.id, kind, amount
        ):
            await ctx.send("Столько у тебя нет.")
            return
        await ctx.send(f"Вложено {_money(amount)} в «{name}».")

    @commands.hybrid_command(
        name="withdraw", aliases=["забрать"], description="Закрыть вклад"
    )
    @app_commands.describe(kind="simple или compound")
    @commands.guild_only()
    async def withdraw(self, ctx: commands.Context, kind: str) -> None:
        kind = kind.lower().strip()
        if kind not in DEPOSITS:
            await ctx.send("Есть `simple` и `compound`.")
            return

        await self._q(_profile_sync, ctx.guild.id, ctx.author.id)
        await self._q(_settle_deposits_sync, ctx.guild.id, ctx.author.id)
        amount = await self._q(
            _deposit_close_sync, ctx.guild.id, ctx.author.id, kind
        )
        if not amount:
            await ctx.send("У тебя нет такого вклада.")
            return
        await ctx.send(f"Вклад закрыт, вернулось {_money(amount)}.")


async def setup(bot: "MarkovBot") -> None:
    await bot.add_cog(Career(bot))
