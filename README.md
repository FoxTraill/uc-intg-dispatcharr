# uc-intg-dispatcharr

[Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) integration for the
Unfolded Circle Remote 3. It runs natively on the remote as a custom
integration (aarch64 binary) and shows what is currently playing on one of
your IPTV clients.

## Features

- **Media player widget** with the channel logo, the current EPG program and
  its progress, filtered by the IP address of your playback device
  (e.g. an NVIDIA Shield or Apple TV).
- **Stream source switching** for the running channel:
  - next / previous source from the widget,
  - direct selection from the source list,
  - tapping the logo also switches to the next source.
- **"Next Source" button entity** that can be placed on UI pages or used in
  macros, without the media player and without polling.
- **Logo proxy** that trims and resizes channel logos so they look consistent
  in the widget.

Playback itself is not controlled by this integration; use the integration
of your player (e.g. ADB Bridge) in the same activity.

## Installation

1. Download `uc-intg-dispatcharr-<version>-aarch64.tar.gz` from the
   [latest release](https://github.com/FoxTraill/uc-intg-dispatcharr/releases/latest).
2. Web configurator → Integrations → Add new → Install custom → upload the
   archive (do not extract it). When updating, check
   "Update existing driver".
3. Fill in the setup form:
   - **Dispatcharr URL**: e.g. `http://192.168.1.10:9191`
   - **API key**: key of a user with admin rights (without admin rights,
     switching sources fails with HTTP 403)
   - **Client IP**: IP address of the playback device
   - **Poll interval**: 5–60 s, default 10 s
4. Add the entities to your activities:
   - `dispatcharr_now_playing` (media player) — widget
   - `dispatcharr_next_source` (button) — source switching without polling

## Power usage

- The poll loop only runs while the media player entity is subscribed,
  i.e. while an activity using it is active.
- The button alone does not cause any polling; it fetches the current
  channel when pressed.
- `enter_standby` stops the runtime, `exit_standby` starts it again if one of
  the entities is still subscribed.
- EPG data is cached for 60 s and source lists for 10 min.

## Build

Builds run automatically on GitHub (see [Releases](#releases)). To build
locally you need Docker; on Apple Silicon the build runs natively:

```bash
chmod +x build.sh
./build.sh
```

Result: `uc-intg-dispatcharr-<version>-aarch64.tar.gz`.

The build uses the official `unfoldedcircle/r2-pyinstaller` image with
`--platform=linux/arm64` and a PyInstaller `--onedir` bundle, as recommended
by Unfolded Circle.

### Where the driver looks for driver.json

`driver.py` looks for `driver.json` next to `__file__` first, then in the
working directory. In the frozen bundle `__file__` points to `_internal/`,
so `build.sh` places the file in three locations:

- `artifacts/driver.json` — metadata for the web configurator (required)
- `artifacts/bin/driver.json` — fallback via `os.getcwd()`
- inside the bundle via `--add-data` — found via `__file__`

This way the driver starts regardless of the working directory the core
uses.

## Releases

GitHub Actions builds the archive (`.github/workflows/build.yml`):

- **Pull request** → test build, the archive is attached to the workflow run
  under *Actions*.
- **Version tag** → build plus GitHub release with the `.tar.gz` and its
  SHA256 checksum.

To publish a new version:

1. Bump `version` in `driver.json` and add a changelog entry below.
2. Merge into `main`.
3. On GitHub: *Releases → Draft a new release → Choose a tag* → enter
   `v<version>` (e.g. `v0.8.4`) → *Create new tag* → *Publish release*.
   Or locally: `git tag v0.8.4 && git push origin v0.8.4`.

If the tag does not match the version in `driver.json`, the build fails.
The build runs under emulation and takes a few minutes.

## Migrating from the Docker variant

The on-device variant uses a different `driver_id` than the former Docker
variant, so the core treats them as separate integrations and both can run
side by side while testing.

| | Docker (`dispatcharr_ext`) | On-device (`dispatcharr_local`) |
|---|---|---|
| Runtime | LXC container, Dockge | directly on the remote |
| Config | `/opt/stacks/intg-dispatcharr/data/config.json` | `$UC_CONFIG_HOME/config.json` (core sandbox) |
| Logo proxy | port 19191 on the LXC | port 19191 on the remote |
| Update | `docker compose up -d --build` | upload a new release in the web configurator |

Port 19191 is outside the ranges blocked by the core (8000–9200, 13333).

Once the on-device variant works:

1. Stop the `intg-dispatcharr` Docker stack in Dockge.
2. Web configurator → Integrations → "Dispatcharr Now Playing (extern)" →
   delete.
3. Remove the stack in Dockge.

## Changelog

### 0.8.4 — Logos, UC guidelines

- **Logos** (`image_proxy.py`) — empty borders (transparent or solid color)
  are trimmed before scaling. The height now uses 94 % instead of 75 % of the
  canvas; the width stays at 75 % because the widget only crops at the
  sides. Square logos: 96×96 → 120×120 px, logos with built-in padding up to
  more than three times their previous size. The logo URL carries
  `?v=<RENDER_VERSION>` so the remote does not show stale cached images for
  up to 24 h.
- **`driver.json`** — API key as password field, developer and home page
  point to this repository instead of the Dispatcharr project.
- **`driver.py`**
  - `media_type` is `channel` instead of `tv_show` (live TV).
  - Log level can be set via `UC_LOG_LEVEL`.
  - Channels without EPG data re-downloaded the full EPG response (~200 KB)
    on every poll; the 60 s cache now applies there as well.
  - `exit_standby` only starts the runtime if an entity is subscribed.
  - An invalid poll interval in setup returns a `SetupError` instead of
    raising an exception; values are clamped to 5–60 s.
- **GitHub Actions** — automated builds and releases.

### 0.8.3 — Wakeup robustness (on-device)

Two bugs that only occur on-device: the remote re-establishes its Wi-Fi only
after the `EXIT_STANDBY` event, but the first HTTP request follows about
35 ms later. In the LXC container this time window never existed.

- **`client.py`** — `refresh_channel_cache()` unconditionally overwrote the
  existing cache, even when the fetch failed. A network error on wakeup
  therefore deleted logos or channels until the next regular refresh six
  hours later. The existing cache is now kept when a fetch fails.
- **`driver.py`** — `_start_runtime()` made exactly one attempt and set
  `DeviceStates.ERROR` with `return False` on an empty cache. No further
  attempt followed, so the integration stayed dead until the next standby
  cycle. Now five attempts with backoff (2/4/6/8 s, at most 20 s), ERROR only
  after that.
