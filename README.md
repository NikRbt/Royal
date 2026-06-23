# Live Roulette — Multiplayer Web-App

Einfacher Login, Admin-Center, Coin-System und Live-Roulette, bei dem mehrere Spieler
gleichzeitig in Echtzeit-Runden setzen.

## Features

- 🔑 **Einfacher Login** — nur Name eingeben, kein Passwort-Aufwand
- 👑 **Admin-Center** — Coins anpassen, Transaktionslog einsehen
- 🪙 **Coin-System** — Startguthaben 1000, automatische Buchungen bei jedem Einsatz/Gewinn
- 🎡 **Live-Roulette** — läuft in echten Runden, die für ALLE Spieler gleichzeitig zählen:
  - **15 Sekunden** Setzen
  - **3 Sekunden** "Kugel rollt"
  - **5 Sekunden** Ergebnis anzeigen, danach startet automatisch die nächste Runde
- Setzbar auf: einzelne Zahl (35:1), Rot/Schwarz, Gerade/Ungerade, 1–18/19–36 (alle 1:1)
- Andere Spieler sehen live, wie viele Coins auf welches Feld gesetzt wurden

## Setup (lokal)

Voraussetzung: Python 3.8+ installiert.

```bash
pip install -r requirements.txt
```

Falls Fehlermeldung "externally-managed-environment":
```bash
pip install -r requirements.txt --break-system-packages
```

## Starten

```bash
python app.py
```

Ausgabe:
```
✅ Roulette App läuft auf http://localhost:3000
```

Browser auf `http://localhost:3000` öffnen.

## Admin werden

Nutzername **"admin"** (egal welche Schreibweise) wird automatisch zum Admin.
Weitere Admin-Namen in `app.py` ergänzen:
```python
ADMIN_NAMES = ['admin']  # weitere Namen in Kleinschreibung ergänzen
```
Admins sehen oben rechts den Link "🛠️ Admin", erreichen das Panel auch direkt über `/admin.html`.

## Im LAN für Kollegen freigeben

1. Deine lokale IP herausfinden: `ipconfig` (Windows) → Wert bei "IPv4-Adresse"
2. App starten (`python app.py`) — lauscht automatisch auf allen Netzwerk-Schnittstellen
3. Windows-Firewall: beim ersten Start Zugriff erlauben, oder manuell Port 3000 (TCP) in den
   eingehenden Regeln freigeben
4. Kollegen rufen `http://DEINE-IP:3000` im Browser auf

## Kostenlos online stellen (außerhalb des LAN)

**Cloudflare Tunnel** (am einfachsten, kein Account nötig):
```bash
cloudflared tunnel --url http://localhost:3000
```
Du bekommst eine `https://....trycloudflare.com`-URL — funktioniart mit WebSockets, dein PC
muss aber laufen, solange jemand zugreift.

**Render.com Free-Tier** (läuft 24/7 unabhängig von deinem PC):
1. Code in ein GitHub-Repo pushen
2. Auf render.com → "New Web Service" → Repo verbinden
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `python app.py`
5. Fertig — Render gibt dir eine permanente URL

Hinweis: Render Free-Tier schläft nach 15 Minuten Inaktivität ein (erster Aufruf danach
dauert ~30–60 Sekunden zum "Aufwachen") — für ein gelegentliches Kollegen-Spiel meist okay.

## Daten

Alle Daten (User, Coins, Transaktionen, Rundenverlauf) liegen in `db.json`. Zum Zurücksetzen:
App stoppen, `db.json` löschen, neu starten.

## Wichtiger Hinweis

Diese App hat **keine echte Passwort-Sicherheit** — jeder kann sich mit jedem Namen anmelden,
und der Admin-Status hängt nur am Namen "admin". Das ist für ein internes Spaß-Tool unter
Kollegen okay, aber NICHT für den produktiven oder öffentlichen Einsatz mit echtem Geld gedacht.
Die "Coins" sind virtuelle Spielwährung ohne echten Wert.
