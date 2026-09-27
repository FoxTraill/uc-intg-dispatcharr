#!/usr/bin/env python3
"""
Dispatcharr Now Playing - UC Remote 3 Custom Integration.

Liefert eine Media Player Entity, die per Polling den aktiven
Dispatcharr-Stream der konfigurierten Client-IP (z.B. die Shield)
anzeigt, plus einen Button für den Quellenwechsel. Die Wiedergabe
selbst steuert weiterhin die ADB Bridge Integration.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

import ucapi
from ucapi import (
    DeviceStates,
    DriverSetupRequest,
    Events,
    IntegrationSetupError,
    MediaContentType,
    SetupAction,
    SetupComplete,
    SetupDriver,
    SetupError,
    StatusCodes,
    UserDataResponse,
)
from ucapi import button, media_player

from client import DispatcharrClient
from config import DriverConfig, load_config, save_config
from image_proxy import LogoProxy

# ----------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    stream=sys.stdout,
)
_LOG = logging.getLogger("intg-dispatcharr")
# UC-Konvention: Log-Level über UC_LOG_LEVEL steuerbar (DEBUG, INFO, ...)
_LOG_LEVEL = os.getenv("UC_LOG_LEVEL", "INFO").upper()
for _name in ("intg-dispatcharr", "ucapi", "client", "config", "image_proxy"):
    logging.getLogger(_name).setLevel(_LOG_LEVEL)


# ----------------------------------------------------------------------
# Globals
# ----------------------------------------------------------------------
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
api = ucapi.IntegrationAPI(loop)

cfg: DriverConfig = load_config()
client: Optional[DispatcharrClient] = None
logo_proxy: Optional[LogoProxy] = None

ENTITY_ID = "dispatcharr_now_playing"
ENTITY_NAME = {"en": "TV Now Playing", "de": "TV Now Playing"}

# Separate Button-Entity: frei auf UI-Pages platzierbar und in
# Activities/Macros nutzbar, ohne den Media Player zu brauchen.
BUTTON_ID = "dispatcharr_next_source"
BUTTON_NAME = {"en": "Next Source", "de": "Nächste Quelle"}

# Welche unserer Entities die Remote gerade abonniert hat.
# Der Poll-Loop läuft nur wenn der Media Player dabei ist - der
# Button allein braucht kein Polling, der holt sich den aktuellen
# Kanal beim Druck frisch ab.
_subscribed: set[str] = set()

# Aktuell laufender Kanal/Quelle (aus dem letzten Status-Poll).
_current_channel_uuid: Optional[str] = None
_current_channel_id: Optional[int] = None
_current_stream_id: Optional[int] = None

# Quellenliste pro Kanal: channel_id -> (streams, fetch_timestamp)
# Ändert sich selten, aber nicht nie (Provider kommen/gehen).
_source_cache: dict[int, tuple[list[dict[str, Any]], float]] = {}
_SOURCE_TTL_SECONDS = 600.0

# Polling-Tasks
_poll_task: Optional[asyncio.Task] = None
_cache_task: Optional[asyncio.Task] = None
# Standby-Pause: wenn gesetzt, läuft polling normal. Wenn clear, wartet
# der poll loop bis EXIT_STANDBY den Event wieder setzt.
_active_event: Optional[asyncio.Event] = None
# Counter für EPG-Throttling: nicht jeder Poll holt EPG, nur jeder N-te
_poll_counter: int = 0

# Letzter bekannter Zustand (zum Vergleich vor entity_change Events)
_last_attrs: dict[str, Any] = {}


# ----------------------------------------------------------------------
# Entity factory
# ----------------------------------------------------------------------
def _make_entity() -> media_player.MediaPlayer:
    """
    Media Player Entity.

    Anzeige ist weiterhin der Kern (Logo, EPG-Titel, Fortschritt).
    Dazu kommen genau drei aktive Kommandos, alle für den Wechsel der
    Dispatcharr-Quelle des laufenden Senders:

    - NEXT     -> nächste Quelle (⏭ im Widget, Ein-Tap-Lösung)
    - PREVIOUS -> vorherige Quelle
    - SELECT_SOURCE -> gezielte Auswahl aus der Quellenliste

    Kein PLAY/PAUSE/STOP - die Wiedergabe steuert weiterhin der Player
    selbst (ADB Bridge in der Activity).
    """
    return media_player.MediaPlayer(
        identifier=ENTITY_ID,
        name=ENTITY_NAME,
        features=[
            # Passive Anzeige
            media_player.Features.MEDIA_TITLE,
            media_player.Features.MEDIA_ARTIST,
            media_player.Features.MEDIA_IMAGE_URL,
            media_player.Features.MEDIA_DURATION,
            media_player.Features.MEDIA_POSITION,
            media_player.Features.MEDIA_TYPE,
            # Quellenwechsel
            media_player.Features.NEXT,
            media_player.Features.PREVIOUS,
            media_player.Features.SELECT_SOURCE,
        ],
        attributes={
            media_player.Attributes.STATE: media_player.States.OFF,
            media_player.Attributes.MEDIA_TITLE: "",
            media_player.Attributes.MEDIA_ARTIST: "",
            media_player.Attributes.MEDIA_IMAGE_URL: "",
            media_player.Attributes.MEDIA_DURATION: 0,
            media_player.Attributes.MEDIA_POSITION: 0,
            media_player.Attributes.MEDIA_TYPE: MediaContentType.CHANNEL,
            media_player.Attributes.SOURCE: "",
            media_player.Attributes.SOURCE_LIST: [],
        },
        device_class=media_player.DeviceClasses.STREAMING_BOX,
        cmd_handler=_cmd_handler,
    )


def _make_button() -> ucapi.Button:
    """Standalone-Button 'Nächste Quelle' für UI-Pages und Macros."""
    return ucapi.Button(
        identifier=BUTTON_ID,
        name=BUTTON_NAME,
        icon="uc:sync",
        cmd_handler=_button_cmd_handler,
    )


# ----------------------------------------------------------------------
# Command handling
# ----------------------------------------------------------------------
async def _cmd_handler(
    entity: media_player.MediaPlayer,
    cmd_id: str,
    params: Optional[dict[str, Any]],
    *args: Any,
) -> StatusCodes:
    """Quellenwechsel-Kommandos; alles andere wird abgelehnt."""
    if cmd_id == media_player.Commands.NEXT:
        return await _switch_relative(+1)
    if cmd_id == media_player.Commands.PREVIOUS:
        return await _switch_relative(-1)
    if cmd_id == media_player.Commands.SELECT_SOURCE:
        source = (params or {}).get("source")
        if not source:
            return StatusCodes.BAD_REQUEST
        return await _switch_to_source_name(str(source))

    # Ein Tap auf das Logo im Media-Widget schickt play_pause - die
    # Firmware hat dafür kein eigenes Kommando und das Artwork ist fest
    # verdrahtet. Wir haben keine Wiedergabesteuerung (die läuft über
    # ADB Bridge), also ist play_pause hier frei und wird auf den
    # Quellenwechsel gelegt: Tap aufs Logo = nächste Quelle.
    if cmd_id == media_player.Commands.PLAY_PAUSE:
        return await _switch_relative(+1)

    _LOG.debug("Command not supported: %s %s", cmd_id, params)
    return StatusCodes.NOT_IMPLEMENTED


async def _button_cmd_handler(
    entity: ucapi.Button,
    cmd_id: str,
    params: Optional[dict[str, Any]],
    *args: Any,
) -> StatusCodes:
    """
    Button-Druck = nächste Quelle.

    Der Button kann abonniert sein ohne dass der Media Player läuft
    (und damit ohne Poll-Loop). Deshalb holen wir uns hier den aktuell
    laufenden Kanal notfalls frisch ab, statt uns auf den zuletzt
    gepollten Zustand zu verlassen.
    """
    if cmd_id != button.Commands.PUSH:
        return StatusCodes.NOT_IMPLEMENTED
    return await _switch_relative(+1, allow_refresh=True)


async def _resolve_current_channel(allow_refresh: bool) -> Optional[str]:
    """
    Liefert die UUID des Kanals, der gerade auf unserer Client-IP läuft.

    Normalerweise steht das aus dem letzten Poll bereit. Wenn nicht
    (Button ohne Poll-Loop), holen wir den Status einmalig ab.
    """
    if _current_channel_uuid:
        return _current_channel_uuid
    if not allow_refresh or client is None:
        return None
    await _poll_once()
    return _current_channel_uuid


async def _switch_relative(
    offset: int, allow_refresh: bool = False
) -> StatusCodes:
    """
    Quellenwechsel um +1 / -1 in der Kanal-Reihenfolge.

    Vorwärts nutzt den Dispatcharr-Endpunkt next_stream - der macht die
    Rotation inklusive Wrap-around serverseitig und kennt den aktuellen
    Stream aus Redis, das ist robuster als unsere lokale Sicht.

    Rückwärts gibt es keinen Endpunkt, also rechnen wir den Index selbst
    aus und wechseln gezielt per change_stream.
    """
    channel_uuid = await _resolve_current_channel(allow_refresh)
    if not channel_uuid or client is None:
        _LOG.info("Source switch requested but no channel is playing")
        return StatusCodes.SERVICE_UNAVAILABLE

    if offset > 0:
        ok, msg = await client.next_stream(channel_uuid)
        if ok:
            await _refresh_after_switch()
            return StatusCodes.OK
        # "No alternate streams available" ist kein Serverfehler,
        # sondern schlicht: der Sender hat nur eine Quelle.
        if "alternate" in msg.lower():
            return StatusCodes.NOT_FOUND
        return StatusCodes.SERVER_ERROR

    # Rückwärts: Position in der Quellenliste selbst bestimmen
    sources = await _get_sources_cached(_current_channel_id)
    if not sources or len(sources) < 2:
        return StatusCodes.NOT_FOUND

    idx = next(
        (i for i, s in enumerate(sources) if s.get("id") == _current_stream_id),
        None,
    )
    if idx is None:
        _LOG.info(
            "Current stream %s not found in source list, falling back to first",
            _current_stream_id,
        )
        target = sources[0]
    else:
        target = sources[(idx + offset) % len(sources)]

    ok, _ = await client.change_stream(channel_uuid, target["id"])
    if not ok:
        return StatusCodes.SERVER_ERROR
    await _refresh_after_switch()
    return StatusCodes.OK


async def _switch_to_source_name(name: str) -> StatusCodes:
    """SELECT_SOURCE: Anzeigename aus source_list zurück auf stream_id mappen."""
    channel_uuid = await _resolve_current_channel(allow_refresh=True)
    if not channel_uuid or client is None:
        return StatusCodes.SERVICE_UNAVAILABLE

    sources = await _get_sources_cached(_current_channel_id)
    if not sources:
        return StatusCodes.NOT_FOUND

    labels = _source_labels(sources)
    idx = next((i for i, lbl in enumerate(labels) if lbl == name), None)
    if idx is None:
        _LOG.warning("Unknown source name requested: %s", name)
        return StatusCodes.BAD_REQUEST
    target = sources[idx]

    ok, _ = await client.change_stream(channel_uuid, target["id"])
    if not ok:
        return StatusCodes.SERVER_ERROR
    await _refresh_after_switch()
    return StatusCodes.OK


async def _refresh_after_switch() -> None:
    """
    Nach einem Wechsel kurz warten und den Zustand neu holen.

    Dispatcharr bestätigt den Switch erst nachdem der neue Upstream
    tatsächlich offen ist; ein sofortiger Poll würde noch die alte
    stream_id aus Redis lesen.
    """
    await asyncio.sleep(1.5)
    try:
        await _poll_once()
    except Exception as exc:
        _LOG.warning("Refresh after source switch failed: %s", exc)


# ----------------------------------------------------------------------
# Polling
# ----------------------------------------------------------------------
def _source_labels(sources: list[dict[str, Any]]) -> list[str]:
    """
    Anzeigenamen für die Quellenliste.

    Aufbau: "<Priorität>. <Provider> - <Streamname>"

    Die Priorität steht vorne, weil sie die Failover-Reihenfolge zeigt
    und die Labels garantiert eindeutig macht (Voraussetzung fürs
    Rückmapping bei SELECT_SOURCE). Der Provider steht davor, weil die
    Streamnamen verschiedener Provider für denselben Sender oft
    komplett unterschiedlich lauten - der Provider ist das stabilere
    Merkmal, an dem du die Quelle wiedererkennst.
    """
    labels: list[str] = []
    for i, s in enumerate(sources):
        name = (s.get("name") or "").strip()
        provider = None
        if client is not None:
            provider = client.get_provider_name(s.get("m3u_account"))

        if provider and name:
            body = f"{provider} - {name}"
        elif provider:
            body = provider
        elif name:
            body = name
        else:
            body = f"Stream {s.get('id')}"

        if len(body) > 48:
            body = body[:47] + "…"
        labels.append(f"{i + 1}. {body}")
    return labels


async def _get_sources_cached(
    channel_id: Optional[int],
) -> Optional[list[dict[str, Any]]]:
    """Quellenliste eines Senders mit TTL-Cache (siehe _SOURCE_TTL_SECONDS)."""
    import time

    if channel_id is None or client is None:
        return None

    now = time.time()
    cached = _source_cache.get(channel_id)
    if cached is not None and (now - cached[1]) < _SOURCE_TTL_SECONDS:
        return cached[0]

    sources = await client.get_channel_streams(channel_id)
    if sources is None:
        # Fehler: alten Cache behalten statt die Liste zu leeren
        return cached[0] if cached else None

    _source_cache[channel_id] = (sources, now)
    return sources


def _parse_iso(ts: str) -> Optional[datetime]:
    """Dispatcharr liefert UTC-Timestamps wie '2026-04-26T11:05:00Z'."""
    if not ts:
        return None
    try:
        # Python 3.11+ akzeptiert 'Z' direkt; für ältere Versionen ersetzen
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _source_line(
    sources: Optional[list[dict[str, Any]]],
    active_idx: Optional[int],
    stream: dict[str, Any],
) -> str:
    """
    Kurzform der aktiven Quelle für die Zeile unter dem Titel.

    Format: "<Streamname> · <Position>/<Gesamt> · <Provider>"
    Beispiel: "RTL · 2/7 · TS4_proxy_AUS"

    Auf dem Display ist wenig Platz, deshalb bewusst kompakt. Teile,
    die nicht bekannt sind, fallen weg statt als Platzhalter zu
    erscheinen - so bleibt die Zeile bei dünner Datenlage kurz statt
    mit Fragezeichen gefüllt.
    """
    parts: list[str] = []

    if active_idx is not None and sources:
        entry = sources[active_idx]
        name = (entry.get("name") or "").strip()
        provider = None
        if client is not None:
            provider = client.get_provider_name(entry.get("m3u_account"))

        if name:
            parts.append(name)
        # Position auch bei nur einer Quelle zeigen ("1/1") - dann sieht
        # man auf einen Blick, dass es keine Alternative gibt.
        parts.append(f"{active_idx + 1}/{len(sources)}")
        if provider:
            parts.append(provider)
    else:
        # Aktive stream_id nicht in der Liste (z.B. während eines
        # Failovers) - wenigstens den Rohnamen aus dem Status zeigen.
        name = (stream.get("stream_name") or "").strip()
        if name:
            parts.append(name)

    return " · ".join(parts)


def _build_attrs_from_stream_and_program(
    stream: dict[str, Any],
    channel_info: Optional[dict[str, Any]],
    program: Optional[dict[str, Any]],
    sources: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Mapt API-Daten auf Media Player Attribute."""
    # Sender-Name: nur als Fallback wenn KEIN Logo da ist (sonst redundant)
    has_logo = bool(channel_info and channel_info.get("logo_id") is not None and logo_proxy)

    if channel_info and channel_info.get("name"):
        channel_name = channel_info["name"]
    else:
        channel_name = stream.get("stream_name") or "Unknown"

    # Logo - via lokalem Proxy mit normalisierter Größe.
    # Test-Mode: wenn Channel-Name "ruler" oder "widget_test" enthält,
    # liefere das Original-Bild ohne Padding (zur Widget-Vermessung).
    logo_url = ""
    if has_logo:
        cname_lower = (channel_info.get("name") or "").lower()
        raw = "ruler" in cname_lower or "widget_test" in cname_lower
        logo_url = logo_proxy.url_for(channel_info["logo_id"], raw=raw)

    # Programm-Titel (title Zeile - Hauptanzeige)
    title = ""
    duration_s = 0
    position_s = 0
    if program:
        prog_title = program.get("title") or ""
        sub = program.get("sub_title")
        if sub:
            title = f"{prog_title} - {sub}"
        else:
            title = prog_title

        start = _parse_iso(program.get("start_time"))
        end = _parse_iso(program.get("end_time"))
        if start and end:
            now = datetime.now(timezone.utc)
            total = int((end - start).total_seconds())
            elapsed = int((now - start).total_seconds())
            duration_s = max(total, 0)
            position_s = max(0, min(elapsed, duration_s))

    # Wenn kein Programm da: Sendername als Titel zeigen, kein Artist
    # Wenn Programm + Logo da: Programm als Titel, KEIN Artist (Logo zeigt Sender schon)
    # Wenn Programm da aber kein Logo: Programm als Titel, Sender als Artist
    if not title:
        title = channel_name
        artist = ""
    elif has_logo:
        artist = ""
    else:
        artist = channel_name

    # Quellenliste + aktuell aktive Quelle.
    # Die aktive Quelle wird über die stream_id aus dem Status-Poll
    # bestimmt, nicht über den Namen - Namen sind bei mehreren Providern
    # oft doppelt.
    source_list: list[str] = []
    source = ""
    active_idx: Optional[int] = None
    if sources:
        source_list = _source_labels(sources)
        active_id = stream.get("stream_id")
        if active_id is not None:
            for i, s in enumerate(sources):
                if s.get("id") == active_id:
                    active_idx = i
                    source = source_list[i]
                    break
        if not source:
            # Quelle läuft, taucht aber nicht in der Liste auf
            # (z.B. gerade entfernt) - Rohnamen aus dem Status zeigen
            source = stream.get("stream_name") or ""

    # Zeile unter dem Titel: welche Quelle gerade läuft. Das Feld war
    # bisher leer, wenn ein Logo da ist (Sendername wäre dort neben dem
    # Logo redundant) - genau dieser Platz wird jetzt genutzt.
    source_line = _source_line(sources, active_idx, stream)
    if source_line:
        if has_logo:
            artist = source_line
        else:
            # Ohne Logo trägt keine andere Stelle den Sendernamen, wenn
            # oben ein Sendungstitel steht - dann beides zeigen.
            artist = f"{channel_name} · {source_line}" if artist else source_line

    return {
        media_player.Attributes.STATE: media_player.States.PLAYING,
        media_player.Attributes.MEDIA_TITLE: title,
        media_player.Attributes.MEDIA_ARTIST: artist,
        media_player.Attributes.MEDIA_IMAGE_URL: logo_url,
        media_player.Attributes.MEDIA_DURATION: duration_s,
        media_player.Attributes.MEDIA_POSITION: position_s,
        media_player.Attributes.MEDIA_TYPE: MediaContentType.CHANNEL,
        media_player.Attributes.SOURCE: source,
        media_player.Attributes.SOURCE_LIST: source_list,
    }


