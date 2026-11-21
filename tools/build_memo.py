"""Record what app/memo.py replays, by running sqlglot's tests unpatched.

usage: build_memo.py <sqlglot checkout> <out memo.pkl> [test class ...]

Runs each given unittest class (default: the executor tests, which compare
sqlglot's Python executor against duckdb) in this process with process pools
inline, and records:
  - every read query's DataFrame per duckdb connection history (app/memo.py);
  - per pool-based test, each job's argument digest, pickled result, CPU ms,
    digest of the tree optimize() returned (when the job called it once) and
    CPU ms up to that point; the lines of the checkout each job ran outside
    optimize(); and
    the digest of the pool's initializer arguments;
  - CPU seconds per test in four modes, for the cost estimate: as recorded,
    replaying duckdb only, replaying duckdb and every job, and replaying
    duckdb with every job ending at optimize().
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
opt_hits = [0]
outside = {}  # test -> {file: {line: bitmask of jobs}}
state = {"test": None, "n": 0, "mode": "record"}
job = {"in_opt": False, "lines": None, "digest": None, "t0": 0.0, "opt_ms": None, "opt_calls": 0}
SRC_PREFIX = src + os.sep

# lines of the checkout (sqlglot/, tests/) a job may run outside optimize():
# every line of every code object (function, lambda, module body) that starts
# or resumes outside optimize() during the job (sys.monitoring PY_START and
# PY_RESUME). Whole code objects, not LINE events: a lambda's body shares its
# line with the lambda and reports no LINE event of its own. Each code object
# is reported once per job (DISABLE, re-armed by restart_events at the next
# job); inside optimize() nothing is reported and events stay armed.
MON = sys.monitoring
TOOL = 3
MON.use_tool_id(TOOL, "br-memo")
_code_lines = {}


def _start(code, offset):
    if job["in_opt"]:
        return None
    f = code.co_filename
    if f.startswith(SRC_PREFIX):
        lines = _code_lines.get(code)
        if lines is None:
            rel = os.path.relpath(f, src)
            lines = _code_lines[code] = {(rel, ln) for _, _, ln in code.co_lines() if ln is not None}
            lines.add((rel, code.co_firstlineno))
        job["lines"] |= lines
    return MON.DISABLE


MON.register_callback(TOOL, MON.events.PY_START, _start)
MON.register_callback(TOOL, MON.events.PY_RESUME, _start)
EVENTS = MON.events.PY_START | MON.events.PY_RESUME

# files of the checkout a job opens (its input beyond the pickled arguments)
import builtins  # noqa: E402
import io  # noqa: E402

job_opens = {}  # test -> set of paths
_real_open = builtins.open


def _open(file, *a, **k):
    if job["lines"] is not None and isinstance(file, (str, bytes, os.PathLike)):
        p = os.path.abspath(os.fsdecode(file))
        if p.startswith(SRC_PREFIX):
            job_opens.setdefault(state["test"], set()).add(os.path.relpath(p, src))
    return _real_open(file, *a, **k)


builtins.open = _open
io.open = _open


def _wrap_optimize():
    import sqlglot.executor as ex

    if getattr(ex.optimize, "_br_build", False):
        return
    real = ex.optimize

    def optimize(*a, **k):
        if job["lines"] is None and state["mode"] != "replay_opt":
            return real(*a, **k)
        job["opt_calls"] += 1
        job["in_opt"] = True
        try:
            tree = real(*a, **k)
        finally:
            job["in_opt"] = False
        job["opt_ms"] = round((cpu() - job["t0"]) * 1000)
        job["digest"] = memo.tree_digest(tree)
        if state["mode"] == "replay_opt" and job.get("expect") == job["digest"]:
            raise memo.OptimizedAsRecorded()
        return tree

    optimize._br_build = True
    ex.optimize = optimize


class InlineExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        blob = pickle.dumps(initargs)
        if state["mode"] == "record":
            inits[state["test"]] = memo.blob_key(blob)
        if initializer is not None:
            initializer(*pickle.loads(blob))

    def submit(self, fn, *args, **kwargs):
        _wrap_optimize()
        fut = cf.Future()
        i = state["n"]
        state["n"] += 1
        key = memo.args_key(args, kwargs)
        rec = jobs.setdefault(state["test"], [])
        mode = state["mode"]
        if mode == "replay_all" and i < len(rec) and rec[i] is not None and rec[i][0] == key:
            fut.set_result(pickle.loads(rec[i][1]))
            return fut
        job.update(digest=None, opt_ms=None, t0=cpu(), expect=None, opt_calls=0)
        if mode == "replay_opt" and i < len(rec) and rec[i] is not None and rec[i][0] == key:
            job["expect"] = rec[i][3]
        if mode == "record":
            job["lines"] = set()
            MON.restart_events()
            MON.set_events(TOOL, EVENTS)
        try:
            try:
                a, k = pickle.loads(pickle.dumps((args, kwargs)))
                blob = pickle.dumps(fn(*a, **k))
            finally:
                if mode == "record":
                    MON.set_events(TOOL, 0)
            if mode == "record":
                # the optimizer step can stand for the job only if it ran once
                digest = job["digest"] if job["opt_calls"] == 1 else None
                rec.append([key, blob, round((cpu() - job["t0"]) * 1000), digest, None])
                per_file = outside.setdefault(state["test"], {})
                for f, ln in job["lines"]:
                    per_file.setdefault(f, {})
                    per_file[f][ln] = per_file[f].get(ln, 0) | (1 << i)
            fut.set_result(pickle.loads(blob))
        except memo.OptimizedAsRecorded:
            rec[i][4] = job["opt_ms"]  # CPU of a job that ends at optimize()
            opt_hits[0] += 1
            fut.set_result(pickle.loads(rec[i][1]))
        except BaseException as e:  # noqa: BLE001
            if mode == "record":
                rec.append(None)
            fut.set_exception(e)
        finally:
            job["lines"] = None
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
# never fetch duckdb extensions while the image builds: a query that needs one
# fails, the recording is dropped, and the selector does without it
_OFFLINE = {"autoinstall_known_extensions": False, "autoload_known_extensions": False}


def _connect(*a, **k):
    k["config"] = {**_OFFLINE, **(k.get("config") or {})}
    return memo.Connection(_real_connect(*a, **k), store, "record" if state["mode"] == "record" else "replay")


duckdb.connect = _connect


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


total = {m: run(m) for m in ("record", "replay", "replay_all", "replay_opt")}
data = {
    "duckdb": dict(store),
    "jobs": {t: rec for t, rec in jobs.items() if rec},
    "inits": inits,
    "outside": outside,
    "job_opens": {t: sorted(job_opens.get(t, ())) for t, rec in jobs.items() if rec},
    "cpu_ms": times,
    "total_s": total,
}
with open(out_path, "wb") as fh:
    pickle.dump(data, fh, protocol=4)
print(f"memo: {len(store)} queries, {sum(len(v) for v in data['jobs'].values())} jobs in {len(data['jobs'])} tests, "
      f"{opt_hits[0]} jobs end at optimize() in a fresh replay, "
      f"cpu s {', '.join(f'{m} {t:.1f}' for m, t in total.items())}", file=sys.stderr)
