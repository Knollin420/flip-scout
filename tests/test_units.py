import json
from types import SimpleNamespace

import pytest

from flip_scout.ai import AIChecker, AIUnavailable, build_schema, build_system
from flip_scout.notify import CAPTION_LIMIT, format_hit
from flip_scout.rules import decide, find_word
from flip_scout.store import Search
from flip_scout.urlimport import is_catalog_link, parse_vinted_url
from flip_scout.vinted import VintedClient, description_from_page, parse_catalog_item

from .conftest import JPEG, ai_answer, make_item

# --- URL import -------------------------------------------------------------


def test_url_import_translates_browser_params():
    imp = parse_vinted_url(
        "https://www.vinted.at/catalog?search_text=switch%20oled&catalog[]=3026&catalog[]=3027"
        "&brand_ids[]=1&status_ids[]=6&status_ids[]=2&price_to=180&price_from=50"
        "&order=newest_first&time=1700000000"
    )
    assert imp.domain == "at" and imp.term == "switch oled"
    assert imp.params == {"catalog_ids": "3026,3027", "brand_ids": "1", "status_ids": "6,2"}
    assert (imp.price_from, imp.price_to) == (50, 180)


def test_url_import_category_path():
    imp = parse_vinted_url("https://www.vinted.at/catalog/2329-smartphones")
    assert imp.params == {"catalog_ids": "2329"} and imp.term == ""
    assert is_catalog_link("https://www.vinted.at/catalog/2329-smartphones")
    assert not is_catalog_link("https://www.vinted.at/items/123-iphone")


# --- rules ------------------------------------------------------------------


def test_find_word_is_case_insensitive_substring():
    assert find_word("Original Apple HANDYHÜLLE", ["hülle"]) == "hülle"
    assert find_word("iPhone 13", ["hülle"]) is None


@pytest.mark.parametrize(
    "answer, price, max_price, notify, reason",
    [
        (ai_answer(), 150, None, True, ""),
        (ai_answer(passt="nein"), 100, None, False, "passt nicht"),
        (ai_answer(no_go=True, no_go_grund="Wasserschaden"), 100, None, False, "Wasserschaden"),
        (ai_answer(defekte_ok=False), 100, None, False, "Defekte"),
        (ai_answer(icloud="gesperrt"), 100, None, False, "iCloud"),
        (ai_answer(modell_key="iPhone 13 Pro"), 200, None, False, "> 180"),
        (ai_answer(modell_key="keins"), 100, None, False, "Preistabelle"),
        (ai_answer(modell_key="keins"), 100, 250, True, ""),
    ],
)
def test_decide(profiles, answer, price, max_price, notify, reason):
    search = Search(id=1, name="x", max_price=max_price)
    d = decide(profiles["iphone"], search, price, answer, final=True)
    assert d.notify is notify
    assert reason in d.reason or notify


def test_decide_icloud_warning_and_unclear(profiles):
    d = decide(profiles["iphone"], Search(1, "x"), 100, ai_answer(passt="unklar"), final=True)
    assert "❓ iCloud: unklar – Verkäufer fragen!" in d.warnings
    assert any("unsicher" in w for w in d.warnings)
    d = decide(profiles["iphone"], Search(1, "x"), 100, ai_answer(icloud="frei"), final=False)
    assert d.notify and d.warnings == []


# --- AI request -------------------------------------------------------------


class FakeMessages:
    def __init__(self, stop_reason="end_turn"):
        self.kwargs = None
        self.stop_reason = stop_reason

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="text", text=json.dumps(ai_answer()))],
            usage=SimpleNamespace(input_tokens=1234, output_tokens=210),
        )


