#!/bin/bash
# Rebuild maps/ from scratch: run sqlglot's suite under coverage inside the
# submission image (linux/amd64, same Python and lock), then compact the result.
# usage: tools/build_maps.sh [image] [workdir]
set -eu
IMAGE=${1:-br-teti}
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK=${2:-$REPO/.mapwork}
mkdir -p "$WORK"
docker run --rm --platform linux/amd64 --cpus "${CPUS:-4}" -e JOBS="${JOBS:-4}" \
  -v "$REPO/tools:/tools:ro" -v "$WORK:/out" -v "$REPO/maps:/maps" "$IMAGE" \
  bash -c 'pip install -q --no-cache-dir coverage && python -m coverage --version | head -1 && /tools/run_coverage.sh && python /tools/build_maps.py /out/cov /opt/sqlglot /maps'
