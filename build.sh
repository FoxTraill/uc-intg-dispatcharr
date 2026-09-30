#!/usr/bin/env bash
# Build aarch64 binary for the UC Remote 3 and pack the installable tar.gz.
# Requires Docker. Runs natively on Apple Silicon, elsewhere under arm64
# emulation (QEMU/binfmt).
set -euo pipefail
cd "$(dirname "$0")"

VERSION=$(python3 -c "import json;print(json.load(open('driver.json'))['version'])")
IMAGE=docker.io/unfoldedcircle/r2-pyinstaller:3.11.13

# Keep the archive small (the remote has limited space and memory):
# --strip removes debug symbols (libpython alone ships ~20 MB of them).
# Excluded modules are never used by the driver:
# - pydantic/pydantic_core: only an optional integration in yarl
# - AVIF, color management (lcms2) and Tk support in Pillow: logos
#   are PNG/JPEG/WebP/GIF, and plugins are loaded lazily, so a
#   missing plugin simply isn't registered
EXCLUDES="--exclude-module pydantic --exclude-module pydantic_core \
  --exclude-module PIL._avif --exclude-module PIL.AvifImagePlugin \
  --exclude-module PIL._imagingcms --exclude-module PIL.ImageCms \
  --exclude-module PIL._imagingtk --exclude-module PIL.ImageTk"

rm -rf dist build artifacts intg-dispatcharr.spec

docker run --rm --name dispatcharr-builder \
  --platform=linux/arm64 \
  --user="$(id -u):$(id -g)" \
  -v "$PWD":/workspace \
  "$IMAGE" \
  bash -c "cd /workspace && \
    python -m pip install -r requirements.txt && \
    pyinstaller --clean --onedir --strip --name intg-dispatcharr \
      --add-data 'driver.json:.' \
      ${EXCLUDES} \
      intg-dispatcharr/driver.py"

mkdir -p artifacts/bin
mv dist/intg-dispatcharr/* artifacts/bin
mv artifacts/bin/intg-dispatcharr artifacts/bin/driver

# driver.json in all three places where the core or the driver looks for it:
# - artifacts/        -> metadata for the web configurator (required)
# - artifacts/bin/    -> fallback via os.getcwd()
# - in the bundle     -> via --add-data, fallback via __file__
cp driver.json artifacts/
cp driver.json artifacts/bin/
cp LICENSE artifacts/

OUT="uc-intg-dispatcharr-${VERSION}-aarch64.tar.gz"
tar czf "$OUT" -C artifacts .
rm -rf dist build artifacts intg-dispatcharr.spec

echo ""
echo "Done: $OUT"
echo "Install: web configurator -> Integrations -> Add new -> Install custom -> $OUT"
