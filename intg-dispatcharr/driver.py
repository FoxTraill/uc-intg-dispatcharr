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
    that there are exactly three active commands, all for switching the
    Dispatcharr source of the running channel:

    - NEXT     -> next source (⏭ in the widget, one-tap solution)
    - PREVIOUS -> previous source
    - SELECT_SOURCE -> pick a specific source from the list

    No PLAY/PAUSE/STOP - playback is still controlled by the player
    itself (e.g. ADB Bridge in the activity).
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
            # Source switching
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

    # Tapping the logo in the media widget sends play_pause - the
    # firmware has no dedicated command for it and the artwork action is
    # hard-wired. We have no playback control (that runs through the
    # player's integration), so play_pause is free here and mapped to
    # source switching: tap on the logo = next source.
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
            await _refresh_after_switch()
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
    await _refresh_after_switch()
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
    await _refresh_after_switch()
    return StatusCodes.OK


async def _refresh_after_switch() -> None:
    """
    Waits briefly after a switch and fetches the state again.

    Dispatcharr only confirms the switch once the new upstream is
    actually open; an immediate poll would still read the old stream_id
    from Redis.
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
    Display names for the source list.

    Format: "<priority>. <provider> - <stream name>"

    The priority comes first because it shows the failover order and
    guarantees unique labels (required for mapping back on
    SELECT_SOURCE). The provider comes before the stream name because
    different providers often name the same channel completely
    differently - the provider is the more stable feature to recognize
    a source by.
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
        # Error: keep the old cache instead of clearing the list
        return cached[0] if cached else None

    _source_cache[channel_id] = (sources, now)
    return sources


def _parse_iso(ts: str) -> Optional[datetime]:
    """Dispatcharr returns UTC timestamps like '2026-04-26T11:05:00Z'."""
    if not ts:
        return None
    try:
        # Python 3.11+ accepts 'Z' directly; replace it for older versions
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
    Example: "RTL · 2/7 · TS4_proxy_AUS"

    Space on the display is limited, so this is deliberately compact.
    Unknown parts are left out instead of showing placeholders - with
    sparse data the line stays short instead of filled with question
    marks.
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
        # Show the position even with a single source ("1/1") - that
        # tells at a glance that there is no alternative.
        parts.append(f"{active_idx + 1}/{len(sources)}")
        if provider:
            parts.append(provider)
    else:
        # Active stream_id not in the list (e.g. during a failover) -
        # at least show the raw name from the status.
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
    # Channel name: only as fallback when there is NO logo (redundant otherwise)
    has_logo = bool(channel_info and channel_info.get("logo_id") is not None and logo_proxy)

    if channel_info and channel_info.get("name"):
        channel_name = channel_info["name"]
    else:
        channel_name = stream.get("stream_name") or "Unknown"

    # Logo - via the local proxy with normalized size.
    # Test mode: if the channel name contains "ruler" or "widget_test",
    # serve the original image unprocessed (for measuring the widget).
    logo_url = ""
    if has_logo:
        cname_lower = (channel_info.get("name") or "").lower()
        raw = "ruler" in cname_lower or "widget_test" in cname_lower
        logo_url = logo_proxy.url_for(channel_info["logo_id"], raw=raw)

    # Program title (title line - main display)
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

    # No program: channel name as title, no artist
    # Program + logo: program as title, NO artist (the logo shows the channel)
    # Program but no logo: program as title, channel as artist
    if not title:
        title = channel_name
        artist = ""
    elif has_logo:
        artist = ""
    else:
        artist = channel_name

    # Source list + currently active source.
    # The active source is determined via the stream_id from the status
    # poll, not via the name - names are often duplicated across
    # providers.
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
            # Source is running but not in the list (e.g. just
            # removed) - show the raw name from the status
            source = stream.get("stream_name") or ""

    # Line below the title: which source is currently running. This field
    # used to be empty when there is a logo (the channel name would be
    # redundant next to the logo) - that space is used for it now.
    source_line = _source_line(sources, active_idx, stream)
    if source_line:
        if has_logo:
            artist = source_line
        else:
            # Without a logo nothing else shows the channel name when a
            # program title is shown above - so show both.
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
# Avoids polling the 200KB EPG JSON every 10s.
_epg_cache: dict[str, tuple[Optional[dict[str, Any]], float]] = {}
_EPG_TTL_SECONDS = 60.0


async def _get_program_cached(channel_uuid: str) -> Optional[dict[str, Any]]:
    """
    Fetches the EPG program for a channel, with a local cache.

    The cache is invalidated when:
    1. It is older than _EPG_TTL_SECONDS (default 60s)
    2. The cached program has already ended according to its end_time
       (which means a new program has started)

    Saves bandwidth while watching continuously, but still makes sure
    program changes are detected.
    """
    import time
    from datetime import datetime, timezone
    now = time.time()
    cached = _epg_cache.get(channel_uuid)
    if cached is not None:
        prog, ts = cached
        cache_age = now - ts

        # TTL check. "No program" (None) is cached as well - otherwise a
        # channel without EPG re-fetches the ~200KB response on every poll.
        if cache_age < _EPG_TTL_SECONDS and prog is None:
            return None
        if cache_age < _EPG_TTL_SECONDS:
            # Also check end_time - if the program has already ended
            # according to the EPG, reload (program change!)
            end_str = prog.get("end_time", "")
            if end_str:
                try:
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    now_utc = datetime.now(timezone.utc)
                    if now_utc < end_dt:
                        # Program still running according to EPG -> use cache
                        return prog
                    # Otherwise fall through -> cache is stale, fetch again
                    _LOG.info(
                        "EPG cache for %s expired (program ended %s)",
                        channel_uuid, end_str,
                    )
                except Exception:
                    # On parse errors: use the cache anyway
                    return prog
            else:
                return prog

    # Cache miss / expired: fetch again
    prog = await client.get_current_program_for(channel_uuid)
    _epg_cache[channel_uuid] = (prog, now)
    return prog


async def _poll_once() -> None:
    """One polling cycle: fetch streams, filter, update the entity."""
    if client is None:
        return

    streams = await client.get_active_streams()

    # None = network/HTTP error, NOT "no active streams".
    # Keep the last known state so the logo doesn't suddenly disappear,
    # e.g. when Wi-Fi is not fully up after wakeup or Dispatcharr is
    # briefly unreachable.
    if streams is None:
        _LOG.debug("Streams unavailable (network error?), keeping last state")
        return

    # Find the stream that belongs to our client IP
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

    # If unknown: cache may be stale -> trigger a refresh
    if channel_uuid and channel_info is None:
        _LOG.info("Unknown channel uuid %s, refreshing cache", channel_uuid)
        await client.refresh_channel_cache()
        channel_info = client.get_channel_info(channel_uuid)

    # Remember the current channel/source - all switch commands refer
    # to it.
    _current_channel_uuid = channel_uuid
    _current_channel_id = channel_info.get("id") if channel_info else None
    raw_stream_id = matching.get("stream_id")
    _current_stream_id = int(raw_stream_id) if raw_stream_id is not None else None

    # EPG from the cache (60s TTL) instead of fetching the ~200KB EPG
    # response from Dispatcharr every time
    program = (
        await _get_program_cached(channel_uuid)
        if channel_uuid
        else None
    )

    # Source list from the cache (10min TTL) - one request per channel,
    # not per poll.
    sources = await _get_sources_cached(_current_channel_id)

    attrs = _build_attrs_from_stream_and_program(
        matching, channel_info, program, sources
    )
    await _push(attrs)


async def _poll_loop() -> None:
    """
    Polls endlessly every cfg.poll_interval seconds.

    Respects _active_event - in standby the loop is paused and only
    resumes once the event is set again.
    """
    _LOG.info("Poll loop started (every %ds)", cfg.poll_interval)
    while True:
        try:
            # In standby: wait until EXIT_STANDBY sets the event again
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
    """
    Initializes the Dispatcharr client and starts the polling tasks.
    Called on subscribe, on exit_standby and after a successful setup.
    """
    global client, logo_proxy, _poll_task, _cache_task

    if not cfg.is_configured():
        _LOG.warning("Cannot start runtime: not configured")
        await api.set_device_state(DeviceStates.ERROR)
        return False

    # Clean up previous tasks, if any
    await _stop_runtime()

    client = DispatcharrClient(cfg.url, cfg.api_key)

    # Load the initial cache - with backoff retry.
    #
    # On-device, _start_runtime() runs right on the EXIT_STANDBY event,
    # and the remote only re-establishes its Wi-Fi afterwards. The first
    # request then reliably fails with "Network is unreachable". A single
    # attempt would leave the integration stuck in ERROR without any
    # further attempt.
    #
    # 2s + 4s + 6s + 8s = at most 20s of waiting across 5 attempts.
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

    # Start the logo proxy - serves processed logos to the remote
    logo_proxy = LogoProxy(cfg.url, port=cfg.logo_proxy_port)
    try:
        await logo_proxy.start()
    except Exception as exc:
        _LOG.error("Logo proxy failed to start: %s", exc)
        # Not fatal - we continue without logos
        logo_proxy = None

    # Initialize the standby event (set = active, cleared = paused)
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
    Starts/stops the poll loop according to the media player subscription.

    The button alone needs no polling - it fetches the current channel
    when pressed. Only the widget has to be updated continuously. So a
    button on a UI page costs not a single extra request while idle.
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
    # Discard the source state: after a runtime restart a different
    # channel may be playing by now.
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
    The remote establishes the WebSocket connection to the driver.

    Important: we do NOT start the runtime here. The SUBSCRIBE_ENTITIES
    handler does that once the remote actually wants to use an entity.

    This way we only poll Dispatcharr while the widget is visible
    somewhere - in other activities we do nothing. The Kodi integration
    follows the same pattern (device.connect() in SUBSCRIBE).
    """
    _LOG.info("Remote connected")
    if cfg.is_configured():
        await api.set_device_state(DeviceStates.CONNECTED)
    else:
        await api.set_device_state(DeviceStates.DISCONNECTED)


