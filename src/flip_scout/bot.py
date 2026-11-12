"""Telegram control: send a Vinted link to add a search, /menu for everything else.

Only messages from the configured chat are answered.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time

from .config import Config
from .monitor import status_text
from .notify import TelegramAPI, TelegramError
from .store import Search, Store
from .urlimport import find_vinted_link, is_catalog_link, parse_vinted_url

log = logging.getLogger(__name__)

HELP = (
    "👋 So funktioniert's:\n"
    "1️⃣ Stell auf vinted.at deine Suche ein (Begriff, Kategorie, Marke, Zustand, Preis).\n"
    "2️⃣ Kopier den Link aus dem Browser und schick ihn mir.\n"
    "3️⃣ Tipp an, welche Art von Suche es ist – fertig.\n\n"
    "/menu – Suchen ansehen, ändern, pausieren, löschen\n"
    "/status – Zahlen von heute\n\n"
    "Ich melde nur Anzeigen, die NACH dem Anlegen einer Suche neu hochgeladen werden."
)

FILTER_NAMES = {
    "catalog_ids": "Kategorie",
    "brand_ids": "Marke",
    "size_ids": "Größe",
    "status_ids": "Zustand",
    "color_ids": "Farbe",
    "material_ids": "Material",
}


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def price_text(search: Search) -> str:
    lo, hi = search.min_price, search.max_price
    if lo is not None and hi is not None:
        return f"{lo:g}–{hi:g} €"
    if hi is not None:
        return f"bis {hi:g} €"
    if lo is not None:
        return f"ab {lo:g} €"
    return "ohne Grenze"


def parse_price(text: str) -> tuple[float | None, float | None] | None:
    """'160' -> (None, 160) · '30-160' -> (30, 160) · '0' -> (None, None)."""
    nums = re.findall(r"\d+(?:[.,]\d+)?", text)
    values = [float(n.replace(",", ".")) for n in nums]
    if len(values) == 1:
        return (None, values[0] or None)
    if len(values) == 2:
        lo, hi = sorted(values)
        return (lo or None, hi or None)
    return None


class TelegramBot:
    def __init__(self, cfg: Config, store: Store, api: TelegramAPI, chat_id: str):
        self.cfg = cfg
        self.store = store
        self.api = api
        self.chat_id = str(chat_id)
        self.awaiting: tuple[str, int] | None = None  # (action, search_id)

    # --- plumbing ---------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        try:
            self.api.call(
                "setMyCommands",
                {
                    "commands": [
                        {"command": "menu", "description": "Suchen verwalten"},
                        {"command": "status", "description": "Zahlen von heute"},
                        {"command": "hilfe", "description": "So funktioniert's"},
                    ]
                },
            )
        except TelegramError as exc:
            log.warning("Befehle nicht gesetzt: %s", exc)
        offset = None
        while not stop.is_set():
            try:
                updates = (
                    self.api.call(
                        "getUpdates",
                        {
                            "offset": offset,
                            "timeout": 30,
                            "allowed_updates": ["message", "callback_query"],
                        },
                        timeout=45,
                    )
                    or []
                )
            except TelegramError as exc:
                log.warning("Telegram-Abfrage fehlgeschlagen: %s", exc)
                stop.wait(10)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    self.handle_update(update)
                except Exception:
                    log.exception("Fehler bei Telegram-Update")

    def send(self, text: str, buttons: list[list[dict]] | None = None) -> None:
        self.api.call(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": buttons} if buttons else None,
            },
        )

    def show(self, message: dict | None, text: str, buttons: list[list[dict]] | None) -> None:
        """Edit the menu message in place if possible, otherwise send a new one."""
        if message and "text" in message:
            try:
                self.api.call(
                    "editMessageText",
                    {
                        "chat_id": self.chat_id,
                        "message_id": message["message_id"],
                        "text": text,
                        "disable_web_page_preview": True,
                        "reply_markup": {"inline_keyboard": buttons} if buttons else None,
                    },
                )
                return
            except TelegramError as exc:
                if "not modified" in str(exc):
                    return
        self.send(text, buttons)

    def handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            cq = update["callback_query"]
            if str(cq.get("message", {}).get("chat", {}).get("id")) != self.chat_id:
                return
            toast = self.on_callback(cq.get("data", ""), cq.get("message"))
            self.api.call("answerCallbackQuery", {"callback_query_id": cq["id"], "text": toast})
        elif "message" in update:
            msg = update["message"]
            if str(msg.get("chat", {}).get("id")) != self.chat_id:
                log.warning(
                    "Nachricht von fremdem Chat %s ignoriert", msg.get("chat", {}).get("id")
                )
                return
            self.on_text((msg.get("text") or "").strip())

    # --- text messages ----------------------------------------------------

    def on_text(self, text: str) -> None:
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if command:
            self.awaiting = None
            if command in ("/menu", "/suchen", "/liste"):
                return self.show(None, *self.menu())
            if command == "/status":
                return self.send(status_text(self.cfg, self.store))
            if command == "/abbrechen":
                return self.send("Abgebrochen.")
            return self.send(HELP)

        if self.awaiting:
            action, search_id = self.awaiting
            self.awaiting = None
            return self.on_input(action, search_id, text)

        link = find_vinted_link(text)
        if link and is_catalog_link(link):
            return self.new_search_from_link(link)
        if link:
            return self.send(
                "Das ist ein Link zu einer einzelnen Anzeige. Schick mir den Link "
                "einer SUCHE (vinted.at/catalog?...)."
            )
        self.send(HELP)

    def new_search_from_link(self, link: str) -> None:
        try:
            imp = parse_vinted_url(link)
        except ValueError as exc:
            return self.send(f"❌ {exc}")
        pending_id = str(int(time.time() * 1000) % 10_000_000)
        name = imp.term.strip().title() if imp.term else "Suche ohne Begriff"
        self.store.set(
            f"pending:{pending_id}",
            json.dumps(
                {
                    "name": name,
                    "term": imp.term,
                    "params": imp.params,
                    "price_from": imp.price_from,
                    "price_to": imp.price_to,
                }
            ),
        )
        hint = ""
        if imp.domain != self.cfg.settings.domain:
            hint = (
                f"\n⚠️ Link ist von vinted.{imp.domain}, ich suche auf "
                f"vinted.{self.cfg.settings.domain}. Kategorie-IDs können abweichen."
            )
        preview = Search(id=0, name=name, max_price=imp.price_to, min_price=imp.price_from)
        filters = ", ".join(FILTER_NAMES.get(k, k) for k in imp.params) or "keine"
        buttons = [
            [_btn(f"{p.emoji} {p.label}", f"np:{pending_id}:{key}")]
            for key, p in self.cfg.profiles.items()
        ]
        buttons.append([_btn("❌ Abbrechen", f"nx:{pending_id}")])
        self.send(
            f"➕ Neue Suche: „{name}“\nPreis: {price_text(preview)}\nFilter: {filters}"
            f"{hint}\n\nWelche Art?",
            buttons,
        )

    def on_input(self, action: str, search_id: int, text: str) -> None:
        search = self.store.get_search(search_id)
        if not search:
            return self.send("Diese Suche gibt es nicht mehr.")
        if action == "price":
            parsed = parse_price(text)
            if parsed is None:
                self.awaiting = (action, search_id)
                return self.send("Bitte nur Zahlen, z.B. 160 oder 30-160 (0 = ohne Grenze).")
            lo, hi = parsed
            fields = {"max_price": hi}
            if "-" in text or "–" in text:
                fields["min_price"] = lo
            self.store.update_search(search_id, **fields)
            self.store.reset_seeding(search_id)
            search = self.store.get_search(search_id)
            self.send(f"✅ Preis: {price_text(search)}")
        elif action == "filter":
            link = find_vinted_link(text)
            if not link or not is_catalog_link(link):
                self.awaiting = (action, search_id)
                return self.send("Bitte einen Vinted-Suchlink schicken (oder /abbrechen).")
            imp = parse_vinted_url(link)
            fields = {"params": imp.params}
            if imp.price_to is not None:
                fields["max_price"] = imp.price_to
            if imp.price_from is not None:
                fields["min_price"] = imp.price_from
            self.store.update_search(search_id, **fields)
            if imp.term:
                self.store.add_term(search_id, imp.term)
            self.store.reset_seeding(search_id)
            self.send("✅ Filter übernommen. Ab jetzt nur neue Anzeigen.")
        elif action == "term":
            added = [
                t
                for t in re.split(r"[,\n]", text)
                if t.strip() and self.store.add_term(search_id, t.strip().lower())
            ]
            self.send(
                "✅ Hinzugefügt: " + ", ".join(t.strip() for t in added)
                if added
                else "Diesen Begriff gibt es schon."
            )
        elif action == "rename":
            self.store.update_search(search_id, name=text[:60])
            self.send(f"✅ Umbenannt in „{text[:60]}“")
        self.show(None, *self.search_view(search_id))

    # --- menus ------------------------------------------------------------

    def menu(self) -> tuple[str, list[list[dict]]]:
        searches = self.store.list_searches()
        if not searches:
            return "Noch keine Suchen.\n\n" + HELP, [[_btn("📊 Status", "st")]]
        lines, buttons, row = ["🔎 Deine Suchen:"], [], []
        for i, s in enumerate(searches, start=1):
            p = self.cfg.profile(s.profile)
            state = "✅" if s.active else "⏸"
            lines.append(f"{i}. {p.emoji} {s.name} {state} · {price_text(s)}")
            row.append(_btn(f"{i}. {s.name}"[:30], f"s:{s.id}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([_btn("📊 Status", "st")])
        lines.append("\nNeue Suche: einfach einen Vinted-Link schicken.")
        return "\n".join(lines), buttons

    def search_view(self, search_id: int) -> tuple[str, list[list[dict]]]:
        s = self.store.get_search(search_id)
        if not s:
            return self.menu()
        p = self.cfg.profile(s.profile)
        terms = " · ".join(t for t in s.terms if t) or "(nur Filter)"
        filters = (
            ", ".join(
                f"{FILTER_NAMES.get(k, k)} ({len(v.split(','))})" for k, v in s.params.items()
            )
            or "keine"
        )
        text = (
            f"{p.emoji} {s.name} – {'✅ aktiv' if s.active else '⏸ pausiert'}\n"
            f"Art: {p.label}\nBegriffe: {terms}\nPreis: {price_text(s)}\nFilter: {filters}"
        )
        i = s.id
        buttons = [
            [
                _btn("⏸ Pausieren" if s.active else "▶️ Fortsetzen", f"p:{i}"),
                _btn("💶 Preis", f"pr:{i}"),
            ],
            [
                _btn("➕ Begriff", f"t:{i}"),
                _btn("➖ Begriff", f"tr:{i}"),
                _btn("🔗 Filter", f"f:{i}"),
            ],
            [_btn("✏️ Name", f"n:{i}"), _btn("🔄 Art", f"pf:{i}"), _btn("🗑 Löschen", f"d:{i}")],
            [_btn("⬅️ Alle Suchen", "m")],
        ]
        return text, buttons

    # --- buttons ----------------------------------------------------------

    def on_callback(self, data: str, message: dict | None) -> str | None:
        action, _, rest = data.partition(":")
        args = rest.split(":") if rest else []

        if action == "m":
            self.show(message, *self.menu())
        elif action == "st":
            self.show(message, status_text(self.cfg, self.store), [[_btn("⬅️ Zurück", "m")]])
        elif action == "np":
            return self._create_pending(args[0], args[1], message)
        elif action == "nx":
            self.store.delete(f"pending:{args[0]}")
            self.show(message, "Abgebrochen.", None)
        else:
            search_id = int(args[0]) if args else 0
            search = self.store.get_search(search_id)
            if not search:
                return "Suche gibt es nicht mehr"
            return self._search_action(action, search, args, message)
        return None

    def _create_pending(self, pending_id: str, profile_key: str, message) -> str | None:
        raw = self.store.get(f"pending:{pending_id}")
        if not raw:
            return "Schon erledigt oder abgelaufen"
        self.store.delete(f"pending:{pending_id}")
        data = json.loads(raw)
        profile = self.cfg.profile(profile_key)
        search_id = self.store.add_search(
            data["name"],
            profile=profile.key,
            terms=[data["term"]],
            params=data["params"],
            max_price=data["price_to"],
            min_price=data["price_from"],
        )
        search = self.store.get_search(search_id)
        self.show(
            message,
            f"✅ Gespeichert: {profile.emoji} {search.name}\nArt: {profile.label} · "
            f"{price_text(search)}\n\nAb dem nächsten Durchlauf melde ich neue Angebote.",
            [[_btn("⚙️ Bearbeiten", f"s:{search_id}"), _btn("✏️ Name ändern", f"n:{search_id}")]],
        )
        return "Gespeichert"

    def _search_action(self, action: str, s: Search, args: list[str], message) -> str | None:
        i = s.id
        if action == "s":
            self.show(message, *self.search_view(i))
        elif action == "p":
            self.store.update_search(i, active=not s.active)
            self.show(message, *self.search_view(i))
            return "Pausiert" if s.active else "Läuft wieder"
        elif action == "hp":  # pause button under a hit message
            self.store.update_search(i, active=False)
            return f"⏸ „{s.name}“ pausiert – /menu zum Fortsetzen"
        elif action == "pr":
            self.awaiting = ("price", i)
            self.send(
                f"💶 Aktuell: {price_text(s)}\nNeuer Höchstpreis? z.B. 160\n"
                "Oder Mindest- und Höchstpreis: 30-160 · 0 = ohne Grenze"
            )
        elif action == "f":
            self.awaiting = ("filter", i)
            self.send(
                "🔗 Stell die Filter auf vinted.at ein und schick mir den neuen Link.\n"
                "Begriffe und Name bleiben erhalten."
            )
        elif action == "t":
            self.awaiting = ("term", i)
            self.send("➕ Welcher Suchbegriff? Mehrere mit Komma trennen.")
        elif action == "tr":
            terms = [t for t in s.terms if t]
            if not terms:
                return "Keine Begriffe vorhanden"
            buttons = [[_btn(f"➖ {t}"[:40], f"rt:{i}:{n}")] for n, t in enumerate(s.terms) if t]
            buttons.append([_btn("⬅️ Zurück", f"s:{i}")])
            self.show(message, "Welchen Begriff entfernen?", buttons)
        elif action == "rt":
            n = int(args[1])
            if n < len(s.terms):
                self.store.remove_term(i, s.terms[n])
            self.show(message, *self.search_view(i))
            return "Entfernt"
        elif action == "n":
            self.awaiting = ("rename", i)
            self.send("✏️ Wie soll die Suche heißen?")
        elif action == "pf":
            buttons = [
                [
                    _btn(
                        ("✅ " if key == s.profile else "") + f"{p.emoji} {p.label}",
                        f"sp:{i}:{key}",
                    )
                ]
                for key, p in self.cfg.profiles.items()
            ]
            buttons.append([_btn("⬅️ Zurück", f"s:{i}")])
            self.show(message, "Welche Art von Suche?", buttons)
        elif action == "sp":
            self.store.update_search(i, profile=self.cfg.profile(args[1]).key)
            self.show(message, *self.search_view(i))
            return "Geändert"
        elif action == "d":
            self.show(
                message,
                f"🗑 „{s.name}“ wirklich löschen?",
                [[_btn("✅ Ja, löschen", f"dd:{i}"), _btn("Abbrechen", f"s:{i}")]],
            )
        elif action == "dd":
            self.store.delete_search(i)
            self.show(message, *self.menu())
            return "Gelöscht"
        return None