def _build_attrs_off() -> dict[str, Any]:
    return {
        media_player.Attributes.STATE: media_player.States.OFF,
        media_player.Attributes.MEDIA_TITLE: "",
        media_player.Attributes.MEDIA_ARTIST: "",
        media_player.Attributes.MEDIA_IMAGE_URL: "",
        media_player.Attributes.MEDIA_DURATION: 0,
        media_player.Attributes.MEDIA_POSITION: 0,
        media_player.Attributes.MEDIA_TYPE: MediaContentType.CHANNEL,
        media_player.Attributes.SOURCE: "",
        media_player.Attributes.SOURCE_LIST: [],
    }


async def _push(attrs: dict[str, Any]) -> None:
    """Update entity attributes only if something changed."""
    global _last_attrs
    if attrs == _last_attrs:
        return
    api.configured_entities.update_attributes(ENTITY_ID, attrs)
    _last_attrs = dict(attrs)
    _LOG.debug("Pushed attrs: %s", attrs)


# EPG-Cache: channel_uuid -> (program_dict, fetch_timestamp)
# Vermeidet das alle-10s 200KB EPG-JSON Polling.
_epg_cache: dict[str, tuple[Optional[dict[str, Any]], float]] = {}
_EPG_TTL_SECONDS = 60.0


