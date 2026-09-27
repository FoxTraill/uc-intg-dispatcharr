# Third-party licenses

The source code of this project is licensed under the [MIT License](../LICENSE).

The installation archive (`uc-intg-dispatcharr-<version>-aarch64.tar.gz`) is
built with PyInstaller and bundles the Python runtime and the following
third-party packages. They are included unmodified and remain under their own
licenses.

## Direct dependencies

| Package | License | Project |
|---|---|---|
| ucapi | MPL-2.0 | https://github.com/unfoldedcircle/integration-python-library |
| aiohttp | Apache-2.0 AND MIT | https://github.com/aio-libs/aiohttp |
| Pillow | MIT-CMU | https://github.com/python-pillow/Pillow |

## Transitive dependencies

| Package | License | Project |
|---|---|---|
| aiohappyeyeballs | PSF-2.0 | https://github.com/aio-libs/aiohappyeyeballs |
| aiosignal | Apache-2.0 | https://github.com/aio-libs/aiosignal |
| attrs | MIT | https://github.com/python-attrs/attrs |
| frozenlist | Apache-2.0 | https://github.com/aio-libs/frozenlist |
| idna | BSD-3-Clause | https://github.com/kjd/idna |
| ifaddr | MIT | https://github.com/pydron/ifaddr |
| multidict | Apache-2.0 | https://github.com/aio-libs/multidict |
| propcache | Apache-2.0 | https://github.com/aio-libs/propcache |
| protobuf | BSD-3-Clause | https://github.com/protocolbuffers/protobuf |
| pyee | MIT | https://github.com/jfhbrook/pyee |
| typing_extensions | PSF-2.0 | https://github.com/python/typing_extensions |
| websockets | BSD-3-Clause | https://github.com/python-websockets/websockets |
| yarl | Apache-2.0 | https://github.com/aio-libs/yarl |
| zeroconf | LGPL-2.1-or-later | https://github.com/python-zeroconf/python-zeroconf |

## Runtime and build tools

| Component | License | Project |
|---|---|---|
| Python | PSF-2.0 | https://www.python.org |
| PyInstaller bootloader | GPL-2.0 with bootloader exception | https://github.com/pyinstaller/pyinstaller |

Notes:

- `zeroconf` (LGPL) is shipped as a separate, replaceable module in the
  PyInstaller one-folder bundle (`bin/_internal/`), not statically linked.
- The PyInstaller bootloader exception allows distributing the resulting
  executable under any license.
- The list reflects the dependencies at the time of writing. Regenerate it
  with `pip-licenses` in a clean virtual environment after installing
  `requirements.txt` when dependencies change.
