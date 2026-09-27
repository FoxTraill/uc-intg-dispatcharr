"""
Image Proxy - lädt Logos von Dispatcharr, padded sie auf Quadrat,
serviert sie für die UC Remote.

Hintergrund: Dispatcharr-Logos sind 512px breit, aber Höhe stark variabel
(66px bis 513px). Das UC Media Widget rendert media_image_url als großes
Quadrat - rechteckige Logos werden dadurch zentriert und winzig dargestellt.

Lösung: Wir padden zur Laufzeit auf 512x512 mit transparentem Hintergrund.
Das Originallogo bleibt mittig, das Widget hat ein quadratisches Bild
zum Anzeigen.

Cache: PNG-Bytes pro logo_id im RAM. Cap = MAX_CACHE_BYTES, damit wir
nicht Out-Of-Memory laufen (UC Sandbox: ~100MB Limit pro Driver).
"""

import asyncio
import io
import logging
import socket
from typing import Optional

import aiohttp
from aiohttp import web
from PIL import Image

_LOG = logging.getLogger(__name__)

# Canvas: 512x128 (4:1) wie v0.6.1 - hat dem User am besten gefallen.
# Lange Logos (Eurosport 512x52) wurden aber seitlich abgeschnitten.
# Fix: SAFE_RATIO 0.80 = Logo nutzt max 80% der Canvas-Größe,
# bekommt 51px transparenten Rand links/rechts.
TARGET_W = 512
TARGET_H = 128
SAFE_RATIO = 0.75
MAX_CACHE_ENTRIES = 200
MAX_FETCH_TIMEOUT = 5.0


