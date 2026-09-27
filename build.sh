#!/usr/bin/env bash
# Build aarch64 binary for the UC Remote 3 and pack the installable tar.gz.
# Run on the Mac (Apple Silicon = native arm64). Requires Docker Desktop.
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

# driver.json an alle drei Stellen, an denen der Core bzw. der Treiber sucht:
# - artifacts/        -> Metadaten fuer den Web-Configurator (Pflicht)
# - artifacts/bin/    -> Fallback ueber os.getcwd()
# - im Bundle         -> ueber --add-data, Fallback ueber __file__
cp driver.json artifacts/
cp driver.json artifacts/bin/
cp LICENSE artifacts/

OUT="uc-intg-dispatcharr-${VERSION}-aarch64.tar.gz"
tar czf "$OUT" -C artifacts .
rm -rf dist build artifacts intg-dispatcharr.spec

echo ""
echo "Fertig: $OUT"
echo "Installation: Web-Configurator -> Integrationen -> Hinzufuegen -> Eigene Integration -> $OUT"