async def _get_program_cached(channel_uuid: str) -> Optional[dict[str, Any]]:
    """
    Holt das EPG-Programm für einen Kanal, mit lokalem Cache.

    Cache wird invalidiert wenn:
    1. Älter als _EPG_TTL_SECONDS (Standard 60s)
    2. Das gecachte Programm laut seiner end_time bereits zu Ende ist
       (das bedeutet: ein neues Programm ist gestartet)

    Spart bei kontinuierlichem Schauen Bandbreite, sorgt aber dafür
    dass Programmwechsel erkannt werden.
    """
    import time
    from datetime import datetime, timezone
    now = time.time()
    cached = _epg_cache.get(channel_uuid)
    if cached is not None:
        prog, ts = cached
        cache_age = now - ts

        # TTL-Check. Auch "kein Programm" (None) wird gecacht - sonst
        # holt ein Sender ohne EPG bei jedem Poll die ~200KB Antwort neu.
        if cache_age < _EPG_TTL_SECONDS and prog is None:
            return None
        if cache_age < _EPG_TTL_SECONDS:
            # Zusätzlich: end_time prüfen - wenn das Programm laut
            # EPG schon vorbei ist, neu laden (Programmwechsel!)
            end_str = prog.get("end_time", "")
            if end_str:
                try:
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    now_utc = datetime.now(timezone.utc)
                    if now_utc < end_dt:
                        # Programm läuft noch laut EPG -> Cache nutzen
                        return prog
                    # Sonst durchfallen -> Cache ist stale, neu holen
                    _LOG.info(
                        "EPG cache for %s expired (program ended %s)",
                        channel_uuid, end_str,
                    )
                except Exception:
                    # Bei Parse-Fehler: Cache trotzdem nutzen
                    return prog
            else:
                return prog

    # Cache miss / abgelaufen: neu holen
    prog = await client.get_current_program_for(channel_uuid)
    _epg_cache[channel_uuid] = (prog, now)
    return prog


