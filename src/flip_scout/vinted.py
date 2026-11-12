"""Vinted access via the internal JSON API, without login.

- Fetches the anonymous ``access_token_web`` cookie from the start page.
- Uses curl_cffi with a Chrome TLS fingerprint.
- On 401/403: fetch a new cookie once and retry.
- Description: detail API first, then the public item page (og:description) as fallback.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


class VintedError(Exception):
    """Request failed (network, blocked, unexpected answer)."""


@dataclass
class Item:
    id: int
    title: str
    price: float
    currency: str = "EUR"
    brand: str = ""
    size: str = ""
    condition: str = ""
    url: str = ""
    photo_urls: list[str] = field(default_factory=list)
    created_ts: float | None = None
    reserved: bool = False
    closed: bool = False
    description: str = ""
    description_source: str = ""  # "api", "page" or "" (not available)
    photo_cache: dict = field(default_factory=dict, repr=False)  # url -> (bytes, type) | None

    def age_minutes(self, now: float | None = None) -> float | None:
        if self.created_ts is None:
            return None
        return ((now or time.time()) - self.created_ts) / 60


def _price(raw) -> tuple[float, str]:
    if isinstance(raw, dict):
        return float(raw.get("amount") or 0), raw.get("currency_code") or "EUR"
    try:
        return float(str(raw).replace(",", ".")), "EUR"
    except (TypeError, ValueError):
        return 0.0, "EUR"


def _photo_url(photo: dict) -> str:
    return photo.get("full_size_url") or photo.get("url") or ""


def parse_catalog_item(raw: dict, base_url: str) -> Item:
    price, currency = _price(raw.get("price"))
    photo = raw.get("photo") or {}
    photos = [_photo_url(p) for p in raw.get("photos") or [] if _photo_url(p)]
    if not photos and _photo_url(photo):
        photos = [_photo_url(photo)]
    ts = (photo.get("high_resolution") or {}).get("timestamp")
    item_id = int(raw["id"])
    return Item(
        id=item_id,
        title=raw.get("title") or "",
        price=price,
        currency=currency,
        brand=raw.get("brand_title") or "",
        size=raw.get("size_title") or "",
        condition=raw.get("status") or "",
        url=raw.get("url") or f"{base_url}/items/{item_id}",
        photo_urls=photos,
        created_ts=float(ts) if ts else None,
        reserved=bool(raw.get("is_reserved")),
        closed=bool(raw.get("is_closed") or raw.get("is_hidden")),
    )


_OG_DESC = re.compile(r'<meta[^>]+property="og:description"[^>]+content="([^"]*)"', re.I)
_LD_JSON = re.compile(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.I | re.S)


def description_from_page(page: str) -> str:
    """Extracts the description from the public item page."""
    for block in _LD_JSON.findall(page):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for entry in data if isinstance(data, list) else [data]:
            if isinstance(entry, dict) and entry.get("description"):
                return html.unescape(str(entry["description"])).strip()
    match = _OG_DESC.search(page)
    return html.unescape(match.group(1)).strip() if match else ""


def sniff_media_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


class VintedClient:
    MAX_PHOTO_BYTES = 5 * 1024 * 1024  # Anthropic image limit

    def __init__(self, base_url: str, timeout: float = 20.0, session=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        if session is None:
            from curl_cffi import requests as cffi_requests

            session = cffi_requests.Session(impersonate="chrome")
        self.session = session
        self._has_cookie = False

    def refresh_cookie(self) -> None:
        self.session.cookies.clear()
        try:
            resp = self.session.get(self.base_url + "/", timeout=self.timeout)
        except Exception as exc:  # network errors from curl_cffi
            raise VintedError(f"Startseite nicht erreichbar: {exc}") from exc
        if resp.status_code >= 400:
            raise VintedError(f"Startseite: HTTP {resp.status_code}")
        if "access_token_web" not in self.session.cookies.keys():
            raise VintedError("Kein access_token_web-Cookie erhalten (Bot-Schutz?)")
        self._has_cookie = True
        log.info("Neues Vinted-Cookie geholt")

    def _get(self, path: str, params: dict | None = None, retry: bool = True):
        if not self._has_cookie:
            self.refresh_cookie()
        try:
            resp = self.session.get(
                self.base_url + path,
                params=params,
                timeout=self.timeout,
                headers={"Accept": "application/json, text/plain, */*"},
            )
        except Exception as exc:  # network errors from curl_cffi
            raise VintedError(f"Netzwerkfehler: {exc}") from exc
        if resp.status_code in (401, 403) and retry:
            log.info("HTTP %s – hole neues Cookie und versuche erneut", resp.status_code)
            self._has_cookie = False
            return self._get(path, params, retry=False)
        if resp.status_code != 200:
            raise VintedError(f"{path}: HTTP {resp.status_code}")
        return resp

    def search(
        self,
        term: str,
        params: dict[str, str],
        price_from: float | None = None,
        price_to: float | None = None,
        per_page: int = 48,
    ) -> list[Item]:
        query = {"page": 1, "per_page": per_page, "order": "newest_first", **params}
        if term:
            query["search_text"] = term
        if price_from is not None:
            query["price_from"] = f"{price_from:g}"
        if price_to is not None:
            query["price_to"] = f"{price_to:g}"
        resp = self._get("/api/v2/catalog/items", query)
        try:
            data = resp.json()
        except ValueError as exc:
            raise VintedError("Antwort ist kein JSON (Bot-Schutz-Seite?)") from exc
        return [parse_catalog_item(raw, self.base_url) for raw in data.get("items", [])]

    def load_details(self, item: Item) -> None:
        """Fills description (and full photo list). Never raises."""
        try:
            data = self._get(f"/api/v2/items/{item.id}", retry=False).json().get("item") or {}
            desc = (data.get("description") or "").strip()
            photos = [_photo_url(p) for p in data.get("photos") or [] if _photo_url(p)]
            if photos:
                item.photo_urls = photos
            if desc:
                item.description, item.description_source = desc, "api"
                return
        except Exception as exc:
            log.debug("Detail-API für %s nicht verfügbar: %s", item.id, exc)
        try:
            resp = self.session.get(item.url, timeout=self.timeout)
            if resp.status_code == 200:
                desc = description_from_page(resp.text)
                if desc:
                    item.description, item.description_source = desc, "page"
                    return
            log.info("Keine Beschreibung für %s (HTTP %s)", item.id, resp.status_code)
        except Exception as exc:
            log.info("Anzeigenseite für %s nicht ladbar: %s", item.id, exc)

    def download_photo(self, url: str) -> tuple[bytes, str] | None:
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
