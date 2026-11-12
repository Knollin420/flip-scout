"""Claude check: reads listing text (and photos) and returns structured JSON.

- Thinking disabled: extraction does not need it and it would cost output tokens.
- Structured outputs (output_config.format) guarantee valid JSON in our schema.
- Photos are sent in the resolution Vinted delivers (no downscaling), so that
  text on screens (battery health, iCloud, serial numbers) stays readable.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass

from .config import Profile
from .rules import NO_MODEL
from .vinted import Item

log = logging.getLogger(__name__)


class AIRetryLater(Exception):
    """Temporary API problem (rate limit, overload, network): try the item again later."""


class AIUnavailable(Exception):
    """Permanent problem (refusal, bad request, invalid key): skip this item."""


@dataclass
class AIResponse:
    data: dict
    input_tokens: int
    output_tokens: int


def build_schema(profile: Profile) -> dict:
    model_keys = [*profile.max_prices.keys(), NO_MODEL]
    text_list = {"type": "array", "items": {"type": "string"}}
    props = {
        "passt": {"type": "string", "enum": ["ja", "nein", "unklar"]},
        "modell": {"type": "string"},
        "modell_key": {"type": "string", "enum": model_keys},
        "zustand": {"type": "string"},
        "defekte": text_list,
        "defekte_ok": {"type": "boolean"},
        "no_go": {"type": "boolean"},
        "no_go_grund": {"type": "string"},
        "icloud": {"type": "string", "enum": ["frei", "gesperrt", "unklar", "egal"]},
        "positiv": text_list,
        "offen": text_list,
        "einschaetzung": {"type": "string"},
    }
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {x}" for x in items) if items else "- (keine Angabe)"


def build_system(profile: Profile) -> str:
    goal = profile.ai_check or (
        "Ein Käufer möchte dieses Produkt günstig kaufen. Erstelle ein kurzes Briefing."
    )
    parts = [
        "Du prüfst Second-Hand-Anzeigen von Vinted für einen Käufer in Österreich, der schnell "
        "entscheiden will, ob sich der Kauf lohnt. Antworte ausschließlich mit den geforderten "
        "JSON-Feldern, auf Deutsch, knapp (Stichworte).",
        "",
        "WICHTIGSTE REGEL: Erfinde nichts. Was weder im Text steht noch auf den Fotos klar zu "
        "sehen ist, gilt als unklar. Lieber 'unklar' als raten.",
        "",
        f"Was der Käufer sucht: {goal}",
    ]
    if profile.ok_defects or profile.no_go:
        parts += [
            "",
            "Diese Defekte sind für den Käufer OKAY:",
            _bullets(profile.ok_defects),
            "",
            "NO-GOS (nur auf true setzen, wenn klar erkennbar, nicht bei bloßem Verdacht):",
            _bullets(profile.no_go),
        ]
    if profile.max_prices:
        parts += [
            "",
            "modell_key: Ordne das Gerät EXAKT einem dieser Schlüssel zu, sonst "
            f"'{NO_MODEL}'. Varianten sind verschiedene Modelle (z.B. 'iPhone 13 Pro' ist "
            "nicht 'iPhone 13'). Bei Unsicherheit über das genaue Modell: "
            f"'{NO_MODEL}'.",
            ", ".join(profile.max_prices),
        ]
    else:
        parts += ["", f"modell_key: immer '{NO_MODEL}'."]
    if profile.check_icloud:
        parts += [
            "",
            "icloud (Aktivierungssperre/iCloud/'Mein iPhone suchen'/MDM):",
            "- 'gesperrt': Text oder Foto zeigt klar eine Sperre (z.B. 'iCloud gesperrt', "
            "'Aktivierungssperre', 'für Teile wegen iCloud', Sperrbildschirm der Aktivierung).",
            "- 'frei': ausdrücklich ausgeschlossen (z.B. 'iCloud frei', 'zurückgesetzt und "
            "abgemeldet', 'keine Sperre').",
            "- sonst 'unklar'.",
        ]
    else:
        parts += ["", "icloud: immer 'egal'."]
    parts += [
        "",
        "Felder:",
        "- passt: 'ja' = entspricht dem Gesuchten; 'nein' = klar nicht (Zubehör, Suchanzeige, "
        "anderes Produkt, Einzelteil); 'unklar' = nicht sicher.",
        "- modell: Modell mit Details aus der Anzeige (z.B. Speicher, Farbe).",
        "- zustand: ein kurzer Satz.",
        "- defekte: erkannte Defekte als Stichworte (leer, wenn keine).",
        "- defekte_ok: true, wenn alle erkannten Defekte zu den OKAY-Defekten passen "
        "(oder keine Defekte).",
        "- no_go / no_go_grund: siehe NO-GOS ('' wenn keins).",
        "- positiv: was gut ist (max. 3).",
        "- offen: was unklar ist = Fragen an den Verkäufer (max. 3).",
        "- einschaetzung: eine kurze Preiseinschätzung.",
    ]
    return "\n".join(parts)


def build_listing_text(item: Item) -> str:
    desc = item.description or "(Beschreibung nicht verfügbar – nur Titel bekannt)"
    return (
        f"Titel: {item.title}\n"
        f"Preis: {item.price:.2f} {item.currency}\n"
        f"Marke: {item.brand or '-'} | Größe: {item.size or '-'} | Zustand laut Vinted: "
        f"{item.condition or '-'}\n"
        f"Beschreibung:\n{desc}"
    )


class AIChecker:
    def __init__(self, api_key: str, model: str, client=None):
        self.model = model
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=90)
        self.client = client

    def check(
        self, profile: Profile, item: Item, images: list[tuple[bytes, str]] | None = None
    ) -> AIResponse:
        import anthropic

        content: list[dict] = []
        for i, (data, media_type) in enumerate(images or [], start=1):
            content.append({"type": "text", "text": f"Foto {i}:"})
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.standard_b64encode(data).decode("ascii"),
                    },
                }
            )
        task = (
            "Prüfe die Anzeige anhand von Text UND Fotos."
            if images
            else "Prüfe die Anzeige anhand des Textes (Fotos folgen ggf. später)."
        )
        content.append({"type": "text", "text": f"{task}\n\n{build_listing_text(item)}"})

        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=2000,
                thinking={"type": "disabled"},
                system=build_system(profile),
                messages=[{"role": "user", "content": content}],
                output_config={"format": {"type": "json_schema", "schema": build_schema(profile)}},
            )
        except (
            anthropic.RateLimitError,
            anthropic.InternalServerError,
            anthropic.APIConnectionError,
        ) as exc:
            raise AIRetryLater(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            raise AIUnavailable(f"HTTP {exc.status_code}: {exc.message}") from exc

        if resp.stop_reason == "refusal":
            raise AIUnavailable("KI hat die Anfrage abgelehnt")
        if resp.stop_reason == "max_tokens":
            raise AIUnavailable("KI-Antwort abgeschnitten")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise AIUnavailable("Ungültiges JSON") from exc
        return AIResponse(
            data=data, input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens
        )
