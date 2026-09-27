"""
Image proxy - loads logos from Dispatcharr, normalizes them to a fixed
canvas and serves them to the UC remote.

Background: Dispatcharr logos are 512px wide, but their height varies a
lot (66px to 513px), and many come with their own empty border. Passed
directly to the media widget, they appear in different sizes and are
often tiny.

Per logo:
1. Trim empty borders (transparent or solid color).
2. Scale proportionally into the canvas - with separate margins for
   width and height, because the widget only crops at the sides.
3. Center it on a transparent background.

Cache: PNG bytes per logo_id in RAM, at most MAX_CACHE_ENTRIES entries,
so we don't run out of memory (UC sandbox: ~100MB limit per driver).
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

# Canvas: 512x128 (4:1) as in v0.6.1 - looked best on the remote.
TARGET_W = 512
TARGET_H = 128
# Usable share of the canvas, per direction.
# Width: long logos (Eurosport 512x52) were cropped at the sides of the
# widget, hence 75% (64px margin left/right).
# Height: nothing was ever cropped at the top/bottom. It used to be 75%
# as well, which gave a square logo only 96x96px. With 94% it gets
# 120x120px, about 50% more area.
MAX_W_RATIO = 0.75
MAX_H_RATIO = 0.94
# Pixels with alpha <= this value count as empty when trimming
# (catches anti-aliasing leftovers and nearly invisible shadows).
TRIM_ALPHA_THRESHOLD = 16
# Color distance up to which an opaque border counts as "background"
# (JPEG artifacts on white borders).
TRIM_COLOR_TOLERANCE = 24
# Written into the logo URL. Bump it on every change to the image
# processing, otherwise the remote keeps showing the old version for up
# to 24h because of Cache-Control max-age.
RENDER_VERSION = 2
MAX_CACHE_ENTRIES = 200
MAX_FETCH_TIMEOUT = 5.0


class LogoProxy:
    """Local HTTP server that serves the processed logos."""

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
        Finds the IP of the interface we use to reach the LAN.
        Trick: "connect" a UDP socket to a public address - this sends
        nothing, but Linux selects the outbound interface.
        """
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"

    @property
    def public_url_base(self) -> str:
        """Base URL used in media_image_url."""
        ip = self._public_ip or self._detect_local_ip()
        return f"http://{ip}:{self._port}"

    def url_for(self, logo_id: Optional[int], raw: bool = False) -> str:
        """
        raw=True: original image without processing (test mode for
        measuring the widget).
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
        Trims empty borders so the actual logo uses the available area.

        - With transparency: everything outside the visible pixels.
        - Without transparency: a solid-color border, provided all four
          corners have the same color (typically a logo on a white area).
          The area itself is kept, only the excess is removed.

        If nothing sensible is found, the image is returned unchanged.
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
        # Don't treat tiny leftovers (single pixels) as a logo
        if right - left < 4 or bottom - top < 4:
            return src
        return src.crop(bbox)

    @staticmethod
    def _pad_to_canvas(raw: bytes, passthrough: bool = False) -> bytes:
        """
        Trims the logo and fits it into the TARGET_W x TARGET_H canvas
        (512x128). At most MAX_W_RATIO of the width and MAX_H_RATIO of the
        height are used, the rest is a transparent margin.

        passthrough=True: returns the original PNG unchanged (for tests).
        """
        if passthrough:
            return raw

        src = Image.open(io.BytesIO(raw)).convert("RGBA")
        src = LogoProxy._trim(src)
        w, h = src.size

        avail_w = int(TARGET_W * MAX_W_RATIO)
        avail_h = int(TARGET_H * MAX_H_RATIO)

        # fit-contain: scale proportionally into the available box
        scale = min(avail_w / w, avail_h / h)
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))

        if (new_w, new_h) != (w, h):
            src = src.resize((new_w, new_h), Image.LANCZOS)

        # Center the logo on the full canvas (transparent margin around it)
        canvas = Image.new("RGBA", (TARGET_W, TARGET_H), (0, 0, 0, 0))
        x = (TARGET_W - new_w) // 2
        y = (TARGET_H - new_h) // 2
        canvas.paste(src, (x, y), src)

        out = io.BytesIO()
        canvas.save(out, format="PNG", optimize=True)
        return out.getvalue()

    async def _fetch_and_pad(self, logo_id: int, passthrough: bool = False) -> Optional[bytes]:
        """Loads the original from Dispatcharr and processes it. Caches the result.

        passthrough=True: returns the original PNG without processing and
        caches it in a separate slot.
        """
        cache_key = (logo_id, passthrough)
        # Cache hit fast path
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Prevent concurrent fetches of the same logo_id
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

            # Move CPU-bound Pillow work to a thread so the event loop
            # is not blocked
            try:
                padded = await asyncio.to_thread(self._pad_to_canvas, raw, passthrough)
            except Exception as exc:
                _LOG.warning("Logo %s padding failed: %s", logo_id, exc)
                return None

            # Simple LRU-ish: when full, drop the oldest entry
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

        # Test mode: ?raw=1 passes the original PNG through unprocessed
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
        # Bind to 0.0.0.0 so the remote UI (other interface) can reach it
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
        """Called when the Dispatcharr URL changes (reconfigure)."""
        new_base = new_base.rstrip("/")
        if new_base != self._base:
            _LOG.info("Dispatcharr base URL changed, clearing logo cache")
            self._base = new_base
            self._cache.clear()
