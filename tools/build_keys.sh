#!/bin/bash
# Rebuild maps/keys.json.gz: trace which tests look up which keys of sqlglot's
# class-level dict tables (tools/keytrace.py) inside the submission image
# (linux/amd64, same Python and lock), then compact the traces.
# usage: tools/build_keys.sh [image] [workdir]
set -eu
IMAGE=${1:-br-teti}
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK=${2:-$REPO/.mapwork}
mkdir -p "$WORK"
docker run --rm --platform linux/amd64 --cpus "${CPUS:-4}" -e JOBS="${JOBS:-4}" -e KT_APP=/kapp \
  -v "$REPO/tools:/tools:ro" -v "$REPO/app:/kapp/app:ro" -v "$WORK:/out" -v "$REPO/maps:/maps" "$IMAGE" \
  bash -c '/tools/run_keytrace.sh /out/keys && python /tools/build_keys.py /out/keys /maps'
