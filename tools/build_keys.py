"""Compact the per-test key traces of tools/keytrace.py into maps/keys.json.gz.

usage: build_keys.py <trace dir> <maps dir>

Layout: {"tests": [test id], "sets": [hex bitmask over "tests"],
         "tables": {table: {key digest | "*" | "#size": set index}},
         "aliases": {fingerprint path: table}}
Tables read while a test file was imported count as read whole by every test
of that file. The build stops if some test did not pass in its forked child
(its trace could be incomplete).
"""

import glob
import gzip
import json
import os
import sys
from collections import defaultdict

trace_dir, maps_dir = sys.argv[1], sys.argv[2]
with open(os.path.join(trace_dir, "aliases.json")) as fh:
    meta = json.load(fh)

tests = []
per_table = defaultdict(lambda: defaultdict(set))
bad_tests = []
for p in sorted(glob.glob(os.path.join(trace_dir, "t.*.json"))):
    with open(p) as fh:
        d = json.load(fh)
    i = len(tests)
    tests.append(d["test"])
    if not d["ok"]:
        bad_tests.append(d["test"])
    for table, kd in d["keys"]:
        per_table[table][kd].add(i)

# Tables read while pytest imported a test file: whatever that built (a cache,
# a module-level instance) is visible to every test of that file without a
# lookup, so those tests count as reading the whole table.
collect = set()
for p in glob.glob(os.path.join(trace_dir, "collect.*.json")):
    with open(p) as fh:
        d = json.load(fh)
    read = {table for table, _ in d["keys"]}
    if not read:
        continue
    files = d.get("files")
    if not files:
        collect |= read  # unknown files: leave these tables out
        continue
    in_files = {i for i, t in enumerate(tests) if t.split("::", 1)[0] in files}
    for table in read:
        per_table[table]["*"] |= in_files

if bad_tests:
    # a test whose run failed or crashed may have stopped before some lookups
    sys.exit(f"tests did not pass under tracing: {bad_tests[:10]} ({len(bad_tests)})")

sets, index = [], {}


def intern(s):
    mask = 0
    for i in s:
        mask |= 1 << i
    h = format(mask, "x")
    if h not in index:
        index[h] = len(sets)
        sets.append(h)
    return index[h]


tables = {
    t: {k: intern(s) for k, s in sorted(km.items())}
    for t, km in sorted(per_table.items())
    if t not in collect
}
# tables nobody looked up at all are known too: no test can see them
for t in set(meta["aliases"].values()) - set(per_table) - collect:
    tables[t] = {}
out = {"tests": tests, "sets": sets, "tables": tables, "aliases": meta["aliases"]}
os.makedirs(maps_dir, exist_ok=True)
with open(os.path.join(maps_dir, "keys.json.gz"), "wb") as fh:
    fh.write(gzip.compress(json.dumps(out, separators=(",", ":"), sort_keys=True).encode(), compresslevel=9, mtime=0))
print(f"{len(tests)} tests, {len(tables)} tables ({len(collect)} read during collection, "
      f"{len(meta['untraced'])} untraced), {len(sets)} sets", file=sys.stderr)