def test_ai_request_shape(profiles):
    messages = FakeMessages()
    checker = AIChecker("key", "claude-haiku-5-5", client=SimpleNamespace(messages=messages))
    item = make_item(1, "iPhone 13 Pro", description="Display kaputt")
    resp = checker.check(profiles["iphone"], item, [(JPEG, "image/jpeg")] * 2)
    kw = messages.kwargs
    assert kw["model"] == "claude-haiku-5-5"
    assert kw["thinking"] == {"type": "disabled"}
    assert "temperature" not in kw
    schema = kw["output_config"]["format"]["schema"]
    assert schema["properties"]["modell_key"]["enum"] == ["iPhone 13 Pro", "iPhone 12", "keins"]
    content = kw["messages"][0]["content"]
    assert sum(1 for c in content if c["type"] == "image") == 2
    assert "Display kaputt" in content[-1]["text"]
    assert resp.data["passt"] == "ja" and resp.input_tokens == 1234


def test_ai_refusal_is_unavailable(profiles):
    checker = AIChecker("k", "m", client=SimpleNamespace(messages=FakeMessages("refusal")))
    with pytest.raises(AIUnavailable):
        checker.check(profiles["iphone"], make_item(1))


def test_schema_and_prompt(profiles):
    schema = build_schema(profiles["standard"])
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert "icloud: immer 'egal'" in build_system(profiles["standard"])
    system = build_system(profiles["iphone"])
    assert "Wasserschaden" in system and "Erfinde nichts" in system


# --- message format ---------------------------------------------------------


def test_message_never_exceeds_caption_and_keeps_link(profiles):
    long = ai_answer(positiv=["x" * 300] * 3, offen=["y" * 300] * 3)
    item = make_item(99, "iPhone " * 20)
    text = format_hit(Search(1, "iPhone defekt"), profiles["iphone"], item, long, ["❓ test"])
    assert len(text) <= CAPTION_LIMIT
    assert text.endswith("https://www.vinted.at/items/99")


# --- Vinted parsing / cookie retry ------------------------------------------


def test_parse_catalog_item():
    raw = {
        "id": 42,
        "title": "iPhone 13",
        "price": {"amount": "120.0", "currency_code": "EUR"},
        "brand_title": "Apple",
        "size_title": "",
        "status": "Gut",
        "is_reserved": False,
        "url": "https://www.vinted.at/items/42-iphone",
        "photo": {
            "url": "https://img/f800.jpg",
            "full_size_url": "https://img/full.jpg",
            "high_resolution": {"timestamp": 1700000000},
        },
    }
    item = parse_catalog_item(raw, "https://www.vinted.at")
    assert (item.id, item.price, item.brand) == (42, 120.0, "Apple")
    assert item.photo_urls == ["https://img/full.jpg"] and item.created_ts == 1700000000


def test_description_from_page():
    ld = '<script type="application/ld+json">{"@type":"Product","description":"Akku 85 %"}</script>'
    assert description_from_page(ld) == "Akku 85 %"
    og = '<meta property="og:description" content="Display &amp; Akku ok">'
    assert description_from_page(og) == "Display & Akku ok"


