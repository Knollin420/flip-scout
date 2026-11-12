"""One search run: fetch new listings and check each one in stages.

Stages per new listing (cheap first, AI only when needed):
  1. free filters on search data: age, reserved, price, exclude words in the title
  2. load the description (detail API, fallback: item page)
  3. exclude words in the description
  4. keywords (must_include / any_include) -> hit, AI only writes the briefing
  5. AI text check (profile has ai_check) -> 6. AI photo check -> script decides
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

from .ai import AIChecker, AIResponse, AIRetryLater, AIUnavailable
from .config import Config, Profile
from .notify import Notifier, format_hit
from .rules import Decision, decide, find_word, keyword_hit
from .store import Search, Store
from .vinted import Item, VintedClient, VintedError

log = logging.getLogger(__name__)


class RetryItem(Exception):
    """Check this item again in the next run (forget it in 'seen')."""


@dataclass
class RunResult:
    requests_ok: int = 0
    requests_failed: int = 0
    new_items: int = 0
    hits: int = 0

    @property
    def failed(self) -> bool:
        return self.requests_failed > 0 and self.requests_ok == 0


class Pipeline:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        vinted: VintedClient,
        ai: AIChecker | None,
        notifier: Notifier,
        sleep=time.sleep,
        now=time.time,
        rand=random.uniform,
    ):
        self.cfg = cfg
        self.store = store
        self.vinted = vinted
        self.ai = ai
        self.notifier = notifier
        self.sleep = sleep
        self.now = now
        self.rand = rand

    # --- helpers ----------------------------------------------------------

    @property
    def today(self) -> str:
        return self.cfg.settings.day(self.now())

    def _count(self, key: str, amount: float = 1) -> None:
        self.store.incr(self.today, key, amount)

    def ai_budget_left(self) -> bool:
        if self.ai is None:
            return False
        used = self.store.stats(self.today).get("ai_calls", 0)
        return used < self.cfg.ai.max_checks_per_day

    def _pause(self, bounds: tuple[float, float]) -> None:
        self.sleep(self.rand(*bounds))

    # --- run --------------------------------------------------------------

    def reseed_after_pause(self) -> bool:
        """Vinted no longer sends upload times, so after a long pause everything uploaded
        meanwhile would look new. After a pause longer than max_age_minutes all searches
        only remember the current listings again (like a first run)."""
        last = self.store.get("last_search_ts")
        if not last:
            return False
        pause = self.now() - float(last)
        if pause <= self.cfg.settings.max_age_minutes * 60:
            return False
        for search in self.store.list_searches():
            self.store.reset_seeding(search.id)
        log.info("Pause von %.0f min – merke mir zuerst nur den aktuellen Stand", pause / 60)
        return True

    def run_once(self) -> RunResult:
        result = RunResult()
        self.reseed_after_pause()
        first_request = True
        for search in self.store.list_searches(active_only=True):
            profile = self.cfg.profile(search.profile)
            price_from = search.min_price if search.min_price is not None else profile.min_price
            candidates: list[Item] = []
            for term in search.terms:
                if not first_request:
                    self._pause(self.cfg.settings.pause_between_searches)
                first_request = False
                try:
                    items = self.vinted.search(
                        term, search.params, price_from=price_from, price_to=search.max_price
                    )
                except VintedError as exc:
                    result.requests_failed += 1
                    log.warning("[%s] Suche %r fehlgeschlagen: %s", search.name, term, exc)
                    continue
                except Exception:
                    result.requests_failed += 1
                    log.exception("[%s] Unerwarteter Fehler bei Suche %r", search.name, term)
                    continue
                result.requests_ok += 1

                if not self.store.is_seeded(search.id, term):
                    for item in items:
                        self.store.mark_seen(search.id, item.id)
                    self.store.mark_seeded(search.id, term)
                    log.info(
                        "[%s] %r: erster Lauf – %d bestehende Anzeigen gemerkt",
                        search.name,
                        term or "(Filter)",
                        len(items),
                    )
                    continue

                for item in items:
                    if not self.store.is_seen(search.id, item.id):
                        self.store.mark_seen(search.id, item.id)
                        candidates.append(item)

            for item in candidates:
                result.new_items += 1
                try:
                    if self.process(search, profile, item):
                        result.hits += 1
                except RetryItem as exc:
                    log.info("[%s] %s: später erneut (%s)", search.name, item.id, exc)
                    self.store.unmark_seen(search.id, item.id)
                except Exception:
                    log.exception("[%s] Fehler bei Anzeige %s", search.name, item.id)
        if result.requests_ok:
            self.store.set("last_search_ts", str(self.now()))
        return result

    # --- one listing ------------------------------------------------------

    def _skip(self, search: Search, item: Item, reason: str) -> bool:
        log.info("[%s] ✗ %s (%.0f €) – %s", search.name, item.title[:60], item.price, reason)
        return False

    def process(self, search: Search, profile: Profile, item: Item) -> bool:
        """Checks one new listing. Returns True when a hit was delivered."""
        self._count("checked")
        s = self.cfg.settings

        # 1. free filters
        age = item.age_minutes(self.now())
        if age is not None and age > s.max_age_minutes:
            return self._skip(search, item, f"zu alt ({age:.0f} min)")
        if item.reserved or item.closed:
            return self._skip(search, item, "reserviert/verkauft")
        if item.promoted:
            return self._skip(search, item, "beworbene (hochgeschobene) Anzeige")
        min_price = search.min_price if search.min_price is not None else profile.min_price
        if min_price is not None and item.price < min_price:
            return self._skip(search, item, f"unter Mindestpreis {min_price:.0f} €")
        if search.max_price is not None and item.price > search.max_price:
            return self._skip(search, item, f"über Höchstpreis {search.max_price:.0f} €")
        word = find_word(item.title, profile.exclude + profile.exclude_desc)
        if word:
            return self._skip(search, item, f"Ausschlusswort im Titel: {word!r}")

        # 2. + 3. description
        self._pause(s.pause_between_details)
        self.vinted.load_details(item)
        self._count("details")
        word = find_word(item.description, profile.exclude_desc)
        if word:
            return self._skip(search, item, f"Ausschlusswort in Beschreibung: {word!r}")

        full_text = f"{item.title}\n{item.description}"
        ai_data: dict | None = None
        warnings: list[str] = []

        if keyword_hit(profile, full_text):
            # 4. keyword hit: always report, AI only writes the briefing
            if profile.briefing:
                ai_data = self._briefing(profile, item, warnings)
            decision = Decision(True, "Stichwort")
        elif profile.ai_check:
            # 5. + 6. AI decides in two steps
            if not self.ai_budget_left():
                return self._skip(search, item, "KI nicht verfügbar oder Tageslimit erreicht")
            try:
                text_check = self._ai(profile, item, [])
            except AIUnavailable as exc:
                return self._skip(search, item, f"KI-Fehler: {exc}")
            decision = decide(profile, search, item.price, text_check.data, final=False)
            if not decision.notify:
                return self._skip(search, item, f"Text-Check: {decision.reason}")
            ai_data = text_check.data
            photos = self._photos(item)
            if photos and self.ai_budget_left():
                try:
                    ai_data = self._ai(profile, item, photos).data
                except AIUnavailable as exc:
                    warnings.append(f"⚠️ Foto-Check fehlgeschlagen ({exc})")
            decision = decide(profile, search, item.price, ai_data, final=True)
            if not decision.notify:
                return self._skip(search, item, f"Foto-Check: {decision.reason}")
        elif profile.must_include or profile.any_include:
            return self._skip(search, item, "Stichwörter passen nicht")
        else:
            # plain search: everything that passes the filters is a hit
            if profile.briefing:
                ai_data = self._briefing(profile, item, warnings)
                if ai_data and ai_data.get("passt") == "nein":
                    return self._skip(search, item, "KI: passt nicht (z.B. Zubehör)")
            decision = Decision(True, "Filter")

        return self._deliver(search, profile, item, ai_data, decision.warnings + warnings)

    def _photos(self, item: Item, count: int | None = None) -> list[tuple[bytes, str]]:
        count = self.cfg.ai.max_images if count is None else count
        for url in item.photo_urls[:count]:
            if url not in item.photo_cache:
                item.photo_cache[url] = self.vinted.download_photo(url)
        return [p for url in item.photo_urls[:count] if (p := item.photo_cache.get(url))]

    def _ai(self, profile: Profile, item: Item, photos: list[tuple[bytes, str]]) -> AIResponse:
        assert self.ai is not None
        try:
            resp = self.ai.check(profile, item, photos)
        except AIRetryLater as exc:
            raise RetryItem(f"KI vorübergehend nicht erreichbar: {exc}") from exc
        finally:
            self._count("ai_calls")
        self._count("tokens_in", resp.input_tokens)
        self._count("tokens_out", resp.output_tokens)
        return resp

    def _briefing(self, profile: Profile, item: Item, warnings: list[str]) -> dict | None:
        if not self.ai_budget_left():
            warnings.append("ℹ️ Ohne KI-Briefing (Tageslimit/KI aus)")
            return None
        try:
            return self._ai(profile, item, self._photos(item)).data
        except AIUnavailable as exc:
            warnings.append(f"⚠️ KI-Briefing fehlgeschlagen ({exc})")
            return None

    def _deliver(
        self,
        search: Search,
        profile: Profile,
        item: Item,
        ai_data: dict | None,
        warnings: list[str],
    ) -> bool:
        text = format_hit(search, profile, item, ai_data, warnings)
        photos = self._photos(item, count=1)
        buttons = [
            [
                {"text": "🔗 Öffnen", "url": item.url},
                {"text": "⏸ Suche pausieren", "callback_data": f"hp:{search.id}"},
            ]
        ]
        first_url = item.photo_urls[0] if item.photo_urls else ""
        if not self.notifier.send_hit(text, photos[0] if photos else None, first_url, buttons):
            raise RetryItem("Telegram nicht erreichbar")
        self._count("hits")
        self.store.add_hit(search.id, item.id, item.title, item.price, item.url, text)
        log.info(
            "[%s] ✓ TREFFER %s (%.0f €) %s", search.name, item.title[:60], item.price, item.url
        )
        return True