async def _poll_once() -> None:
    """Ein Polling-Zyklus: Streams holen, filtern, Entity updaten."""
    if client is None:
        return

    streams = await client.get_active_streams()

    # None = Netzwerk-/HTTP-Fehler, NICHT "keine Streams aktiv".
    # Den letzten bekannten State behalten damit das Logo nicht
    # plötzlich verschwindet wenn z.B. WLAN nach Wakeup noch nicht
    # voll da ist oder Dispatcharr kurz nicht erreichbar.
    if streams is None:
        _LOG.debug("Streams unavailable (network error?), keeping last state")
        return

    # Stream finden, der zu unserer Client-IP gehört
    matching: Optional[dict[str, Any]] = None
    for stream in streams:
        clients_list = stream.get("clients", []) or []
        for c in clients_list:
            if c.get("ip_address") == cfg.client_ip:
                matching = stream
                break
        if matching:
            break

    global _current_channel_uuid, _current_channel_id, _current_stream_id

    if not matching:
        _current_channel_uuid = None
        _current_channel_id = None
        _current_stream_id = None
        await _push(_build_attrs_off())
        return

    channel_uuid = matching.get("channel_id")
    channel_info = client.get_channel_info(channel_uuid) if channel_uuid else None

    # Wenn unbekannt: Cache evtl stale -> Refresh anstoßen
    if channel_uuid and channel_info is None:
        _LOG.info("Unknown channel uuid %s, refreshing cache", channel_uuid)
        await client.refresh_channel_cache()
        channel_info = client.get_channel_info(channel_uuid)

    # Aktuellen Kanal/Quelle merken - darauf beziehen sich alle
    # Wechsel-Kommandos.
    _current_channel_uuid = channel_uuid
    _current_channel_id = channel_info.get("id") if channel_info else None
    raw_stream_id = matching.get("stream_id")
    _current_stream_id = int(raw_stream_id) if raw_stream_id is not None else None

    # EPG aus Cache (60s TTL) statt jedes Mal die ~200KB EPG-Antwort
    # von Dispatcharr zu holen
    program = (
        await _get_program_cached(channel_uuid)
        if channel_uuid
        else None
    )

    # Quellenliste aus Cache (10min TTL) - nur ein Request pro Sender,
    # nicht pro Poll.
    sources = await _get_sources_cached(_current_channel_id)

    attrs = _build_attrs_from_stream_and_program(
        matching, channel_info, program, sources
    )
    await _push(attrs)


