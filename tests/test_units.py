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
    long = ai_answer(positiv=["x" * 300] * 3, offen=["y" * 300] * 3, einschaetzung="z" * 300)
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
    assert headers["Authorization"] == "Bearer tok"
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
    }
    item = parse_catalog_item(raw, "https://www.vinted.at")
    assert (item.brand, item.size, item.condition) == ("Apple", "128 GB", "Gut")
    assert item.url == "https://www.vinted.at/items/3" and item.created_ts is None


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
