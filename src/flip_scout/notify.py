"""Telegram (and optional ntfy.sh) notifications. Uses only the standard library."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
import uuid

from .config import Profile
from .store import Search
from .vinted import Item

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


class TelegramError(Exception):
    pass


class TelegramAPI:
    def __init__(self, token: str, timeout: float = 30.0):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.timeout = timeout

    def call(
        self,
        method: str,
        params: dict | None = None,
        photo: tuple[bytes, str] | None = None,
        timeout: float | None = None,
    ):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if photo:
            body, content_type = _multipart(params, photo)
        else:
            body, content_type = json.dumps(params).encode(), "application/json"
        req = urllib.request.Request(
            self.base + method, data=body, headers={"Content-Type": content_type}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read())
            except ValueError:
                raise TelegramError(f"{method}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TelegramError(f"{method}: {exc}") from exc
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description')}")
        return data.get("result")


def _multipart(params: dict, photo: tuple[bytes, str]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for key, value in params.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value)
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    data, media_type = photo
    ext = media_type.split("/")[-1]
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; '
        f'filename="photo.{ext}"\r\nContent-Type: {media_type}\r\n\r\n'.encode()
    )
    parts.append(data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def format_hit(
    search: Search,
    profile: Profile,
    item: Item,
    ai: dict | None,
    warnings: list[str],
    limit: int = CAPTION_LIMIT,
) -> str:
    """Builds the hit message. The link always stays at the end, the middle gets cut."""
    head = [
        f"{profile.emoji} {search.name}",
        item.title,
        f"💶 {item.price:.2f} € · {item.brand or '-'} · {item.size or '-'}",
    ]
    body: list[str] = []
    if ai:
        model = ai.get("modell") or ""
        zustand = ai.get("zustand") or ""
        if model or zustand:
            body.append("🔎 " + " – ".join(x for x in (model, zustand) if x))
        if ai.get("defekte"):
            body.append("⚠️ Defekt: " + ", ".join(ai["defekte"]))
        if profile.check_icloud and ai.get("icloud") == "frei":
            body.append("✅ iCloud: frei laut Anzeige")
    body += warnings
    if ai:
        body += [f"✅ {p}" for p in ai.get("positiv", [])]
        body += [f"❓ {o}" for o in ai.get("offen", [])]
        if ai.get("einschaetzung"):
            body.append(f"💡 {ai['einschaetzung']}")
    if item.description_source == "":
        body.append("ℹ️ Beschreibung nicht geladen")

    tail = item.url
    text = "\n".join(head + body + [tail])
    while len(text) > limit and body:
        body.pop()
        text = "\n".join(head + body + ["…", tail])
    if len(text) > limit:
        text = _cut("\n".join(head), limit - len(tail) - 1) + "\n" + tail
    return text


class Notifier:
    def __init__(
        self,
        api: TelegramAPI | None,
        chat_id: str,
        ntfy_topic: str = "",
        retries: int = 3,
        retry_wait: float = 5.0,
    ):
        self.api = api
        self.chat_id = chat_id
        self.ntfy_topic = ntfy_topic
        self.retries = retries
        self.retry_wait = retry_wait

    def _with_retry(self, fn) -> bool:
        for attempt in range(1, self.retries + 1):
            try:
                fn()
                return True
            except Exception as exc:
                log.warning("Senden fehlgeschlagen (Versuch %d/%d): %s", attempt, self.retries, exc)
                if attempt < self.retries:
                    time.sleep(self.retry_wait * attempt)
        return False

    def send_text(self, text: str, buttons: list[list[dict]] | None = None) -> bool:
        ok = True
        if self.api:
            markup = {"inline_keyboard": buttons} if buttons else None
            ok = self._with_retry(
                lambda: self.api.call(
                    "sendMessage",
                    {
                        "chat_id": self.chat_id,
                        "text": _cut(text, TEXT_LIMIT),
                        "disable_web_page_preview": True,
                        "reply_markup": markup,
                    },
                )
            )
        self._ntfy(text)
        return ok

    def send_hit(
        self,
        text: str,
        photo: tuple[bytes, str] | None,
        photo_url: str,
        buttons: list[list[dict]] | None = None,
    ) -> bool:
        """Photo with caption; if the photo fails: text only. True = delivered."""
        if not self.api:
            self._ntfy(text)
            return True
        markup = {"inline_keyboard": buttons} if buttons else None
        caption = _cut(text, CAPTION_LIMIT)

        def send() -> None:
            params = {"chat_id": self.chat_id, "caption": caption, "reply_markup": markup}
            try:
                if photo:
                    self.api.call("sendPhoto", params, photo=photo, timeout=60)
                elif photo_url:
                    self.api.call("sendPhoto", {**params, "photo": photo_url})
                else:
                    raise TelegramError("kein Foto")
            except TelegramError as exc:
                log.info("Foto nicht gesendet (%s) – sende nur Text", exc)
                self.api.call(
                    "sendMessage", {"chat_id": self.chat_id, "text": text, "reply_markup": markup}
                )

        ok = self._with_retry(send)
        self._ntfy(text)
        return ok

    def _ntfy(self, text: str) -> None:
        if not self.ntfy_topic:
            return
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{self.ntfy_topic}", data=text.encode("utf-8")
            )
            urllib.request.urlopen(req, timeout=15).close()
        except Exception as exc:
            log.info("ntfy fehlgeschlagen: %s", exc)