@api.listens_to(Events.DISCONNECT)
async def on_disconnect() -> None:
    _LOG.info("Remote disconnected")
    # Unlike in standby, the subscription is really gone here.
    _subscribed.clear()
    await _stop_runtime()


@api.listens_to(Events.ENTER_STANDBY)
async def on_enter_standby() -> None:
    """
    The remote goes into standby - display off, Wi-Fi possibly power
    managed.

    According to the UC docs: "the WebSocket connection might get
    disconnected during remote standby!" - hence the official pattern
    from Home Assistant integration issue #50: disconnect from the
    backend completely and start again on EXIT_STANDBY.

    This stops polling Dispatcharr while the remote sleeps, when nobody
    needs the data.
    """
    _LOG.info("Remote entering standby - stopping runtime")
    await _stop_runtime()


@api.listens_to(Events.EXIT_STANDBY)
async def on_exit_standby() -> None:
    """
    The remote is active again - start the runtime completely.

    According to the sequence diagram in the UC docs, the remote usually
    calls get_device_state and get_entity_states after exit_standby.
    Our _start_runtime() sets device_state=CONNECTED and the first poll
    cycle provides the entity states.
    """
    if not _subscribed:
        # Nothing subscribed (e.g. another activity is active) - then
        # nobody needs the channel list, logo proxy or cache loop either.
        # on_subscribe starts the runtime as soon as it is needed.
        _LOG.info("Remote exiting standby - no subscribed entities, staying idle")
        return
    _LOG.info("Remote exiting standby - starting runtime")
    if cfg.is_configured():
        await _start_runtime()


