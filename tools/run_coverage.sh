#!/bin/bash
# Runs inside the image (see build_maps.sh): per-test line coverage of sqlglot
# and tests/, one pytest process per test file, $JOBS at a time.
set -u
SRC=/opt/sqlglot; OUT=${1:-/out/cov}; JOBS=${JOBS:-4}
TOOLS="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$OUT"; rm -rf "$OUT"/*
cat > "$OUT/coveragerc" <<RC
[run]
source = $SRC/sqlglot
         $SRC/tests
         $SRC/sqlglotc
parallel = True
data_file = $OUT/.coverage
patch = subprocess
context = \${COVMAP_CTX-}
RC
cd "$SRC"
export COVMAP_PREIMPORT=${PREIMPORT:-1} SKIP_INTEGRATION=0 COVERAGE_CORE=ctrace COVMAP_ROOT="$SRC" PYTHONPATH="$SRC:$TOOLS" PYTHONDONTWRITEBYTECODE=1
i=0
# biggest files first so the long ones start early
for f in $(find tests -name 'test_*.py' -printf '%s %p\n' | sort -rn | cut -d' ' -f2); do
  i=$((i+1))
  COVMAP_FILE="$f" COVMAP_OPENS="$OUT/opens.$i.jsonl" python -m coverage run --rcfile="$OUT/coveragerc" \
    -m pytest -q -p covplugin -p no:cacheprovider "$f" > "$OUT/log.$i.txt" 2>&1 &
  while [ "$(jobs -rp | wc -l)" -ge "$JOBS" ]; do sleep 0.5; done
done
wait
python -m coverage combine --rcfile="$OUT/coveragerc" -q
echo coverage done
