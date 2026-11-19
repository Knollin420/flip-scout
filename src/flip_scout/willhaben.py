"""willhaben.at: send a message to a seller with the owner's own login.

The owner logs in in his browser (including the captcha) and stores the browser's
cookie header as WILLHABEN_COOKIE in .env. Nothing here logs in or solves captchas.
The request is the same one the website sends when you press "Nachricht absenden".
"""

from __future__ import annotations

import json
import logging
import re

from .vinted import VintedError

log = logging.getLogger(__name__)

BASE = "https://www.willhaben.at"
SEND_PATHS = ("/webapi/iad-messaging/sendrequest", "/webapi/iad-messaging/sendrequest/chat")
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class WillhabenError(VintedError):
    """willhaben request failed (counts like a failed Vinted request)."""


class WillhabenLoginExpired(WillhabenError):
    """The stored cookie is no longer logged in."""


def _cookie_value(cookie: str, name: str) -> str:
    for part in cookie.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return ""


def account_from_page(page: str) -> tuple[str, str]:
    """(e-mail, first name) of the logged-in user from a willhaben page, or ("", "")."""
    m = _NEXT_DATA.search(page)
    data = m.group(1) if m else ""
    email = re.search(r'"(?:email|emailAddress)"\s*:\s*"([^"@\s]+@[^"\s]+)"', data)
    first = re.search(r'"firstName"\s*:\s*"([^"]{1,40})"', data)
    return (email.group(1) if email else "", first.group(1) if first else "")


def message_text(
    plain: str, offer: str, name: str, price: float, limit: float | None
) -> tuple[str, bool]:
    """The automatic message: with a price proposal when the price is above the owner's
    own limit. Returns (text, with_offer)."""
    with_offer = bool(offer) and limit is not None and price > limit
    template = offer if with_offer else plain
    if not template:
        return "", False
    text = template.format(name=(name or "").strip(), limit=f"{limit:g} €" if limit else "")
    text = re.sub(r"Hallo\s+,", "Hallo,", text)
    return text.strip(), with_offer


def message_payload(ad_id: int, text: str, email: str, first_name: str) -> dict:
    """Exactly what the website sends (copied from a real request)."""
    return {
        "fromFullName": first_name,
        "firstName": first_name,
        "shareTenantProfile": False,
        "from": email,
        "adId": int(ad_id),
        "copyToSender": False,
        "showTelephoneNumber": False,
        "telephone": "",
        "selectedContactSuggestions": [],
        "mailContent": text,
    }


class WillhabenMessenger:
    def __init__(self, cookie: str, session=None, timeout: float = 25.0):
        if session is None:
            from curl_cffi import requests

            session = requests.Session(impersonate="chrome")
        self.session = session
        self.cookie = cookie
        self.timeout = timeout

    def _headers(self, referer: str) -> dict:
        return {
            "Cookie": self.cookie,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": BASE,
            "Referer": referer,
            "x-bbx-csrf-token": _cookie_value(self.cookie, "x-bbx-csrf-token"),
            "X-WH-Client": "api@willhaben.at;responsive_web;server;1.0.0;desktop",
        }

    def account(self, ad_url: str) -> tuple[str, str]:
        resp = self.session.get(ad_url, headers={"Cookie": self.cookie}, timeout=self.timeout)
        email, first = account_from_page(resp.text)
        if not email:
            raise WillhabenLoginExpired("willhaben-Login abgelaufen oder Cookie ungültig")
        return email, first

    def send(self, ad_id: int, ad_url: str, text: str) -> str:
        """Sends one message. Returns the endpoint used. Raises if nothing was sent."""
        email, first = self.account(ad_url)
        body = json.dumps(message_payload(ad_id, text, email, first))
        last = ""
        for path in SEND_PATHS:
            resp = self.session.post(
                BASE + path, data=body, headers=self._headers(ad_url), timeout=self.timeout
            )
            if 200 <= resp.status_code < 300:
                return path
            last = f"HTTP {resp.status_code}: {resp.text[:200]}"
            if resp.status_code in (401, 403):
                raise WillhabenLoginExpired(last)
            if resp.status_code != 404 or "Cannot POST" not in resp.text:
                break  # a real answer from the right endpoint: do not try the other one
        raise WillhabenError(last)


# --- search ------------------------------------------------------------------

IMAGE_BASE = "https://cache.willhaben.at/mmo/"
_TAG = re.compile(r"<[^>]+>")


def _attrs(ad: dict) -> dict[str, str]:
    return {
        a.get("name"): (a.get("values") or [""])[0]
        for a in (ad.get("attributes") or {}).get("attribute", [])
    }


def _next_data(page: str) -> dict:
    m = _NEXT_DATA.search(page)
    if not m:
        raise WillhabenError("Keine Seitendaten (Bot-Schutz-Seite?)")
    return json.loads(m.group(1))


def html_to_text(text: str) -> str:
    import html

    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    return html.unescape(_TAG.sub("", text)).strip()


