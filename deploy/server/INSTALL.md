# flip-scout auf einem Linux-Server (Ubuntu 24.04)

Alles liegt in `/opt/flip-scout`. Der Bot läuft als eigener Benutzer `flipscout` und kann
weder `/root` noch `/home` sehen. Andere Programme auf dem Server bleiben unberührt.

## 1. Installieren (als root)

```bash
apt update && apt install -y python3-venv git
cd /opt && git clone https://github.com/Knollin420/flip-scout.git && cd flip-scout
python3 -m venv .venv && .venv/bin/pip install -e .
cp profiles.example.toml profiles.toml
.venv/bin/python -m flip_scout --diagnose     # muss überall ✅ zeigen
```

## 2. Einstellungen vom PC übernehmen

**Auf dem PC** in PowerShell (nicht auf dem Server). Damit kommen `.env`, `profiles.toml`
und die Datenbank mit deinen Suchen auf den Server:

```powershell
scp $HOME\flip-scout\.env $HOME\flip-scout\profiles.toml root@<server-ip>:/opt/flip-scout/
scp -r $HOME\flip-scout\data root@<server-ip>:/opt/flip-scout/
```

## 3. Eigenen Benutzer anlegen und Rechte setzen (Server)

```bash
useradd --system --home-dir /opt/flip-scout --shell /usr/sbin/nologin flipscout
mkdir -p /opt/flip-scout/data
chown -R flipscout:flipscout /opt/flip-scout
chmod 600 /opt/flip-scout/.env
sudo -u flipscout /opt/flip-scout/.venv/bin/python -m flip_scout --test-notify
```

## 4. Als Dienst starten

Vorher den Bot auf dem PC beenden (Fenster schließen), sonst kommt alles doppelt.

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
| Einstellungen bearbeiten | `nano /opt/flip-scout/profiles.toml`, danach neu starten |
| Update von GitHub | `sudo -u flipscout git -C /opt/flip-scout pull && sudo -u flipscout /opt/flip-scout/.venv/bin/pip install -q -e /opt/flip-scout && systemctl restart flip-scout` |
| Absturzmeldungen | `journalctl -u flip-scout -n 50` |
