"""Translates a Vinted browser link into API search parameters.

https://www.vinted.at/catalog?search_text=switch&catalog[]=3026&brand_ids[]=1&price_to=180
  -> domain "at", term "switch", params {"catalog_ids": "3026", "brand_ids": "1"},
     price_to 180.0
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlparse

# Browser parameter name -> API parameter name (everything else: strip "[]")
_RENAME = {"catalog": "catalog_ids"}
_ATTRIBUTE = {
    "catalog": "catalog_ids",
    "brand": "brand_ids",
    "size": "size_ids",
    "status": "status_ids",
    "color": "color_ids",
    "material": "material_ids",
}
# Parameters that belong to the browser session, not to the search itself
_IGNORE = {
    "order",
    "time",
    "page",
    "per_page",
    "search_id",
    "search_by_image_uuid",
    "disabled_personalization",
    "currency",
    "search_text",
    "price_from",
    "price_to",
}

LINK_RE = re.compile(r"https?://(?:www\.)?vinted\.[a-z.]+/\S+", re.IGNORECASE)


@dataclass
class ImportedSearch:
    domain: str
    term: str
    params: dict[str, str] = field(default_factory=dict)
    price_from: float | None = None
    price_to: float | None = None


def find_vinted_link(text: str) -> str | None:
    match = LINK_RE.search(text or "")
    return match.group(0) if match else None


def is_catalog_link(url: str) -> bool:
    parsed = urlparse(url)
    return "vinted." in parsed.netloc and (
        parsed.path.rstrip("/").endswith("/catalog")
        or "/catalog/" in parsed.path
        or "search_text=" in parsed.query
    )


def _to_float(value: str) -> float | None:
    try:
        return float(value.replace(",", "."))
    except (ValueError, AttributeError):
        return None


def parse_vinted_url(url: str) -> ImportedSearch:
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()
    if "vinted." not in host:
        raise ValueError("Das ist kein Vinted-Link.")
    domain = host.split("vinted.", 1)[1]

    collected: dict[str, list[str]] = {}
    term = ""
    price_from = price_to = None
    for key, value in parse_qsl(parsed.query, keep_blank_values=False):
        base = key[:-2] if key.endswith("[]") else key
        if base == "search_text":
            term = value.strip()
            continue
        if base == "price_from":
            price_from = _to_float(value)
            continue
        if base == "price_to":
            price_to = _to_float(value)
            continue
        if base in _IGNORE:
            continue
        # newer links use attribute_ids[catalog] etc. -> store as catalog_ids
        attr = re.fullmatch(r"attribute_ids\[(\w+)\]", base)
        if attr:
            base = _ATTRIBUTE.get(attr.group(1), base)
        name = _RENAME.get(base, base)
        collected.setdefault(name, [])
        if value not in collected[name]:
            collected[name].append(value)

    # Category links like /catalog/2329-smartphones
    match = re.search(r"/catalog/(\d+)", parsed.path)
    if match and "catalog_ids" not in collected:
        collected["catalog_ids"] = [match.group(1)]

    params = {k: ",".join(v) for k, v in collected.items()}
    return ImportedSearch(
        domain=domain, term=term, params=params, price_from=price_from, price_to=price_to
    )
