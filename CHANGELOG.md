# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- MIT license and list of bundled third-party licenses.
- Issue templates for bug reports and feature requests.

### Changed
- README rewritten in English; build and release details moved to
  `docs/development.md`, changelog moved to this file.

## [0.8.4] - 2026-09-27

### Added
- Automated builds and GitHub releases with GitHub Actions.

### Changed
- Bigger channel logos: empty borders (transparent or solid color) are
  trimmed before scaling, and logos use 94 % instead of 75 % of the canvas
  height. The width stays at 75 % because the widget only crops at the sides.
  Square logos grow from 96×96 to 120×120 px, logos with built-in padding up
  to more than three times their previous size.
- Logo URLs carry a render version (`?v=…`) so the remote does not show
  stale cached images for up to 24 h.
- The API key is entered in a password field during setup.
- Media type is `channel` (live TV) instead of `tv_show`.
- Log level can be set via `UC_LOG_LEVEL`.
- Developer and home page in `driver.json` point to this repository.

### Fixed
- Channels without EPG data re-downloaded the full EPG response (~200 KB) on
  every poll; the 60 s cache now applies there as well.
- `exit_standby` started the runtime even when no entity was subscribed.
- An invalid poll interval in setup raised an exception instead of returning
  a setup error; values are now clamped to 5–60 s.

## 0.8.3 - 2026-09-12

On-device only: the remote re-establishes its Wi-Fi after the
`EXIT_STANDBY` event, but the first HTTP request follows about 35 ms later.

### Fixed
- A failed channel or logo fetch on wakeup overwrote the existing cache, so
  logos or channels were missing until the next regular refresh six hours
  later. The existing cache is now kept when a fetch fails.
- The runtime made a single start attempt and stayed in `ERROR` until the next
  standby cycle if it failed. It now retries five times with backoff
  (2/4/6/8 s, at most 20 s).

[Unreleased]: https://github.com/FoxTraill/uc-intg-dispatcharr/compare/v0.8.4...HEAD
[0.8.4]: https://github.com/FoxTraill/uc-intg-dispatcharr/releases/tag/v0.8.4
