"""flip-scout entry point.

python -m flip_scout                 run forever (search loop + Telegram control)
python -m flip_scout --once          one search run, then exit
python -m flip_scout --test-notify   send a test message to Telegram
"""

from __future__ import annotations

import argparse
import logging
import random
import signal
import socket
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import load_config
from .store import Store

log = logging.getLogger("flip_scout")


MIN_GAP_SECONDS = 30.0


def seconds_until_next_run(
    settings, elapsed: float, rand=random.uniform, tick_minutes: float | None = None
) -> float:
    """Fixed rhythm: a run starts every tick (plus jitter), no matter how long the
    previous run took. At least MIN_GAP_SECONDS pause after a very long run."""
    tick = 60 * (tick_minutes or settings.interval_minutes)
    jitter = min(settings.jitter_seconds, 0.15 * tick)
    return max(MIN_GAP_SECONDS, tick + rand(-jitter, jitter) - elapsed)


def acquire_instance_lock(port: int = 47913) -> socket.socket | None:
    """Only one running bot per computer (two would send every alert twice).

    Holding a local port works the same on Windows, Linux and Android; the OS frees it
    automatically when the process ends, even after a crash.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        sock.close()
        return None
    return sock


def build(args: argparse.Namespace):
    from .ai import AIChecker
    from .monitor import Monitor
    from .notify import Notifier, TelegramAPI
    from .pipeline import Pipeline
    from .vinted import VintedClient

    cfg = load_config(Path(args.config), Path(args.env), Path(args.data) if args.data else None)
    store = Store(cfg.data_dir / "flip-scout.db")

    api = TelegramAPI(cfg.secrets.telegram_token) if cfg.secrets.telegram_token else None
    if api is None:
        log.warning("Kein TELEGRAM_TOKEN – Treffer erscheinen nur im Log.")
    notifier = Notifier(api, cfg.secrets.telegram_chat_id)
    ai = AIChecker(cfg.secrets.anthropic_api_key, cfg.ai.model) if cfg.ai_available else None
    if ai is None:
        log.warning("KI aus – nur Stichwort- und Filter-Treffer ohne Briefing.")
    willhaben = messenger = None
    if cfg.settings.willhaben_enabled:
        from .willhaben import WillhabenClient, WillhabenMessenger

        s = cfg.settings
        willhaben = WillhabenClient(s.willhaben_area_id, s.willhaben_rows, s.willhaben_private_only)
        if s.willhaben_auto_message and cfg.secrets.willhaben_cookie:
            messenger = WillhabenMessenger(cfg.secrets.willhaben_cookie)
        elif s.willhaben_auto_message:
            log.warning("Kein WILLHABEN_COOKIE – keine automatischen willhaben-Nachrichten.")
    pipeline = Pipeline(
        cfg,
        store,
        VintedClient(cfg.settings.base_url),
        ai,
        notifier,
        willhaben=willhaben,
        messenger=messenger,
    )
    monitor = Monitor(cfg, store, notifier)
    return cfg, store, api, notifier, pipeline, monitor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="flip-scout", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", default="profiles.toml", help="Profile/Einstellungen")
    parser.add_argument("--env", default=".env", help="Datei mit Tokens und API-Key")
    parser.add_argument("--data", help="Datenordner (Standard: ./data)")
    parser.add_argument("--once", action="store_true", help="ein Durchlauf, dann Ende")
    parser.add_argument("--test-notify", action="store_true", help="Telegram-Testnachricht")
    parser.add_argument("--diagnose", action="store_true", help="Vinted-Verbindung prüfen")
    parser.add_argument("--log-file", help="zusätzlich in Datei loggen (rotiert ab 5 MB)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles: never crash on emojis
        sys.stdout.reconfigure(errors="replace")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if args.log_file:
        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(args.log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        )
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    if not Path(args.config).exists():
        log.error("%s fehlt (profiles.toml liegt im Repository)", args.config)
        return 2

    cfg, store, api, notifier, pipeline, monitor = build(args)

    if args.test_notify:
        if api is None or not cfg.secrets.telegram_chat_id:
            log.error("TELEGRAM_TOKEN oder TELEGRAM_CHAT_ID fehlt in %s", args.env)
            return 1
        ok = notifier.send_text("🧪 Testnachricht von flip-scout – Telegram funktioniert!")
        log.info("Testnachricht %s", "gesendet" if ok else "FEHLGESCHLAGEN")
        return 0 if ok else 1

    if args.diagnose:
        from .diagnose import run_diagnose

        return 0 if run_diagnose(pipeline.vinted, cfg.data_dir) else 1

    if args.once:
        result = pipeline.run_once()
        log.info(
            "Fertig: %d Anfragen ok, %d fehlgeschlagen, %d neue Anzeigen, %d Treffer",
            result.requests_ok,
            result.requests_failed,
            result.new_items,
            result.hits,
        )
        return 1 if result.failed else 0

    lock = acquire_instance_lock()
    if lock is None:
        log.error("flip-scout läuft bereits auf diesem Computer – zweiter Start abgebrochen.")
        return 3

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    if api is not None and cfg.secrets.telegram_chat_id:
        from .bot import TelegramBot

        bot = TelegramBot(cfg, store, api, cfg.secrets.telegram_chat_id)
        threading.Thread(target=bot.run, args=(stop,), name="telegram", daemon=True).start()

    monitor.started()
    pruned_day = ""
    fast = {p.label: p.interval_minutes for p in cfg.profiles.values() if p.interval_minutes}
    log.info(
        "flip-scout läuft – Intervall %g min%s",
        cfg.settings.interval_minutes,
        "".join(f", {label}: {m:g} min" for label, m in fast.items()),
    )
    while not stop.is_set():
        started = time.monotonic()
        try:
            result = pipeline.run_once(only_due=True)
            if result.requests_ok + result.requests_failed:  # no search due -> nothing to log
                log.info(
                    "Durchlauf: %d neue Anzeigen, %d Treffer (%d/%d Anfragen ok)",
                    result.new_items,
                    result.hits,
                    result.requests_ok,
                    result.requests_ok + result.requests_failed,
                )
                monitor.after_run(result)
            monitor.maybe_heartbeat()
            today = cfg.settings.day(time.time())
            if today != pruned_day:
                store.prune_seen(cfg.settings.seen_retention_days)
                pruned_day = today
        except Exception:
            log.exception("Unerwarteter Fehler im Durchlauf")
        elapsed = time.monotonic() - started
        stop.wait(seconds_until_next_run(cfg.settings, elapsed, tick_minutes=cfg.tick_minutes()))
    log.info("flip-scout beendet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
