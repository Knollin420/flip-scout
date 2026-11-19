import json

from flip_scout.config import Profile
from flip_scout.rules import original_limit
from flip_scout.store import Search, Store
from flip_scout.willhaben import (
    apply_item_page,
    message_payload,
    message_text,
    parse_search_page,
)

from .conftest import NOW, FakeAI, ai_answer, make_item

PLAIN = "Hallo {name},\nich hätte großes Interesse.\nvg,\ncolin"
OFFER = "Hallo {name},\nWäre es für dich noch in Ordnung auf {limit} herunterzugehen?\nvg,\ncolin"


def page(data: dict) -> str:
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'


def attrs(**values) -> dict:
    return {"attribute": [{"name": k, "values": [str(v)]} for k, v in values.items()]}


def test_parse_search_page():
    ad = {
        "id": "1813496832",
        "attributes": attrs(
            ADID=1813496832,
            HEADING="NVIDIA GTX 1080",
            PRICE=120,
            SEO_URL="kaufen/d/gtx-1813496832",
            ALL_IMAGE_URLS="2/181/a.jpg;2/181/b.jpg",
            PUBLISHED=1795092489838,
            ISPRIVATE=1,
            LOCATION="Wien, 14. Bezirk, Penzing",
            BODY_DYN="Verkaufe<br/>super Zustand",
        ),
    }
    data = {
        "props": {"pageProps": {"searchResult": {"advertSummaryList": {"advertSummary": [ad]}}}}
    }
    (item,) = parse_search_page(page(data))
    assert (item.id, item.title, item.price, item.source) == (
        1813496832,
        "NVIDIA GTX 1080",
        120,
        "willhaben",
    )
    assert item.url == "https://www.willhaben.at/iad/kaufen/d/gtx-1813496832"
    assert item.photo_urls == [
        "https://cache.willhaben.at/mmo/2/181/a.jpg",
        "https://cache.willhaben.at/mmo/2/181/b.jpg",
    ]
    assert item.private is True and item.location.startswith("Wien, 14.")
    assert item.created_ts == 1795092489.838 and item.description == "Verkaufe\nsuper Zustand"


def test_item_page_gives_name_full_text_and_photos():
    item = make_item(1)
    details = {
        "attributes": attrs(
            DESCRIPTION="Verkaufe meine gtx 1080<br/><br/>super Zustand", ISPRIVATE=1
        ),
        "advertImageList": {
            "advertImage": [{"referenceImageUrl": "https://cache.willhaben.at/mmo/x.jpg"}]
        },
        "sellerProfileUserData": {"name": "Alexander"},
    }
    apply_item_page(item, page({"props": {"pageProps": {"advertDetails": details}}}))
    assert item.seller_name == "Alexander" and item.private is True
    assert item.description == "Verkaufe meine gtx 1080\n\nsuper Zustand"
    assert item.photo_urls == ["https://cache.willhaben.at/mmo/x.jpg"]


def test_message_text_with_and_without_offer():
    text, offer = message_text(PLAIN, OFFER, "Alexander", 95, 100)
    assert not offer and text.startswith("Hallo Alexander,\nich hätte")
    text, offer = message_text(PLAIN, OFFER, "Alexander", 118, 100)
    assert offer and "auf 100 € herunterzugehen" in text
    text, _ = message_text(PLAIN, OFFER, "", 50, None)
    assert text.startswith("Hallo,\n")  # no name -> no gap


def test_payload_is_what_the_website_sends():
    p = message_payload(1813496832, "Hallo", "a@b.at", "Colin")
    assert p["adId"] == 1813496832 and p["mailContent"] == "Hallo" and p["from"] == "a@b.at"
    assert p["copyToSender"] is False and p["telephone"] == ""


def test_original_limit():
    p = Profile(key="x", max_prices={"iPhone 13": 80, "iPhone 14": 129}, title_models=True)
    assert original_limit(p, Search(1, "S"), {"modell_key": "iPhone 14"}, "") == 129
    assert original_limit(p, Search(1, "S"), {"modell_key": "keins"}, "iPhone 13 blau") == 80
    assert original_limit(p, Search(1, "S", max_price=90), None, "") == 90
    assert (
        original_limit(
            Profile(key="c", cpu_check="laptop", max_prices={"Laptop": 100}),
            Search(1, "S"),
            None,
            "",
        )
        == 100
    )


# --- pipeline -----------------------------------------------------------------


class FakeMessenger:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    def send(self, ad_id, url, text):
        self.sent.append((ad_id, text))
        return "/webapi/iad-messaging/sendrequest"


def wh_env(env, store, cfg):
    s = cfg.settings
    s.willhaben_price_factor, s.willhaben_auto_message = 1.2, True
    s.willhaben_message, s.willhaben_message_offer = PLAIN, OFFER
    s.willhaben_max_messages_per_day = 2
    ai = FakeAI(ai_answer(modell_key="iPhone 12"))  # table: iPhone 12 = 90 €
    pipe, vinted, _, notifier = env(ai)
    pipe.willhaben, pipe.messenger = vinted, FakeMessenger()
    sid = store.add_search("iPhone", "iphone", [""], source="willhaben")
    return pipe, vinted, notifier, sid, ai


def wh_item(n, price, private=True, name="Alexander"):
    item = make_item(n, price=price)
    item.source, item.private, item.seller_name = "willhaben", private, name
    return item


