"""Движок цепей Маркова: обучение на корпусе сообщений и генерация текста.

Модуль сознательно не знает ничего про Discord — это чистый Python,
который можно тестировать и использовать отдельно от бота.
"""

from __future__ import annotations

import random
import re
from collections import defaultdict
from typing import Iterable

# Служебные токены начала и конца сообщения. Символы взяты управляющие,
# чтобы они гарантированно не встретились в реальном тексте из чата.
START = "\x02"
END = "\x03"

_TOKEN_RE = re.compile(r"\S+")

# Сколько состояний максимум помним для одного слова в индексе затравок.
# Ограничение нужно, чтобы на большом корпусе память не росла бесконечно.
MAX_STATES_PER_WORD = 256

# Сколько первых букв считаем основой слова при подборе затравки.
STEM_LENGTH = 5
MIN_STEM_LENGTH = 4


def tokenize(text: str) -> list[str]:
    """Разбить текст на токены по пробелам.

    Пунктуацию намеренно не отделяем: в чатах она часть слова
    («ору!!!», «хм...»), и именно это делает генерацию узнаваемой.
    """
    return _TOKEN_RE.findall(text)


class MarkovModel:
    """Цепь Маркова порядка ``order``.

    Состояние — кортеж из ``order`` последних токенов. Продолжения хранятся
    списком с повторами: чем чаще слово встречалось после состояния, тем
    больше у него шансов при ``random.choice`` — так вес учитывается сам собой.
    """

    def __init__(self, order: int = 2) -> None:
        if order < 1:
            raise ValueError("order должен быть >= 1")
        self.order = order
        self._chain: dict[tuple[str, ...], list[str]] = defaultdict(list)
        self._starts: list[tuple[str, ...]] = []
        self._index: dict[str, list[tuple[str, ...]]] = defaultdict(list)
        self.sample_count = 0

    def __len__(self) -> int:
        return self.sample_count

    @property
    def is_ready(self) -> bool:
        """Есть ли чему генерировать. Пустая модель молчит, а не падает."""
        return self.sample_count > 0 and bool(self._starts)

    @property
    def state_count(self) -> int:
        return len(self._chain)

    # ------------------------------------------------------------------
    # обучение
    # ------------------------------------------------------------------

    def train(self, text: str) -> bool:
        """Скормить модели одно сообщение. Возвращает True, если оно училось."""
        tokens = tokenize(text)
        if not tokens:
            return False

        padded = [START] * self.order + tokens + [END]
        self._starts.append(tuple(padded[: self.order]))

        for i in range(len(padded) - self.order):
            state = tuple(padded[i : i + self.order])
            self._chain[state].append(padded[i + self.order])

            # Индекс «слово -> состояния» нужен для генерации с затравкой.
            head = state[0]
            if head not in (START, END):
                bucket = self._index[head.lower()]
                if len(bucket) < MAX_STATES_PER_WORD:
                    bucket.append(state)

        self.sample_count += 1
        return True

    def train_many(self, texts: Iterable[str]) -> int:
        return sum(1 for text in texts if self.train(text))

    # ------------------------------------------------------------------
    # генерация
    # ------------------------------------------------------------------

    def find_seed(self, word: str) -> str | None:
        """Подобрать затравку под слово с поправкой на падежи.

        В русском «погода» и «погоде» — разные токены, поэтому точный
        поиск часто промахивается на живой речи. Если точного совпадения
        нет, ищем в корпусе слово с той же основой.
        """
        if not word:
            return None

        key = word.strip().lower()
        if key in self._index:
            return key

        stem = key[:STEM_LENGTH]
        if len(stem) < MIN_STEM_LENGTH:
            return None

        matches = [known for known in self._index if known.startswith(stem)]
        if not matches:
            return None
        return random.choice(matches)

    def _pick_start(self, seed: str | None) -> tuple[str, ...] | None:
        if seed:
            states = self._index.get(seed.strip().lower())
            if states:
                return random.choice(states)
            return None
        if not self._starts:
            return None
        return random.choice(self._starts)

    def _walk(self, state: tuple[str, ...], max_tokens: int) -> list[str]:
        out = [token for token in state if token not in (START, END)]
        for _ in range(max_tokens):
            options = self._chain.get(state)
            if not options:
                break
            nxt = random.choice(options)
            if nxt == END:
                break
            out.append(nxt)
            state = state[1:] + (nxt,)
        return out

    def generate(
        self,
        seed: str | None = None,
        max_tokens: int = 60,
        min_words: int = 2,
        attempts: int = 12,
    ) -> str | None:
        """Сгенерировать сообщение.

        ``seed`` — слово, с которого начать (если оно есть в корпусе).
        Возвращает ``None``, если корпус пуст или затравка не найдена.
        """
        if not self.is_ready:
            return None

        best: list[str] = []
        for _ in range(max(1, attempts)):
            state = self._pick_start(seed)
            if state is None:
                return None
            tokens = self._walk(state, max_tokens)
            if len(tokens) >= min_words:
                return " ".join(tokens)
            if len(tokens) > len(best):
                best = tokens

        # Ни одна попытка не дотянула до min_words — отдаём лучшее, что вышло.
        return " ".join(best) if best else None

    def generate_dialog(
        self,
        lines: int = 4,
        max_tokens: int = 40,
        min_words: int = 2,
    ) -> list[str]:
        """Сгенерировать несколько реплик подряд — получается «диалог»."""
        result = []
        for _ in range(max(1, lines)):
            line = self.generate(max_tokens=max_tokens, min_words=min_words)
            if line:
                result.append(line)
        return result


def build_model(texts: Iterable[str], order: int = 2) -> MarkovModel:
    """Собрать модель из готового корпуса — удобная обёртка."""
    model = MarkovModel(order=order)
    model.train_many(texts)
    return model
