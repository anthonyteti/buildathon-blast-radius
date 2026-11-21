#!/bin/bash
# Runs inside the image (see build_keys.sh): per-test key lookups of sqlglot's
# class-level dict tables (tools/keytrace.py), one pytest process per test
# file, $JOBS at a time; every test runs in its own forked child.
set -u
SRC=${SRC:-/opt/sqlglot}; OUT=${1:-/out/keys}; JOBS=${JOBS:-4}
TOOLS="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$OUT"; rm -rf "$OUT"/*
cd "$SRC"
export SKIP_INTEGRATION=0 KT_OUT="$OUT" PYTHONPATH="$SRC:$TOOLS" PYTHONDONTWRITEBYTECODE=1
KT_ALIASES=1 python -c "import keytrace" > "$OUT/aliases.log" 2>&1
i=0
for f in $(find tests -name 'test_*.py' | xargs ls -S); do
  i=$((i+1))
  python -m pytest -q -p keytrace -p no:cacheprovider "$f" > "$OUT/log.$i.txt" 2>&1 &
  while [ "$(jobs -rp | wc -l)" -ge "$JOBS" ]; do sleep 0.5; done
done
wait
echo keytrace done
