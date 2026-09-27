"""
Dispatcharr API Client.

Gekapselte HTTP-Calls gegen die Dispatcharr REST API.
Alles async via aiohttp.
"""

import asyncio
import logging
from typing import Any, Optional

import aiohttp

_LOG = logging.getLogger(__name__)


class DispatcharrClient:
    """Async HTTP client for Dispatcharr."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 5.0):
        # Normalize: kein trailing slash
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        # Timeouts:
        # - sock_connect 5s: TCP-Connect muss schnell sein im LAN
        # - total 20s: gesamte Request darf nicht länger dauern
        # - KEIN sock_read - der hat in v0.7.1 fälschlich EPG-Calls
        #   abgeschossen weil 200KB Response auf der Remote-CPU
        #   manchmal länger als 5s dauert (parallel zu Logo-Pillow-Processing)
        self._timeout = aiohttp.ClientTimeout(
            total=max(timeout, 20.0),
            sock_connect=5.0,
        )
        self._session: Optional[aiohttp.ClientSession] = None
        # Cache: channel_uuid -> {"name": str, "logo_id": int|None, "logo_url": str|None}
        self._channel_cache: dict[str, dict[str, Any]] = {}
        self._logo_cache: dict[int, str] = {}  # logo_id -> public cache_url
        self._m3u_cache: dict[int, str] = {}  # m3u_account_id -> Provider-Name

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={"X-API-Key": self._api_key},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    async def get_active_streams(self) -> Optional[list[dict[str, Any]]]:
        """
        GET /proxy/ts/status -> list of active stream dicts.

        Rückgabe:
        - list (auch leer): erfolgreich geantwortet, Liste ist authoritativ
        - None: HTTP/Netzwerk-Fehler, Status unbekannt - aufrufender Code
                soll den letzten Zustand beibehalten statt OFF zu setzen
        """
        session = await self._get_session()
        url = f"{self._base}/proxy/ts/status"
        try:
            async with session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data.get("channels", []) or []
        except Exception as exc:
            _LOG.warning("get_active_streams failed: %s", exc)
            return None

    async def get_current_program_for(self, channel_uuid: str) -> Optional[dict[str, Any]]:
        """
        POST /api/epg/current-programs/ with empty body returns flat list of
        currently-airing programs across all channels. We filter for our UUID.

        Wir holen immer alle Programme statt nur eins, weil Dispatcharr
        keinen single-channel Filter im Request akzeptiert (zumindest in
        der aktuellen Version) und der Response ohnehin klein ist (~150
        Channels).
        """
        session = await self._get_session()
        url = f"{self._base}/api/epg/current-programs/"
        try:
            async with session.post(url, json={}) as resp:
                resp.raise_for_status()
                programs = await resp.json()
                if not isinstance(programs, list):
                    return None
                for prog in programs:
                    if prog.get("channel_uuid") == channel_uuid:
                        return prog
                return None
        except Exception as exc:
            _LOG.warning("get_current_program_for(%s) failed: %s", channel_uuid, exc)
            return None

    async def refresh_channel_cache(self) -> None:
        """
        Holt alle Channels (paginiert) UND alle Logos (paginiert) und baut
        einen In-Memory-Cache auf. Wird beim Start und periodisch (alle ~6h)
        ausgeführt.

        Das Logo kommt nicht direkt am Channel-Objekt — wir bekommen nur die
        logo_id. Die Auflösung zu einer abrufbaren URL passiert via
        /api/channels/logos/.
        """
        session = await self._get_session()

        # 1) Logos cachen (logo_id -> cache_url, public!)
        logos: dict[int, str] = {}
        logos_ok = True
        page = 1
        while True:
            url = f"{self._base}/api/channels/logos/?page={page}&page_size=100"
            try:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            except Exception as exc:
                _LOG.warning("logo fetch page %d failed: %s", page, exc)
                logos_ok = False
                break
            for logo in data.get("results", []) or []:
                lid = logo.get("id")
                cache_url = logo.get("cache_url")
                if lid is not None and cache_url:
                    logos[int(lid)] = cache_url
            if not data.get("next"):
                break
            page += 1
            if page > 50:  # safety brake
                _LOG.warning("logo pagination > 50 pages, stopping")
                break

        # Wenn der Logo-Fetch (teilweise) gescheitert ist, die Logos aus dem
        # Bestandscache dazunehmen - sonst bekommen alle Channels logo_url
        # None obwohl wir die URLs noch kennen.
        logo_lookup: dict[int, str] = {} if logos_ok else dict(self._logo_cache)
        logo_lookup.update(logos)

        # 2) Channels cachen (uuid -> {name, logo_id, logo_url})
        channels: dict[str, dict[str, Any]] = {}
        channels_ok = True
        page = 1
        while True:
            url = f"{self._base}/api/channels/channels/?page={page}&page_size=100"
            try:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            except Exception as exc:
                _LOG.warning("channel fetch page %d failed: %s", page, exc)
                channels_ok = False
                break
            for ch in data.get("results", []) or []:
                uuid = ch.get("uuid")
                if not uuid:
                    continue
                logo_id = ch.get("logo_id")
                channels[uuid] = {
                    # Numerische DB-ID: brauchen wir für
                    # /api/channels/channels/<id>/streams/ (Quellenliste).
                    # Die Proxy-Endpunkte wollen dagegen die UUID.
                    "id": ch.get("id"),
                    "name": ch.get("name") or "Unknown",
                    "logo_id": logo_id,
                    "logo_url": logo_lookup.get(int(logo_id)) if logo_id is not None else None,
                    "channel_number": ch.get("channel_number"),
                    "tvg_id": ch.get("tvg_id"),
                }
            if not data.get("next"):
                break
            page += 1
            if page > 50:
                _LOG.warning("channel pagination > 50 pages, stopping")
                break

        # Einen intakten Cache NIE mit einem Teilergebnis ueberschreiben.
        # Nach dem Aufwachen aus dem Standby ist das WLAN der Remote
        # teilweise noch nicht da; ein gescheiterter Fetch wuerde sonst
        # Logos bzw. Kanaele loeschen - bis zum naechsten regulaeren
        # Refresh in 6h.
        if logos_ok:
            self._logo_cache = logos
        elif logos:
            self._logo_cache.update(logos)
        else:
            _LOG.warning(
                "Logo fetch failed - keeping %d cached logos",
                len(self._logo_cache),
            )

        if channels_ok:
            self._channel_cache = channels
        elif channels:
            self._channel_cache.update(channels)
        else:
            _LOG.warning(
                "Channel fetch failed - keeping %d cached channels",
                len(self._channel_cache),
            )
        await self._refresh_m3u_cache()
        _LOG.info(
            "Cache refreshed: %d channels, %d logos, %d providers",
            len(channels),
            len(logos),
            len(self._m3u_cache),
        )

    async def _refresh_m3u_cache(self) -> None:
        """
        GET /api/m3u/accounts/ -> id -> Name.

        Die Quellen eines Senders heißen bei verschiedenen Providern oft
        völlig unterschiedlich ("DE: ARD HD" vs "Das Erste FHD"). Der
        Provider-Name ist das stabilere Unterscheidungsmerkmal, deshalb
        landet er mit im Label der Quellenliste.

        Fehler sind nicht fatal - dann bleiben die Labels eben ohne
        Provider.
        """
        session = await self._get_session()
        url = f"{self._base}/api/m3u/accounts/"
        try:
            async with session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except Exception as exc:
            _LOG.warning("m3u account fetch failed: %s", exc)
            return

        # Endpoint liefert je nach Version eine flache Liste oder ein
        # paginiertes Objekt
        items = data.get("results", []) if isinstance(data, dict) else data
        accounts: dict[int, str] = {}
        for acc in items or []:
            aid = acc.get("id")
            name = acc.get("name")
            if aid is not None and name:
                accounts[int(aid)] = str(name)
        self._m3u_cache = accounts

    def get_provider_name(self, m3u_account_id: Any) -> Optional[str]:
        """Provider-Name zu einer m3u_account-ID, oder None wenn unbekannt."""
        if m3u_account_id is None:
            return None
        try:
            return self._m3u_cache.get(int(m3u_account_id))
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Quellen (Streams) eines Senders
    # ------------------------------------------------------------------

    async def get_channel_streams(
        self, channel_id: int
    ) -> Optional[list[dict[str, Any]]]:
        """
        GET /api/channels/channels/<id>/streams/

        Liefert die Quellen des Senders in der im Channel definierten
        Reihenfolge (Priorität) - exakt die Reihenfolge, die
        /proxy/ts/next_stream durchläuft.

        WICHTIG: hier die numerische Channel-ID, nicht die UUID.

        Rückgabe: Liste von Stream-Dicts, oder None bei Fehler.
        """
        session = await self._get_session()
        url = f"{self._base}/api/channels/channels/{channel_id}/streams/"
        try:
            async with session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data if isinstance(data, list) else []
        except Exception as exc:
            _LOG.warning("get_channel_streams(%s) failed: %s", channel_id, exc)
            return None

    async def next_stream(self, channel_uuid: str) -> tuple[bool, str]:
        """
        POST /proxy/ts/next_stream/<channel_uuid>

        Springt auf die nächste Quelle in der Kanal-Reihenfolge, mit
        Wrap-around am Ende. Funktioniert nur solange der Kanal aktiv
        im Proxy-Modus läuft - der aktuelle Stream wird serverseitig
        aus Redis gelesen.

        Rückgabe: (erfolg, meldung)
        """
        return await self._post_switch(
            f"{self._base}/proxy/ts/next_stream/{channel_uuid}", None
        )

    async def change_stream(
        self, channel_uuid: str, stream_id: int
    ) -> tuple[bool, str]:
        """
        POST /proxy/ts/change_stream/<channel_uuid> mit {"stream_id": N}

        Wechselt gezielt auf eine bestimmte Quelle. Dispatcharr setzt
        dabei intern die Liste der bereits probierten Streams zurück,
        das automatische Failover fängt danach wieder sauber an.

        Rückgabe: (erfolg, meldung)
        """
        return await self._post_switch(
            f"{self._base}/proxy/ts/change_stream/{channel_uuid}",
            {"stream_id": int(stream_id)},
        )

    async def _post_switch(
        self, url: str, payload: Optional[dict[str, Any]]
    ) -> tuple[bool, str]:
        """
        Gemeinsamer POST-Pfad für next_stream / change_stream.

        Beide Endpunkte sind mit IsAdmin geschützt und antworten mit
        aussagekräftigem JSON im Fehlerfall - das loggen wir mit, weil
        die häufigsten Ursachen (Kanal läuft nicht, nur eine Quelle
        vorhanden, Key ist kein Admin) sonst schwer zu unterscheiden
        sind.
        """
        session = await self._get_session()
        try:
            async with session.post(url, json=payload or {}) as resp:
                body: Any = None
                try:
                    body = await resp.json()
                except Exception:
                    body = await resp.text()

                if resp.status == 200:
                    msg = ""
                    if isinstance(body, dict):
                        msg = (
                            f"{body.get('previous_stream_id')} -> "
                            f"{body.get('new_stream_id')}"
                        )
                    _LOG.info("Stream switch ok (%s): %s", url, msg)
                    return True, msg

                err = ""
                if isinstance(body, dict):
                    err = str(body.get("error") or body)
                else:
                    err = str(body)[:200]

                if resp.status in (401, 403):
                    _LOG.error(
                        "Stream switch denied (HTTP %d) - the API key user "
                        "needs admin rights for %s",
                        resp.status,
                        url,
                    )
                else:
                    _LOG.warning(
                        "Stream switch failed (HTTP %d) %s: %s",
                        resp.status,
                        url,
                        err,
                    )
                return False, err
        except Exception as exc:
            _LOG.warning("Stream switch request failed (%s): %s", url, exc)
            return False, str(exc)

    def get_channel_info(self, channel_uuid: str) -> Optional[dict[str, Any]]:
        """Lookup im lokalen Cache. Gibt None zurück wenn unbekannt."""
        return self._channel_cache.get(channel_uuid)

    @property
    def channel_count(self) -> int:
        return len(self._channel_cache)
