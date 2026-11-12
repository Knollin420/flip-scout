"""Free text filters and the final decision from the AI's structured answer.

The AI only delivers data. Whether a listing is reported is decided here.
"""

from __future__ import annotations

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
    if ai.get("no_go"):
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
        elif search.max_price is None:
            return Decision(False, f"Modell nicht in Preistabelle ({ai.get('modell') or '?'})")
        else:
            warnings.append(f"ℹ️ Modell nicht in Preistabelle – nur Grenze {search.max_price:.0f} €")

    if passt == "unklar" and final:
        warnings.append("❓ KI unsicher – selbst prüfen")
    return Decision(True, "passt" if passt == "ja" else "unklar", warnings)
