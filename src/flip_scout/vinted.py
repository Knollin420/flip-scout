"""Vinted access via the internal JSON API, without login.

Since September 2026 the catalogue search lives on a separate host:
  GET https://api.vinted.<tld>/svc-catalogue/items
- Session: one request to the www start page gives the anonymous ``access_token_web``
  cookie and an anonymous id (``X-Anon-Id`` header or ``anon_id`` cookie).
- Filters are named ``attribute_ids[catalog]`` etc. instead of ``catalog_ids``.
- The JSON item detail API is blocked by bot protection, so the description and photos
  come from the public item page (JSON-LD / OpenGraph tags).
- curl_cffi with a Chrome TLS fingerprint. On 401/403: new session, then retry.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin

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


# Our stored filter names (as in old browser links) -> svc-catalogue parameter names
API_PARAM_NAMES = {
    "catalog_ids": "attribute_ids[catalog]",
    "brand_ids": "attribute_ids[brand]",
    "size_ids": "attribute_ids[size]",
    "status_ids": "attribute_ids[status]",
    "color_ids": "attribute_ids[color]",
    "material_ids": "attribute_ids[material]",
}


def api_params(params: dict[str, str]) -> dict[str, str]:
    """Translates stored filters to the new API names and drops empty values
    (the API answers 400 for blank parameters)."""
    return {API_PARAM_NAMES.get(k, k): v for k, v in params.items() if str(v).strip()}


def parse_catalog_item(raw: dict, base_url: str) -> Item:
    price, currency = _price(raw.get("price"))
    photo = raw.get("photo") or {}
    photos = [_photo_url(p) for p in raw.get("photos") or [] if _photo_url(p)]
    if not photos and _photo_url(photo):
        photos = [_photo_url(photo)]
    ts = (photo.get("high_resolution") or {}).get("timestamp")
    item_id = int(raw["id"])
    title = raw.get("title") or ""

    # Brand/size/condition: old flat fields, otherwise the new item_box lines
    box = raw.get("item_box") or {}
    brand = raw.get("brand_title") or ""
    size = raw.get("size_title") or ""
    condition = raw.get("status") if isinstance(raw.get("status"), str) else ""
    first, second = (box.get("first_line") or "").strip(), (box.get("second_line") or "").strip()
    if not brand and first and first != title:
        brand = first
    if second and not (size or condition):
        parts = [p.strip() for p in re.split(r"\s+[·•|]\s+", second) if p.strip()]
        if len(parts) >= 2:
            size, condition = parts[0], parts[-1]
        elif parts:
            condition = parts[0]

    link = raw.get("url") or raw.get("path") or f"/items/{item_id}"
    return Item(
        id=item_id,
        title=title,
        price=price,
        currency=currency,
        brand=brand,
        size=size,
        condition=condition,
        url=urljoin(base_url + "/", link),
        photo_urls=photos,
        created_ts=float(ts) if ts else None,
        reserved=bool(raw.get("is_reserved")),
        closed=bool(raw.get("is_closed") or raw.get("is_hidden")),
    )


_OG_DESC = re.compile(r'<meta[^>]+property="og:description"[^>]+content="([^"]*)"', re.I)
_LD_JSON = re.compile(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.I | re.S)


_OG_IMAGE = re.compile(r'<meta[^>]+property="og:image"[^>]+content="([^"]*)"', re.I)
_CSRF = re.compile(r'CSRF_TOKEN\\?"\s*:\s*\\?"([^"\\]+)')


def _ld_entries(page: str) -> list[dict]:
    entries: list[dict] = []
    for block in _LD_JSON.findall(page):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        entries += [e for e in (data if isinstance(data, list) else [data]) if isinstance(e, dict)]
    return entries


def description_from_page(page: str) -> str:
    """Extracts the description from the public item page."""
    for entry in _ld_entries(page):
        if entry.get("description"):
            return html.unescape(str(entry["description"])).strip()
    match = _OG_DESC.search(page)
    return html.unescape(match.group(1)).strip() if match else ""


def photos_from_page(page: str) -> list[str]:
    """All photo URLs of the item page (JSON-LD image list, else og:image)."""
    urls: list[str] = []
    for entry in _ld_entries(page):
        images = entry.get("image")
        for img in images if isinstance(images, list) else [images]:
            url = img.get("url") if isinstance(img, dict) else img
            if isinstance(url, str) and url.startswith("http") and url not in urls:
                urls.append(html.unescape(url))
    if not urls:
        urls = [html.unescape(u) for u in _OG_IMAGE.findall(page)]
    return urls


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
    SEARCH_PATH = "/svc-catalogue/items"

    def __init__(self, base_url: str, timeout: float = 20.0, session=None):
        self.base_url = base_url.rstrip("/")
        self.api_url = self.base_url.replace("://www.", "://api.", 1)
        self.timeout = timeout
        if session is None:
            from curl_cffi import requests as cffi_requests

            session = cffi_requests.Session(impersonate="chrome")
        self.session = session
        self._has_session = False
        self.token = ""
        self.anon_id = ""
        self.csrf = ""
        self.use_bearer = True

    # --- session ----------------------------------------------------------

    def refresh_cookie(self) -> None:
        """Opens the start page like a browser: cookie, anonymous id, CSRF token."""
        self.session.cookies.clear()
        try:
            resp = self.session.get(self.base_url + "/", timeout=self.timeout)
        except Exception as exc:  # network errors from curl_cffi
            raise VintedError(f"Startseite nicht erreichbar: {exc}") from exc
        if resp.status_code >= 400:
            raise VintedError(f"Startseite: HTTP {resp.status_code}")
        cookies = self.session.cookies
        if "access_token_web" not in cookies.keys():
            raise VintedError("Kein access_token_web-Cookie erhalten (Bot-Schutz?)")
        self.token = cookies.get("access_token_web") or ""
        headers = getattr(resp, "headers", {}) or {}
        self.anon_id = headers.get("x-anon-id") or cookies.get("anon_id") or ""
        match = _CSRF.search(getattr(resp, "text", "") or "")
        self.csrf = match.group(1) if match else ""
        self._has_session = True
        log.info(
            "Neue Vinted-Sitzung (Anon-ID: %s, CSRF: %s)",
            "ja" if self.anon_id else "nein",
            "ja" if self.csrf else "nein",
        )

    def api_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "de-AT,de;q=0.9,en;q=0.8",
            "Origin": self.base_url,
            "Referer": self.base_url + "/catalog",
            "Platform": "web",
        }
        if self.anon_id:
            headers["X-Anon-Id"] = self.anon_id
        if self.csrf:
            headers["X-Csrf-Token"] = self.csrf
        if self.use_bearer and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _api_get(self, path: str, params: dict | None = None):
        """GET on the api host. 401/403: new session; still refused: other auth variant."""
        for attempt in range(3):
            if not self._has_session:
                self.refresh_cookie()
            try:
                resp = self.session.get(
                    self.api_url + path,
                    params=params,
                    timeout=self.timeout,
                    headers=self.api_headers(),
                )
            except Exception as exc:  # network errors from curl_cffi
                raise VintedError(f"Netzwerkfehler: {exc}") from exc
            if resp.status_code in (401, 403) and attempt < 2:
                log.info("HTTP %s – neue Sitzung und neuer Versuch", resp.status_code)
                self._has_session = False
                if attempt == 1:
                    self.use_bearer = not self.use_bearer
                continue
            if resp.status_code != 200:
                detail = (getattr(resp, "text", "") or "")[:200].replace("\n", " ")
                raise VintedError(f"{path}: HTTP {resp.status_code} {detail}".strip())
            return resp
        raise VintedError(f"{path}: Zugriff verweigert")

    # --- search / details -------------------------------------------------

    def search(
        self,
        term: str,
        params: dict[str, str],
        price_from: float | None = None,
        price_to: float | None = None,
        per_page: int = 48,
    ) -> list[Item]:
        query = {"page": 1, "per_page": per_page, "order": "newest_first", **api_params(params)}
        if term:
            query["search_text"] = term
        if price_from is not None:
            query["price_from"] = f"{price_from:g}"
        if price_to is not None:
            query["price_to"] = f"{price_to:g}"
        resp = self._api_get(self.SEARCH_PATH, query)
        try:
            data = resp.json()
        except ValueError as exc:
            raise VintedError("Antwort ist kein JSON (Bot-Schutz-Seite?)") from exc
        return [parse_catalog_item(raw, self.base_url) for raw in data.get("items", [])]

    def load_details(self, item: Item) -> None:
        """Description and all photos from the public item page. Never raises."""
        try:
            resp = self.session.get(item.url, timeout=self.timeout)
        except Exception as exc:
            log.info("Anzeigenseite für %s nicht ladbar: %s", item.id, exc)
            return
        if resp.status_code != 200:
            log.info("Keine Beschreibung für %s (HTTP %s)", item.id, resp.status_code)
            return
        page = resp.text
        photos = photos_from_page(page)
        if len(photos) > len(item.photo_urls):
            item.photo_urls = photos
        desc = description_from_page(page)
        if desc:
            item.description, item.description_source = desc, "page"
        else:
            log.info("Keine Beschreibung auf der Anzeigenseite von %s", item.id)

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
