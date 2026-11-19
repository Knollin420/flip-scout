import time

from flip_scout.__main__ import acquire_instance_lock
from flip_scout.bot import TelegramBot, parse_price
from flip_scout.monitor import Monitor, status_text
from flip_scout.pipeline import RunResult

from .conftest import FakeNotifier

CHAT = "4711"


class FakeAPI:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def call(self, method, params=None, **kwargs):
        self.calls.append((method, params or {}))
        return {}

    def last(self, method):
        return [p for m, p in self.calls if m == method][-1]


def msg(text, chat=CHAT):
    return {"update_id": 1, "message": {"chat": {"id": int(chat)}, "text": text}}


def click(data, with_text=True):
    message = {"message_id": 5, "chat": {"id": int(CHAT)}}
    if with_text:
        message["text"] = "menu"
    return {"update_id": 2, "callback_query": {"id": "c", "data": data, "message": message}}


def test_add_search_by_link_and_buttons(cfg, store):
    api = FakeAPI()
    bot = TelegramBot(cfg, store, api, CHAT)
    bot.handle_update(
        msg(
            "Schau: https://www.vinted.at/catalog?search_text=switch+oled"
            "&catalog[]=3026&price_to=180"
        )
    )
    buttons = api.last("sendMessage")["reply_markup"]["inline_keyboard"]
    choose = next(b[0]["callback_data"] for b in buttons if "Normal" in b[0]["text"])
    bot.handle_update(click(choose))
    search = store.list_searches()[0]
    assert (search.name, search.profile, search.max_price) == ("Switch Oled", "standard", 180)
    assert search.terms == ["switch oled"] and search.params == {"catalog_ids": "3026"}

    # change price via button + text answer
    bot.handle_update(click(f"pr:{search.id}"))
    bot.handle_update(msg("30-160"))
    search = store.get_search(search.id)
    assert (search.min_price, search.max_price) == (30, 160)

    # add terms, pause from a hit message (photo message without text)
    bot.handle_update(click(f"t:{search.id}"))
    bot.handle_update(msg("switch lite, Switch V2"))
    assert store.terms(search.id) == ["switch oled", "switch lite", "switch v2"]
    bot.handle_update(click(f"hp:{search.id}", with_text=False))
    assert not store.get_search(search.id).active
    assert "pausiert" in api.last("answerCallbackQuery")["text"]


def test_foreign_chat_is_ignored(cfg, store):
    api = FakeAPI()
    TelegramBot(cfg, store, api, CHAT).handle_update(
        msg("https://www.vinted.at/catalog?search_text=x", chat="999")
    )
    assert api.calls == []


def test_parse_price():
    assert parse_price("160") == (None, 160)
    assert parse_price("30 - 160 €") == (30, 160)
    assert parse_price("0") == (None, None)
    assert parse_price("billig") is None


def test_repo_profiles_load(repo_config):
    p = repo_config.profiles
    assert {"standard", "iphone_defekt", "macbook_defekt", "laptop", "minipc"} <= set(p)
    assert p["iphone_defekt"].max_prices["iPhone 13 Pro"] > 0
    assert p["laptop"].cpu_check == "laptop" and p["minipc"].cpu_check == "minipc"
    assert repo_config.settings.interval_minutes >= 3
    assert repo_config.settings.willhaben_message and repo_config.settings.willhaben_message_offer


def test_monitor_alarm_recovery_and_heartbeat(cfg, store):
    notifier = FakeNotifier()
    now = [time.mktime((2026, 11, 12, 10, 0, 0, 0, 0, -1))]
    monitor = Monitor(cfg, store, notifier, now=lambda: now[0])
    failed = RunResult(requests_failed=2)
    for _ in range(3):
        monitor.after_run(failed)
    assert sum("blockiert" in t for t in notifier.texts) == 1
    monitor.after_run(RunResult(requests_ok=1))
    assert "funktioniert wieder" in notifier.texts[-1]

    monitor.started()
    monitor.started()  # within the hour -> no second message
    assert sum("gestartet" in t for t in notifier.texts) == 1

    monitor.maybe_heartbeat()
    monitor.maybe_heartbeat()
    assert sum("läuft" in t for t in notifier.texts) == 1
    assert "Status" in status_text(cfg, store)


def test_only_one_instance():
    first = acquire_instance_lock(47999)
    assert first is not None
    assert acquire_instance_lock(47999) is None
    first.close()
    second = acquire_instance_lock(47999)
    assert second is not None
    second.close()


def test_search_view_shows_categories_and_price_list(cfg, store):
    api = FakeAPI()
    bot = TelegramBot(cfg, store, api, CHAT)
    sid = store.add_search("iPhone", "iphone", [""], {"catalog_ids": "3661", "brand_ids": "54661"})
    bot.handle_update(click(f"s:{sid}"))
    text = api.last("editMessageText")["text"]
    assert "Filter: Kategorie: Mobiltelefone · Marke: Apple" in text
    assert "Preis: laut Preisliste, max. 180 €" in text
    assert "Suchwörter: keine (nur Kategorie/Filter)" in text

    bot.handle_update(click(f"pl:{sid}"))
    text = api.last("editMessageText")["text"]
    assert "• iPhone 13 Pro: 180 €" in text and "• iPhone 12: 90 €" in text

    bot.handle_update(click(f"pr:{sid}"))
    assert "Preisliste pro Modell" in api.last("sendMessage")["text"]
    bot.handle_update(msg("0"))  # 0 = keep using the price list
    assert store.get_search(sid).max_price is None


def test_unknown_filter_ids_are_shown_as_numbers():
    from flip_scout.bot import filter_text

    assert filter_text({"catalog_ids": "9999", "size_ids": "1,2"}) == (
        "Kategorie: 9999 · Größe: 2 gewählt"
    )


def test_fixed_rhythm_between_runs():
    from flip_scout.__main__ import seconds_until_next_run
    from flip_scout.config import Settings

    s = Settings(interval_minutes=3, jitter_seconds=20)
    no_jitter = lambda a, b: 0  # noqa: E731
    assert seconds_until_next_run(s, 40, no_jitter) == 140  # 180 s rhythm minus 40 s run
    assert seconds_until_next_run(s, 170, no_jitter) == 30  # always at least 30 s pause
    assert seconds_until_next_run(s, 10, no_jitter, tick_minutes=1) == 50  # 1-minute tick


def test_interval_below_three_minutes_is_refused(tmp_path):
    import pytest

    from flip_scout.config import load_config

    f = tmp_path / "p.toml"
    f.write_text("[settings]\ninterval_minutes = 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mindestens 3"):
        load_config(f)
    f.write_text("[settings]\ninterval_minutes = 3\n", encoding="utf-8")
    assert load_config(f).settings.interval_minutes == 3


def test_profile_interval_minimum_is_one_minute(tmp_path):
    import pytest

    from flip_scout.config import load_config

    f = tmp_path / "p.toml"
    f.write_text("[profile.x]\ninterval_minutes = 0.5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mindestens 1"):
        load_config(f)
    f.write_text(
        "[settings]\ninterval_minutes = 3\n[profile.x]\ninterval_minutes = 1\n", encoding="utf-8"
    )
    assert load_config(f).tick_minutes() == 1
