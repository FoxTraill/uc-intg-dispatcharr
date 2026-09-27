# uc-intg-dispatcharr v0.8.4 (on-device)

Dispatcharr-Integration für die Unfolded Circle Remote 3, nativ auf der Remote
als aarch64-Binary. Funktionsgleich zur Docker-Variante v0.8.2 — nur anders
verpackt.

## Unterschiede zur Docker-Variante

| | Docker (`dispatcharr_ext`) | On-Device (`dispatcharr_local`) |
|---|---|---|
| driver_id | `dispatcharr_ext` | `dispatcharr_local` |
| Laufzeit | LXC 132, Dockge | direkt auf der Remote |
| Config | `/opt/stacks/intg-dispatcharr/data/config.json` | `$UC_CONFIG_HOME/config.json` (Core-Sandbox) |
| Logo-Proxy | Port 19191 auf der LXC | Port 19191 auf der Remote |
| Update | `docker compose up -d --build` | neu bauen + im Web-Configurator hochladen |

Die abweichende `driver_id` ist Absicht: Core behandelt beide als getrennte
Integrationen, dadurch kannst du die On-Device-Variante installieren und
testen, während die Docker-Variante noch läuft.

Am Python-Code wurde **nichts** geändert. `config.py` liest bereits
`UC_CONFIG_HOME`, und der Logo-Proxy-Port 19191 liegt ausserhalb der vom
Core gesperrten Bereiche (8000–9200, 13333).

## Build

Auf dem Mac, Docker Desktop muss laufen:

```bash
chmod +x build.sh
./build.sh
```

Ergebnis: `uc-intg-dispatcharr-0.8.4-aarch64.tar.gz` (~21 MB, entpackt ~52 MB).

Der Build läuft im offiziellen `unfoldedcircle/r2-pyinstaller:3.11.13`-Image
unter `--platform=linux/arm64`. Auf Apple Silicon ist das nativ und schnell,
das Image wird beim ersten Lauf gezogen.

## Release über GitHub

Das Archiv baut GitHub automatisch (`.github/workflows/build.yml`):

- **Pull Request** → Testbuild, das Archiv hängt unter *Actions* am Lauf.
- **Versions-Tag** → Build plus Release mit `.tar.gz` und SHA256.

Neue Version veröffentlichen:

1. `version` in `driver.json` erhöhen, Changelog unten ergänzen.
2. Auf `main` mergen.
3. Auf GitHub: *Releases → Draft a new release → Choose a tag* →
   `v<version>` eintippen (z.B. `v0.8.4`) → *Create new tag* → *Publish*.
   Oder lokal: `git tag v0.8.4 && git push origin v0.8.4`.

Passt der Tag nicht zur Version in `driver.json`, bricht der Build ab.
Der Build läuft per Emulation und dauert einige Minuten.

## driver.json — wo der Treiber sie sucht

`driver.py` sucht die `driver.json` erst neben `__file__`, dann im
Arbeitsverzeichnis. Im gefrorenen Bundle zeigt `__file__` auf `_internal/`,
deshalb legt `build.sh` die Datei an drei Stellen ab:

- `artifacts/driver.json` — Metadaten für den Web-Configurator (Pflicht)
- `artifacts/bin/driver.json` — Fallback über `os.getcwd()`
- im Bundle über `--add-data` — greift über `__file__`

Damit startet der Treiber unabhängig davon, mit welchem Arbeitsverzeichnis
der Core ihn aufruft.

## Installation

1. Web-Configurator → Integrationen → Hinzufügen → Eigene Integration
2. `uc-intg-dispatcharr-0.8.4-aarch64.tar.gz` hochladen (nicht entpacken)
3. Setup ausfüllen:
   - **URL**: `http://192.168.1.10:9191`
   - **API Key**: Key eines Users mit Admin-Rechten (ohne Admin → HTTP 403
     beim Quellenwechsel)
   - **Client IP**: IP des Abspielgeräts
   - **Poll-Intervall**: 10 s
4. Entities in den Aktivitäten neu zuweisen:
   - `dispatcharr_now_playing` (Media Player) — Widget
   - `dispatcharr_next_source` (Button) — Quellenwechsel ohne Polling

## Danach

Erst wenn die On-Device-Variante läuft:

1. Docker-Stack `intg-dispatcharr` in Dockge stoppen
2. Web-Configurator → Integrationen → „Dispatcharr Now Playing (extern)" → löschen
3. Stack in Dockge entfernen

## Energieverhalten (unverändert)

- Poll-Loop läuft nur, wenn die Media-Player-Entity abonniert ist
- Der Button allein verursacht kein Polling
- `enter_standby` pausiert die Runtime, `exit_standby` nimmt sie wieder auf
- Gemessen: ~16 `entity_change`-Events/Stunde

## Changelog

### 0.8.4 — Logos, UC-Richtlinien

- **Logos** (`image_proxy.py`) — leerer Rand (transparent oder einfarbig)
  wird vor dem Skalieren abgeschnitten. Die Höhe nutzt jetzt 94 % statt
  75 % der Canvas; die Breite bleibt bei 75 %, weil das Widget nur
  seitlich abschneidet. Quadratische Logos: 96×96 → 120×120 px, Logos
  mit eingebautem Rand teils mehr als dreimal so groß. Die Logo-URL trägt
  `?v=<RENDER_VERSION>`, damit die Remote nicht 24 h lang alte Bilder
  aus dem Cache zeigt.
- **`driver.json`** — API-Key als Passwortfeld, Entwickler/Homepage auf
  dieses Repo statt auf das Dispatcharr-Projekt.
- **`driver.py`**
  - `media_type` ist `channel` statt `tv_show` (Live-TV).
  - Log-Level über `UC_LOG_LEVEL` einstellbar.
  - Sender ohne EPG holten bei jedem Poll die komplette EPG-Antwort
    (~200 KB) neu, jetzt greift der 60-s-Cache auch dort.
  - `exit_standby` startet die Runtime nur noch, wenn eine Entity
    abonniert ist.
  - Ungültiges Poll-Intervall im Setup führt zu `SetupError` statt zu
    einer Exception, Werte werden auf 5–60 s begrenzt.

### 0.8.3 — Wakeup-Robustheit (on-device)

Zwei Fehler, die ausschliesslich on-device auftreten: Die Remote baut ihr
WLAN erst nach dem `EXIT_STANDBY`-Event wieder auf, der erste HTTP-Request
kommt aber rund 35 ms danach. In der LXC gab es dieses Zeitfenster nie.

- **`client.py`** — `refresh_channel_cache()` hat den bestehenden Cache
  bedingungslos überschrieben, auch wenn der Fetch fehlschlug. Ein
  Netzwerkfehler beim Aufwachen löschte damit Logos bzw. Kanäle, bis der
  nächste reguläre Refresh sechs Stunden später lief. Jetzt bleibt der
  Bestand erhalten, wenn ein Fetch scheitert.
- **`driver.py`** — `_start_runtime()` machte genau einen Versuch und setzte
  bei leerem Cache `DeviceStates.ERROR` mit `return False`. Danach folgte
  kein weiterer Versuch, die Integration blieb bis zum nächsten
  Standby-Zyklus tot. Jetzt fünf Versuche mit Backoff (2/4/6/8 s, maximal
  20 s), ERROR erst danach.
