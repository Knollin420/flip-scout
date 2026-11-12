"""Start message, daily heartbeat, block alarm and the status text."""

from __future__ import annotations

import logging
import time

from .config import Config
from .notify import Notifier
from .pipeline import RunResult
from .store import Store

log = logging.getLogger(__name__)


def cost_usd(cfg: Config, stats: dict[str, float]) -> float:
    return (
        stats.get("tokens_in", 0) * cfg.ai.price_input_per_mtok
        + stats.get("tokens_out", 0) * cfg.ai.price_output_per_mtok
    ) / 1_000_000


def status_text(cfg: Config, store: Store, now: float | None = None) -> str:
    now = now or time.time()
    day = cfg.settings.day(now)
    yesterday = cfg.settings.day(now - 86400)
    st, sy = store.stats(day), store.stats(yesterday)
    searches = store.list_searches()
    active = sum(1 for s in searches if s.active)

    last = store.get("last_run_ts")
    if last:
        t = cfg.settings.local_time(float(last)).strftime("%H:%M")
        state = "✅ ok" if store.get("last_run_ok") == "1" else "⚠️ fehlgeschlagen"
        last_line = f"Letzter Durchlauf: {t} Uhr – {state}"
    else:
        last_line = "Letzter Durchlauf: noch keiner"

    ai_line = (
        f"KI-Aufrufe: {st.get('ai_calls', 0):.0f} / {cfg.ai.max_checks_per_day}"
        if cfg.ai_available
        else "KI: aus (kein API-Key oder deaktiviert)"
    )
    return "\n".join(
        [
            "📊 Status",
            last_line,
            f"Suchen: {active} aktiv, {len(searches) - active} pausiert",
            "",
            "Heute:",
            f"Neue Anzeigen geprüft: {st.get('checked', 0):.0f}",
            ai_line,
            f"Treffer: {st.get('hits', 0):.0f}",
            f"KI-Kosten: ca. ${cost_usd(cfg, st):.3f}",
            "",
            f"Gestern: {sy.get('checked', 0):.0f} geprüft · {sy.get('ai_calls', 0):.0f} KI · "
            f"{sy.get('hits', 0):.0f} Treffer · ${cost_usd(cfg, sy):.3f}",
        ]
    )


class Monitor:
    def __init__(self, cfg: Config, store: Store, notifier: Notifier, now=time.time):
        self.cfg = cfg
        self.store = store
        self.notifier = notifier
        self.now = now
        self.failures = 0

    def started(self) -> None:
        last = float(self.store.get("last_start_msg", "0") or 0)
        if self.now() - last < 3600:
            log.info("Start-Meldung übersprungen (letzte vor < 1 h)")
            return
        active = len(self.store.list_searches(active_only=True))
        ai = "mit KI" if self.cfg.ai_available else "ohne KI"
        if self.notifier.send_text(
            f"🚀 flip-scout gestartet – {active} aktive Suchen, {ai}.\n/menu zum Verwalten"
        ):
            self.store.set("last_start_msg", str(self.now()))

    def after_run(self, result: RunResult) -> None:
        self.store.set("last_run_ts", str(self.now()))
        self.store.set("last_run_ok", "0" if result.failed else "1")
        threshold = self.cfg.settings.alarm_after_failed_runs
        if result.failed:
            self.failures += 1
            log.warning("Durchlauf fehlgeschlagen (%d in Folge)", self.failures)
            if self.failures == threshold:
                self.notifier.send_text(
                    f"🚨 Vinted blockiert evtl. – {self.failures} Durchläufe hintereinander "
                    "fehlgeschlagen. Ich versuche es weiter und melde mich, wenn es wieder geht."
                )
        else:
            if self.failures >= threshold:
                self.notifier.send_text("✅ Vinted funktioniert wieder.")
            self.failures = 0

    def maybe_heartbeat(self) -> None:
        now = self.now()
        local = self.cfg.settings.local_time(now)
        today = local.date().isoformat()
        if (
            local.hour < self.cfg.settings.heartbeat_hour
            or self.store.get("heartbeat_day") == today
        ):
            return
        y = self.store.stats(self.cfg.settings.day(now - 86400))
        text = (
            f"✅ flip-scout läuft\nGestern: {y.get('checked', 0):.0f} Anzeigen geprüft · "
            f"{y.get('ai_calls', 0):.0f} KI-Aufrufe · {y.get('hits', 0):.0f} Treffer · "
            f"ca. ${cost_usd(self.cfg, y):.2f}"
        )
        if self.notifier.send_text(text):
            self.store.set("heartbeat_day", today)
