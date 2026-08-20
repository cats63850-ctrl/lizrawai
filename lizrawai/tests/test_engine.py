"""Тесты движка, фильтров и хранилища. Discord для них не нужен.

Запуск:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import os
import random
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import filters
from markov import MarkovModel, build_model, tokenize
from storage import Storage

CORPUS = [
    "кто пойдёт играть вечером",
    "кто пойдёт в магазин",
    "я пойду играть вечером в доту",
    "вечером в доту это святое",
    "сегодня никто не пойдёт никуда",
    "магазин закрыт уже вечером",
]


class TestTokenize(unittest.TestCase):
    def test_splits_on_whitespace(self):
        self.assertEqual(tokenize("привет   мир\nещё раз"), ["привет", "мир", "ещё", "раз"])

    def test_keeps_punctuation_attached(self):
        self.assertEqual(tokenize("ору!!! хм..."), ["ору!!!", "хм..."])

    def test_empty(self):
        self.assertEqual(tokenize("   "), [])


class TestMarkovModel(unittest.TestCase):
    def setUp(self):
        random.seed(1337)
        self.model = build_model(CORPUS, order=2)

    def test_empty_model_is_not_ready(self):
        model = MarkovModel(order=2)
        self.assertFalse(model.is_ready)
        self.assertIsNone(model.generate())

    def test_trains_and_reports_size(self):
        self.assertEqual(self.model.sample_count, len(CORPUS))
        self.assertTrue(self.model.is_ready)
        self.assertGreater(self.model.state_count, 0)

    def test_single_word_message_is_learned(self):
        model = MarkovModel(order=2)
        self.assertTrue(model.train("ага"))
        self.assertFalse(model.train("   "))
        self.assertEqual(model.sample_count, 1)

    def test_generates_non_empty_text(self):
        for _ in range(50):
            text = self.model.generate(max_tokens=40, min_words=1)
            self.assertIsInstance(text, str)
            self.assertTrue(text.strip())

    def test_output_has_no_service_tokens(self):
        for _ in range(100):
            text = self.model.generate(max_tokens=40, min_words=1)
            self.assertNotIn("\x02", text)
            self.assertNotIn("\x03", text)

    def test_respects_max_tokens(self):
        for _ in range(30):
            text = self.model.generate(max_tokens=5, min_words=1)
            # order=2 может добавить до 2 токенов затравочного состояния
            self.assertLessEqual(len(text.split()), 7)

    def test_seed_appears_in_output(self):
        for _ in range(20):
            text = self.model.generate(seed="вечером", min_words=1)
            self.assertIsNotNone(text)
            self.assertIn("вечером", text)

    def test_seed_is_case_insensitive(self):
        self.assertIsNotNone(self.model.generate(seed="ВЕЧЕРОМ", min_words=1))

    def test_unknown_seed_returns_none(self):
        self.assertIsNone(self.model.generate(seed="абракадабра"))

    def test_order_one_still_works(self):
        model = build_model(CORPUS, order=1)
        self.assertTrue(model.is_ready)
        self.assertTrue(model.generate(min_words=1))

    def test_order_must_be_positive(self):
        with self.assertRaises(ValueError):
            MarkovModel(order=0)

    def test_dialog_returns_requested_lines(self):
        lines = self.model.generate_dialog(lines=4, min_words=1)
        self.assertEqual(len(lines), 4)

    def test_recombines_rather_than_quoting(self):
        """Смысл цепи — склеивать куски разных сообщений, а не цитировать."""
        seen = {self.model.generate(min_words=1) for _ in range(400)}
        novel = seen - set(CORPUS)
        self.assertTrue(novel, "модель выдаёт только дословные исходники")


class TestFilters(unittest.TestCase):
    def test_removes_links(self):
        cleaned = filters.clean_for_learning("смотри https://example.com/x круто")
        self.assertEqual(cleaned, "смотри круто")

    def test_keeps_links_when_disabled(self):
        cleaned = filters.clean_for_learning("смотри https://example.com круто",
                                             remove_links=False)
        self.assertIn("https://example.com", cleaned)

    def test_removes_mentions(self):
        self.assertEqual(filters.clean_for_learning("<@123> привет <#456>"), "привет")

    def test_removes_role_mentions(self):
        self.assertEqual(filters.clean_for_learning("<@&999> сбор"), "сбор")

    def test_everyone_never_learned(self):
        cleaned = filters.clean_for_learning("@everyone подъём", remove_mentions=False)
        self.assertNotIn("@everyone", cleaned)

    def test_invites_always_stripped(self):
        cleaned = filters.clean_for_learning(
            "залетай discord.gg/abcdef", remove_links=False
        )
        self.assertNotIn("discord.gg", cleaned)

    def test_custom_emoji_optional(self):
        text = "лол <:kek:123456789>"
        self.assertIn("<:kek:", filters.clean_for_learning(text))
        self.assertNotIn("<:kek:", filters.clean_for_learning(text, remove_emoji=True))

    def test_sanitize_breaks_everyone_ping(self):
        out = filters.sanitize_output("@everyone подъём")
        self.assertNotIn("@everyone", out)
        self.assertIn("подъём", out)

    def test_sanitize_truncates_to_discord_limit(self):
        out = filters.sanitize_output("а" * 5000)
        self.assertLessEqual(len(out), filters.DISCORD_MESSAGE_LIMIT)

    def test_looks_like_command(self):
        self.assertTrue(filters.looks_like_command("g.generate", ("g.", "/")))
        self.assertTrue(filters.looks_like_command("G.CONFIG", ("g.", "/")))
        self.assertFalse(filters.looks_like_command("просто текст", ("g.", "/")))


class TestStorage(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.storage = Storage(self.tmp.name)
        await self.storage.setup()

    async def asyncTearDown(self):
        await self.storage.close()
        os.unlink(self.tmp.name)

    async def test_default_settings_are_privacy_safe(self):
        settings = await self.storage.get_settings(1)
        self.assertFalse(settings.reading_enabled)
        self.assertFalse(settings.autogen_enabled)
        self.assertTrue(settings.remove_mentions)
        self.assertTrue(settings.remove_links)

    async def test_update_and_read_back(self):
        await self.storage.update_settings(1, reading_enabled=True, prefix="!")
        settings = await self.storage.get_settings(1)
        self.assertTrue(settings.reading_enabled)
        self.assertEqual(settings.prefix, "!")

    async def test_unknown_setting_is_rejected(self):
        with self.assertRaises(ValueError):
            await self.storage.update_settings(1, guild_id_or_something=5)

    async def test_settings_are_isolated_per_guild(self):
        await self.storage.update_settings(1, reading_enabled=True)
        other = await self.storage.get_settings(2)
        self.assertFalse(other.reading_enabled)

    async def test_messages_roundtrip(self):
        for text in CORPUS:
            await self.storage.add_message(1, 10, 100, text)
        self.assertEqual(await self.storage.count_messages(1), len(CORPUS))
        self.assertEqual(sorted(await self.storage.corpus(1)), sorted(CORPUS))

    async def test_wipe_by_pattern(self):
        for text in CORPUS:
            await self.storage.add_message(1, 10, 100, text)
        removed = await self.storage.wipe(1, pattern="магазин")
        self.assertEqual(removed, 2)
        self.assertEqual(await self.storage.count_messages(1), len(CORPUS) - 2)

    async def test_wipe_by_author(self):
        await self.storage.add_message(1, 10, 100, "моё сообщение")
        await self.storage.add_message(1, 10, 200, "чужое сообщение")
        removed = await self.storage.wipe(1, author_id=100)
        self.assertEqual(removed, 1)
        self.assertEqual(await self.storage.count_messages(1), 1)

    async def test_wipe_does_not_touch_other_guilds(self):
        await self.storage.add_message(1, 10, 100, "текст")
        await self.storage.add_message(2, 10, 100, "текст")
        await self.storage.wipe(1)
        self.assertEqual(await self.storage.count_messages(1), 0)
        self.assertEqual(await self.storage.count_messages(2), 1)

    async def test_ignored_channels(self):
        self.assertEqual(await self.storage.ignored_channels(1), set())
        await self.storage.set_channel_ignored(1, 55, True)
        self.assertIn(55, await self.storage.ignored_channels(1))
        await self.storage.set_channel_ignored(1, 55, False)
        self.assertNotIn(55, await self.storage.ignored_channels(1))

    async def test_opt_out(self):
        await self.storage.load_opted_out()
        self.assertFalse(self.storage.is_opted_out(777))
        await self.storage.set_opt_out(777, True)
        self.assertTrue(self.storage.is_opted_out(777))
        await self.storage.set_opt_out(777, False)
        self.assertFalse(self.storage.is_opted_out(777))

    async def test_opt_out_survives_restart(self):
        await self.storage.set_opt_out(888, True)
        fresh = Storage(self.tmp.name)
        await fresh.setup()
        await fresh.load_opted_out()
        self.assertTrue(fresh.is_opted_out(888))
        await fresh.close()

    async def test_user_messages_export(self):
        await self.storage.add_message(1, 10, 100, "первое")
        await self.storage.add_message(1, 10, 200, "чужое")
        lines = await self.storage.user_messages(1, 100)
        self.assertEqual(len(lines), 1)
        self.assertIn("первое", lines[0])

    async def test_concurrent_writes(self):
        """Лок хранилища должен выдерживать параллельные вставки."""
        await asyncio.gather(
            *(self.storage.add_message(1, 10, 100, f"строка {i}") for i in range(50))
        )
        self.assertEqual(await self.storage.count_messages(1), 50)


class TestImportDedup(unittest.IsolatedAsyncioTestCase):
    """Повторный импорт истории не должен задваивать корпус."""

    async def asyncSetUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.storage = Storage(self.tmp.name)
        await self.storage.setup()

    async def asyncTearDown(self):
        await self.storage.close()
        os.unlink(self.tmp.name)

    def rows(self, start=1, count=3):
        return [
            (1, 10, 100, f"сообщение номер {i}", "2026-01-01T00:00:00+00:00", i)
            for i in range(start, start + count)
        ]

    async def test_bulk_insert_returns_count(self):
        added = await self.storage.add_messages_bulk(self.rows(count=5))
        self.assertEqual(added, 5)
        self.assertEqual(await self.storage.count_messages(1), 5)

    async def test_bulk_insert_of_empty_list(self):
        self.assertEqual(await self.storage.add_messages_bulk([]), 0)

    async def test_reimport_adds_nothing(self):
        await self.storage.add_messages_bulk(self.rows(count=5))
        again = await self.storage.add_messages_bulk(self.rows(count=5))
        self.assertEqual(again, 0)
        self.assertEqual(await self.storage.count_messages(1), 5)

    async def test_reimport_picks_up_only_new(self):
        await self.storage.add_messages_bulk(self.rows(start=1, count=5))
        # было 1-5, пришли 3-7: три дубля отброшены, добавились только 6 и 7
        added = await self.storage.add_messages_bulk(self.rows(start=3, count=5))
        self.assertEqual(added, 2)
        self.assertEqual(await self.storage.count_messages(1), 7)

    async def test_live_message_with_id_is_deduped(self):
        await self.storage.add_message(1, 10, 100, "привет всем", message_id=999)
        await self.storage.add_message(1, 10, 100, "привет всем", message_id=999)
        self.assertEqual(await self.storage.count_messages(1), 1)

    async def test_messages_without_id_are_not_deduped(self):
        """У старых записей message_id пустой — они не должны схлопываться."""
        await self.storage.add_message(1, 10, 100, "одно и то же")
        await self.storage.add_message(1, 10, 100, "одно и то же")
        self.assertEqual(await self.storage.count_messages(1), 2)

    async def test_migration_from_schema_without_message_id(self):
        """Старая база должна доезжать до новой схемы без потери данных."""
        legacy = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        legacy.close()
        conn = sqlite3.connect(legacy.name)
        conn.executescript(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO messages (guild_id, channel_id, author_id, content, created_at)"
            " VALUES (1, 10, 100, 'старое сообщение', '2025-01-01T00:00:00')"
        )
        conn.commit()
        conn.close()

        storage = Storage(legacy.name)
        await storage.setup()
        try:
            self.assertEqual(await storage.count_messages(1), 1)
            self.assertIn("старое сообщение", await storage.corpus(1))
            # новая схема работает: id проставляется и дедуп включён
            await storage.add_message(1, 10, 100, "новое сообщение", message_id=1)
            await storage.add_message(1, 10, 100, "новое сообщение", message_id=1)
            self.assertEqual(await storage.count_messages(1), 2)
        finally:
            await storage.close()
            os.unlink(legacy.name)


class TestEndToEnd(unittest.IsolatedAsyncioTestCase):
    """Путь сообщения целиком: фильтры -> база -> модель -> отправка."""

    async def test_pipeline(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        storage = Storage(tmp.name)
        await storage.setup()
        try:
            raw = [
                "<@42> пойдём вечером играть https://example.com",
                "@everyone вечером играть в доту",
                "вечером в доту это святое",
            ]
            for text in raw:
                cleaned = filters.clean_for_learning(text)
                await storage.add_message(1, 10, 100, cleaned)

            corpus = await storage.corpus(1)
            self.assertTrue(all("@everyone" not in c for c in corpus))
            self.assertTrue(all("http" not in c for c in corpus))

            model = build_model(corpus, order=2)
            out = filters.sanitize_output(model.generate(min_words=1) or "")
            self.assertTrue(out)
            self.assertNotIn("@everyone", out)
            self.assertLessEqual(len(out), filters.DISCORD_MESSAGE_LIMIT)
        finally:
            await storage.close()
            os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