async def _poll_loop() -> None:
    """
    Endlos pollen alle cfg.poll_interval Sekunden.

    Respektiert _active_event - bei Standby wird der Loop pausiert
    und nimmt erst wieder Fahrt auf wenn das Event gesetzt wird.
    """
    _LOG.info("Poll loop started (every %ds)", cfg.poll_interval)
    while True:
        try:
            # Bei Standby: warten bis EXIT_STANDBY den Event wieder setzt
            if _active_event is not None:
                await _active_event.wait()
            await _poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOG.exception("poll error: %s", exc)
        try:
            await asyncio.sleep(cfg.poll_interval)
        except asyncio.CancelledError:
            raise


async def _cache_refresh_loop() -> None:
    """Periodisch Channel/Logo-Cache neu laden (Logos können sich ändern)."""
    _LOG.info(
        "Cache refresh loop started (every %ds)", cfg.cache_refresh_interval
    )
    while True:
        try:
            await asyncio.sleep(cfg.cache_refresh_interval)
            if client:
                await client.refresh_channel_cache()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOG.exception("cache refresh error: %s", exc)


# ----------------------------------------------------------------------
# Lifecycle: Connect / Disconnect / Subscribe
# ----------------------------------------------------------------------
async def _start_runtime() -> bool:
    """
    Initialisiert den Dispatcharr-Client und startet die Polling-Tasks.
    Wird bei CONNECT und nach erfolgreichem Setup aufgerufen.
    """
    global client, logo_proxy, _poll_task, _cache_task

    if not cfg.is_configured():
        _LOG.warning("Cannot start runtime: not configured")
        await api.set_device_state(DeviceStates.ERROR)
        return False

    # Vorherige Tasks aufräumen falls vorhanden
    await _stop_runtime()

    client = DispatcharrClient(cfg.url, cfg.api_key)

    # Initial cache laden - mit Backoff-Retry.
    #
    # On-device laeuft _start_runtime() direkt auf das EXIT_STANDBY-Event,
    # und die Remote baut ihr WLAN erst danach wieder auf. Der erste
    # Request scheitert dann reproduzierbar mit "Network is unreachable".
    # Ein einzelner Versuch wuerde die Integration in ERROR nageln, ohne
    # dass jemals wieder ein Versuch folgt.
    #
    # 2s + 4s + 6s + 8s = max. 20s Wartezeit ueber 5 Versuche.
    cache_ok = False
    for attempt in range(5):
        try:
            await client.refresh_channel_cache()
        except Exception as exc:
            _LOG.warning(
                "Channel cache refresh attempt %d/5 failed: %s",
                attempt + 1,
                exc,
            )
        else:
            if client.channel_count > 0:
                cache_ok = True
                if attempt > 0:
                    _LOG.info(
                        "Channel cache ready after %d attempts (%d channels)",
                        attempt + 1,
                        client.channel_count,
                    )
                break
            _LOG.warning(
                "Channel cache empty on attempt %d/5 - network not up yet?",
                attempt + 1,
            )
        if attempt < 4:
            await asyncio.sleep(2 * (attempt + 1))

    if not cache_ok:
        _LOG.error(
            "Channel cache still empty after 5 attempts - "
            "check network, API key and URL"
        )
        await api.set_device_state(DeviceStates.ERROR)
        return False

    # Logo proxy starten - liefert quadratisch-gepaddete Logos für die Remote
    logo_proxy = LogoProxy(cfg.url, port=cfg.logo_proxy_port)
    try:
        await logo_proxy.start()
    except Exception as exc:
        _LOG.error("Logo proxy failed to start: %s", exc)
        # Nicht fatal - wir laufen ohne Logos weiter
        logo_proxy = None

    # Standby-Event initialisieren (gesetzt = aktiv, clear = pausiert)
    global _active_event
    _active_event = asyncio.Event()
    _active_event.set()

    _cache_task = loop.create_task(_cache_refresh_loop())
    _sync_poll_task()
    await api.set_device_state(DeviceStates.CONNECTED)
    _LOG.info("Runtime started")
    return True


