import time

from flip_scout.__main__ import acquire_instance_lock, import_seed_searches
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


def test_example_config_loads_and_seeds(example_config, store):
    assert {"standard", "iphone_defekt", "macbook_defekt"} <= set(example_config.profiles)
    assert example_config.profiles["iphone_defekt"].max_prices["iPhone 13 Pro"] > 0
    import_seed_searches(example_config, store)
    names = [s.name for s in store.list_searches()]
    assert names == ["iPhone defekt", "MacBook defekt", "Switch OLED"]
    # deleted seed searches are not re-imported on restart
    store.delete_search(store.list_searches()[0].id)
    import_seed_searches(example_config, store)
    assert len(store.list_searches()) == 2


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
