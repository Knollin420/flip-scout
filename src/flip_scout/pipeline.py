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
from dataclasses import dataclass, replace

from .ai import AIChecker, AIResponse, AIRetryLater, AIUnavailable
from .config import Config, Profile
from .notify import Notifier, format_hit
from .rules import (
    Decision,
    decide,
    find_phrase,
    find_word,
    fixed_price_word,
    keyword_hit,
    min_price,
    original_limit,
    price_limit,
    seller_problem,
    size_problem,
    title_problem,
)
from .store import Search, Store
from .vinted import Item, VintedClient, VintedError
from .willhaben import WillhabenLoginExpired, message_text

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
        willhaben=None,
        messenger=None,
    ):
        self.cfg = cfg
        self.store = store
        self.vinted = vinted
        self.willhaben = willhaben  # WillhabenClient or None
        self.messenger = messenger  # WillhabenMessenger or None (automatic messages)
        self.ai = ai
        self.notifier = notifier
        self.sleep = sleep
        self.now = now
        self.rand = rand
        self.last_searched: dict[int, float] = {}  # search id -> time of last search

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

    def _still_active(self, search_id: int) -> bool:
        current = self.store.get_search(search_id)
        return current is not None and current.active

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
        if not self.store.list_searches(active_only=True):
            # Everything paused: nothing to search, so nothing to remember yet. The
            # reseed happens in the first run after a search is resumed.
            return False
        for search in self.store.list_searches():
            self.store.reset_seeding(search.id)
        log.info("Pause von %.0f min – merke mir zuerst nur den aktuellen Stand", pause / 60)
        return True

    def search_interval(self, profile: Profile, search: Search | None = None) -> float:
        """Seconds between two runs of a search with this profile."""
        if search is not None and search.source == "willhaben":
            return 60 * self.cfg.settings.willhaben_interval_minutes
        return 60 * (profile.interval_minutes or self.cfg.settings.interval_minutes)

    def is_due(self, search: Search, profile: Profile) -> bool:
        last = self.last_searched.get(search.id)
        if last is None:
            return True
        half_tick = 30 * self.cfg.tick_minutes()  # tolerate the loop's own jitter
        return self.now() - last >= self.search_interval(profile, search) - half_tick

    def client_for(self, source: str):
        return self.willhaben if source == "willhaben" else self.vinted

    def source_view(self, profile: Profile, search: Search) -> tuple[Profile, Search]:
        """willhaben: all price limits raised by willhaben_price_factor (e.g. +20 %)."""
        factor = self.cfg.settings.willhaben_price_factor
        if search.source != "willhaben" or factor == 1:
            return profile, search
        scaled = {k: round(v * factor, 2) for k, v in profile.max_prices.items()}
        top = search.max_price * factor if search.max_price is not None else None
        return replace(profile, max_prices=scaled), replace(search, max_price=top)

    def run_once(self, only_due: bool = False) -> RunResult:
        """One pass over the active searches. only_due=True skips searches whose own
        interval has not passed yet (used by the main loop)."""
        result = RunResult()
        self.reseed_after_pause()
        first_request = True
        for search in self.store.list_searches(active_only=True):
            profile = self.cfg.profile(search.profile)
            if only_due and not self.is_due(search, profile):
                continue
            client = self.client_for(search.source)
            if client is None:
                continue  # willhaben not set up
            self.last_searched[search.id] = self.now()
            original = (profile, search)
            profile, search = self.source_view(profile, search)
            price_from = min_price(profile, search, self.cfg.settings.min_price_everywhere)
            candidates: list[Item] = []
            for term in search.terms:
                if not self._still_active(search.id):
                    break
                if not first_request:
                    self._pause(self.cfg.settings.pause_between_searches)
                first_request = False
                try:
                    items = client.search(
                        term,
                        search.params,
                        price_from=price_from,
                        price_to=price_limit(profile, search),
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
                        if search.source == "willhaben" and self.store.seen_by_sibling(
                            search, item.id
                        ):
                            continue  # already checked by the twin search (no double work)
                        candidates.append(item)

            for n, item in enumerate(candidates):
                # Paused or deleted via Telegram while this run is busy: stop right away.
                # The remaining listings stay "seen", so they are not reported later either.
                if not self._still_active(search.id):
                    log.info(
                        "[%s] pausiert/gelöscht – %d weitere Anzeigen übersprungen",
                        search.name,
                        len(candidates) - n,
                    )
                    break
                result.new_items += 1
                try:
                    if self.process(search, profile, item, original):
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

    def process(
        self, search: Search, profile: Profile, item: Item, original: tuple | None = None
    ) -> bool:
        """Checks one new listing. Returns True when a hit was delivered.
        original: (profile, search) before willhaben's price factor, for the message."""
        self._count("checked")
        s = self.cfg.settings
        self._original = original or (profile, search)

        # 1. free filters
        age = item.age_minutes(self.now())
        if age is not None and age > s.max_age_minutes:
            return self._skip(search, item, f"zu alt ({age:.0f} min)")
        if item.reserved or item.closed:
            return self._skip(search, item, "reserviert/verkauft")
        if item.promoted:
            return self._skip(search, item, "beworbene (hochgeschobene) Anzeige")
        if item.source == "willhaben" and s.willhaben_private_only and item.private is False:
            return self._skip(search, item, "Händleranzeige")
        lowest = min_price(profile, search, s.min_price_everywhere)
        if lowest is not None and item.price < lowest:
            return self._skip(search, item, f"unter Mindestpreis {lowest:g} €")
        max_price = price_limit(profile, search)
        if max_price is not None and item.price > max_price:
            return self._skip(search, item, f"über Höchstpreis {max_price:.0f} €")
        word = find_word(item.title, profile.exclude + profile.exclude_desc + s.exclude_everywhere)
        if word:
            return self._skip(search, item, f"Ausschlusswort im Titel: {word!r}")
        if item.source == "vinted" and (word := find_phrase(item.title, s.vinted_exclude_words)):
            return self._skip(search, item, f"Zahlung außerhalb von Vinted: {word!r}")
        problem = title_problem(profile, item.title, item.price) or size_problem(profile, item.size)
        if problem:
            return self._skip(search, item, problem)

        # 2. + 3. description
        self._pause(s.pause_between_details)
        self.client_for(item.source).load_details(item)
        self._count("details")
        word = find_word(item.description, profile.exclude_desc + s.exclude_everywhere)
        if word:
            return self._skip(search, item, f"Ausschlusswort in Beschreibung: {word!r}")
        if item.source == "vinted" and (
            word := find_phrase(item.description, s.vinted_exclude_words)
        ):
            return self._skip(search, item, f"Zahlung außerhalb von Vinted: {word!r}")

        problem = seller_problem(item, s.no_feedback_max_price, profile.min_seller_feedback)
        if problem:
            return self._skip(search, item, problem)

        full_text = f"{item.title}\n{item.description}"
        if profile.require_any and not find_word(full_text, profile.require_any):
            return self._skip(search, item, "keines der Pflichtwörter in Titel/Beschreibung")
        ai_data: dict | None = None
        warnings: list[str] = []
        sure = True  # for the automatic willhaben message: not when the AI was unsure

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
            if not profile.photo_check:
                decision = decide(profile, search, item.price, ai_data, final=True)
                if not decision.notify:
                    return self._skip(search, item, f"Text-Check: {decision.reason}")
                return self._deliver(
                    search,
                    profile,
                    item,
                    ai_data,
                    decision.warnings + warnings,
                    sure=decision.reason == "passt",
                )
            photos = self._photos(item)
            if photos and self.ai_budget_left():
                try:
                    ai_data = self._ai(profile, item, photos).data
                except AIUnavailable as exc:
                    warnings.append(f"⚠️ Foto-Check fehlgeschlagen ({exc})")
            decision = decide(profile, search, item.price, ai_data, final=True)
            if not decision.notify:
                return self._skip(search, item, f"Foto-Check: {decision.reason}")
            sure = decision.reason == "passt"
        elif profile.must_include or profile.any_include:
            return self._skip(search, item, "Stichwörter passen nicht")
        else:
            # plain search: everything that passes the filters is a hit
            if profile.briefing:
                ai_data = self._briefing(profile, item, warnings)
                if ai_data and ai_data.get("passt") == "nein":
                    return self._skip(search, item, "KI: passt nicht (z.B. Zubehör)")
            decision = Decision(True, "Filter")

        return self._deliver(
            search, profile, item, ai_data, decision.warnings + warnings, sure=sure
        )

    def _photos(self, item: Item, count: int | None = None) -> list[tuple[bytes, str]]:
        count = self.cfg.ai.max_images if count is None else count
        for url in item.photo_urls[:count]:
            if url not in item.photo_cache:
                item.photo_cache[url] = self.client_for(item.source).download_photo(url)
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
        sure: bool = True,
    ) -> bool:
        limit = None
        if item.source == "willhaben":
            o_profile, o_search = self._original
            limit = original_limit(o_profile, o_search, ai_data, item.title)
            if limit is not None and item.price > limit:
                fixed = fixed_price_word(
                    f"{item.title}\n{item.description}",
                    self.cfg.settings.willhaben_fixed_price_words,
                )
                if fixed:
                    return self._skip(
                        search, item, f"über deinem Limit {limit:g} € und Festpreis ({fixed!r})"
                    )
                warnings = [*warnings, f"💬 Über deinem Limit von {limit:g} € (willhaben +20 %)"]
        text = format_hit(search, profile, item, ai_data, warnings)
        photos = self._photos(item, count=3)
        # The link to the listing is at the end of the text; only the pause button here.
        buttons = [[{"text": "⏸ Suche pausieren", "callback_data": f"hp:{search.id}"}]]
        first_url = item.photo_urls[0] if item.photo_urls else ""
        if not self.notifier.send_hit(
            text, photos[0] if photos else None, first_url, buttons, photos=photos
        ):
            raise RetryItem("Telegram nicht erreichbar")
        self._count("hits")
        self.store.add_hit(search.id, item.id, item.title, item.price, item.url, text)
        log.info(
            "[%s] ✓ TREFFER %s (%.0f €) %s", search.name, item.title[:60], item.price, item.url
        )
        if item.source == "willhaben":
            self._auto_message(search, item, limit, sure)
        return True

    # --- willhaben: automatic message to the seller -------------------------

    def _auto_message(self, search: Search, item: Item, limit: float | None, sure: bool) -> None:
        s = self.cfg.settings
        if not (self.messenger and s.willhaben_auto_message):
            return
        if self.store.get("wh_auto_message") == "0":  # switched off with /nachrichten aus
            return
        reason = ""
        if item.private is not True:
            reason = "keine Privatanzeige"
        elif not sure:
            reason = "KI war unsicher – selbst prüfen"
        elif self.store.get(f"wh_msg:{item.id}"):
            reason = "diese Anzeige wurde schon angeschrieben"
        elif self.store.stats(self.today).get("wh_messages", 0) >= s.willhaben_max_messages_per_day:
            reason = f"Tageslimit von {s.willhaben_max_messages_per_day} Nachrichten erreicht"
        if reason:
            self.notifier.send_text(f"✉️ Keine automatische Nachricht: {reason}")
            return
        text, offer = message_text(
            s.willhaben_message, s.willhaben_message_offer, item.seller_name, item.price, limit
        )
        if not text:
            return
        try:
            self.messenger.send(item.id, item.url, text)
        except WillhabenLoginExpired:
            log.warning("willhaben-Login abgelaufen – keine Nachricht an %s", item.id)
            if self.store.get("wh_login_alert_day") != self.today:
                self.store.set("wh_login_alert_day", self.today)
                self.notifier.send_text(
                    "🔑 willhaben-Login abgelaufen – automatische Nachrichten gehen nicht.\n"
                    "Bitte neues Cookie eintragen (wie beim ersten Mal)."
                )
            return
        except Exception as exc:
            log.warning("willhaben-Nachricht an %s fehlgeschlagen: %s", item.id, exc)
            self.notifier.send_text(f"⚠️ Automatische Nachricht fehlgeschlagen: {exc}"[:300])
            return
        self.store.set(f"wh_msg:{item.id}", str(self.now()))
        self._count("wh_messages")
        who = item.seller_name or "den Verkäufer"
        extra = f" – mit Preisvorschlag {limit:g} €" if offer else ""
        log.info("[%s] ✉️ Nachricht an %s gesendet (%s)%s", search.name, who, item.id, extra)
        self.notifier.send_text(f"✉️ Nachricht an {who} gesendet{extra}.")
