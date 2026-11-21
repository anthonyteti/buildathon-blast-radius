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
from app import policy  # noqa: E402
from app import state  # noqa: E402
from app.probe import instrument  # noqa: E402
from app.verify import SRC, Verifier  # noqa: E402

DATA = os.environ.get("BR_DATA", "/data")
# Past this many seconds, remaining commits get the run-everything answer.
DEADLINE = float(os.environ.get("BR_DEADLINE", "100"))
# Run-time verification on/off, and its wall-clock allowance per commit.
VERIFY = os.environ.get("BR_VERIFY", "1") == "1"
# Import sqlglot with the patch for changes that run at import time (safety, not cost).
IMPORT_CHECK = os.environ.get("BR_IMPORT_CHECK", "1") == "1"
VERIFY_WALL_PER_COMMIT = float(os.environ.get("BR_VERIFY_WALL", "2.0"))


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def select(text, analyzer):
    """-> (tests or ALL, reason, per-file [(FileDiff, tests)], info) where info
    has the runtime part, the import-time-by-names part and an import-time flag."""
    diffs = patchlib.parse(text)
    info = {"runtime": set(), "names": set(), "import_time": False}
    if not diffs:
        return set(), "empty-patch", [], info
    tests = set()
    why = []
    parts = []
    for fd in diffs:
        t, w = analyzer.analyze(fd)
        why.append(f"{fd.path}: {w}")
        if t is ALL:
            return ALL, "; ".join(why), [], info
        parts.append((fd, t))
        tests |= t
        info["runtime"] |= analyzer.runtime_part
        info["names"] |= analyzer.names_part
        info["import_time"] |= analyzer.import_time
    return tests, "; ".join(why), parts, info


def patched_texts(parts):
    """{path: new text} for an in-memory overlay, or None when the change
    cannot be reproduced that way (added/removed/renamed/binary files)."""
    out = {}
    for fd, _ in parts:
        if fd.status != "modify" or fd.binary or not fd.path.startswith(("sqlglot/", "tests/")):
            return None
        with open(os.path.join(SRC, fd.path), encoding="utf-8") as fh:
            old = fh.read().split("\n")
        new, _ = patchlib.apply(old, fd.hunks)
        out[fd.path] = "\n".join(new)
    return out


def p_none(parts):
    p = 1.0
    for fd, t in parts:
        if t:
            p *= policy.P_NONE_CODE if fd.path.endswith(".py") else policy.P_NONE_DATA
    return p


class Commit:
    def __init__(self, cid):
        self.cid = cid
        self.sel = ALL
        self.why = "fallback"
        self.parts = []
        self.patched = None
        self.plan = []
        self.gain = 0.0
        self.skip_heavy = False
        self.info = None


def import_checks(commits, analyzer, verifier, start):
    """For changes that run at import time: a patched import that fails means
    every test can fail; otherwise the tests guessed by names are replaced by
    the tests naming the attributes whose import-time value actually changed."""
    todo = [c for c in commits if c.sel is not ALL and c.info and c.info["import_time"]]
    if not todo:
        return
    base = verifier.baseline_state()
    if base[0] != "ok":
        log("baseline import failed; keeping static answers:", base[1])
        return
    unstable = base[3]
    for c in todo:
        if time.monotonic() - start > DEADLINE * 0.6:
            c.why += " | import check skipped (time)"
            continue
        if c.patched is None:
            continue  # added/removed/renamed files: the static answer stands
        st = verifier.import_state(c.patched)
        if st[0] != "ok":
            c.sel = ALL
            c.why += f" | patched import failed: {st[1]}"
            continue
        broken = state.crashed(base[1], st[1])
        if broken:
            c.sel = ALL
            c.why += f" | patched import breaks {broken[:3]}"
            continue
        changed = [p for p in state.diff(base[2], st[2]) if p not in unstable]
        fp_tests = analyzer.state_tests(changed)
        if changed:
            # tests that import sqlglot in a fresh subprocess see all of it
            fp_tests |= analyzer.maps.subprocess_tests
        before = len(c.sel)
        c.sel = c.info["runtime"] | fp_tests
        c.why += f" | import-time changes {changed[:4]}{'...' if len(changed) > 4 else ''} -> {len(fp_tests)} (was {before})"


def affected_modules(patched, analyzer):
    """Patched sqlglot modules and every sqlglot module importing them."""
    rev = analyzer.importers()
    seen = set()
    todo = [p for p in patched if p.startswith("sqlglot/") and p.endswith(".py")]
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        todo.extend(q for q in rev.get(p, ()) if q.startswith("sqlglot/"))
    out = set()
    for p in seen:
        m = p[:-3].replace("/", ".")
        out.add(m[: -len(".__init__")] if m.endswith(".__init__") else m)
    return out