class FakeResponse:
    def __init__(self, status, payload=None, text="", headers=None):
        self.status_code = status
        self.payload = payload or {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self.payload


class FakeCookies(dict):
    def clear(self):
        super().clear()


class FakeSession:
    def __init__(self, answers):
        self.answers = list(answers)
        self.cookies = FakeCookies()
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if url.endswith(".at/"):
            self.cookies["access_token_web"] = "tok"
            return FakeResponse(200, text='{"CSRF_TOKEN":"c-1"}', headers={"x-anon-id": "anon-1"})
        return self.answers.pop(0)


def test_search_uses_svc_catalogue_with_session_headers():
    session = FakeSession([FakeResponse(200, {"items": [{"id": 1, "path": "/items/1-x"}]})])
    client = VintedClient("https://www.vinted.at", session=session)
    items = client.search("iphone", {"catalog_ids": "1,2", "status_ids": ""}, price_to=100)
    url, kwargs = session.requests[-1]
    assert url == "https://api.vinted.at/svc-catalogue/items"
    assert kwargs["params"]["attribute_ids[catalog]"] == "1,2"
    assert "attribute_ids[status]" not in kwargs["params"]  # empty values are dropped
    assert kwargs["params"]["price_to"] == "100" and "price_from" not in kwargs["params"]
    headers = kwargs["headers"]
    assert headers["X-Anon-Id"] == "anon-1" and headers["X-Csrf-Token"] == "c-1"
    assert headers["Authorization"] == "Bearer tok" and headers["Locale"] == "de-AT"
    assert items[0].url == "https://www.vinted.at/items/1-x"


def test_vinted_refreshes_session_and_switches_auth_variant():
    ok = FakeResponse(200, {"items": [{"id": 1}]})
    session = FakeSession([FakeResponse(401), FakeResponse(403), ok])
    client = VintedClient("https://www.vinted.at", session=session)
    assert [i.id for i in client.search("x", {})] == [1]
    start_page_calls = [u for u, _ in session.requests if u.endswith(".at/")]
    assert len(start_page_calls) == 3  # initial + two refreshes
    assert "Authorization" not in session.requests[-1][1]["headers"]  # switched variant


def test_404_is_an_error_not_a_retry():
    from flip_scout.vinted import VintedError

    session = FakeSession([FakeResponse(404, text="not found")])
    client = VintedClient("https://www.vinted.at", session=session)
    with pytest.raises(VintedError, match="404"):
        client.search("x", {})


def test_item_box_fields():
    raw = {
        "id": 3,
        "title": "iPhone 13",
        "price": {"amount": "80", "currency_code": "EUR"},
        "item_box": {"first_line": "Apple", "second_line": "128 GB · Gut"},
        "promoted": True,
    }
    item = parse_catalog_item(raw, "https://www.vinted.at")
    assert (item.brand, item.size, item.condition) == ("Apple", "128 GB", "Gut")
    assert item.url == "https://www.vinted.at/items/3" and item.created_ts is None
    assert item.promoted


def test_details_from_item_page():
    page = (
        '<script type="application/ld+json">{"@type":"Product","description":"Akku 85 %",'
        '"image":["https://img/1.jpg","https://img/2.jpg"]}</script>'
    )
    session = FakeSession([FakeResponse(200, text=page)])
    client = VintedClient("https://www.vinted.at", session=session)
    item = make_item(7, photos=1)
    client.load_details(item)
    assert item.description == "Akku 85 %" and item.description_source == "page"
    assert item.photo_urls == ["https://img/1.jpg", "https://img/2.jpg"]


def test_url_import_new_attribute_names():
    imp = parse_vinted_url(
        "https://www.vinted.at/catalog?search_text=x"
        "&attribute_ids[catalog][]=2329&attribute_ids[status][]=6"
    )
    assert imp.params == {"catalog_ids": "2329", "status_ids": "6"}


def test_network_error_on_cookie_is_a_vinted_error():
    from flip_scout.vinted import VintedError

    class Broken(FakeSession):
        def get(self, url, **kwargs):
            raise ConnectionError("tunnel failed")

    client = VintedClient("https://www.vinted.at", session=Broken([]))
    with pytest.raises(VintedError):
        client.search("x", {})


def _flight_page(*photo_lists, ld_image=""):
    """Item page like vinted.at: JSON-LD with the main photo, photo lists escaped in
    the embedded Next.js data."""
    ld = (
        '<script type="application/ld+json">{"@type":"Product","image":"' + ld_image + '"}</script>'
        if ld_image
        else ""
    )
    pushes = "".join(
        "<script>self.__next_f.push([1,"
        + json.dumps(json.dumps({"item": {"photos": [{"url": u, "is_main": False} for u in urls]}}))
        + "])</script>"
        for urls in photo_lists
    )
    return ld + pushes


def test_all_photos_from_embedded_page_data():
    own = [f"https://images1.vinted.net/t/0{n}_own{n}/f800/a.webp?s=x" for n in range(5)]
    other = ["https://images1.vinted.net/t/09_other/f800/b.webp?s=y"] * 6
    page = _flight_page(other, own, ld_image=own[0])
    from flip_scout.vinted import photos_from_page

    assert photos_from_page(page) == own  # list of a similar item is never used


def test_embedded_photos_need_matching_main_photo():
    from flip_scout.vinted import photos_from_page

    other = [f"https://images1.vinted.net/t/0{n}_other/f800/b.webp" for n in range(4)]
    main = "https://images1.vinted.net/t/01_main/f800/a.webp"
    assert photos_from_page(_flight_page(other, ld_image=main)) == [main]
    assert photos_from_page(_flight_page(other)) == []  # no main photo -> trust nothing


def test_search_full_size_photo_is_kept_with_page_photos():
    # search: only the main photo, as signed 1200x1600 full_size_url
    main_full = "https://images1.vinted.net/tc/01_main/abc.webp?s=big"
    # page: all photos, but only as 800 px versions
    page_photos = [
        "https://images1.vinted.net/t/01_main/f800/abc.webp?s=small",
        "https://images1.vinted.net/t/02_second/f800/def.webp?s=x",
        "https://images1.vinted.net/t/03_third/f800/ghi.webp?s=y",
    ]
    page = _flight_page(page_photos, ld_image=page_photos[0])
    client = VintedClient(
        "https://www.vinted.at", session=FakeSession([FakeResponse(200, text=page)])
    )
    item = make_item(8, photos=0)
    item.photo_urls = [main_full]
    client.load_details(item)
    assert item.photo_urls == [main_full, *page_photos[1:]]


def test_seller_ratings_from_page_need_matching_login():
    from flip_scout.vinted import seller_from_page

    user = {"id": 5, "login": "anna", "feedback_count": 43, "feedback_reputation": 0.98}
    other = {"id": 6, "login": "bob", "feedback_count": 7, "feedback_reputation": 1}
    page = (
        "<script>self.__next_f.push([1," + json.dumps(json.dumps({"seller": user})) + "])</script>"
    )
    assert seller_from_page(page, "anna") == (43, 0.98)
    assert seller_from_page(page, "bob") is None
    page2 = "<script>" + json.dumps({"x": other}) + "</script>"
    assert seller_from_page(page2, "bob") == (7, 1.0)
    assert seller_from_page(page2, "") is None
    # vinted.at today: sidebar block with "name" instead of "login"
    sidebar = {"data": {"feedback_count": 0, "feedback_reputation": 0, "name": "teolo81"}}
    page3 = "<script>self.__next_f.push([1," + json.dumps(json.dumps(sidebar)) + "])</script>"
    assert seller_from_page(page3, "teolo81") == (0, 0.0)


def test_seller_login_from_search():
    raw = {"id": 1, "title": "x", "price": {"amount": "5"}, "user": {"id": 9, "login": "anna"}}
    assert parse_catalog_item(raw, "https://www.vinted.at").seller_login == "anna"


def test_title_model_detection():
    from flip_scout.config import Profile
    from flip_scout.rules import title_model, title_problem

    p = Profile(
        key="i",
        max_prices={
            "iPhone 13": 80,
            "iPhone 13 Pro": 100,
            "iPhone 13 Pro Max": 100,
            "iPhone 16": 350,
            "iPhone 16e": 350,
            "iPhone Air": 400,
        },
        title_models=True,
        title_skip_models=["iPhone 6", "iPhone X", "iPhone XR", "iPhone SE"],
    )
    assert title_model(p, "Apple iPhone13 Pro-Max 256GB") == "iPhone 13 Pro Max"
    assert title_model(p, "iPhone 13 pro") == "iPhone 13 Pro"
    assert title_model(p, "iPhone 13, weiß") == "iPhone 13"
    assert title_model(p, "iPhone 16 e 128giga") == "iPhone 16e"
    assert title_model(p, "iphone 6s 64gb") == "iPhone 6"  # 6s is not in the list
    assert title_model(p, "Iphone XR 64G") == "iPhone XR"  # not 'iPhone X'
    assert title_model(p, "iPhone 16") == "iPhone 16"  # not 'iPhone 6'
    assert title_model(p, "Smartphone Apple") is None  # unclear -> AI
    assert title_model(p, "Lot iPhone 13 + iPhone XR") is None  # two models -> AI
    assert title_problem(p, "iPhone 13, weiß", 300) == "Titel: iPhone 13: 300 € > 80 €"
    assert title_problem(p, "iPhone 13", 80) is None  # price fits -> normal checks
    assert title_problem(p, "Iphone XR 64G", 20) == "Titel: iPhone XR nicht in Preisliste"
    p.title_models = False
    assert title_problem(p, "iPhone 13", 300) is None


def test_min_seller_feedback_regardless_of_price():
    from flip_scout.rules import seller_problem

    item = make_item(1, price=20)
    item.seller_feedback = 0
    assert seller_problem(item, 50, 1)  # cheap, but 0 ratings -> out
    assert seller_problem(item, 50, 0) is None  # other searches: fine under 50 €
    item.seller_feedback = 1
    assert seller_problem(item, 50, 1) is None
    item.seller_feedback = None
    assert seller_problem(item, 50, 1) is None  # unknown never blocks


@pytest.mark.parametrize(
    "cpu,cores,mobile,ok",
    [
        ("Intel Core i5-8250U", 4, False, True),
        ("Intel Core i7-1165G7", 4, False, True),
        ("Intel Core i3-1125G4", 4, False, True),
        ("Intel Core i3-1115G4", 2, False, False),
        ("Intel Core i3-10110U", 0, False, False),  # i3 before 11th gen, even without cores
        ("Intel Core i5-7200U", 0, False, False),
        ("Intel Core m3-8100Y", 2, False, False),
        ("Intel Pentium Gold 4425Y", 2, False, False),
        ("Intel N4020", 0, False, False),
        ("MediaTek Kompanio 520", 8, False, False),
        ("Intel N100", 4, True, True),
        ("Intel Core i3-N305", 8, True, True),
        ("AMD Ryzen 5 3500U", 4, False, True),
        ("AMD Ryzen 3 3200U", 0, False, False),
        ("AMD Ryzen 5 5600G", 6, True, False),
        ("Intel Core i5-8500T", 6, True, False),  # desktop CPU in a mini PC
        ("Apple M1", 8, False, True),
        ("Intel Core i5 8th Gen", 0, False, True),
    ],
)
def test_cpu_rules(cpu, cores, mobile, ok):
    from flip_scout.cpu import cpu_problem

    assert (cpu_problem(cpu, cores, mobile) is None) == ok


def test_cpu_unknown():
    from flip_scout.cpu import UNKNOWN, cpu_problem

    assert cpu_problem("", 0) == UNKNOWN
    assert cpu_problem("Intel Core i5", 0) == UNKNOWN
    assert cpu_problem("Intel Core i5 8th Gen", 0, mobile_suffix_required=True) == UNKNOWN


def test_hit_as_album_is_one_message():
    from flip_scout.notify import Notifier

    class API:
        def __init__(self):
            self.calls = []

        def call(self, method, params=None, photo=None, timeout=None, files=None):
            self.calls.append((method, params, sorted(files) if files else None))

    api = API()
    n = Notifier(api, "1")
    photos = [
        (b"a", "image/jpeg"),
        (b"b", "image/jpeg"),
        (b"c", "image/webp"),
        (b"d", "image/jpeg"),
    ]
    buttons = [[{"text": "x", "url": "https://v"}]]
    assert n.send_hit("🔵 VINTED\n📱 iPhone\nText", photos[0], "", buttons, photos=photos)
    ((m1, p1, f1),) = api.calls  # exactly one message -> one notification
    assert m1 == "sendMediaGroup" and f1 == ["p0", "p1", "p2"]  # max. 3 photos
    assert p1["media"][0]["caption"].startswith("🔵 VINTED") and "caption" not in p1["media"][1]


def test_single_photo_keeps_buttons_on_photo():
    from flip_scout.notify import Notifier

    calls = []

    class API:
        def call(self, method, params=None, photo=None, timeout=None, files=None):
            calls.append(method)

    assert Notifier(API(), "1").send_hit(
        "t", (b"a", "image/jpeg"), "", None, photos=[(b"a", "image/jpeg")]
    )
    assert calls == ["sendPhoto"]
