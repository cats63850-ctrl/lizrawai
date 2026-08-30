"""Демо без Discord: скармливаем движку кусок «чата» и смотрим, что выйдет.

    python demo.py

Полезно, чтобы покрутить порядок цепи и понять, сколько сообщений
нужно накопить, прежде чем звать бота на живой сервер.
"""

from __future__ import annotations

import random

import filters
from markov import build_model

FAKE_CHAT = [
    "кто вечером в доту",
    "я за, только сначала поем",
    "опять эта дота, давайте лучше в кс",
    "в кс без микрофона делать нечего",
    "у меня микрофон сдох ещё в прошлом году",
    "поем и приду, ждите",
    "ждём уже сорок минут между прочим",
    "сорок минут это ещё по-божески",
    "кто-нибудь видел мою мышку",
    "мышка была на столе вчера вечером",
    "вчера вечером тут вообще никого не было",
    "я был, просто молчал",
    "молчал он, ага",
    "давайте уже начинать, а то так и просидим",
    "начинаем через пять минут, финальный ответ",
    "пять минут по-твоему это сколько",
    "по-моему это сорок минут",
    "опять сорок минут, я спать",
    "спать это святое",
    "дота это святое, а спать вторично",
]


def main() -> None:
    random.seed()
    corpus = [filters.clean_for_learning(line) for line in FAKE_CHAT]

    for order in (1, 2, 3):
        model = build_model(corpus, order=order)
        print(f"\n=== порядок цепи {order} "
              f"({model.sample_count} сообщений, {model.state_count} состояний) ===")
        for _ in range(5):
            text = model.generate(max_tokens=25, min_words=2)
            print(" •", filters.sanitize_output(text or "(пусто)"))

    model = build_model(corpus, order=2)
    print("\n=== с затравкой «сорок» ===")
    for _ in range(3):
        print(" •", model.generate(seed="сорок", max_tokens=25, min_words=2))

    print("\n=== диалог ===")
    for line in model.generate_dialog(lines=4, max_tokens=20, min_words=2):
        print(" —", line)


if __name__ == "__main__":
    main()
