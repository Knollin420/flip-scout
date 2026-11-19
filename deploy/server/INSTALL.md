# flip-scout auf einem Linux-Server (Ubuntu 24.04)

Alles liegt in `/opt/flip-scout`. Der Bot läuft als eigener Benutzer `flipscout` und kann
weder `/root` noch `/home` sehen. Andere Programme auf dem Server bleiben unberührt.

## 1. Installieren (als root)

```bash
apt update && apt install -y python3-venv git
cd /opt && git clone https://github.com/Knollin420/flip-scout.git && cd flip-scout
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python -m flip_scout --diagnose     # muss überall ✅ zeigen
```

`profiles.toml` (Profile und Preislisten) liegt schon im Repository.

## 2. Geheimnisse eintragen (`.env`)

```bash
cp .env.example .env
```

Telegram-Token, Chat-ID und Anthropic-Key eintragen, ohne sie irgendwo anzuzeigen. Ohne
Editor geht das z. B. so (die Eingabe ist unsichtbar):

```bash
printf "Wert einfügen, dann Enter: "; stty -echo -icanon; IFS= read -r V; stty echo icanon; \
  sed -i "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=$V|" /opt/flip-scout/.env; echo; unset V
```

**Optional – automatische willhaben-Nachrichten:** Im Browser bei willhaben anmelden
(„Angemeldet bleiben“), F12 → Network → F5 → ersten Eintrag anklicken → Request Headers →
`cookie` → „Copy value“. Dann auf dem Server:

```bash
printf "Cookie einfügen, dann Enter: "; stty -echo -icanon; IFS= read -r C; stty echo icanon; \
  printf 'WILLHABEN_COOKIE="%s"\n' "$C" >> /opt/flip-scout/.env; echo; unset C
```

Läuft das Cookie ab, meldet der Bot „🔑 willhaben-Login abgelaufen“; dann die Schritte
wiederholen (vorher die alte Zeile `WILLHABEN_COOKIE=` aus `.env` entfernen).

## 3. Eigenen Benutzer anlegen und Rechte setzen (Server)

```bash
useradd --system --home-dir /opt/flip-scout --shell /usr/sbin/nologin flipscout
mkdir -p /opt/flip-scout/data
chown -R flipscout:flipscout /opt/flip-scout
chmod 600 /opt/flip-scout/.env
sudo -u flipscout /opt/flip-scout/.venv/bin/python -m flip_scout --test-notify
```

## 4. Als Dienst starten

Der Bot darf mit demselben Telegram-Token nur an einer Stelle laufen.

```bash
cp /opt/flip-scout/deploy/server/flip-scout.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now flip-scout
systemctl status flip-scout          # sollte "active (running)" zeigen
```

Auf Telegram kommt „🚀 flip-scout gestartet“. Der Dienst startet nach einem Server-Neustart
und nach Abstürzen automatisch.

## Alltag

| Was | Befehl |
|---|---|
| Log live ansehen | `tail -f /opt/flip-scout/data/flip-scout.log` |
| Status | `systemctl status flip-scout` |
| Neu starten (nach Änderung an `.env` / `profiles.toml`) | `systemctl restart flip-scout` |
| Stoppen | `systemctl stop flip-scout` |
| Einstellungen ändern | `profiles.toml` anpassen (liegt im Repository), danach neu starten |
| Update von GitHub | `sudo -u flipscout git -C /opt/flip-scout pull && sudo -u flipscout /opt/flip-scout/.venv/bin/pip install -q -e /opt/flip-scout && systemctl restart flip-scout` |
| Absturzmeldungen | `journalctl -u flip-scout -n 50` |
