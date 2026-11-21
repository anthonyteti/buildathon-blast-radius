"""Blast Radius selector: for each patch in /data/commits print the tests that
could fail and how to split them over workers. Logs go to stderr only."""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import patch as patchlib  # noqa: E402
from app.analyze import ALL, Analyzer  # noqa: E402
from app.maps import Maps  # noqa: E402
from app.pack import pack  # noqa: E402

DATA = os.environ.get("BR_DATA", "/data")
# Past this many seconds, remaining commits get the run-everything answer.
DEADLINE = float(os.environ.get("BR_DEADLINE", "100"))


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def select(text, analyzer):
    diffs = patchlib.parse(text)
    if not diffs:
        return set(), "empty-patch"
    tests = set()
    why = []
    for fd in diffs:
        t, w = analyzer.analyze(fd)
        why.append(f"{fd.path}: {w}")
        if t is ALL:
            return ALL, "; ".join(why)
        tests |= t
    return tests, "; ".join(why)


def main():
    start = time.monotonic()
    with open(os.path.join(DATA, "tests.json"), encoding="utf-8") as fh:
        tests_json = json.load(fh)
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests_json["tests"]}
    everything = None
    commits = sorted(
        f[: -len(".patch")] for f in os.listdir(os.path.join(DATA, "commits")) if f.endswith(".patch")
    )
    analyzer = None
    try:
        analyzer = Analyzer(Maps(tests_json))
    except Exception as e:  # noqa: BLE001
        log("maps failed to load, running everything:", repr(e))
    out = sys.stdout
    for cid in commits:
        workers = None
        why = "fallback"
        if analyzer is not None and time.monotonic() - start < DEADLINE:
            try:
                with open(os.path.join(DATA, "commits", cid + ".patch"), "rb") as fh:
                    text = fh.read().decode("utf-8", "replace")
                sel, why = select(text, analyzer)
                if sel is not ALL:
                    workers = pack(sel, ms)[1]
            except Exception as e:  # noqa: BLE001
                why = f"error {e!r}"
        if workers is None:
            if everything is None:
                everything = pack(ms, ms)[1]
            workers = everything
        out.write(json.dumps({"commit": cid, "workers": workers}) + "\n")
        log(cid, sum(len(w) for w in workers), "tests", why[:300])
    out.flush()
    log(f"done in {time.monotonic() - start:.2f}s wall, {time.process_time():.2f}s cpu")


if __name__ == "__main__":
    main()