def _sync_poll_task() -> None:
    """
    Startet/stoppt den Poll-Loop passend zur Media-Player-Subscription.

    Der Button allein braucht kein Polling - der holt den aktuellen
    Kanal beim Druck ab. Nur das Widget muss laufend aktualisiert
    werden. Damit kostet ein Button auf einer UI-Page keinen einzigen
    zusätzlichen Request im Leerlauf.
    """
    global _poll_task

    want_poll = ENTITY_ID in _subscribed
    if want_poll and (_poll_task is None or _poll_task.done()):
        _poll_task = loop.create_task(_poll_loop())
        _LOG.info("Poll loop started (media player subscribed)")
    elif not want_poll and _poll_task is not None and not _poll_task.done():
        _poll_task.cancel()
        _poll_task = None
        _LOG.info("Poll loop stopped (media player not subscribed)")


async def _stop_runtime() -> None:
    global client, logo_proxy, _poll_task, _cache_task, _active_event, _epg_cache
    for t in (_poll_task, _cache_task):
        if t and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
    _poll_task = None
    _cache_task = None
    _active_event = None
    _epg_cache = {}
    # Quellen-State verwerfen: nach einem Neustart der Runtime kann
    # längst ein anderer Sender laufen.
    global _current_channel_uuid, _current_channel_id, _current_stream_id
    _current_channel_uuid = None
    _current_channel_id = None
    _current_stream_id = None
    _source_cache.clear()
    if logo_proxy:
        try:
            await logo_proxy.stop()
        except Exception:
            pass
        logo_proxy = None
    if client:
        await client.close()
        client = None
    _LOG.info("Runtime stopped")


