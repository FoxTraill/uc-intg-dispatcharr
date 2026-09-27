"""
Image Proxy - lädt Logos von Dispatcharr, normalisiert sie auf eine
feste Canvas und serviert sie für die UC Remote.

Hintergrund: Dispatcharr-Logos sind 512px breit, aber Höhe stark variabel
(66px bis 513px), und viele bringen eigenen leeren Rand mit. Direkt ins
Media Widget gegeben, erscheinen sie dadurch unterschiedlich groß und
oft winzig.

Lösung pro Logo:
1. Leeren Rand abschneiden (transparent oder einfarbig).
2. Proportional in die Canvas einpassen - mit getrennten Rändern für
   Breite und Höhe, weil das Widget nur seitlich abschneidet.
3. Mittig auf transparenten Hintergrund setzen.

Cache: PNG-Bytes pro logo_id im RAM, max. MAX_CACHE_ENTRIES Einträge,
damit wir nicht Out-Of-Memory laufen (UC Sandbox: ~100MB Limit pro Driver).
"""

import asyncio
import io
import logging
import socket
from typing import Optional

import aiohttp
from aiohttp import web
from PIL import Image, ImageChops

_LOG = logging.getLogger(__name__)

# Canvas: 512x128 (4:1) wie v0.6.1 - hat dem User am besten gefallen.
TARGET_W = 512
TARGET_H = 128
# Nutzbarer Anteil der Canvas, getrennt nach Richtung.
# Breite: lange Logos (Eurosport 512x52) wurden im Widget seitlich
# abgeschnitten, deshalb 75% (64px Rand links/rechts).
# Höhe: oben/unten wurde nie abgeschnitten. Früher galten dort
# ebenfalls 75% - damit bekam ein quadratisches Logo nur 96x96px.
# Mit 94% sind es 120x120px, gut 50% mehr Fläche.
MAX_W_RATIO = 0.75
MAX_H_RATIO = 0.94
# Pixel mit Alpha <= diesem Wert zählen beim Zuschneiden als leer
# (fängt Anti-Aliasing-Reste und fast unsichtbare Schatten ab).
TRIM_ALPHA_THRESHOLD = 16
# Farbabstand, bis zu dem ein opaker Rand als "Hintergrund" gilt
# (JPEG-Artefakte an weißen Rändern).
TRIM_COLOR_TOLERANCE = 24
# Wird in die Logo-URL geschrieben. Bei jeder Änderung an der
# Bildaufbereitung erhöhen, sonst zeigt die Remote wegen
# Cache-Control max-age bis zu 24h die alte Version.
RENDER_VERSION = 2
MAX_CACHE_ENTRIES = 200
MAX_FETCH_TIMEOUT = 5.0


class LogoProxy:
    """Lokaler HTTP-Server der gepaddete Logos ausliefert."""

    def __init__(self, dispatcharr_base_url: str, port: int = 19191):
        self._base = dispatcharr_base_url.rstrip("/")
        self._port = port
        # (logo_id, passthrough) -> PNG bytes
        self._cache: dict[tuple[int, bool], bytes] = {}
        self._fetch_locks: dict[tuple[int, bool], asyncio.Lock] = {}
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

    def url_for(self, logo_id: Optional[int], raw: bool = False) -> str:
        """
        raw=True: Original ohne Aufbereitung (Test-Mode zur
        Widget-Vermessung).
        """
        if logo_id is None:
            return ""
        url = f"{self.public_url_base}/logo/{int(logo_id)}.png?v={RENDER_VERSION}"
        if raw:
            url += "&raw=1"
        return url

    # ------------------------------------------------------------------
    # Image processing
    # ------------------------------------------------------------------
    @staticmethod
    def _trim(src: Image.Image) -> Image.Image:
        """
        Schneidet leeren Rand ab, damit das eigentliche Logo die
        verfügbare Fläche nutzt.

        - Mit Transparenz: alles außerhalb der sichtbaren Pixel.
        - Ohne Transparenz: ein einfarbiger Rand, sofern alle vier
          Ecken dieselbe Farbe haben (typisch: Logo auf weißer Fläche).
          Die Fläche selbst bleibt erhalten, nur der Überstand geht weg.

        Findet sich nichts Sinnvolles, kommt das Bild unverändert zurück.
        """
        alpha = src.getchannel("A")
        if alpha.getextrema()[0] < 255:
            mask = alpha.point(lambda a: 255 if a > TRIM_ALPHA_THRESHOLD else 0)
            bbox = mask.getbbox()
        else:
            rgb = src.convert("RGB")
            w, h = rgb.size
            corners = [
                rgb.getpixel(p) for p in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))
            ]
            bg = corners[0]
            if any(
                max(abs(c[i] - bg[i]) for i in range(3)) > TRIM_COLOR_TOLERANCE
                for c in corners[1:]
            ):
                return src
            diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, bg))
            mask = diff.convert("L").point(
                lambda d: 255 if d > TRIM_COLOR_TOLERANCE else 0
            )
            bbox = mask.getbbox()

        if not bbox:
            return src
        left, top, right, bottom = bbox
        # Winzige Reste (einzelne Pixel) nicht als Logo werten
        if right - left < 4 or bottom - top < 4:
            return src
        return src.crop(bbox)

    @staticmethod
    def _pad_to_canvas(raw: bytes, passthrough: bool = False) -> bytes:
        """
        Logo zuschneiden und in die TARGET_W x TARGET_H Canvas (512x128)
        einpassen. Genutzt werden max. MAX_W_RATIO der Breite und
        MAX_H_RATIO der Höhe, der Rest ist transparente Margin.

        passthrough=True: Original-PNG unverändert zurück (für Tests).
        """
        if passthrough:
            return raw

        src = Image.open(io.BytesIO(raw)).convert("RGBA")
        src = LogoProxy._trim(src)
        w, h = src.size

        avail_w = int(TARGET_W * MAX_W_RATIO)
        avail_h = int(TARGET_H * MAX_H_RATIO)

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