def parse_search_page(page: str):
    """Items of a willhaben search result page (marketplace)."""
    from .vinted import Item

    data = _next_data(page)
    result = (data.get("props", {}).get("pageProps", {}) or {}).get("searchResult") or {}
    ads = (result.get("advertSummaryList") or {}).get("advertSummary") or []
    items = []
    for ad in ads:
        a = _attrs(ad)
        try:
            item_id = int(a.get("ADID") or ad.get("id"))
        except (TypeError, ValueError):
            continue
        price_raw = a.get("PRICE") or a.get("PRICE/AMOUNT") or ""
        try:
            price = float(str(price_raw).replace(",", "."))
        except ValueError:
            price = 0.0
        published = a.get("PUBLISHED")
        photos = [IMAGE_BASE + p for p in (a.get("ALL_IMAGE_URLS") or "").split(";") if p]
        items.append(
            Item(
                id=item_id,
                title=a.get("HEADING") or ad.get("description") or "",
                price=price,
                url=f"{BASE}/iad/{a['SEO_URL']}"
                if a.get("SEO_URL")
                else f"{BASE}/iad/object?adId={item_id}",
                photo_urls=photos,
                created_ts=float(published) / 1000 if published and published.isdigit() else None,
                promoted=a.get("IS_BUMPED") == "1",
                description=html_to_text(a.get("BODY_DYN") or ""),
                description_source="summary" if a.get("BODY_DYN") else "",
                source="willhaben",
                location=a.get("LOCATION") or "",
                private=a.get("ISPRIVATE") == "1" if "ISPRIVATE" in a else None,
            )
        )
    return items


def apply_item_page(item, page: str) -> None:
    """Full description, large photos and the seller's first name from the item page."""
    details = (_next_data(page).get("props", {}).get("pageProps", {}) or {}).get(
        "advertDetails"
    ) or {}
    a = _attrs(details)
    desc = html_to_text(a.get("DESCRIPTION") or "")
    if desc:
        item.description, item.description_source = desc, "page"
    images = (details.get("advertImageList") or {}).get("advertImage") or []
    photos = [i.get("referenceImageUrl") or i.get("mainImageUrl") for i in images]
    if photos := [p for p in photos if p]:
        item.photo_urls = photos
    name = (details.get("sellerProfileUserData") or {}).get("name")
    if name:
        item.seller_name = str(name).strip()
    if "ISPRIVATE" in a:
        item.private = a["ISPRIVATE"] == "1"


class WillhabenClient:
    """Searches willhaben by category and area only (no search words, no price filter in
    the URL: robots.txt excludes those). Everything else is filtered by the bot."""

    MAX_PHOTO_BYTES = 5 * 1024 * 1024
    CACHE_SECONDS = 60

    def __init__(
        self,
        area_id: int = 900,
        rows: int = 30,
        private_only: bool = True,
        session=None,
        timeout: float = 25.0,
    ):
        if session is None:
            from curl_cffi import requests

            session = requests.Session(impersonate="chrome")
        self.session = session
        self.area_id = area_id
        self.rows = rows
        self.private_only = private_only
        self.timeout = timeout
        self._cache: dict[str, tuple[float, list]] = {}

    def search_url(self, params: dict) -> str:
        path = str(params.get("path", "")).strip("/")
        if not path:
            raise WillhabenError("willhaben-Suche ohne Kategorie (params.path)")
        return (
            f"{BASE}/iad/kaufen-und-verkaufen/marktplatz/{path}"
            f"?areaId={self.area_id}&sort=1&rows={self.rows}"
            + ("&ISPRIVATE=1" if self.private_only else "")
        )

    def search(self, term, params, price_from=None, price_to=None, per_page=None):
        """Newest private listings of the category. A search term is NOT sent to willhaben
        (robots.txt); it filters the category's listings here: title or short text must
        contain it (finds listings where the seller forgot to pick the brand)."""
        from dataclasses import replace

        words = " ".join((term or "").lower().split())
        items = [
            replace(i, photo_cache={})
            for i in self._category(params)
            if not words or words in " ".join(f"{i.title} {i.description}".lower().split())
        ]
        return items

    def _category(self, params) -> list:
        import time

        url = self.search_url(params)
        cached = self._cache.get(url)
        if cached and time.time() - cached[0] < self.CACHE_SECONDS:
            return cached[1]  # same category, same run: no second request
        try:
            resp = self.session.get(url, timeout=self.timeout)
        except Exception as exc:
            raise WillhabenError(f"Netzwerkfehler: {exc}") from exc
        if resp.status_code != 200:
            raise WillhabenError(f"HTTP {resp.status_code}")
        try:
            items = parse_search_page(resp.text)
        except ValueError as exc:
            raise WillhabenError("Seitendaten nicht lesbar") from exc
        self._cache[url] = (time.time(), items)
        return items

    def load_details(self, item) -> None:
        """Never raises: without the page the short description from the search is used."""
        try:
            resp = self.session.get(item.url, timeout=self.timeout)
            if resp.status_code == 200:
                apply_item_page(item, resp.text)
            else:
                log.info("willhaben-Anzeigenseite %s: HTTP %s", item.id, resp.status_code)
        except Exception as exc:
            log.info("willhaben-Anzeigenseite %s nicht ladbar: %s", item.id, exc)

    def download_photo(self, url: str):
        from .vinted import sniff_media_type

        try:
            resp = self.session.get(url, timeout=self.timeout)
        except Exception as exc:
            log.info("Foto-Download fehlgeschlagen: %s", exc)
            return None
        if resp.status_code != 200:
            return None
        data = resp.content
        media_type = sniff_media_type(data)
        if not media_type or len(data) > self.MAX_PHOTO_BYTES:
            return None
        return data, media_type
