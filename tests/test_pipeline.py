from flip_scout.ai import AIRetryLater, AIUnavailable

from .conftest import FakeAI, ai_answer, make_item


def seed_and_add(store, vinted, pipe, search_id, term, item):
    """First run only remembers; then a new listing appears."""
    pipe.run_once()
    vinted.results[term] = [item] + vinted.results.get(term, [])


def test_first_run_only_remembers(env, store):
    pipe, vinted, _, notifier = env()
    store.add_search("Switch", "standard", ["switch oled"], max_price=180)
    vinted.results["switch oled"] = [make_item(1), make_item(2)]
    result = pipe.run_once()
    assert result.new_items == 0 and notifier.hits == []
    # second run: same listings -> still nothing
    assert pipe.run_once().new_items == 0


def test_new_listing_after_seeding_is_reported(env, store, cfg):
    cfg.profiles["standard"].briefing = False
    pipe, vinted, _, notifier = env()
    sid = store.add_search("Switch", "standard", ["switch oled"], max_price=180)
    seed_and_add(store, vinted, pipe, sid, "switch oled", make_item(10, "Switch OLED weiß"))
    result = pipe.run_once()
    assert result.hits == 1
    assert "Switch OLED weiß" in notifier.hits[0]
    assert "vinted.at/items/10" in notifier.hits[0]
    assert vinted.calls[-1][3] == 180  # price_to passed to Vinted
    assert pipe.run_once().hits == 0  # not reported twice


def test_seen_is_per_search(env, store, cfg):
    cfg.profiles["standard"].briefing = False
    pipe, vinted, _, notifier = env()
    store.add_search("A", "standard", ["x"])
    store.add_search("B", "standard", ["x"])
    pipe.run_once()
    vinted.results["x"] = [make_item(5)]
    assert pipe.run_once().hits == 2  # both searches check the listing


def test_new_term_is_seeded_separately(env, store, cfg):
    cfg.profiles["standard"].briefing = False
    pipe, vinted, _, notifier = env()
    sid = store.add_search("A", "standard", ["alt"])
    pipe.run_once()
    vinted.results["neu"] = [make_item(7)]
    store.add_term(sid, "neu")
    assert pipe.run_once().hits == 0  # new term: old listings only remembered
    vinted.results["neu"].insert(0, make_item(8))
    assert pipe.run_once().hits == 1


def test_free_filters_cost_nothing(env, store):
    ai = FakeAI(ai_answer())
    pipe, vinted, _, notifier = env(ai)
    store.add_search("iPhone", "iphone", ["iphone defekt"], max_price=300)
    pipe.run_once()
    vinted.results["iphone defekt"] = [
        make_item(1, "iPhone 13 Pro", age_min=90),  # too old
        make_item(2, "iPhone 13 Hülle", price=10),  # exclude word in title
        make_item(3, "iPhone 13 Pro", price=500),  # above max price
    ]
    promoted = make_item(4, "iPhone 13 Pro", price=100)
    promoted.promoted = True  # bumped old listing
    vinted.results["iphone defekt"].append(promoted)
    pipe.run_once()
    assert ai.calls == [] and notifier.hits == [] and vinted.downloads == []


def test_exclude_desc_checks_description_but_exclude_only_title(env, store, cfg):
    cfg.profiles["standard"].briefing = False
    pipe, vinted, _, notifier = env()
    store.add_search("S", "standard", ["switch"])
    pipe.run_once()
    vinted.results["switch"] = [make_item(1, "Switch OLED"), make_item(2, "Switch OLED")]
    vinted.descriptions = {1: "Ich suche eine Switch", 2: "Top Zustand, inkl. Hülle"}
    pipe.run_once()
    assert len(notifier.hits) == 1 and "items/2" in notifier.hits[0]


def test_ai_flow_text_then_photos_and_message(env, store):
    ai = FakeAI(ai_answer())
    pipe, vinted, _, notifier = env(ai)
    store.add_search("iPhone defekt", "iphone", ["iphone defekt"], max_price=300)
    pipe.run_once()
    vinted.results["iphone defekt"] = [make_item(1, "iPhone 13 Pro defekt", price=120)]
    vinted.descriptions = {1: "Display gesprungen, Face ID geht"}
    assert pipe.run_once().hits == 1
    assert [c[2] for c in ai.calls] == [0, 3]  # text check, then 3 photos
    msg = notifier.hits[0]
    for part in (
        "📱 iPhone defekt",
        "💶 120.00 €",
        "⚠️ Defekt: Display gesprungen",
        "❓ iCloud: unklar – Verkäufer fragen!",
        "✅ Face ID geht laut Text",
        "❓ Wasserschaden nicht erwähnt",
        "💡 Fairer Preis",
    ):
        assert part in msg
    assert notifier.photos[0] is not None  # photo uploaded with the message
    assert len(vinted.downloads) == 3  # photos downloaded once, reused for Telegram


