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
from app.verify import MEMO, SRC, Verifier  # noqa: E402
from app import memo  # noqa: E402

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
    info = {"runtime": set(), "names": set(), "import_time": False, "looked_up": {}, "whole_file": False}
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
        info["whole_file"] |= analyzer.whole_file
        for f, ls in analyzer.looked_up.items():
            info["looked_up"].setdefault(f, set()).update(ls)
    return tests, "; ".join(why), parts, info


def patched_texts(parts):
    """{path: new text} for an in-memory overlay, or None when the change
    cannot be reproduced that way (added/removed/renamed/binary files)."""
    out = {}
    for fd, _ in parts:
        if fd.status != "modify" or fd.binary or not fd.path.startswith(("sqlglot/", "tests/")):
            return None
        if fd.path.endswith((".csv", ".gz", ".parquet")):
            return None  # data files duckdb reads from disk, past the open() hook
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
        mods = affected_modules(c.patched, analyzer)
        st = verifier.import_state(c.patched, only=mods)
        if st[0] != "ok":
            c.sel = ALL
            c.why += f" | patched import failed: {st[1]}"
            continue
        broken = state.crashed(base[1], st[1])
        if broken:
            c.sel = ALL
            c.why += f" | patched import breaks {broken[:3]}"
            continue
        changed = [p for p in state.diff(base[2], st[2], modules=set(st[1])) if p not in unstable]
        tables, keys = {}, {}
        for p in changed:
            tc = state.changed_keys(base[4], st[3], p)
            if tc is not None:
                tables[p] = tc
            kd = state.changed_key_digests(base[4], st[3], p)
            if kd is not None:
                keys[p] = kd
        fp_tests = analyzer.state_tests(changed, tables, keys)
        eager = verifier.eager
        if any(eager is None or p.split(":", 1)[0] in eager for p in changed):
            # tests that run `import sqlglot` in a fresh subprocess see what it loads
            fp_tests |= analyzer.maps.subprocess_tests
        before = len(c.sel)
        c.sel = c.info["runtime"] | fp_tests
        c.why += f" | import-time changes {changed[:4]}{'...' if len(changed) > 4 else ''} -> {len(fp_tests)} (was {before})"


def affected_modules(patched, analyzer):
    """Patched sqlglot modules and every sqlglot module whose import runs
    theirs (module-level imports only). Their import-time state is what the
    patch can change; it is compared against a full unpatched import."""
    rev = {}
    for f, deps in analyzer.maps.imports_top.items():
        for d in deps:
            rev.setdefault(d, set()).add(f)
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
            ok = (passed >= set(c.plan) and probes is not None
                  and probes[0] >= policy.SKIP_MIN_RUNS and probes[1] == 0)
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


def _memo_plan(rec, mc, maps, verifier, c, h, tid, cls, exact_jobs):
    """-> (jobs that must run, recorded job count, estimated CPU ms) for
    running test h with the recording (app/memo.py)."""
    recorded_jobs = (rec.get("jobs") or {}).get(tid) or []
    count = len(recorded_jobs)
    r = maps.jobs_reaching(h, c.info["looked_up"], main_ok=True) if exact_jobs else None
    if count and (r is None or r[0] != count):
        jobs = set(range(count))  # every job runs
    else:
        jobs = r[1] if r else set()
    if any(r is None for j, r in enumerate(recorded_jobs) if j in jobs):
        job_ms = mc["replay"] - mc["replay_all"]
    else:
        job_ms = sum(r[2] for j, r in enumerate(recorded_jobs) if r is not None and j in jobs)
    run_ms = policy.FIXED_MS + policy.ALPHA * (maps.class_setup_ms(cls) + mc["replay_all"] + job_ms)
    return jobs, count, run_ms


