"""Record what app/memo.py replays, by running sqlglot's tests unpatched.

usage: build_memo.py <sqlglot checkout> <out memo.pkl> [test class ...]

Runs each given unittest class (default: the executor tests, which compare
sqlglot's Python executor against duckdb) in this process with process pools
inline, and records:
  - every read query's DataFrame per duckdb connection history (app/memo.py);
  - per pool-based test, each job's argument digest, pickled result and CPU
    ms, and
    the digest of the pool's initializer arguments;
  - CPU seconds per test in three modes, for the cost estimate: as recorded,
    replaying duckdb only, and replaying duckdb and every job.
Writes nothing unless every test passes in all three modes. Run at image
build time (Dockerfile), so the data comes from the pinned commit in the
scoring environment.
"""

import concurrent.futures as cf
import concurrent.futures.process as cfp
import os
import pickle
import resource
import sys
import unittest

src, out_path = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
classes = sys.argv[3:] or ["tests.test_executor.TestExecutor"]
os.environ["SKIP_INTEGRATION"] = "0"
os.environ.setdefault("OMP_NUM_THREADS", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, src)
os.chdir(src)

from app import memo  # noqa: E402

store = {}
jobs = {}
inits = {}
state = {"test": None, "n": 0, "mode": "record"}


class InlineExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        blob = pickle.dumps(initargs)
        if state["mode"] == "record":
            inits[state["test"]] = memo.blob_key(blob)
        if initializer is not None:
            initializer(*pickle.loads(blob))

    def submit(self, fn, *args, **kwargs):
        fut = cf.Future()
        i = state["n"]
        state["n"] += 1
        key = memo.args_key(args, kwargs)
        rec = jobs.setdefault(state["test"], [])
        if state["mode"] == "replay_all" and i < len(rec) and rec[i] is not None and rec[i][0] == key:
            fut.set_result(pickle.loads(rec[i][1]))
            return fut
        try:
            t0 = cpu()
            a, k = pickle.loads(pickle.dumps((args, kwargs)))
            blob = pickle.dumps(fn(*a, **k))
            if state["mode"] == "record":
                rec.append((key, blob, round((cpu() - t0) * 1000)))
            fut.set_result(pickle.loads(blob))
        except BaseException as e:  # noqa: BLE001
            if state["mode"] == "record":
                rec.append(None)
            fut.set_exception(e)
        return fut

    def map(self, fn, *its, **kw):
        return [self.submit(fn, *a).result() for a in zip(*its)]

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


cf.ProcessPoolExecutor = InlineExecutor
cfp.ProcessPoolExecutor = InlineExecutor

import duckdb  # noqa: E402

_real_connect = duckdb.connect
duckdb.connect = lambda *a, **k: memo.Connection(
    _real_connect(*a, **k), store, "record" if state["mode"] == "record" else "replay")


def cpu():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def tid(test):
    # tests.test_executor.TestExecutor.test_x -> tests/test_executor.py::TestExecutor::test_x
    mod, cls, meth = test.id().rsplit(".", 2)
    return f"{mod.replace('.', '/')}.py::{cls}::{meth}"


times = {}


class Result(unittest.TestResult):
    def startTest(self, test):
        super().startTest(test)
        state["test"] = tid(test)
        state["n"] = 0
        self._t0 = cpu()

    def stopTest(self, test):
        super().stopTest(test)
        times.setdefault(state["test"], {})[state["mode"]] = round((cpu() - self._t0) * 1000)


def run(mode):
    state["mode"] = mode
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(c) for c in classes)
    res = Result()
    t0 = cpu()
    suite.run(res)
    bad = res.failures + res.errors + [(t, "unexpected success") for t in res.unexpectedSuccesses]
    if bad:
        sys.exit(f"{mode}: {len(bad)} tests did not pass: {[str(t) for t, _ in bad[:5]]}")
    return cpu() - t0


total = {m: run(m) for m in ("record", "replay", "replay_all")}
data = {
    "duckdb": dict(store),
    "jobs": {t: rec for t, rec in jobs.items() if rec},
    "inits": inits,
    "cpu_ms": times,
    "total_s": total,
}
with open(out_path, "wb") as fh:
    pickle.dump(data, fh, protocol=4)
print(f"memo: {len(store)} queries, {sum(len(v) for v in data['jobs'].values())} jobs in {len(data['jobs'])} tests, "
      f"cpu s {', '.join(f'{m} {t:.1f}' for m, t in total.items())}", file=sys.stderr)
