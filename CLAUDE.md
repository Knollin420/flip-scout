# flip-scout – Projektstand und Regeln für Claude

Telegram-Bot, der Vinted (vinted.at) alle 5 Minuten durchsucht und neue passende Anzeigen
mit KI-Briefing meldet. Gekauft wird immer manuell. Der Besitzer (Colin, Wien) spricht
**Deutsch**: Antworten und Erklärungen auf Deutsch, einfach und Schritt für Schritt.
Code, Commits und README auf Englisch.

## Regeln für die Arbeit auf dem Server (1blu-VPS, Ubuntu 24.04)

- Auf dem Server läuft **ein weiteres Tool für die Uni**. Nichts davon ändern, verschieben,
  stoppen oder löschen. Vor jeder Änderung außerhalb von `/opt/flip-scout` fragen.
- flip-scout bleibt **komplett in `/opt/flip-scout`** (Code, `.venv`, `data/`, `.env`,
  `profiles.toml`). Einzige Ausnahme: die systemd-Dienstdatei
  `/etc/systemd/system/flip-scout.service`.
- flip-scout soll als eigener Systembenutzer `flipscout` laufen, nicht als root.
- Keine globalen pip-Installationen, nur die venv `/opt/flip-scout/.venv`.
- Geheimnisse (`.env`: Telegram-Token, Anthropic-Key) nie ausgeben, loggen oder committen.
  `.env` mit `chmod 600`, Besitzer `flipscout`.
- Der Bot darf mit demselben Telegram-Token **nur an einer Stelle** laufen. Wenn er auf dem
  Server läuft, muss er auf dem Windows-PC des Besitzers gestoppt sein.
- Vinted-Intervall nie unter 5 Minuten. Nicht mehr Suchbegriffe als nötig.

## Stand (November 2026)

- Code fertig, 48 Offline-Tests (`pytest`), CI auf GitHub (Knollin420/flip-scout).
- Windows-PC: Bot installiert in `C:\Users\anonym\flip-scout`, Telegram-Test und
  `--once` erfolgreich (Erst-Lauf hat 9 Suchbegriffe gemerkt).
- Server: geklont nach `/opt/flip-scout`, venv installiert, `profiles.toml` aus der
  Vorlage kopiert, **`--diagnose` komplett grün** (Vinted akzeptiert die Server-IP).
- Das Uni-Tool liegt in `/root/tuwel` (vermutlich per Cron). Nicht anfassen.
  Weitere Dienste: apache2, exim4, xinetd (Standard des Hosters), ebenfalls nicht anfassen.
- Dienstdatei und Anleitung: `deploy/server/` (systemd, Benutzer `flipscout`,
  ProtectSystem/ProtectHome). Updates als `flipscout`: `sudo -u flipscout git pull`.

## Nächste Schritte

1. `.env` auf dem Server anlegen (Werte vom Besitzer; er trägt sie selbst ein, z. B. mit
   `nano`, und zeigt sie nicht im Chat). `--test-notify` testen.
2. Benutzer `flipscout` anlegen, `/opt/flip-scout` ihm übergeben.
3. systemd-Dienst: `ExecStart=/opt/flip-scout/.venv/bin/python -m flip_scout
   --log-file /opt/flip-scout/data/flip-scout.log`, `WorkingDirectory=/opt/flip-scout`,
   `User=flipscout`, `Restart=always`, `RestartSec=30`. Dienstdatei als
   `deploy/server/flip-scout.service` ins Repo, Anleitung `deploy/server/INSTALL.md`.
4. Bot auf dem Windows-PC stoppen und Autostart dort nicht einrichten.
5. Offener Punkt: pro Anzeige wird nur 1 Foto gefunden (KI sollte bis zu 3 bekommen).
   Ursache prüfen: Die Suchantwort hat ein Feld `photos`. Prüfen, ob dort mehrere Fotos
   stehen, und ob die Anzeigenseite mehr als `og:image` hergibt.
   (`--diagnose` speichert eine Beispielantwort in `data/diagnose-search.json`.)
6. Später: Web-Dashboard (Trefferhistorie, Kosten), das der Besitzer sich gewünscht hat.

## Vinted-Schnittstelle (Stand September 2026)

- Suche: `GET https://api.vinted.at/svc-catalogue/items`. Die alte
  `/api/v2/catalog/items` liefert 404.
- Sitzung: Die Startseite liefert das Cookie `access_token_web` und den Header `x-anon-id`.
  Gesendet werden `X-Anon-Id`, optional `X-Csrf-Token`, `Authorization: Bearer <token>`,
  `Locale: de-AT`.
- Filter heißen `attribute_ids[catalog|brand|size|status|color|material]`. Gespeichert
  werden die alten Namen (`catalog_ids` …), übersetzt wird in `vinted.api_params`.
- Kein Upload-Zeitpunkt mehr. Schutz vor alten Anzeigen: Jeder neue Suchbegriff merkt
  sich beim ersten Lauf den Bestand, und nach einer Pause > 60 min wird neu gemerkt.
  `promoted`-Anzeigen werden übersprungen.
- Die Detail-API ist blockiert. Beschreibung und Fotos kommen von der Anzeigenseite
  (JSON-LD / OpenGraph).

## Architektur-Prinzipien (nicht ohne Rückfrage ändern)

- Die KI (Claude Haiku 5.5, Thinking aus, Structured Outputs) liefert nur JSON. Ob eine
  Anzeige gemeldet wird, entscheidet `rules.py`.
- Erst die kostenlosen Filter, dann Beschreibung, Stichwörter, KI-Text-Check und zuletzt
  der Foto-Check in voller Auflösung.
- Suchen werden per Telegram verwaltet (Link schicken, `/menu`). Profile stehen in
  `profiles.toml`.

## Befehle

```bash
.venv/bin/python -m flip_scout --diagnose      # Vinted-Verbindung prüfen
.venv/bin/python -m flip_scout --test-notify   # Telegram-Test
.venv/bin/python -m flip_scout --once          # ein Durchlauf
.venv/bin/pip install -e ".[dev]" && .venv/bin/pytest -q && .venv/bin/ruff check src tests
```
