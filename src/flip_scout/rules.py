"""Free text filters and the final decision from the AI's structured answer.

The AI only delivers data. Whether a listing is reported is decided here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import Profile
from .store import Search

NO_MODEL = "keins"


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def find_word(text: str, words: list[str]) -> str | None:
    """Returns the first word that occurs in text (case-insensitive substring)."""
    hay = _norm(text)
    for word in words:
        w = _norm(word) if word.strip() else ""
        if w and w in hay:
            return word
    return None


def price_limit(profile: Profile, search: Search) -> float | None:
    """Highest price worth looking at: the search's own limit, else the highest
    price in the profile's table (models not in the table are never reported)."""
    if search.max_price is not None:
        return search.max_price
    return max(profile.max_prices.values()) if profile.max_prices else None


def seller_problem(item, no_feedback_max_price: float | None, min_feedback: int = 0) -> str | None:
    """Reason to skip because of the seller, or None. Unknown ratings never block."""
    if item.seller_feedback is None:
        return None
    if item.seller_feedback < min_feedback:
        return f"Verkäufer hat weniger als {min_feedback} Bewertung(en)"
    if no_feedback_max_price is not None and item.seller_feedback == 0:
        if item.price > no_feedback_max_price:
            return f"Verkäufer ohne Bewertungen, Preis über {no_feedback_max_price:g} €"
    return None


def _model_text(text: str) -> str:
    """'Apple iPhone13 Pro-Max!' -> ' apple iphone 13 pro max ' (spaces around tokens)."""
    text = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
    text = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", text)
    return f" {' '.join(text.split())} "


def title_model(profile: Profile, title: str) -> str | None:
    """The one model named in the title: a price key, a skip model, or None when no
    model or several different models are named (then the AI decides)."""
    names = [*profile.max_prices, *profile.title_skip_models]
    hay = _model_text(title)
    spans = []
    for name in names:
        needle = _model_text(name)
        start = hay.find(needle)
        while start != -1:
            spans.append((start, start + len(needle), name))
            start = hay.find(needle, start + 1)
    # drop matches inside a longer one ('iPhone 13' inside 'iPhone 13 Pro')
    kept = {
        n
        for s, e, n in spans
        if not any(s2 <= s and e <= e2 and (e2 - s2) > (e - s) for s2, e2, _ in spans)
    }
    return kept.pop() if len(kept) == 1 else None


def title_problem(profile: Profile, title: str, price: float) -> str | None:
    """Free check before page load and AI: a model clearly named in the title that is
    not wanted or too expensive. None = no decision, the normal checks follow."""
    if not profile.title_models:
        return None
    model = title_model(profile, title)
    if model is None:
        return None
    if model not in profile.max_prices:
        return f"Titel: {model} nicht in Preisliste"
    limit = profile.max_prices[model]
    if price > limit:
        return f"Titel: {model}: {price:.0f} € > {limit:.0f} €"
    return None


def keyword_hit(profile: Profile, text: str) -> bool:
    hay = _norm(text)
    if profile.must_include and all(_norm(w) in hay for w in profile.must_include):
        return True
    return bool(profile.any_include and find_word(text, profile.any_include))


@dataclass
class Decision:
    notify: bool
    reason: str = ""
    warnings: list[str] = field(default_factory=list)


def decide(profile: Profile, search: Search, price: float, ai: dict, final: bool) -> Decision:
    """final=False: after the text check (unclear -> look at photos).
    final=True: after the photo check (unclear -> report, with questions)."""
    passt = ai.get("passt", "unklar")
    if passt == "nein":
        return Decision(False, "KI: passt nicht")
    if profile.no_go and ai.get("no_go"):
        return Decision(False, f"No-Go: {ai.get('no_go_grund') or 'ja'}")
    if profile.ok_defects and ai.get("defekte") and ai.get("defekte_ok") is False:
        return Decision(False, f"Defekte nicht okay: {', '.join(ai['defekte'])}")

    warnings: list[str] = []
    if profile.check_icloud:
        icloud = ai.get("icloud", "unklar")
        if icloud == "gesperrt":
            return Decision(False, "iCloud gesperrt")
        if icloud == "unklar":
            warnings.append("❓ iCloud: unklar – Verkäufer fragen!")

    if profile.max_prices:
        key = ai.get("modell_key") or NO_MODEL
        if key in profile.max_prices:
            limit = profile.max_prices[key]
            if price > limit:
                return Decision(False, f"{key}: {price:.0f} € > {limit:.0f} €")
        elif passt == "unklar" and not final:
            pass  # model not recognisable from the text alone: the photo check decides
        elif search.max_price is None:
            return Decision(False, f"Modell nicht in Preistabelle ({ai.get('modell') or '?'})")
        else:
            warnings.append(f"ℹ️ Modell nicht in Preistabelle – nur Grenze {search.max_price:.0f} €")

    if passt == "unklar" and final:
        warnings.append("❓ KI unsicher – selbst prüfen")
    return Decision(True, "passt" if passt == "ja" else "unklar", warnings)
