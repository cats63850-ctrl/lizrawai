"""Семейно-экономическая система: брак, дети, развод, имущество, деньги, работа.

Всё живёт в тех же таблицах SQLite, что и корпус бота, но в отдельных
таблицах с префиксом ``rp_``. Схема создаётся при загрузке кога, поэтому
трогать ``storage.py`` не нужно.

Любое действие, затрагивающее другого человека (свадьба, усыновление),
требует его явного согласия кнопкой. Никого нельзя женить или усыновить
против воли — иначе это превращается в инструмент травли.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot import MarkovBot

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
    guild_id INTEGER NOT NULL,
    a_id     INTEGER NOT NULL,
    b_id     INTEGER NOT NULL,
    since    TEXT    NOT NULL,
    PRIMARY KEY (guild_id, a_id, b_id)
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

# работа: (название, минимум, максимум)
JOBS: dict[str, tuple[str, int, int]] = {
    "courier": ("Курьер", 50, 150),
    "taxi": ("Таксист", 60, 200),
    "miner": ("Шахтёр", 80, 260),
    "coder": ("Программист", 120, 320),
    "streamer": ("Стример", 10, 500),
}

WORK_COOLDOWN = timedelta(hours=1)
DAILY_COOLDOWN = timedelta(hours=24)
DAILY_AMOUNT = 250
START_BALANCE = 100

# Сколько процентов баланса уходит бывшему при разводе.
DIVORCE_SHARE = 20

MAX_PARENTS = 2


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
        job = JOBS.get(profile["job"] or "", ("без работы",))[0]
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

    @commands.hybrid_command(
        name="work", aliases=["работать"], description="Сходить на работу и заработать"
    )
    @commands.guild_only()
    async def work(self, ctx: commands.Context) -> None:
        profile = await self._q(_profile_sync, ctx.guild.id, ctx.author.id)
        if not profile["job"]:
            prefix = await self.bot.prefix_for(ctx.guild)
            await ctx.send(f"Ты без работы. Список вакансий: `{prefix}job`.")
            return

        wait = _left(_parse(profile["last_work"]), WORK_COOLDOWN)
        if wait is not None:
            await ctx.send(f"Ты только со смены. Следующая через {_human(wait)}.")
            return

        title, low, high = JOBS[profile["job"]]
        earned = random.randint(low, high)
        await self._q(
            _update_profile_sync,
            ctx.guild.id,
            ctx.author.id,
            {"balance": profile["balance"] + earned, "last_work": _now().isoformat()},
        )
        await ctx.send(f"Смена окончена. {title} заработал {_money(earned)}.")

    @commands.hybrid_command(
        name="job", aliases=["работа"], description="Список вакансий или устроиться"
    )
    @app_commands.describe(key="Ключ вакансии, например coder")
    @commands.guild_only()
    async def job(self, ctx: commands.Context, key: str | None = None) -> None:
        if key is None:
            lines = [
                f"`{k}` — {title}, {low}–{high} {CURRENCY} за смену"
                for k, (title, low, high) in JOBS.items()
            ]
            prefix = await self.bot.prefix_for(ctx.guild)
            lines.append(f"\nУстроиться: `{prefix}job coder`")
            await ctx.send("\n".join(lines))
            return

        key = key.lower().strip()
        if key not in JOBS:
            await ctx.send("Такой вакансии нет.")
            return

        await self._q(
            _update_profile_sync, ctx.guild.id, ctx.author.id, {"job": key}
        )
        await ctx.send(f"Теперь ты {JOBS[key][0].lower()}.")

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
    # отношения
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

        profile = await self._q(_profile_sync, ctx.guild.id, ctx.author.id)
        share = profile["balance"] * DIVORCE_SHARE // 100

        await self._q(_divorce_sync, ctx.guild.id, ctx.author.id, spouse_id)
        if share:
            await self._q(_profile_sync, ctx.guild.id, spouse_id)
            await self._q(
                _transfer_sync, ctx.guild.id, ctx.author.id, spouse_id, share
            )

        name = await self._name(ctx.guild, spouse_id)
        text = f"💔 **{ctx.author.display_name}** и **{name}** развелись."
        if share:
            text += f" При разделе имущества ушло {_money(share)}."
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
        job = JOBS.get(data["job"] or "", ("Безработный",))[0]

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
