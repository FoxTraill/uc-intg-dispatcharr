#!/usr/bin/env bash
# Build aarch64 binary for the UC Remote 3 and pack the installable tar.gz.
# Requires Docker. Runs natively on Apple Silicon, elsewhere under arm64
# emulation (QEMU/binfmt).
set -euo pipefail
cd "$(dirname "$0")"

VERSION=$(python3 -c "import json;print(json.load(open('driver.json'))['version'])")
IMAGE=docker.io/unfoldedcircle/r2-pyinstaller:3.11.13

rm -rf dist build artifacts intg-dispatcharr.spec

docker run --rm --name dispatcharr-builder \
  --platform=linux/arm64 \
  --user="$(id -u):$(id -g)" \
  -v "$PWD":/workspace \
  "$IMAGE" \
  bash -c "cd /workspace && \
    python -m pip install -r requirements.txt && \
    pyinstaller --clean --onedir --name intg-dispatcharr \
      --add-data 'driver.json:.' \
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
