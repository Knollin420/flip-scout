# flip-scout auf einem Android-Handy (Termux + proot-Ubuntu)

Getestet für: Xiaomi Poco X7 Pro, HyperOS, Android 16. Klappt sinngemäß auf den meisten
Android-Handys.

## 1. Handy vorbereiten

- Alle System-Updates installieren, danach automatische Updates **aus**.
- WLAN → dein Netz → **Datenschutz: Geräte-MAC** (nicht zufällig) + im Router feste IP vergeben.
- Entwickleroptionen aktivieren (Über das Telefon → OS-Version 7× antippen), dann:
  **„Untergeordnete Prozessbeschränkungen deaktivieren“** einschalten
  (engl. *Disable child process restrictions*). Ohne das beendet Android Termux-Prozesse.
- Akku → **Ladebegrenzung / Akkuschutz** (80 %) einschalten. Handy nicht abdecken.

## 2. Termux installieren (nur aus F-Droid!)

1. F-Droid von https://f-droid.org installieren.
2. Darüber **Termux** und **Termux:Boot** installieren. Beide aus F-Droid, nie gemischt mit Play Store.
3. **Termux:Boot einmal öffnen** (sonst startet es später nicht automatisch).
4. Einstellungen → Apps → Termux **und** Termux:Boot:
   - Akku-Sparmodus → **Keine Einschränkungen**
   - **Autostart** an
5. In der App-Übersicht Termux mit dem **Schloss** fixieren.

## 3. Termux-Grundlagen

```bash
pkg update && pkg upgrade
pkg install proot-distro openssh
termux-wake-lock
passwd          # Passwort für SSH-Zugang setzen
sshd            # SSH-Server starten (Port 8022)
```

Ab jetzt geht alles bequem vom Laptop: `ssh -p 8022 <handy-ip>` (Benutzername egal).

## 4. Ubuntu + flip-scout

```bash
proot-distro install ubuntu
proot-distro login ubuntu
```

Jetzt in Ubuntu:

```bash
apt update && apt install -y python3 python3-venv git nano
cd /root
git clone https://github.com/<dein-github-name>/flip-scout.git
cd flip-scout
python3 -m venv .venv
.venv/bin/pip install -e .
cp profiles.example.toml profiles.toml
cp .env.example .env
chmod 600 .env
nano .env               # Telegram-Token, Chat-ID, Anthropic-Key eintragen
nano profiles.toml      # max_prices an deine Ankaufpreise anpassen
```

## 5. Testen

```bash
.venv/bin/python -m flip_scout --test-notify   # Telegram-Testnachricht
.venv/bin/python -m flip_scout --once          # ein Suchdurchlauf
```

Beim ersten `--once` merkt sich der Bot nur die aktuellen Anzeigen („erster Lauf – … gemerkt“).
Erscheint `HTTP 403` oder `Kein access_token_web-Cookie`, blockt Vinted dieses Netz → siehe unten.

## 6. Autostart einrichten

Ubuntu mit `exit` verlassen, dann in Termux:

```bash
mkdir -p ~/.termux/boot
cp $PREFIX/var/lib/proot-distro/installed-rootfs/ubuntu/root/flip-scout/deploy/termux/start-flip-scout.sh ~/.termux/boot/
chmod +x ~/.termux/boot/start-flip-scout.sh
```

Handy neu starten und **einmal entsperren**. Kommt „🚀 flip-scout gestartet“ auf Telegram, läuft alles.

## Alltag

| Was | Befehl (in Termux) |
|---|---|
| Log live ansehen | `tail -f $PREFIX/var/lib/proot-distro/installed-rootfs/ubuntu/root/flip-scout/data/flip-scout.log` |
| Nach Änderung an `profiles.toml`/`.env` neu starten | `pkill -f flip_scout` (startet nach 30 s neu) |
| Update von GitHub | `proot-distro login ubuntu -- bash -c 'cd /root/flip-scout && git pull && .venv/bin/pip install -e .'` und dann `pkill -f flip_scout` |
| Manuell starten (ohne Neustart des Handys) | `sh ~/.termux/boot/start-flip-scout.sh` |

Suchen anlegen und ändern geht per Telegram: Vinted-Link schicken oder `/menu`.

## Wenn Vinted blockt

1. Kurz warten. Der Bot versucht es weiter und meldet „✅ funktioniert wieder“.
2. Anderes Netz testen (z. B. Handy-Hotspot eines zweiten Handys).
3. Kein VPN verwenden. Heim-IPs funktionieren am besten.
