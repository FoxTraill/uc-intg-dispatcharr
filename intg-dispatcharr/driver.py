#!/usr/bin/env python3
"""
Dispatcharr Now Playing - UC Remote 3 Custom Integration.

Provides a media player entity that polls and shows the active
Dispatcharr stream of the configured client IP (e.g. an NVIDIA Shield),
plus a button for switching sources. Playback itself is still controlled
by the player's own integration (e.g. ADB Bridge).
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
# UC convention: log level configurable via UC_LOG_LEVEL (DEBUG, INFO, ...)
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

# Separate button entity: can be placed freely on UI pages and used in
# activities/macros without needing the media player.
BUTTON_ID = "dispatcharr_next_source"
BUTTON_NAME = {"en": "Next Source", "de": "Nächste Quelle"}

# Which of our entities the remote has currently subscribed.
# The poll loop only runs while the media player is among them - the
# button alone needs no polling, it fetches the current channel when
# pressed.
_subscribed: set[str] = set()

# Currently running channel/source (from the last status poll).
_current_channel_uuid: Optional[str] = None
_current_channel_id: Optional[int] = None
_current_stream_id: Optional[int] = None

# Source list per channel: channel_id -> (streams, fetch_timestamp)
# Changes rarely, but not never (providers come and go).
_source_cache: dict[int, tuple[list[dict[str, Any]], float]] = {}
_SOURCE_TTL_SECONDS = 600.0

# Polling tasks
_poll_task: Optional[asyncio.Task] = None
_cache_task: Optional[asyncio.Task] = None
# Standby pause: when set, polling runs normally. When cleared, the
# poll loop waits until EXIT_STANDBY sets the event again.
_active_event: Optional[asyncio.Event] = None
# Counter for EPG throttling: not every poll fetches EPG, only every Nth
_poll_counter: int = 0

# Last known state (compared before sending entity_change events)
_last_attrs: dict[str, Any] = {}


# ----------------------------------------------------------------------
# Entity factory
# ----------------------------------------------------------------------
def _make_entity() -> media_player.MediaPlayer:
    """
    Media player entity.

    Displaying is still the core (logo, EPG title, progress). On top of
    that there are active commands for switching the Dispatcharr source of
    the running channel:

    - NEXT     -> next source (⏭ in the widget, one-tap solution)
    - PREVIOUS -> previous source (⏮ in the widget)
    - PLAY_PAUSE -> switches to next source (renders the transport row)
    - SELECT_SOURCE -> pick a specific source from the list
    """
    return media_player.MediaPlayer(
        identifier=ENTITY_ID,
        name=ENTITY_NAME,
        features=[
            # Passive display
            media_player.Features.MEDIA_TITLE,
            media_player.Features.MEDIA_ARTIST,
            media_player.Features.MEDIA_IMAGE_URL,
            media_player.Features.MEDIA_DURATION,
            media_player.Features.MEDIA_POSITION,
            media_player.Features.MEDIA_TYPE,
            # Source switching & Transport controls (füllt den Freiraum)
            media_player.Features.NEXT,
            media_player.Features.PREVIOUS,
            media_player.Features.SELECT_SOURCE,
            media_player.Features.PLAY_PAUSE,
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
    """Standalone 'Next Source' button for UI pages and macros."""
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
    """Source switching commands; everything else is rejected."""
    if cmd_id == media_player.Commands.NEXT:
        return await _switch_relative(+1)
    if cmd_id == media_player.Commands.PREVIOUS:
        return await _switch_relative(-1)
    if cmd_id == media_player.Commands.SELECT_SOURCE:
        source = (params or {}).get("source")
        if not source:
            return StatusCodes.BAD_REQUEST
        return await _switch_to_source_name(str(source))

    # Tapping the logo or pressing play/pause in the media widget
    # switches to the next source.
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
    Button press = next source.

    The button can be subscribed without the media player (and thus
    without a poll loop). So we fetch the currently running channel
    here if needed, instead of relying on the last polled state.
    """
    if cmd_id != button.Commands.PUSH:
        return StatusCodes.NOT_IMPLEMENTED
    return await _switch_relative(+1, allow_refresh=True)


async def _resolve_current_channel(allow_refresh: bool) -> Optional[str]:
    """
    Returns the UUID of the channel currently playing on our client IP.

    Usually this is known from the last poll. If not (button without
    poll loop), we fetch the status once.
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
    Switches the source by +1 / -1 in the channel order.

    Forward uses the Dispatcharr endpoint next_stream - it rotates
    server-side including wrap-around and knows the current stream from
    Redis, which is more robust than our local view.

    There is no endpoint for backward, so we compute the index ourselves
    and switch explicitly via change_stream.
    """
    channel_uuid = await _resolve_current_channel(allow_refresh)
    if not channel_uuid or client is None:
        _LOG.info("Source switch requested but no channel is playing")
        return StatusCodes.SERVICE_UNAVAILABLE

    if offset > 0:
        ok, msg = await client.next_stream(channel_uuid)
        if ok:
            _schedule_refresh_after_switch()
            return StatusCodes.OK
        # "No alternate streams available" is not a server error, it
        # simply means the channel has only one source.
        if "alternate" in msg.lower():
            return StatusCodes.NOT_FOUND
        return StatusCodes.SERVER_ERROR

    # Backward: determine the position in the source list ourselves
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
    _schedule_refresh_after_switch()
    return StatusCodes.OK


