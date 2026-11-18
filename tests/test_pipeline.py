from flip_scout.ai import AIRetryLater, AIUnavailable

from .conftest import NOW, FakeAI, ai_answer, make_item


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
    ):
        assert part in msg
    assert "💡" not in msg  # no price assessment
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


def test_long_pause_only_remembers_again(env, store, cfg):
    cfg.profiles["standard"].briefing = False
    pipe, vinted, _, notifier = env()
    store.add_search("S", "standard", ["s"])
    pipe.run_once()  # first run: remember
    vinted.results["s"] = [make_item(1), make_item(2)]  # uploaded during the night
    store.set("last_search_ts", str(float(store.get("last_search_ts")) - 8 * 3600))
    assert pipe.run_once().hits == 0  # after 8 h pause: only remembered
    vinted.results["s"].insert(0, make_item(3))
    assert pipe.run_once().hits == 1  # really new -> reported


def test_all_paused_reseeds_only_after_resume(env, store, cfg):
    cfg.profiles["standard"].briefing = False
    pipe, vinted, _, _ = env()
    sid = store.add_search("S", "standard", ["s"])
    pipe.run_once()  # first run: remember
    store.update_search(sid, active=False)
    store.set("last_search_ts", str(float(store.get("last_search_ts")) - 8 * 3600))
    assert pipe.reseed_after_pause() is False  # all paused: nothing to do, no log spam
    vinted.results["s"] = [make_item(1), make_item(2)]  # uploaded while paused
    store.update_search(sid, active=True)
    assert pipe.run_once().hits == 0  # after resume: only remembered
    vinted.results["s"].insert(0, make_item(3))
    assert pipe.run_once().hits == 1  # really new -> reported


def test_pause_during_run_stops_remaining_hits(env, store, cfg):
    cfg.profiles["standard"].briefing = False
    pipe, vinted, _, notifier = env()
    sid = store.add_search("Switch", "standard", ["switch oled"])
    pipe.run_once()
    vinted.results["switch oled"] = [make_item(n) for n in range(1, 6)]
    original = notifier.send_hit

    def send_and_pause(*args, **kwargs):
        ok = original(*args, **kwargs)
        store.update_search(sid, active=False)  # "⏸ Suche pausieren" pressed
        return ok

    notifier.send_hit = send_and_pause
    assert pipe.run_once().hits == 1  # the other 4 are not sent anymore
    store.update_search(sid, active=True)
    assert pipe.run_once().hits == 0  # and not later either


def test_price_table_sets_vinted_price_limit(env, store):
    pipe, vinted, _, _ = env(FakeAI(ai_answer()))
    store.add_search("iPhone", "iphone", [""], params={"catalog_ids": "3661"})
    pipe.run_once()
    assert vinted.calls[0][3] == 180  # highest price in the table, sent to Vinted


def test_model_not_in_table_is_never_reported_without_search_limit(env, store):
    ai = FakeAI(ai_answer(modell_key="keins", modell="iPhone X"))
    pipe, vinted, _, notifier = env(ai)
    store.add_search("iPhone", "iphone", [""])
    pipe.run_once()
    vinted.results[""] = [make_item(1, price=50)]
    assert pipe.run_once().hits == 0 and not notifier.hits


def test_require_any_skips_before_ai(env, store, cfg):
    cfg.profiles["iphone"].require_any = ["switch"]
    ai = FakeAI(ai_answer())
    pipe, vinted, _, _ = env(ai)
    store.add_search("Konsolen", "iphone", [""])
    pipe.run_once()
    vinted.results[""] = [make_item(1, title="Nintendo DS"), make_item(2, title="Switch Lite")]
    pipe.run_once()
    assert [c[1] for c in ai.calls] == [2, 2]  # only the Switch reaches text + photo check


def test_require_any_also_checks_description(env, store, cfg):
    cfg.profiles["iphone"].require_any = ["3060"]
    ai = FakeAI(ai_answer())
    pipe, vinted, _, _ = env(ai)
    store.add_search("GPU", "iphone", [""])
    pipe.run_once()
    vinted.results[""] = [make_item(1, title="Grafikkarte Nvidia")]
    vinted.descriptions[1] = "Gigabyte RTX 3060 12 GB, läuft einwandfrei"
    pipe.run_once()
    assert ai.calls  # found in the description


def test_no_go_ignored_when_profile_has_none(env, store, cfg):
    cfg.profiles["iphone"].no_go = []
    ai = FakeAI(ai_answer(no_go=True, no_go_grund="Wasserschaden"))
    pipe, vinted, _, notifier = env(ai)
    store.add_search("iPhone", "iphone", [""])
    pipe.run_once()
    vinted.results[""] = [make_item(1, price=150)]
    assert pipe.run_once().hits == 1


def test_unclear_model_in_text_goes_to_photo_check(env, store):
    ai = FakeAI(
        ai_answer(passt="unklar", modell_key="keins"),  # text: type not mentioned
        ai_answer(passt="ja", modell_key="iPhone 12"),  # photos: label shows it
    )
    pipe, vinted, _, notifier = env(ai)
    store.add_search("S", "iphone", [""])
    pipe.run_once()
    vinted.results[""] = [make_item(1, price=80)]
    assert pipe.run_once().hits == 1
    assert [c[2] for c in ai.calls] == [0, 3]  # text check, then photo check with 3 photos