def plan_jobs(commits, analyzer, verifier, ms):
    """Expensive tests still selected that the recorded unpatched run
    (app/memo.py, maps/memo.pkl) can make cheap to run here:
    - pool-based tests (test_execute_tpcds, ...): jobs that reach a changed
      line run, the others return their recorded result when their pool
      initializer and arguments are byte-identical to the recorded run, and
      run otherwise;
    - duckdb read queries with the recorded connection history and text return
      the recorded frame.
    Everything sqlglot computes outside those jobs runs for real, so a pass
    means the test passes on this change.
    Without a recording: pool-based tests whose changed lines ran only inside
    jobs run just those jobs (the others return None, which they skip).
    -> [(commit, test, jobs, job count, mode, estimated ms)] worth running."""
    maps = analyzer.maps
    rec = memo.load() if MEMO else None
    rec_cpu = (rec or {}).get("cpu_ms", {})
    out = []
    for c in commits:
        if c.sel is ALL or c.patched is None or not c.info:
            continue
        exact_jobs = not (c.info["whole_file"] or c.info["import_time"])
        for h in sorted(c.sel):
            if ms[h] < policy.HEAVY_MS or h in maps.unverifiable:
                continue
            tid = verifier.tests[h]["id"]
            cls = verifier.class_of(h)
            mc = rec_cpu.get(tid)
            if mc:
                try:
                    plan = _memo_plan(rec, mc, maps, verifier, c, h, tid, cls, exact_jobs)
                except Exception:  # noqa: BLE001  (an unreadable recording: skip it)
                    continue
                jobs, count, run_ms = plan
                mode = "memo"
            else:
                if not exact_jobs:
                    continue
                r = maps.jobs_reaching(h, c.info["looked_up"])
                if r is None or not r[1]:
                    continue
                count, jobs = r
                # measured: a small fixed part (class setup, parsing every job's input)
                # plus each job at ~1.6x the test's average per-job CPU
                run_ms = policy.FIXED_MS + policy.ALPHA * (
                    maps.class_setup_ms(cls) + maps.cpu_ms[h] * (0.01 + 1.6 * len(jobs) / count)
                )
                mode = "only"
            saved = pack(c.sel, ms)[0] - pack(c.sel - {h}, ms)[0]
            # a failing job stops the run early: assume half the cost then
            gain = policy.P_HEAVY_PASS * saved - run_ms * (policy.P_HEAVY_PASS + (1 - policy.P_HEAVY_PASS) / 2)
            if gain > policy.MIN_GAIN_MS:
                out.append((gain, c, h, jobs, count, mode, run_ms))
    # these tests need pandas/numpy/duckdb: worth importing only for enough gain
    if out and not verifier.heavy_loaded and sum(g for g, *_ in out) < policy.HEAVY_IMPORT_MS:
        return []
    return [x[1:] for x in sorted(out, key=lambda x: -x[0])]


def run_jobs(plans, verifier, ms, cpu, verify_until):
    for c, h, jobs, count, mode, run_ms in plans:
        if h not in c.sel:
            continue
        left = verify_until - time.monotonic()
        if left <= 1:
            c.why += " | jobs: out of time"
            continue
        # CPU cap from the planned job count: a change can make a few jobs very
        # slow, and running into the cap just keeps the test selected
        limit = max(2.0, 1.5 * run_ms / 1000)
        try:
            cpu0 = verifier.cpu_children
            passed, failed, _ = verifier.run(c.patched, [h], {h: limit}, min(left, 5 + limit),
                                             jobs={h: (jobs, count, mode)} if count or mode == "only" else None)
        except Exception as e:  # noqa: BLE001
            c.why += f" | jobs error {e!r}"
            continue
        if h in passed:
            c.sel = c.sel - {h}
        c.why += (f" | ran {len(jobs)}/{count} jobs of {h} ({mode}): {'passed' if h in passed else 'kept'},"
                  f" cpu {verifier.cpu_children - cpu0:.2f}s")


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

    def phase(name):
        kids = getattr(verifier, "cpu_children", 0.0) if verifier is not None else 0.0
        log(f"phase {name}: {time.monotonic() - start:.2f}s wall, self {time.process_time():.2f}s, children {kids:.2f}s")

    verifier = None
    phase("loaded")
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

    phase("analysed")
    # 2. changes that run while sqlglot is imported: import it with the patch
    if analyzer is not None and IMPORT_CHECK:
        verifier = Verifier(tests_json["tests"])
        try:
            import_checks(commits, analyzer, verifier, start)
        except Exception as e:  # noqa: BLE001
            log("import checks failed:", repr(e))

    phase("import-checked")
    # 3. run-time verification where it is expected to pay
    if VERIFY and analyzer is not None:
        try:
            if not IMPORT_CHECK:
                verifier = Verifier(tests_json["tests"])
            heavy, lg, hg = plan_all(commits, analyzer, verifier, ms)
            log(f"verification plans: heavy libs {'on' if heavy else 'off'} (gain est light {lg:.0f}ms, heavy {hg:.0f}ms)")
            verify_until = start + min(DEADLINE * 0.8, VERIFY_WALL_PER_COMMIT * len(commits))
            run_plans(commits, verifier, ms, analyzer.maps.cpu_ms, verify_until)
            if policy.JOB_SUBSETS:
                jp = plan_jobs(commits, analyzer, verifier, ms)
                if jp:
                    log(f"job-subset runs planned: {len(jp)}")
                    run_jobs(jp, verifier, ms, analyzer.maps.cpu_ms, verify_until)
        except Exception as e:  # noqa: BLE001
            log("verification failed:", repr(e))

    phase("verified")
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