async def _switch_to_source_name(name: str) -> StatusCodes:
    """SELECT_SOURCE: map a display name from source_list back to a stream_id."""
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
    _schedule_refresh_after_switch()
    return StatusCodes.OK


# Background refreshes after a switch (referenced so they aren't
# garbage collected while running).
_refresh_tasks: set[asyncio.Task] = set()


def _schedule_refresh_after_switch() -> None:
    """
    Refreshes the widget after a switch without delaying the command
    response.
    """
    task = loop.create_task(_refresh_after_switch())
    _refresh_tasks.add(task)
    task.add_done_callback(_refresh_tasks.discard)


async def _refresh_after_switch() -> None:
    """
    Waits briefly after a switch and fetches the state again.
    """
    global _last_attrs
    await asyncio.sleep(1.5)
    try:
        _last_attrs = {}
        await _poll_once()
    except Exception as exc:
        _LOG.warning("Refresh after source switch failed: %s", exc)


# ----------------------------------------------------------------------
# Polling
# ----------------------------------------------------------------------
def _source_labels(sources: list[dict[str, Any]]) -> list[str]:
    """
    Display names for the source list.

    Format: "<priority>. <provider> - <stream name>"
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
    """Source list of a channel with TTL cache (see _SOURCE_TTL_SECONDS)."""
    import time

    if channel_id is None or client is None:
        return None

    now = time.time()
    cached = _source_cache.get(channel_id)
    if cached is not None and (now - cached[1]) < _SOURCE_TTL_SECONDS:
        return cached[0]

    sources = await client.get_channel_streams(channel_id)
    if sources is None:
        return cached[0] if cached else None

    _source_cache[channel_id] = (sources, now)
    return sources


def _parse_iso(ts: str) -> Optional[datetime]:
    """Dispatcharr returns UTC timestamps like '2026-04-26T11:05:00Z'."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _source_line(
    sources: Optional[list[dict[str, Any]]],
    active_idx: Optional[int],
    stream: dict[str, Any],
) -> str:
    """
    Short form of the active source for the line below the title.

    Format: "<stream name> · <position>/<total> · <provider>"
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
        parts.append(f"{active_idx + 1}/{len(sources)}")
        if provider:
            parts.append(provider)
    else:
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
    """Maps API data to media player attributes."""
    has_logo = bool(channel_info and channel_info.get("logo_id") is not None and logo_proxy)

    if channel_info and channel_info.get("name"):
        channel_name = channel_info["name"]
    else:
        channel_name = stream.get("stream_name") or "Unknown"

    logo_url = ""
    if has_logo:
        cname_lower = (channel_info.get("name") or "").lower()
        raw = "ruler" in cname_lower or "widget_test" in cname_lower
        logo_url = logo_proxy.url_for(channel_info["logo_id"], raw=raw)

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

    if not title:
        title = channel_name
        artist = ""
    elif has_logo:
        artist = ""
    else:
        artist = channel_name

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
            source = stream.get("stream_name") or ""

    source_line = _source_line(sources, active_idx, stream)
    if source_line:
        if has_logo:
            artist = source_line
        else:
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


# EPG cache: channel_uuid -> (program_dict, fetch_timestamp)
_epg_cache: dict[str, tuple[Optional[dict[str, Any]], float]] = {}
_EPG_TTL_SECONDS = 60.0


async def _get_program_cached(channel_uuid: str) -> Optional[dict[str, Any]]:
    """Fetches the EPG program for a channel, with a local cache."""
    import time
    from datetime import datetime, timezone
    now = time.time()
    cached = _epg_cache.get(channel_uuid)
    if cached is not None:
        prog, ts = cached
        cache_age = now - ts

        if cache_age < _EPG_TTL_SECONDS and prog is None:
            return None
        if cache_age < _EPG_TTL_SECONDS:
            end_str = prog.get("end_time", "")
            if end_str:
                try:
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    now_utc = datetime.now(timezone.utc)
                    if now_utc < end_dt:
                        return prog
                    _LOG.info(
                        "EPG cache for %s expired (program ended %s)",
                        channel_uuid, end_str,
                    )
                except Exception:
                    return prog
            else:
                return prog

    prog = await client.get_current_program_for(channel_uuid)
    _epg_cache[channel_uuid] = (prog, now)
    return prog


async def _poll_once() -> None:
    """One polling cycle: fetch streams, filter, update the entity."""
    if client is None:
        return

    streams = await client.get_active_streams()

    if streams is None:
        _LOG.debug("Streams unavailable (network error?), keeping last state")
        return

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

    if channel_uuid and channel_info is None:
        _LOG.info("Unknown channel uuid %s, refreshing cache", channel_uuid)
        await client.refresh_channel_cache()
        channel_info = client.get_channel_info(channel_uuid)

    _current_channel_uuid = channel_uuid
    _current_channel_id = channel_info.get("id") if channel_info else None
    raw_stream_id = matching.get("stream_id")
    _current_stream_id = int(raw_stream_id) if raw_stream_id is not None else None

    program = (
        await _get_program_cached(channel_uuid)
        if channel_uuid
        else None
    )

    sources = await _get_sources_cached(_current_channel_id)

    attrs = _build_attrs_from_stream_and_program(
        matching, channel_info, program, sources
    )
    await _push(attrs)


async def _poll_loop() -> None:
    """Polls endlessly every cfg.poll_interval seconds."""
    _LOG.info("Poll loop started (every %ds)", cfg.poll_interval)
    while True:
        try:
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
    """Periodically reloads the channel/logo cache (logos can change)."""
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
    global client, logo_proxy, _poll_task, _cache_task

    if not cfg.is_configured():
        _LOG.warning("Cannot start runtime: not configured")
        await api.set_device_state(DeviceStates.ERROR)
        return False

    await _stop_runtime()

    client = DispatcharrClient(cfg.url, cfg.api_key)

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

    logo_proxy = LogoProxy(cfg.url, port=cfg.logo_proxy_port)
    try:
        await logo_proxy.start()
    except Exception as exc:
        _LOG.error("Logo proxy failed to start: %s", exc)
        logo_proxy = None

    global _active_event
    _active_event = asyncio.Event()
    _active_event.set()

    _cache_task = loop.create_task(_cache_refresh_loop())
    _sync_poll_task()
    await api.set_device_state(DeviceStates.CONNECTED)
    _LOG.info("Runtime started")
    return True


def _sync_poll_task() -> None:
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
    _LOG.info("Remote connected")
    if cfg.is_configured():
        await api.set_device_state(DeviceStates.CONNECTED)
    else:
        await api.set_device_state(DeviceStates.DISCONNECTED)


@api.listens_to(Events.DISCONNECT)
async def on_disconnect() -> None:
    _LOG.info("Remote disconnected")
    _subscribed.clear()
    await _stop_runtime()


@api.listens_to(Events.ENTER_STANDBY)
async def on_enter_standby() -> None:
    _LOG.info("Remote entering standby - stopping runtime")
    await _stop_runtime()


@api.listens_to(Events.EXIT_STANDBY)
async def on_exit_standby() -> None:
    if not _subscribed:
        _LOG.info("Remote exiting standby - no subscribed entities, staying idle")
        return
    _LOG.info("Remote exiting standby - starting runtime")
    if cfg.is_configured():
        await _start_runtime()


@api.listens_to(Events.SUBSCRIBE_ENTITIES)
async def on_subscribe(entity_ids: list[str]) -> None:
    _LOG.info("Subscribe: %s", entity_ids)
    ours = {e for e in entity_ids if e in (ENTITY_ID, BUTTON_ID)}
    if not ours:
        return

    _subscribed.update(ours)

    if client is None and cfg.is_configured():
        _LOG.info("Subscribe triggered runtime start")
        await _start_runtime()
        return

    _sync_poll_task()

    if client and ENTITY_ID in ours:
        try:
            await _poll_once()
        except Exception as exc:
            _LOG.warning("initial poll on subscribe failed: %s", exc)


@api.listens_to(Events.UNSUBSCRIBE_ENTITIES)
async def on_unsubscribe(entity_ids: list[str]) -> None:
    _LOG.info("Unsubscribe: %s", entity_ids)
    ours = {e for e in entity_ids if e in (ENTITY_ID, BUTTON_ID)}
    if not ours:
        return

    _subscribed.difference_update(ours)

    if _subscribed:
        _sync_poll_task()
        return

    _LOG.info("Last entity unsubscribed - stopping runtime to save battery")
    await _stop_runtime()


# ----------------------------------------------------------------------
# Setup Flow
# ----------------------------------------------------------------------
async def setup_handler(msg: SetupDriver) -> SetupAction:
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
    api.available_entities.add(_make_entity())
    api.available_entities.add(_make_button())

    driver_path = os.path.join(os.path.dirname(__file__), "driver.json")
    if not os.path.isfile(driver_path):
        driver_path = os.path.join(os.getcwd(), "driver.json")
    await api.init(driver_path, setup_handler)


def _self_test() -> int:
    """
    Build check, run with --self-test: processes sample logos in all
    formats Dispatcharr typically serves with the bundled Pillow, then
    exits.
    """
    import io

    from PIL import Image

    samples = (("PNG", "RGBA"), ("JPEG", "RGB"), ("WEBP", "RGBA"), ("GIF", "P"))
    for fmt, mode in samples:
        buf = io.BytesIO()
        Image.new(mode, (200, 80), 1 if mode == "P" else (200, 30, 30)).save(buf, format=fmt)
        size = Image.open(io.BytesIO(LogoProxy._pad_to_canvas(buf.getvalue()))).size
        if size != (512, 512):
            print(f"Self-test FAILED: {fmt} logo -> {size}")
            return 1
    print("Self-test OK")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
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
