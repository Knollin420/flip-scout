"""--diagnose: checks the Vinted connection step by step and saves a sample answer.

Prints no secrets (no cookie or token values). The sample file contains only public
listing data and helps to adapt the parser when Vinted changes its API.
"""

from __future__ import annotations

import json
from pathlib import Path

from .vinted import VintedClient, VintedError, api_params


def run_diagnose(client: VintedClient, out_dir: Path, term: str = "iphone") -> bool:
    out_dir.mkdir(parents=True, exist_ok=True)
    ok = True
    print(f"1) Startseite {client.base_url}")
    try:
        client.refresh_cookie()
    except VintedError as exc:
        print(f"   ❌ {exc}")
        return False
    print(
        f"   ✅ Cookie ok · Anon-ID: {'ja' if client.anon_id else 'NEIN'} · "
        f"CSRF: {'ja' if client.csrf else 'nein'}"
    )

    print(f"2) Suche auf {client.api_url}{client.SEARCH_PATH}")
    sample = None
    for bearer in (True, False):
        client.use_bearer = bearer
        label = "mit Bearer-Token" if bearer else "nur Cookie"
        try:
            resp = client.session.get(
                client.api_url + client.SEARCH_PATH,
                timeout=client.timeout,
                headers=client.api_headers(),
                params={"search_text": term, "page": 1, "per_page": 48, "order": "newest_first"},
            )
        except Exception as exc:
            print(f"   ❌ {label}: {exc}")
            continue
        status = resp.status_code
        body = (resp.text or "")[:150].replace("\n", " ")
        if status == 200:
            data = resp.json()
            items = data.get("items", [])
            print(f"   ✅ {label}: HTTP 200, {len(items)} Anzeigen")
            if sample is None:
                sample = data
                (out_dir / "diagnose-search.json").write_text(
                    json.dumps({**data, "items": items[:3]}, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                break
        else:
            print(f"   ❌ {label}: HTTP {status} {body}")
    if sample is None:
        return False

    items = sample.get("items", [])
    if items:
        print(f"   Felder einer Anzeige: {', '.join(sorted(items[0].keys()))}")
    print("3) Erkannte Anzeigen (so liest der Bot sie):")
    from .vinted import parse_catalog_item

    parsed = [parse_catalog_item(raw, client.base_url) for raw in items[:3]]
    for it in parsed:
        age = f"{it.age_minutes():.0f} min alt" if it.created_ts else "Alter unbekannt"
        print(
            f"   • {it.title[:50]} | {it.price:.2f} {it.currency} | {it.brand or '-'} | "
            f"{it.size or '-'} | {it.condition or '-'} | {age}"
        )
        print(f"     {it.url}")

    print("4) Filter (Zustand + Preis bis 100 €)")
    try:
        filtered = client.search(term, {"status_ids": "2"}, price_to=100)
        cheap = all(i.price <= 100 for i in filtered)
        print(
            f"   {'✅' if cheap else '⚠️'} {len(filtered)} Anzeigen, "
            f"alle ≤ 100 €: {'ja' if cheap else 'NEIN'}"
        )
        print(f"   Parameter: {api_params({'status_ids': '2'})}")
    except VintedError as exc:
        ok = False
        print(f"   ❌ {exc}")

    if parsed:
        print("5) Anzeigenseite (Beschreibung + Fotos)")
        item = parsed[0]
        client.load_details(item)
        desc = item.description.replace("\n", " ")[:120]
        print(f"   {'✅' if desc else '❌'} Beschreibung: {desc or 'nicht gefunden'}")
        print(f"   Fotos: {len(item.photo_urls)}")
        if item.photo_urls:
            photo = client.download_photo(item.photo_urls[0])
            print(
                f"   {'✅' if photo else '❌'} Foto-Download: "
                f"{len(photo[0]) // 1024 if photo else 0} KB"
            )
    print(f"\nBeispielantwort gespeichert: {out_dir / 'diagnose-search.json'}")
    return ok