class LogoProxy:
    """Lokaler HTTP-Server der gepaddete Logos ausliefert."""

    def __init__(self, dispatcharr_base_url: str, port: int = 19191):
        self._base = dispatcharr_base_url.rstrip("/")
        self._port = port
        # logo_id -> PNG bytes (gepaddet)
        self._cache: dict[int, bytes] = {}
        self._fetch_locks: dict[int, asyncio.Lock] = {}
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._public_ip: Optional[str] = None

    @staticmethod
    def _detect_local_ip() -> str:
        """
        Findet die IP des Interface, über das wir das LAN erreichen.
        Trick: UDP socket "verbinden" zu einer öffentlichen Adresse - das
        verschickt nichts, aber Linux setzt das Outbound-Interface.
        """
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"

    @property
    def public_url_base(self) -> str:
        """Basis-URL die in media_image_url gesetzt wird."""
        ip = self._public_ip or self._detect_local_ip()
        return f"http://{ip}:{self._port}"

    def url_for(self, logo_id: Optional[int]) -> str:
        if logo_id is None:
            return ""
        return f"{self.public_url_base}/logo/{int(logo_id)}.png"

    # ------------------------------------------------------------------
    # Image processing
    # ------------------------------------------------------------------
    @staticmethod
    def _pad_to_canvas(raw: bytes, passthrough: bool = False) -> bytes:
        """
        Logo wird in TARGET_W x TARGET_H Canvas (512x128) eingepasst.
        Maximal SAFE_RATIO (88%) der Canvas-Größe wird genutzt - die
        restlichen 12% sind transparente Sicherheits-Margin damit das
        Logo nicht an den Canvas-Rändern abgeschnitten wird.

        passthrough=True: Original-PNG unverändert zurück (für Tests).
        """
        if passthrough:
            return raw

        src = Image.open(io.BytesIO(raw)).convert("RGBA")
        w, h = src.size

        # Verfügbare effektive Größe nach SAFE_RATIO Margin
        avail_w = int(TARGET_W * SAFE_RATIO)
        avail_h = int(TARGET_H * SAFE_RATIO)

        # fit-contain: skaliere proportional in die avail-Box
        scale = min(avail_w / w, avail_h / h)
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))

        if (new_w, new_h) != (w, h):
            src = src.resize((new_w, new_h), Image.LANCZOS)

        # Logo mittig in den vollen Canvas (transparente Margin rundum)
        canvas = Image.new("RGBA", (TARGET_W, TARGET_H), (0, 0, 0, 0))
        x = (TARGET_W - new_w) // 2
        y = (TARGET_H - new_h) // 2
        canvas.paste(src, (x, y), src)

        out = io.BytesIO()
        canvas.save(out, format="PNG", optimize=True)
        return out.getvalue()

    async def _fetch_and_pad(self, logo_id: int, passthrough: bool = False) -> Optional[bytes]:
        """Lädt Original von Dispatcharr und padded. Cached das Ergebnis.

        passthrough=True: gibt das Original-PNG ohne Verarbeitung zurück
        und cached unter einem separaten Slot.
        """
        cache_key = (logo_id, passthrough)
        # Cache-Hit Fast-Path
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Prevent concurrent fetches der gleichen logo_id
        lock = self._fetch_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

            url = f"{self._base}/api/channels/logos/{logo_id}/cache/"
            timeout = aiohttp.ClientTimeout(total=MAX_FETCH_TIMEOUT)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            _LOG.warning(
                                "Logo %s: dispatcharr returned %d",
                                logo_id, resp.status,
                            )
                            return None
                        raw = await resp.read()
            except Exception as exc:
                _LOG.warning("Logo %s fetch failed: %s", logo_id, exc)
                return None

            # CPU-bound Pillow Arbeit in Thread auslagern damit der
            # Event-Loop nicht blockiert
            try:
                padded = await asyncio.to_thread(self._pad_to_canvas, raw, passthrough)
            except Exception as exc:
                _LOG.warning("Logo %s padding failed: %s", logo_id, exc)
                return None

            # Simple LRU-ish: wenn voll, ältesten Eintrag droppen
            if len(self._cache) >= MAX_CACHE_ENTRIES:
                first_key = next(iter(self._cache))
                self._cache.pop(first_key, None)

            self._cache[cache_key] = padded
            _LOG.info(
                "Logo %s cached (%d bytes orig -> %d bytes %s)",
                logo_id, len(raw), len(padded),
                "passthrough" if passthrough else "padded",
            )
            return padded

    # ------------------------------------------------------------------
    # HTTP handlers
    # ------------------------------------------------------------------
    async def _handle_logo(self, request: web.Request) -> web.Response:
        try:
            logo_id = int(request.match_info["logo_id"])
        except (KeyError, ValueError):
            return web.Response(status=400, text="bad logo_id")

        # Test-Mode: ?raw=1 gibt das Original-PNG durch ohne Padding
        passthrough = request.query.get("raw") == "1"
        png = await self._fetch_and_pad(logo_id, passthrough=passthrough)
        if png is None:
            return web.Response(status=404, text="logo not available")

        return web.Response(
            body=png,
            content_type="image/png",
            headers={"Cache-Control": "no-cache" if passthrough else "public, max-age=86400"},
        )

    async def _handle_health(self, _req: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "cached_logos": len(self._cache),
                "public_url": self.public_url_base,
            }
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._site is not None:
            _LOG.warning("Logo proxy already running")
            return

        self._public_ip = self._detect_local_ip()

        self._app = web.Application()
        self._app.router.add_get("/logo/{logo_id}.png", self._handle_logo)
        self._app.router.add_get("/health", self._handle_health)

        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        # 0.0.0.0 binden damit die Remote (anderes Interface) drauf zugreifen kann
        self._site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await self._site.start()
        _LOG.info(
            "Logo proxy listening on %s (public URL: %s)",
            self._port, self.public_url_base,
        )

    async def stop(self) -> None:
        if self._site:
            await self._site.stop()
            self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        self._cache.clear()
        self._fetch_locks.clear()
        _LOG.info("Logo proxy stopped")

    def update_base_url(self, new_base: str) -> None:
        """Wird aufgerufen wenn die Dispatcharr URL sich ändert (Reconfigure)."""
        new_base = new_base.rstrip("/")
        if new_base != self._base:
            _LOG.info("Dispatcharr base URL changed, clearing logo cache")
            self._base = new_base
            self._cache.clear()