async def _immediate_refresh_with_retry() -> None:
    """Tries up to 3 times to fill the widget with the current state."""
    for attempt in range(3):
        try:
            await _poll_once()
            _LOG.info("Wakeup refresh successful (attempt %d)", attempt + 1)
            return
        except Exception as exc:
            _LOG.warning(
                "Wakeup-Refresh attempt %d failed: %s", attempt + 1, exc
            )
        # 2s, 4s, 6s between attempts (backoff)
        await asyncio.sleep(2 * (attempt + 1))
    _LOG.warning("Wakeup refresh: all attempts failed")


@api.listens_to(Events.SUBSCRIBE_ENTITIES)
async def on_subscribe(entity_ids: list[str]) -> None:
    """
    The remote subscribes an entity - the widget is or becomes visible.
    Kodi pattern: make sure the runtime is running (backend connection
    established) so data is delivered.

    If the runtime is already running (e.g. because another activity
    also uses the Dispatcharr entity), this is a no-op.
    """
    _LOG.info("Subscribe: %s", entity_ids)
    ours = {e for e in entity_ids if e in (ENTITY_ID, BUTTON_ID)}
    if not ours:
        return

    _subscribed.update(ours)

    # Start the runtime if not active yet (Kodi does the same with
    # device.connect() per device)
    if client is None and cfg.is_configured():
        _LOG.info("Subscribe triggered runtime start")
        await _start_runtime()
        return

    # Runtime already running - adapt the poll loop to the new subscription
    _sync_poll_task()

    # Poll right away so the remote doesn't wait empty for 10s until
    # the next regular cycle
    if client and ENTITY_ID in ours:
        try:
            await _poll_once()
        except Exception as exc:
            _LOG.warning("initial poll on subscribe failed: %s", exc)


