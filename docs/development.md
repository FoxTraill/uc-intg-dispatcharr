# Development

## Project layout

| Path | Content |
|---|---|
| `intg-dispatcharr/driver.py` | Entry point: entities, commands, polling, setup flow |
| `intg-dispatcharr/client.py` | Async Dispatcharr REST client with channel, logo and provider caches |
| `intg-dispatcharr/image_proxy.py` | Local HTTP server that trims and resizes channel logos |
| `intg-dispatcharr/config.py` | Persistent configuration in `$UC_CONFIG_HOME/config.json` |
| `driver.json` | Integration metadata and setup form |
| `build.sh` | Builds the installation archive |

The integration uses the
[Unfolded Circle Python integration library](https://github.com/unfoldedcircle/integration-python-library)
(`ucapi`) and follows the
[custom integration guidelines](https://github.com/unfoldedcircle/core-api/blob/main/doc/integration-driver/driver-installation.md).

## Building locally

Requires Docker. On Apple Silicon the build runs natively, elsewhere under
arm64 emulation.

```bash
./build.sh
```

Result: `uc-intg-dispatcharr-<version>-aarch64.tar.gz`.

The build uses the official `unfoldedcircle/r2-pyinstaller` image with
`--platform=linux/arm64` and a PyInstaller `--onedir` bundle, as recommended
by Unfolded Circle (a one-file bundle uses about 100 MB more memory).

### Where the driver looks for driver.json

`driver.py` looks for `driver.json` next to `__file__` first, then in the
working directory. In the frozen bundle `__file__` points to `_internal/`,
so `build.sh` places the file in three locations:

- `artifacts/driver.json` — metadata for the web configurator (required)
- `artifacts/bin/driver.json` — fallback via `os.getcwd()`
- inside the bundle via `--add-data` — found via `__file__`

## Releasing

GitHub Actions (`.github/workflows/build.yml`) builds the archive:

- **Pull request** → test build; the archive is attached to the workflow run
  under *Actions* for testing on the remote.
- **Version tag** → build plus GitHub release with the `.tar.gz` and its
  SHA256 checksum.

To publish a new version:

1. Bump `version` (and `release_date`) in `driver.json`.
2. Move the `Unreleased` entries in `CHANGELOG.md` to a new version section.
3. Merge into `main`.
4. On GitHub: *Releases → Draft a new release → Choose a tag* → `v<version>`
   → *Create new tag* → add title and notes → *Publish release*.
   Or locally: `git tag v0.8.4 && git push origin v0.8.4`.

If the tag does not match the version in `driver.json`, the build fails.

## Runtime notes

- Only `$UC_CONFIG_HOME` is writable and persisted on the remote.
- The logo proxy listens on port 19191. Ports 8000–9200 and 13333 are
  reserved by the remote.
- A custom integration should stay below 100 MB of memory. The logo cache
  is limited to 200 entries.
- Changing the logo processing in `image_proxy.py` requires bumping
  `RENDER_VERSION`, otherwise the remote keeps showing cached images.

## Migrating from the Docker variant

The on-device variant uses `driver_id` `dispatcharr_local`, the former Docker
variant `dispatcharr_ext`. The core treats them as separate integrations, so
both can run side by side while testing. Once the on-device variant works,
delete the old integration in the web configurator and stop the container.