def test_unclear_model_after_photos_is_not_reported(env, store):
    ai = FakeAI(ai_answer(passt="unklar", modell_key="keins"))
    pipe, vinted, _, notifier = env(ai)
    store.add_search("S", "iphone", [""])
    pipe.run_once()
    vinted.results[""] = [make_item(1, price=80)]
    assert pipe.run_once().hits == 0 and len(ai.calls) == 2


def test_seller_without_ratings_only_up_to_limit(env, store, cfg):
    cfg.settings.no_feedback_max_price = 50
    ai = FakeAI(ai_answer(modell_key="iPhone 13 Pro"))
    pipe, vinted, _, notifier = env(ai)
    store.add_search("S", "iphone", [""])
    pipe.run_once()
    vinted.results[""] = [make_item(1, price=120), make_item(2, price=50), make_item(3, price=120)]
    vinted.sellers = {1: (0, 0.0), 2: (0, 0.0), 3: (1, 1.0)}
    assert pipe.run_once().hits == 2  # item 1 skipped: 0 ratings and over 50 €
    assert {c[1] for c in ai.calls} == {2, 3}  # skipped before any AI call
    assert "👤 Verkäufer: noch keine Bewertungen" in notifier.hits[0]
    assert "👤 Verkäufer: 1 Bewertung · ★ 5.0" in notifier.hits[1]


def test_unknown_seller_ratings_never_block(env, store, cfg):
    cfg.settings.no_feedback_max_price = 50
    pipe, vinted, _, notifier = env(FakeAI(ai_answer()))
    store.add_search("S", "iphone", [""])
    pipe.run_once()
    vinted.results[""] = [make_item(1, price=120)]
    assert pipe.run_once().hits == 1
    assert "Bewertungen unbekannt" in notifier.hits[0]


def test_title_check_skips_before_page_load_and_ai(env, store, cfg):
    cfg.profiles["iphone"].title_models = True
    cfg.profiles["iphone"].title_skip_models = ["iPhone 8"]
    ai = FakeAI(ai_answer(modell_key="iPhone 12"))
    pipe, vinted, _, notifier = env(ai)
    store.add_search("S", "iphone", [""])
    pipe.run_once()
    vinted.results[""] = [
        make_item(1, title="iPhone 13 Pro", price=170),  # 13 Pro: 180 € -> price fits, AI checks
        make_item(2, title="iPhone 12", price=120),  # 12: 90 € -> out by title
        make_item(3, title="iPhone 8", price=40),  # not wanted -> out by title
        make_item(4, title="Handy Apple", price=80),  # unclear -> AI decides
    ]
    vinted.load_details = lambda item, f=vinted.load_details: (
        vinted.calls.append(("page", item.id)),
        f(item),
    )
    pipe.run_once()
    checked_by_ai = {c[1] for c in ai.calls}
    assert checked_by_ai == {1, 4}
    assert ("page", 2) not in vinted.calls and ("page", 3) not in vinted.calls


def test_searches_run_on_their_own_interval(env, store, cfg):
    cfg.settings.interval_minutes = 3
    cfg.profiles["iphone"].interval_minutes = 1
    pipe, vinted, _, _ = env(FakeAI(ai_answer()))
    clock = [NOW]
    pipe.now = lambda: clock[0]
    store.add_search("iPhone", "iphone", ["iphone"])
    store.add_search("Switch", "standard", ["switch"])
    searched = []
    for minute in range(7):
        clock[0] = NOW + minute * 60
        vinted.calls.clear()
        pipe.run_once(only_due=True)
        searched.append(sorted(c[0] for c in vinted.calls))
    assert [("switch" in s) for s in searched] == [True, False, False, True, False, False, True]
    assert all("iphone" in s for s in searched)  # iPhone every minute


def test_cpu_check_decided_by_rules_not_ai(store):
    from flip_scout.config import Profile
    from flip_scout.rules import decide
    from flip_scout.store import Search

    p = Profile(key="laptop", cpu_check="laptop", max_prices={"Laptop": 100})
    s = Search(1, "Laptop")
    ok = ai_answer(modell_key="Laptop", cpu="Intel Core i5-8250U", cpu_kerne=4)
    assert decide(p, s, 90, ok, final=True).notify
    assert not decide(p, s, 120, ok, final=True).notify  # over 100 €
    weak = ai_answer(modell_key="Laptop", cpu="MediaTek MT8183", cpu_kerne=8)
    assert "zu schwach" in decide(p, s, 50, weak, final=True).reason
    unknown = ai_answer(passt="unklar", modell_key="keins", cpu="", cpu_kerne=0)
    assert decide(p, s, 50, unknown, final=False).notify  # text: unclear -> photo check
    assert not decide(p, s, 50, unknown, final=True).notify  # still unknown after photos