def test_willhaben_price_factor_and_offer_message(env, store, cfg):
    pipe, vinted, notifier, _, _ = wh_env(env, store, cfg)
    pipe.run_once()
    assert vinted.calls[0][3] == 216  # 180 € * 1.2 (Vinted-style limit, ignored by willhaben)
    vinted.results[""] = [wh_item(1, 80), wh_item(2, 105), wh_item(3, 110)]
    assert pipe.run_once().hits == 2  # 80 and 105 (<= 90 * 1.2 = 108), not 110
    (ad1, t1), (ad2, t2) = pipe.messenger.sent
    assert ad1 == 1 and "großes Interesse" in t1 and "herunterzugehen" not in t1
    assert ad2 == 2 and "auf 90 € herunterzugehen" in t2  # proposes the real limit
    assert "Über deinem Limit von 90 €" in notifier.hits[1]
    assert any("Nachricht an Alexander gesendet" in t for t in notifier.texts)


def test_no_message_for_dealers_twice_or_over_daily_limit(env, store, cfg):
    pipe, vinted, notifier, _, _ = wh_env(env, store, cfg)
    pipe.run_once()
    vinted.results[""] = [
        wh_item(1, 50, private=False),
        wh_item(2, 50),
        wh_item(3, 50),
        wh_item(4, 50),
    ]
    pipe.run_once()
    assert [a for a, _ in pipe.messenger.sent] == [2, 3]  # dealer skipped, daily limit 2
    assert any("Tageslimit" in t for t in notifier.texts)


def test_messages_can_be_switched_off(env, store, cfg):
    pipe, vinted, _, _, _ = wh_env(env, store, cfg)
    store.set("wh_auto_message", "0")
    pipe.run_once()
    vinted.results[""] = [wh_item(1, 50)]
    assert pipe.run_once().hits == 1 and pipe.messenger.sent == []


def test_willhaben_runs_every_three_minutes_even_for_fast_profiles(env, store, cfg):
    cfg.profiles["iphone"].interval_minutes = 1
    cfg.settings.willhaben_interval_minutes = 3
    pipe, vinted, _, sid, _ = wh_env(env, store, cfg)
    clock = [NOW]
    pipe.now = lambda: clock[0]
    runs = []
    for minute in range(4):
        clock[0] = NOW + minute * 60
        vinted.calls.clear()
        pipe.run_once(only_due=True)
        runs.append(bool(vinted.calls))
    assert runs == [True, False, False, True]


def test_store_migration_adds_source(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE searches (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,"
        " profile TEXT NOT NULL DEFAULT 'standard', params TEXT NOT NULL DEFAULT '{}',"
        " max_price REAL, min_price REAL, active INTEGER NOT NULL DEFAULT 1,"
        " created_at REAL NOT NULL);"
        "INSERT INTO searches (name, created_at) VALUES ('alt', 0);"
    )
    con.commit()
    con.close()
    store = Store(db)
    assert store.list_searches()[0].source == "vinted"


def test_fixed_price_words():
    from flip_scout.config import Settings
    from flip_scout.rules import fixed_price_word

    words = Settings().willhaben_fixed_price_words
    assert fixed_price_word("Preis ist FIX, bitte keine Anfragen", words) == "preis ist fix"
    assert fixed_price_word("Festpreis!", words) == "festpreis"
    assert fixed_price_word("Nicht verhandelbar.", words) == "nicht verhandelbar"
    assert fixed_price_word("Kein Festpreis, VB", words) is None  # negated
    assert fixed_price_word("Preis VB, gerne Angebote", words) is None


def test_over_limit_with_fixed_price_is_not_reported(env, store, cfg):
    pipe, vinted, notifier, _, _ = wh_env(env, store, cfg)
    pipe.run_once()
    over_fixed = wh_item(1, 105)
    over_fixed.description = "Super Zustand. Festpreis!"
    under_fixed = wh_item(2, 85)
    under_fixed.description = "Festpreis"  # under the real limit: still reported
    over_vb = wh_item(3, 105)
    over_vb.description = "Kein Festpreis, VB"
    vinted.results[""] = [over_fixed, under_fixed, over_vb]
    assert pipe.run_once().hits == 2
    assert [a for a, _ in pipe.messenger.sent] == [2, 3]


def test_search_word_filters_locally_and_shares_the_request():
    from flip_scout.willhaben import WillhabenClient

    ads = [
        {"id": str(n), "attributes": attrs(ADID=n, HEADING=h, PRICE=50, ISPRIVATE=1, BODY_DYN=b)}
        for n, h, b in [
            (1, "Handy schwarz 128 GB", "iPhone 13, Display ok"),
            (2, "Samsung S21", ""),
            (3, "IPHONE 12", ""),
        ]
    ]
    html = page(
        {"props": {"pageProps": {"searchResult": {"advertSummaryList": {"advertSummary": ads}}}}}
    )

    class Resp:
        status_code, text = 200, html

    class Session:
        calls = 0

        def get(self, url, timeout=None):
            Session.calls += 1
            return Resp()

    c = WillhabenClient(session=Session())
    params = {"path": "smartphones-handys-2722"}
    assert [i.id for i in c.search("iphone", params)] == [1, 3]  # brand missing, found by word
    assert [i.id for i in c.search("samsung", params)] == [2]
    assert [i.id for i in c.search("", params)] == [1, 2, 3]
    assert Session.calls == 1  # one request for all three


def test_twin_search_skips_items_the_other_already_checked(env, store, cfg):
    pipe, vinted, notifier, sid, ai = wh_env(env, store, cfg)
    twin = store.add_search("iPhone ohne Marke", "iphone", ["iphone"], source="willhaben")
    pipe.run_once()  # both searches remember the stock
    item = wh_item(7, 50)
    item.title = "iPhone 12"
    vinted.results[""] = [item]
    vinted.results["iphone"] = [item]
    assert pipe.run_once().hits == 1  # reported once, AI checked once
    assert [c[1] for c in ai.calls].count(7) == 2  # text + photo check, not 4
    assert store.is_seen(twin, 7) and store.is_seen(sid, 7)
