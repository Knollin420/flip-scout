"""Test doubles: no network, no API costs."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from flip_scout.ai import AIResponse
from flip_scout.config import AISettings, Config, Profile, Settings, load_config
from flip_scout.pipeline import Pipeline
from flip_scout.store import Store
from flip_scout.vinted import Item

ROOT = Path(__file__).resolve().parent.parent
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 100
NOW = time.time()


def make_item(
    item_id: int,
    title: str = "Artikel",
    price: float = 100.0,
    age_min: float = 5,
    description: str = "",
    photos: int = 3,
) -> Item:
    return Item(
        id=item_id,
        title=title,
        price=price,
        brand="Apple",
        url=f"https://www.vinted.at/items/{item_id}",
        photo_urls=[f"https://img/{item_id}/{n}.jpg" for n in range(photos)],
        created_ts=NOW - age_min * 60,
        description=description,
    )


class FakeVinted:
    def __init__(self):
        self.results: dict[str, list[Item]] = {}
        self.descriptions: dict[int, str] = {}
        self.fail = False
        self.calls: list[tuple] = []
        self.downloads: list[str] = []

    def search(self, term, params, price_from=None, price_to=None):
        from flip_scout.vinted import VintedError

        self.calls.append((term, dict(params), price_from, price_to))
        if self.fail:
            raise VintedError("HTTP 403")
        # fresh copies, like real API answers
        return [Item(**{**i.__dict__, "photo_cache": {}}) for i in self.results.get(term, [])]

    def load_details(self, item):
        if item.id in self.descriptions:
            item.description = self.descriptions[item.id]
            item.description_source = "api"

    def download_photo(self, url):
        self.downloads.append(url)
        return (JPEG, "image/jpeg")


class FakeAI:
    def __init__(self, *answers: dict):
        self.answers = list(answers)
        self.calls: list[tuple[str, int, int]] = []  # (profile, item id, number of images)
        self.raise_exc: Exception | None = None

    def check(self, profile, item, images=None):
        self.calls.append((profile.key, item.id, len(images or [])))
        if self.raise_exc:
            raise self.raise_exc
        data = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return AIResponse(data=dict(data), input_tokens=1000, output_tokens=200)


class FakeNotifier:
    def __init__(self):
        self.hits: list[str] = []
        self.texts: list[str] = []
        self.photos: list = []
        self.ok = True

    def send_hit(self, text, photo, photo_url, buttons=None):
        if self.ok:
            self.hits.append(text)
            self.photos.append(photo)
        return self.ok

    def send_text(self, text, buttons=None):
        self.texts.append(text)
        return self.ok


def ai_answer(**overrides) -> dict:
    base = {
        "passt": "ja",
        "modell": "iPhone 13 Pro 128 GB",
        "modell_key": "iPhone 13 Pro",
        "zustand": "Display gesprungen, sonst ok",
        "defekte": ["Display gesprungen"],
        "defekte_ok": True,
        "no_go": False,
        "no_go_grund": "",
        "icloud": "unklar",
        "positiv": ["Face ID geht laut Text"],
        "offen": ["Wasserschaden nicht erwähnt"],
        "einschaetzung": "Fairer Preis",
    }
    return {**base, **overrides}


@pytest.fixture
def profiles() -> dict[str, Profile]:
    return {
        "standard": Profile(
            key="standard", label="Normal", exclude=["hülle"], exclude_desc=["ich suche"]
        ),
        "iphone": Profile(
            key="iphone",
            label="iPhone defekt",
            emoji="📱",
            exclude=["hülle"],
            exclude_desc=["ich suche"],
            ai_check="Defektes iPhone",
            ok_defects=["Display gesprungen"],
            no_go=["Wasserschaden"],
            check_icloud=True,
            max_prices={"iPhone 13 Pro": 180.0, "iPhone 12": 90.0},
        ),
        "kw": Profile(key="kw", label="Stichwort", any_include=["oled"]),
    }


@pytest.fixture
def cfg(profiles, tmp_path) -> Config:
    settings = Settings(pause_between_searches=(0, 0), pause_between_details=(0, 0))
    return Config(
        settings=settings,
        ai=AISettings(max_images=3, max_checks_per_day=100),
        profiles=profiles,
        data_dir=tmp_path,
    )


@pytest.fixture
def store() -> Store:
    return Store(":memory:")


@pytest.fixture
def env(cfg, store):
    """Returns a factory: env(ai=FakeAI(...)) -> (pipeline, vinted, ai, notifier)."""

    def build(ai: FakeAI | None = None):
        vinted, notifier = FakeVinted(), FakeNotifier()
        pipe = Pipeline(
            cfg,
            store,
            vinted,
            ai,
            notifier,
            sleep=lambda s: None,
            now=lambda: NOW,
            rand=lambda a, b: 0,
        )
        return pipe, vinted, ai, notifier

    return build


@pytest.fixture
def example_config() -> Config:
    return load_config(ROOT / "profiles.example.toml", env_path=None)