def test_ai_rejects_by_model_price(env, store):
    ai = FakeAI(ai_answer(modell_key="iPhone 12"))
    pipe, vinted, _, notifier = env(ai)
    store.add_search("iPhone", "iphone", ["i"], max_price=300)
    pipe.run_once()
    vinted.results["i"] = [make_item(1, "iPhone 12 defekt", price=120)]
    pipe.run_once()
    assert notifier.hits == [] and len(ai.calls) == 1  # stopped after the text check


def test_icloud_locked_is_dropped_after_photo_check(env, store):
    ai = FakeAI(ai_answer(), ai_answer(icloud="gesperrt"))
    pipe, vinted, _, notifier = env(ai)
    store.add_search("iPhone", "iphone", ["i"], max_price=300)
    pipe.run_once()
    vinted.results["i"] = [make_item(1, "iPhone 13 Pro", price=120)]
    pipe.run_once()
    assert notifier.hits == [] and len(ai.calls) == 2


def test_keyword_hit_reports_even_if_ai_would_say_no(env, store):
    ai = FakeAI(ai_answer(passt="nein"))
    pipe, vinted, _, notifier = env(ai)
    store.add_search("K", "kw", ["switch"])
    pipe.run_once()
    vinted.results["switch"] = [make_item(1, "Switch OLED")]
    assert pipe.run_once().hits == 1
    assert ai.calls == [("kw", 1, 3)]  # only the photo briefing


def test_plain_search_drops_when_briefing_says_no(env, store):
    ai = FakeAI(ai_answer(passt="nein"))
    pipe, vinted, _, notifier = env(ai)
    store.add_search("S", "standard", ["switch"])
    pipe.run_once()
    vinted.results["switch"] = [make_item(1, "Switch OLED Karton")]
    assert pipe.run_once().hits == 0


def test_daily_ai_limit(env, store, cfg):
    cfg.ai.max_checks_per_day = 0
    ai = FakeAI(ai_answer())
    pipe, vinted, _, notifier = env(ai)
    store.add_search("iPhone", "iphone", ["i"], max_price=300)
    store.add_search("S", "standard", ["s"])
    pipe.run_once()
    vinted.results["i"] = [make_item(1, "iPhone 13 Pro")]
    vinted.results["s"] = [make_item(2, "Switch")]
    pipe.run_once()
    assert ai.calls == []
    assert len(notifier.hits) == 1 and "Ohne KI-Briefing" in notifier.hits[0]


def test_notify_failure_retries_next_run(env, store, cfg):
    cfg.profiles["standard"].briefing = False
    pipe, vinted, _, notifier = env()
    store.add_search("S", "standard", ["s"])
    pipe.run_once()
    vinted.results["s"] = [make_item(1)]
    notifier.ok = False
    assert pipe.run_once().hits == 0
    notifier.ok = True
    assert pipe.run_once().hits == 1


def test_temporary_ai_error_retries_next_run(env, store):
    ai = FakeAI(ai_answer())
    pipe, vinted, _, notifier = env(ai)
    store.add_search("iPhone", "iphone", ["i"], max_price=300)
    pipe.run_once()
    vinted.results["i"] = [make_item(1, "iPhone 13 Pro", price=100)]
    ai.raise_exc = AIRetryLater("overloaded")
    assert pipe.run_once().hits == 0
    ai.raise_exc = None
    assert pipe.run_once().hits == 1


def test_permanent_ai_error_skips_item(env, store):
    ai = FakeAI(ai_answer())
    ai.raise_exc = AIUnavailable("refusal")
    pipe, vinted, _, notifier = env(ai)
    store.add_search("iPhone", "iphone", ["i"], max_price=300)
    pipe.run_once()
    vinted.results["i"] = [make_item(1, "iPhone 13 Pro")]
    pipe.run_once()
    ai.raise_exc = None
    assert pipe.run_once().hits == 0  # not retried


def test_blocked_run_is_reported_as_failed(env, store):
    pipe, vinted, _, _ = env()
    store.add_search("S", "standard", ["s"])
    vinted.fail = True
    assert pipe.run_once().failed
