# flip-scout – Projektstand und Regeln für Claude

Telegram-Bot, der Vinted (vinted.at) alle 3 Minuten durchsucht und neue passende Anzeigen
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
- Vinted-Intervall nie unter 3 Minuten (Besitzer hat am 18.11.2026 von 5 auf 3 gesenkt;
  bei Block-Warnungen zurück auf 5). Einzelne Profile dürfen per `interval_minutes` öfter
  suchen, nie unter 1 Minute (iPhone: 1 Minute, Besitzer-Entscheidung 18.11.2026).
  Nicht mehr Suchbegriffe als nötig.

## Stand (November 2026)

- Code fertig, Offline-Tests (`pytest`), CI auf GitHub (Knollin420/flip-scout).
- Server (1blu-VPS): Bot läuft **nur noch hier** in `/opt/flip-scout` als Benutzer
  `flipscout`, systemd-Dienst `flip-scout` (Dienstdatei und Anleitung in `deploy/server/`,
  ProtectSystem/ProtectHome). `.env`, `profiles.toml` und `data/` vom PC übernommen,
  `--diagnose` und `--test-notify` grün. Updates als `flipscout`: `sudo -u flipscout git pull`.
- Windows-PC: Bot beendet und gelöscht.
- Dienst läuft (enabled), Vinted- und willhaben-Suchen (Stand 19.11.: 12 + 12) (seit 17.11.2026), Preise vom
  Besitzer in `profiles.toml` (seit 18.11.2026 auf Wunsch des Besitzers im öffentlichen Repo; Sicherungen in `data/profiles.toml.bak-*`):
  iPhone (Handys 3661 + Apple 54661, Preisliste pro Modell, nur iCloud-Sperre schließt aus),
  MacBook ab 2020 (Laptops 3580 + Apple, < 250 €), Nintendo Switch (Konsolen 3025 +
  Nintendo 77600; Lite 40, normal 70, OLED 90), Grafikkarten (3602; 1080 Ti 100,
  3060/3060 Ti 199, funktionsfähig), DDR4-RAM (3603; 8/16/32 GB = 10/30/60 €, Typ notfalls
  per Foto-Etikett PC4-...), Mini-PC (Desktop-Computer 3581; ≤ 100 €, nur Mobil-CPU
  ab N100-Niveau wegen < 20 W im Leerlauf, H-CPUs erlaubt, RAM/SSD dürfen fehlen, funktionsfähig).
- Das Uni-Tool liegt in `/root/tuwel` (vermutlich per Cron). Nicht anfassen.
  Weitere Dienste: apache2, exim4, xinetd (Standard des Hosters), ebenfalls nicht anfassen.
- Claude Code läuft auf dem Server als root (`claude remote-control` in tmux, Sitzung
  `claude`). Befehle für den Bot als `flipscout` ausführen (`sudo -u flipscout ...`),
  danach Rechte prüfen (`chown -R flipscout:flipscout /opt/flip-scout`, `.env` 600).

## Nächste Schritte

1. Treffer der neuen Suchen mit dem Besitzer auswerten und Profile nachschärfen.
2. Später: Web-Dashboard (Trefferhistorie, Kosten), das der Besitzer sich gewünscht hat.

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
- Die Textsuche (`search_text`) ist sehr unscharf (bei „macbook“ ≤ 249 € nur 6 von 96
  Titeln passend). Darum Kategorie (+ Marke) ohne Suchbegriff verwenden. Kategorie- und
  Marken-IDs stehen in den eingebetteten Daten jeder Anzeigenseite (`catalog_id`, `brand_id`).
- Fotos: Suche liefert nur das Hauptfoto (signierte `full_size_url`, 1200x1600). Alle Fotos
  stehen in den eingebetteten Next.js-Daten der Anzeigenseite, aber nur als f800 (600x800).
- Die Detail-API ist blockiert. Beschreibung und Fotos kommen von der Anzeigenseite
  (JSON-LD / OpenGraph).

## willhaben.at (seit 19.11.2026)

- Jede Suche hat eine Quelle (`searches.source`: `vinted` oder `willhaben`). Zu jeder
  Vinted-Suche gibt es eine Kopie „… · willhaben“ mit `params = {"path": "<kategorie-pfad>"}`.
- Suche nur über Kategorie + `areaId=900` (Wien) + `ISPRIVATE=1` + `sort=1`, Seitendaten aus
  `__NEXT_DATA__`. KEINE Suchbegriffe und KEIN Preisfilter in der URL (robots.txt schließt
  `keyword=`/`PRICE_FROM`/`PRICE_TO` aus); Preis/Modell/Größe filtert der Bot selbst.
  „Hochgeschobene“ Anzeigen sind Händler. Takt `willhaben_interval_minutes` (3 Min.).
- Anzeigenseite liefert volle Beschreibung, große Fotos und Vornamen des Verkäufers
  (`advertDetails.sellerProfileUserData.name`).
- Preisgrenzen auf willhaben × `willhaben_price_factor` (1,2). Liegt ein Treffer über dem
  eigentlichen Limit, schlägt die automatische Nachricht das Limit als Preis vor.
- Automatische Nachricht (Wunsch des Besitzers): nach jedem willhaben-Treffer, nur Privat,
  nicht wenn die KI unsicher war, max. 1 pro Anzeige, Tageslimit, Telegram `/nachrichten an|aus`.
  Gesendet über `POST /webapi/iad-messaging/sendrequest` (gleiches JSON wie die Webseite) mit
  dem Login-Cookie des Besitzers (`WILLHABEN_COOKIE` in `.env`, nie anzeigen). Login hat
  reCAPTCHA: nie automatisch einloggen oder Captchas umgehen; bei abgelaufenem Cookie meldet
  der Bot das in Telegram, der Besitzer trägt ein neues ein. Gekauft wird weiterhin nie.

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