@api.listens_to(Events.CONNECT)
async def on_connect() -> None:
    """
    Remote stellt WebSocket-Verbindung zum Driver her.

    Wichtig: Wir starten hier NICHT die Runtime. Das macht der
    SUBSCRIBE_ENTITIES Handler erst wenn die Remote tatsächlich
    auf eine Entity zugreifen will.

    So pollen wir Dispatcharr nur dann wenn das Widget irgendwo
    sichtbar ist - in anderen Activities tun wir nichts. Kodi
    folgt demselben Pattern (device.connect() in SUBSCRIBE).
    """
    _LOG.info("Remote connected")
    if cfg.is_configured():
        await api.set_device_state(DeviceStates.CONNECTED)
    else:
        await api.set_device_state(DeviceStates.DISCONNECTED)


@api.listens_to(Events.DISCONNECT)
async def on_disconnect() -> None:
    _LOG.info("Remote disconnected")
    # Anders als bei Standby ist die Subscription hier tatsächlich weg.
    _subscribed.clear()
    await _stop_runtime()


@api.listens_to(Events.ENTER_STANDBY)
async def on_enter_standby() -> None:
    """
    Remote geht in Standby - Display aus, WLAN ggf. Power-Managed.

    Laut UC-Doku: "the WebSocket connection might get disconnected
    during remote standby!" - daher offizielles Pattern aus dem
    Home-Assistant Integration Issue #50: komplett vom Backend
    trennen, beim EXIT_STANDBY wieder hochfahren.

    Das stoppt das Polling von Dispatcharr während die Remote schläft.
    Sonst würde der externe Container munter weiter pollen obwohl
    niemand die Daten braucht.
    """
    _LOG.info("Remote entering standby - stopping runtime")
    await _stop_runtime()


@api.listens_to(Events.EXIT_STANDBY)
async def on_exit_standby() -> None:
    """
    Remote ist wieder aktiv - Runtime komplett hochfahren.

    Laut Sequenzdiagramm der UC-Doku ruft die Remote nach exit_standby
    typischerweise get_device_state und get_entity_states auf.
    Unser _start_runtime() setzt device_state=CONNECTED und der erste
    Poll-Zyklus liefert die entity_states.
    """
    if not _subscribed:
        # Nichts abonniert (z.B. andere Activity aktiv) - dann braucht
        # auch niemand Senderliste, Logo-Proxy oder Cache-Loop.
        # on_subscribe startet die Runtime, sobald sie gebraucht wird.
        _LOG.info("Remote exiting standby - no subscribed entities, staying idle")
        return
    _LOG.info("Remote exiting standby - starting runtime")
    if cfg.is_configured():
        await _start_runtime()


async def _immediate_refresh_with_retry() -> None:
    """Versucht bis zu 3x das Widget mit aktuellem State zu befüllen."""
    for attempt in range(3):
        try:
            await _poll_once()
            _LOG.info("Wakeup-Refresh erfolgreich (attempt %d)", attempt + 1)
            return
        except Exception as exc:
            _LOG.warning(
                "Wakeup-Refresh attempt %d failed: %s", attempt + 1, exc
            )
        # 2s, 4s, 8s zwischen Versuchen (Backoff)
        await asyncio.sleep(2 * (attempt + 1))
    _LOG.warning("Wakeup-Refresh: alle Versuche fehlgeschlagen")


@api.listens_to(Events.SUBSCRIBE_ENTITIES)
async def on_subscribe(entity_ids: list[str]) -> None:
    """
    Remote subscribed eine Entity - das Widget ist sichtbar oder
    wird sichtbar. Kodi-Pattern: stelle sicher dass die Runtime
    läuft (Backend-Verbindung steht), damit Daten geliefert werden.

    Wenn die Runtime schon läuft (z.B. weil eine andere Activity
    auch die Dispatcharr-Entity benutzt), ist das ein No-Op.
    """
    _LOG.info("Subscribe: %s", entity_ids)
    ours = {e for e in entity_ids if e in (ENTITY_ID, BUTTON_ID)}
    if not ours:
        return

    _subscribed.update(ours)

    # Runtime starten falls noch nicht aktiv (Kodi macht es genauso
    # mit device.connect() pro Device)
    if client is None and cfg.is_configured():
        _LOG.info("Subscribe triggered runtime start")
        await _start_runtime()
        return

    # Runtime läuft schon - Poll-Loop an die neue Subscription anpassen
    _sync_poll_task()

    # Sofort einen Poll machen damit die Remote nicht 10s leer wartet
    # bis zum nächsten regulären Zyklus
    if client and ENTITY_ID in ours:
        try:
            await _poll_once()
        except Exception as exc:
            _LOG.warning("initial poll on subscribe failed: %s", exc)


