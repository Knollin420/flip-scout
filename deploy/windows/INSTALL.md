# flip-scout auf einem Windows-PC

## 1. Programme installieren (einmalig)

1. **Python**: https://www.python.org/downloads/ → „Download Python 3.13“.
   Im Installer ganz unten **„Add python.exe to PATH“ anhaken**, dann „Install Now“.
2. **Git**: https://git-scm.com/download/win → Installer starten, überall „Next“.

## 2. flip-scout herunterladen und einrichten

Startmenü → **PowerShell** öffnen und Zeile für Zeile eingeben:

```powershell
cd $HOME
git clone https://github.com/Knollin420/flip-scout.git
cd flip-scout
py -m venv .venv
.venv\Scripts\pip install -e .
copy profiles.example.toml profiles.toml
copy .env.example .env
notepad .env
```

Im Editor Telegram-Token, Chat-ID und Anthropic-API-Key eintragen, speichern, schließen.
Preise anpassen: `notepad profiles.toml` (Abschnitte `max_prices`).

## 3. Testen

```powershell
.venv\Scripts\python -m flip_scout --test-notify
.venv\Scripts\python -m flip_scout --once
```

- `--test-notify` → auf Telegram kommt „🧪 Testnachricht“.
- `--once` → beim ersten Mal merkt sich der Bot nur die aktuellen Anzeigen („erster Lauf – … gemerkt“).

## 4. Dauerhaft laufen lassen

Doppelklick auf `deploy\windows\start-flip-scout.bat`. Ein Fenster öffnet sich. Lass es offen,
minimieren ist okay. Auf Telegram kommt „🚀 flip-scout gestartet“.

**Automatisch beim Hochfahren starten:**
1. `Windows-Taste + R` → `shell:startup` → Enter. Der Autostart-Ordner öffnet sich.
2. Im Explorer zu `flip-scout\deploy\windows` gehen, `start-flip-scout.bat` mit der
   **rechten Maustaste** in den Autostart-Ordner ziehen → „Verknüpfung hier erstellen“.

**Energiesparen:** Einstellungen → System → Netzbetrieb und Energie → „Bildschirm und
Energiesparmodus“ → **Energiesparmodus: Nie** (Bildschirm ausschalten ist okay). Im
Energiesparmodus kann der Bot nicht suchen.

## Alltag

| Was | Wie |
|---|---|
| Suchen anlegen/ändern | Telegram: Vinted-Link schicken oder `/menu` |
| Log ansehen | `data\flip-scout.log` im Editor öffnen |
| Nach Änderung an `profiles.toml` / `.env` | Fenster schließen, `.bat` neu starten |
| Update von GitHub | In PowerShell im Ordner `flip-scout`: `git pull`, dann `.venv\Scripts\pip install -e .`, Bot neu starten |
| Bot stoppen | Fenster schließen |
