#!/bin/bash
# Rebuild maps/ from scratch: run sqlglot's suite under coverage inside the
# submission image (linux/amd64, same Python and lock), then compact the result.
# usage: tools/build_maps.sh <tests.json> [image] [workdir]
# (maps/cpu.json comes from tools/measure_cpu.py, run separately)
set -eu
TESTS_JSON=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
IMAGE=${2:-br-teti}
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK=${3:-$REPO/.mapwork}
mkdir -p "$WORK"
docker run --rm --platform linux/amd64 --cpus "${CPUS:-4}" -e JOBS="${JOBS:-4}" \
  -v "$REPO/tools:/tools:ro" -v "$WORK:/out" -v "$REPO/maps:/maps" -v "$TESTS_JSON:/tests.json:ro" "$IMAGE" \
  bash -c 'pip install -q --no-cache-dir coverage && python -m coverage --version | head -1 && /tools/run_coverage.sh /out/cov && PREIMPORT=0 /tools/run_coverage.sh /out/cov2 && python /tools/build_maps.py /out/cov,/out/cov2 /opt/sqlglot /maps'
