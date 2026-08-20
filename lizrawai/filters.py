"""Чистка текста: что выкидываем перед обучением и перед отправкой.

Два разных этапа:
  * ``clean_for_learning`` — по настройкам сервера, чтобы в корпус не попал мусор;
  * ``sanitize_output`` — всегда, чтобы бот не смог никого пингануть.
"""

from __future__ import annotations

import re

# <@123>, <@!123>, <@&123> — юзеры и роли; <#123> — каналы
MENTION_RE = re.compile(r"<@[!&]?\d+>|<#\d+>")
EVERYONE_RE = re.compile(r"@(everyone|here)\b", re.IGNORECASE)
LINK_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
INVITE_RE = re.compile(
    r"(?:discord\.gg|discord(?:app)?\.com/invite)/\S+", re.IGNORECASE
)
CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
WHITESPACE_RE = re.compile(r"\s+")

DISCORD_MESSAGE_LIMIT = 2000


def clean_for_learning(
    text: str,
    *,
    remove_mentions: bool = True,
    remove_links: bool = True,
    remove_emoji: bool = False,
) -> str:
    """Подготовить сообщение к попаданию в корпус."""
    if not text:
        return ""

    # Приглашения на серверы режем всегда: пересылать их потом — плохая идея.
    text = INVITE_RE.sub(" ", text)

    if remove_links:
        text = LINK_RE.sub(" ", text)
    if remove_mentions:
        text = MENTION_RE.sub(" ", text)
    if remove_emoji:
        text = CUSTOM_EMOJI_RE.sub(" ", text)

    # @everyone/@here не учим никогда, даже если пинги в целом разрешены.
    text = EVERYONE_RE.sub(" ", text)

    return WHITESPACE_RE.sub(" ", text).strip()


def sanitize_output(text: str) -> str:
    """Обезвредить текст перед отправкой в чат.

    Основную защиту даёт ``AllowedMentions.none()`` на уровне бота, но
    ломаем пинги и в самом тексте — на случай, если сообщение куда-то
    перешлют или скопируют.
    """
    if not text:
        return ""

    text = EVERYONE_RE.sub(lambda m: "@​" + m.group(1), text)
    text = INVITE_RE.sub("[ссылка вырезана]", text)

    if len(text) > DISCORD_MESSAGE_LIMIT:
        text = text[: DISCORD_MESSAGE_LIMIT - 1].rstrip() + "…"
    return text


def looks_like_command(text: str, prefixes: tuple[str, ...]) -> bool:
    """Команды в корпус не пускаем, иначе бот учится на самом себе."""
    lowered = text.lstrip().lower()
    return any(lowered.startswith(p.lower()) for p in prefixes if p)


def word_count(text: str) -> int:
    return len(text.split())