def plan_all(commits, analyzer, verifier, ms):
    """Choose what to verify for every commit. pandas/numpy/duckdb cost about
    a second to import once per run, so they are only allowed when the
    commits that need them gain more than that together."""
    maps = analyzer.maps
    verifiable = maps.mapped - maps.unverifiable

    def setup(v):
        return policy.FIXED_MS + sum(maps.class_setup_ms(c) for c in {verifier.class_of(t) for t in v})

    def plans(allow_heavy):
        out = {}
        pool = verifiable if allow_heavy else {t for t in verifiable if not verifier.needs_heavy([t])}
        for c in commits:
            if c.sel is ALL or not c.sel or c.patched is None:
                continue
            g, v = policy.plan(c.sel, ms, maps.cpu_ms, pool, p_none(c.parts), setup)
            skip = False
            if policy.SKIP_HEAVY and any(ms[t] >= policy.HEAVY_MS for t in c.sel) and probeable(c.parts, c.patched):
                # the rule needs every cheaper verifiable candidate run, so it is
                # only available in light mode when none of them needs pandas & co
                g2, v2 = policy.plan_skip(c.sel, ms, maps.cpu_ms, verifiable, p_none(c.parts), setup)
                if v2 and (allow_heavy or not verifier.needs_heavy(v2)) and g2 > g:
                    g, v, skip = g2, v2, True
            if v:
                out[c.cid] = (g, v, skip)
        return out

    light = plans(False)
    heavy = plans(True)
    light_gain = sum(g for g, _, _ in light.values())
    heavy_gain = sum(g for g, _, _ in heavy.values())
    use = heavy if heavy_gain - policy.HEAVY_IMPORT_MS > light_gain else light
    for c in commits:
        if c.cid in use:
            c.gain, c.plan, c.skip_heavy = use[c.cid]
    return use is heavy, light_gain, heavy_gain


def probeable(parts, patched):
    """Every change that selects tests is a side-effect-free expression
    replacement inside a function (see app/probe.py)."""
    for fd, t in parts:
        if not t:
            continue
        if not fd.path.endswith(".py"):
            return False
        with open(os.path.join(SRC, fd.path), encoding="utf-8") as fh:
            old = fh.read()
        code, n = instrument(old, patched[fd.path], fd.path)
        if code is None or n == 0:
            return False
    return True


def run_plans(commits, verifier, ms, cpu, verify_until):
    for c in sorted(commits, key=lambda c: -c.gain):
        if not c.plan:
            continue
        left = verify_until - time.monotonic()
        if left <= 1:
            c.why += " | static(out of time)"
            continue
        per_test = {t: max(1.0, 4 * cpu[t] / 1000 + 0.5) for t in c.plan}
        budget = min(left, 10 + 3 * sum(cpu[t] for t in c.plan) / 1000)
        cpu0 = verifier.cpu_children
        try:
            passed, failed, probes = verifier.run(c.patched, c.plan, per_test, budget, probe=c.skip_heavy)
        except Exception as e:  # noqa: BLE001
            c.why += f" | verify error {e!r}"
            continue
        if c.skip_heavy:
            ok = passed >= set(c.plan) and probes is not None and probes[0] > 0 and probes[1] == 0
            c.why += f" | probes {probes}"
            if ok:
                dropped = {t for t in c.sel if ms[t] >= policy.HEAVY_MS}
                c.sel = c.sel - dropped
                c.why += f" | skipped expensive {sorted(dropped)}"
        c.sel = c.sel - passed
        c.why += (
            f" | verified {len(passed)}/{len(c.plan)} passed (gain est {c.gain:.0f}ms),"
            f" first fail {failed}, cpu {verifier.cpu_children - cpu0:.2f}s"
        )


def main():
    start = time.monotonic()
    with open(os.path.join(DATA, "tests.json"), encoding="utf-8") as fh:
        tests_json = json.load(fh)
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests_json["tests"]}
    names = sorted(
        f[: -len(".patch")] for f in os.listdir(os.path.join(DATA, "commits")) if f.endswith(".patch")
    )
    commits = [Commit(cid) for cid in names]
    analyzer = None
    try:
        analyzer = Analyzer(Maps(tests_json))
    except Exception as e:  # noqa: BLE001
        log("maps failed to load, running everything:", repr(e))

    # 1. static analysis of every commit
    if analyzer is not None:
        for c in commits:
            if time.monotonic() - start > DEADLINE:
                break
            try:
                with open(os.path.join(DATA, "commits", c.cid + ".patch"), "rb") as fh:
                    text = fh.read().decode("utf-8", "replace")
                c.sel, c.why, c.parts, c.info = select(text, analyzer)
                if c.sel is not ALL and (c.sel or c.info["import_time"]):
                    c.patched = patched_texts(c.parts)
            except Exception as e:  # noqa: BLE001
                c.sel, c.why = ALL, f"error {e!r}"

    # 2. changes that run while sqlglot is imported: import it with the patch
    if analyzer is not None and IMPORT_CHECK:
        verifier = Verifier(tests_json["tests"])
        try:
            import_checks(commits, analyzer, verifier, start)
        except Exception as e:  # noqa: BLE001
            log("import checks failed:", repr(e))

    # 3. run-time verification where it is expected to pay
    if VERIFY and analyzer is not None:
        try:
            if not IMPORT_CHECK:
                verifier = Verifier(tests_json["tests"])
            heavy, lg, hg = plan_all(commits, analyzer, verifier, ms)
            log(f"verification plans: heavy libs {'on' if heavy else 'off'} (gain est light {lg:.0f}ms, heavy {hg:.0f}ms)")
            verify_until = start + min(DEADLINE * 0.8, VERIFY_WALL_PER_COMMIT * len(commits))
            run_plans(commits, verifier, ms, analyzer.maps.cpu_ms, verify_until)
        except Exception as e:  # noqa: BLE001
            log("verification failed:", repr(e))

    # 4. pack and print
    everything = None
    for c in commits:
        if c.sel is ALL:
            if everything is None:
                everything = pack(ms, ms)[1]
            workers = everything
        else:
            workers = pack(c.sel, ms)[1]
        sys.stdout.write(json.dumps({"commit": c.cid, "workers": workers}) + "\n")
        log(c.cid, sum(len(w) for w in workers), "tests", c.why[:400])
    sys.stdout.flush()
    log(f"done in {time.monotonic() - start:.2f}s wall, {time.process_time():.2f}s cpu (self)")


if __name__ == "__main__":
    main()