@api.listens_to(Events.UNSUBSCRIBE_ENTITIES)
async def on_unsubscribe(entity_ids: list[str]) -> None:
    """
    The remote unsubscribes an entity - the widget is no longer visible
    (e.g. switching from the TV activity to another one). Kodi pattern:
    stop the runtime when no other entity needing the same device is
    subscribed.

    If only the button is gone, the runtime keeps running; if only the
    widget is gone, only the poll loop stops. The runtime is stopped
    completely once none of our entities is subscribed anymore.

    This is the actual battery saving mechanism: in the Kodi or any
    other activity there is NO polling of Dispatcharr.
    """
    _LOG.info("Unsubscribe: %s", entity_ids)
    ours = {e for e in entity_ids if e in (ENTITY_ID, BUTTON_ID)}
    if not ours:
        return

    _subscribed.difference_update(ours)

    if _subscribed:
        # At least one entity still active - only adjust the poll loop
        # (e.g. widget gone, button stays).
        _sync_poll_task()
        return

    _LOG.info("Last entity unsubscribed - stopping runtime to save battery")
    await _stop_runtime()


# ----------------------------------------------------------------------
# Setup Flow
# ----------------------------------------------------------------------
async def setup_handler(msg: SetupDriver) -> SetupAction:
    """
    Called by the remote on the initial setup and on reconfigure.
    Takes the values from the setup form, validates them against the
    Dispatcharr API and persists them.
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
    # Same limits as in the setup_data_schema (driver.json)
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
    # Register entities - always at startup, independent of whether
    # Dispatcharr is reachable. Otherwise the core could subscribe
    # before the entities exist.
    api.available_entities.add(_make_entity())
    api.available_entities.add(_make_button())

    # Start the API
    driver_path = os.path.join(os.path.dirname(__file__), "driver.json")
    if not os.path.isfile(driver_path):
        # In the packaged form, driver.json is in ./bin/ next to driver
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