@api.listens_to(Events.UNSUBSCRIBE_ENTITIES)
async def on_unsubscribe(entity_ids: list[str]) -> None:
    """
    Remote unsubscribed eine Entity - das Widget ist nicht mehr
    sichtbar (z.B. Activity-Wechsel weg von TV). Kodi-Pattern:
    wenn keine anderen Entities mehr abonniert sind die das gleiche
    Device brauchen, Runtime stoppen.

    Ist nur der Button weg, läuft die Runtime weiter; ist nur das
    Widget weg, stoppt nur der Poll-Loop. Erst wenn keine unserer
    Entities mehr abonniert ist, wird die Runtime komplett gestoppt.

    Das ist der eigentliche Akku-Spar-Mechanismus: in der Kodi-
    oder anderen Activity läuft KEIN Polling von Dispatcharr.
    """
    _LOG.info("Unsubscribe: %s", entity_ids)
    ours = {e for e in entity_ids if e in (ENTITY_ID, BUTTON_ID)}
    if not ours:
        return

    _subscribed.difference_update(ours)

    if _subscribed:
        # Noch mindestens eine Entity aktiv - nur den Poll-Loop
        # nachziehen (z.B. Widget weg, Button bleibt).
        _sync_poll_task()
        return

    _LOG.info("Last entity unsubscribed - stopping runtime to save battery")
    await _stop_runtime()


# ----------------------------------------------------------------------
# Setup Flow
# ----------------------------------------------------------------------
async def setup_handler(msg: SetupDriver) -> SetupAction:
    """
    Wird von der Remote beim ersten Setup und bei Re-Configure aufgerufen.
    Wir nehmen die Werte aus dem Setup-Form, validieren sie gegen die
    Dispatcharr API und persistieren sie.
    """
    if isinstance(msg, DriverSetupRequest):
        return await _handle_setup(msg.setup_data)
    if isinstance(msg, UserDataResponse):
        return await _handle_setup(msg.input_values)
    return SetupError(error_type=IntegrationSetupError.OTHER)


async def _handle_setup(data: dict[str, str]) -> SetupAction:
    global cfg
    _LOG.info("Setup data received: keys=%s", list(data.keys()))

    try:
        poll_interval = int(float(data.get("poll_interval") or 10))
    except (TypeError, ValueError):
        _LOG.error("Setup: invalid poll interval %r", data.get("poll_interval"))
        return SetupError(error_type=IntegrationSetupError.OTHER)
    # Gleiche Grenzen wie im setup_data_schema (driver.json)
    poll_interval = max(5, min(60, poll_interval))

    new = DriverConfig(
        url=str(data.get("url", "")).strip().rstrip("/"),
        api_key=str(data.get("api_key", "")).strip(),
        client_ip=str(data.get("client_ip", "")).strip(),
        poll_interval=poll_interval,
    )

    if not new.url or not new.api_key or not new.client_ip:
        _LOG.error("Setup incomplete")
        return SetupError(error_type=IntegrationSetupError.OTHER)

    # Validate against Dispatcharr
    test_client = DispatcharrClient(new.url, new.api_key, timeout=8.0)
    try:
        await test_client.refresh_channel_cache()
        if test_client.channel_count == 0:
            _LOG.error("Setup validation: channel cache empty (auth failed?)")
            return SetupError(
                error_type=IntegrationSetupError.AUTHORIZATION_ERROR
            )
    except Exception as exc:
        _LOG.error("Setup validation failed: %s", exc)
        return SetupError(error_type=IntegrationSetupError.CONNECTION_REFUSED)
    finally:
        await test_client.close()

    cfg = new
    save_config(cfg)
    await _start_runtime()
    return SetupComplete()


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
async def main() -> None:
    # Entities registrieren - immer beim Start aus der lokalen Config,
    # unabhängig von der Erreichbarkeit von Dispatcharr. Sonst kann der
    # Core abonnieren bevor die Entities existieren.
    api.available_entities.add(_make_entity())
    api.available_entities.add(_make_button())

    # API starten
    driver_path = os.path.join(os.path.dirname(__file__), "driver.json")
    if not os.path.isfile(driver_path):
        # In gepackter Form liegt driver.json in ./bin/ neben driver
        driver_path = os.path.join(os.getcwd(), "driver.json")
    await api.init(driver_path, setup_handler)


if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
        loop.run_forever()
    except KeyboardInterrupt:
        _LOG.info("Shutdown requested")
    finally:
        try:
            loop.run_until_complete(_stop_runtime())
        except Exception:
            pass
        loop.close()
