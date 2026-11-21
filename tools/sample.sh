#!/bin/bash
# Build the image and run it on a sample/practice-like directory with the
# scorer's exact docker flags, then score.  usage: tools/sample.sh <kit_dir> [image]
set -eu
KIT=$1; IMAGE=${2:-br-teti}
REPO="$(cd "$(dirname "$0")/.." && pwd)"
docker build -q --platform linux/amd64 --tag "$IMAGE" "$REPO" >/dev/null
OUT=$(mktemp)
/usr/bin/time -p docker run --rm --platform linux/amd64 --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume "$KIT/sample/commits:/data/commits:ro" \
	--volume "$KIT/sample/tests.json:/data/tests.json:ro" "$IMAGE" > "$OUT"
python3 "$KIT/score.py" "$KIT/sample" "$KIT/sample/answers.json" "$OUT"
