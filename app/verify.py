"""Run-time verification: run some candidate tests against the patched tree and
report which of them passed. Anything that does not clearly pass (failure,
error, skip, timeout, crash, harness problem) is treated as failing, so the
worst a wrong answer here can do is keep a test in the selection.

The disk is read-only, so the patch is applied in memory: patched modules are
served by an import hook and patched data files by an open() hook. Each
verification runs in a forked child of the selector, which never imports
sqlglot itself; heavy third-party libraries the tests need (pandas, numpy,
duckdb) are imported once in the parent so every child inherits them.
"""

import builtins
import io
import json
import os
import re
import select
import signal
import sys
import time

SRC = os.environ.get("SQLGLOT_SRC", "/opt/sqlglot")
HEAVY_LIBS = ("numpy", "pandas", "duckdb")
_HEAVY_RE = re.compile(r"^\s*(import|from)\s+(pandas|duckdb|numpy)\b", re.M)


class Verifier:
    def __init__(self, tests):
        self.tests = {t["number"]: t for t in tests}
        self._heavy_files = {}
        self.cpu_children = 0.0
        self.heavy_loaded = False

    def import_state(self, patched, budget=8.0, only=None):
        """Import all of sqlglot with the patch applied, in a child.
        -> ["error", message] or ["ok", {module: import error or None}, fingerprint]."""
        return _state_parent(self, patched, budget, only)

    def baseline_state(self):
        """Unpatched import status and fingerprint, made at image build time
        (maps/baseline_state.json, app/state.py); computed here in a child if
        that file is missing."""
        if getattr(self, "_baseline", None) is None:
            self.eager = None  # unknown: treat every module as eager
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "maps", "baseline_state.json")
            try:
                with open(path) as fh:
                    b = json.load(fh)
                self.eager = set(b["eager"])
                self._baseline = ["ok", b["status"], b["fingerprint"], set(b["unstable"])]
            except (OSError, ValueError, KeyError):
                a = self.import_state({})
                self._baseline = a + [set()] if a[0] == "ok" else a
        return self._baseline

    def class_of(self, n):
        return self.tests[n]["id"].rsplit("::", 1)[0]

    def needs_heavy(self, nums):
        for n in nums:
            f = self.tests[n]["file"]
            if f not in self._heavy_files:
                try:
                    with open(os.path.join(SRC, f), encoding="utf-8") as fh:
                        self._heavy_files[f] = bool(_HEAVY_RE.search(fh.read()))
                except OSError:
                    self._heavy_files[f] = True
            if self._heavy_files[f]:
                return True
        return False

    def run(self, patched, nums, per_test, budget, probe=False, jobs=None):
        """patched: {relative path: new text}; nums: tests to run, in order.
        per_test: {num: CPU seconds allowed}. budget: wall seconds for all.
        Stops at the first test that does not pass.
        With probe=True, patched modules carry infection probes (app/probe.py).
        jobs={test: (job indices, job count)}: in that test, process-pool jobs
        outside the set return None without running (numbering as in
        tools/covplugin.py); the test only counts as passed if it submitted
        exactly the recorded number of jobs.
        Returns (passed numbers, first non-passing number or None,
        (probe executions, infected executions) or None)."""
        # keep each class's tests together: unittest re-runs setUpClass (1.5 s
        # for the executor tests) every time the class changes
        first = {}
        for i, n in enumerate(nums):
            first.setdefault(self.class_of(n), i)
        nums = sorted(nums, key=lambda n: (first[self.class_of(n)], nums.index(n)))
        if self.needs_heavy(nums):
            os.environ.setdefault("OMP_NUM_THREADS", "1")
            os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
            self.heavy_loaded = True
            for lib in HEAVY_LIBS:
                try:
                    __import__(lib)
                except Exception:  # noqa: BLE001
                    pass
        import unittest  # noqa: F401  (inherited by children)

        r, w = os.pipe()
        sys.stdout.flush()
        sys.stderr.flush()
        pid = os.fork()
        if pid == 0:  # child
            os.close(r)
            code = 0
            try:
                os.dup2(2, 1)  # nothing a test prints may reach our stdout
                _child(patched, [(n, self.tests[n]["id"], per_test[n]) for n in nums], w, probe,
                       {self.tests[n]["id"]: (set(js), cnt) for n, (js, cnt) in (jobs or {}).items()})
            except BaseException:  # noqa: BLE001
                code = 1
            finally:
                os._exit(code)
        os.close(w)
        passed = set()
        failed = None
        probes = None
        buf = b""
        deadline = time.monotonic() + budget
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            ready, _, _ = select.select([r], [], [], left)
            if not ready:
                break
            chunk = os.read(r, 65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row[0] == "probe":
                    probes = (row[1], row[2])
                    continue
                n, ok = row
                if ok:
                    passed.add(n)
                elif failed is None:
                    failed = n
        os.close(r)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _, status, usage = os.wait4(pid, 0)
        self.cpu_children += usage.ru_utime + usage.ru_stime
        return passed, failed, probes


def _state_parent(self, patched, budget, only=None):
    r, w = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        code = 0
        try:
            os.dup2(2, 1)
            _state_child(patched, w, only)
        except BaseException:  # noqa: BLE001
            code = 1
        finally:
            os._exit(code)
    os.close(w)
    chunks = []
    deadline = time.monotonic() + budget
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        ready, _, _ = select.select([r], [], [], left)
        if not ready:
            break
        chunk = os.read(r, 1 << 20)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(r)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _, _, usage = os.wait4(pid, 0)
    self.cpu_children += usage.ru_utime + usage.ru_stime
    try:
        return json.loads(b"".join(chunks))
    except ValueError:
        return ["error", "no result"]


class _Timeout(BaseException):
    pass


def _install(patched, probe=False):
    """Serve the patched files from memory and run process pools inline.
    Returns the probe recorder."""
    full = {os.path.join(SRC, p): t for p, t in patched.items()}

    # --- patched modules -------------------------------------------------
    import importlib.abc
    import importlib.util

    mods = {}
    for p, text in patched.items():
        if p.endswith(".py"):
            name = p[:-3].replace("/", ".")
            if name.endswith(".__init__"):
                name = name[: -len(".__init__")]
            mods[name] = (os.path.join(SRC, p), text)

    class Overlay(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, name, path, target=None):
            if name not in mods:
                return None
            origin = mods[name][0]
            is_pkg = origin.endswith("__init__.py")
            spec = importlib.util.spec_from_loader(name, self, origin=origin, is_package=is_pkg)
            spec.has_location = True
            if is_pkg:
                spec.submodule_search_locations = [os.path.dirname(origin)]
            return spec

        def create_module(self, spec):
            return None

        def exec_module(self, module):
            origin, text = mods[module.__name__]
            module.__file__ = origin
            code = None
            if probe:
                with orig_open(origin, encoding="utf-8") as fh:
                    code, _ = instrument(fh.read(), text, origin)
                if code is None:
                    recorder.unprobed += 1
            if code is None:
                code = compile(text, origin, "exec")
            exec(code, module.__dict__)

    orig_open = builtins.open
    from .probe import Recorder, instrument

    recorder = Recorder()
    recorder.unprobed = 0
    builtins.__br_probe__ = recorder
    sys.meta_path.insert(0, Overlay())

    # --- patched data files ----------------------------------------------

    def _open(file, mode="r", *a, **k):
        if isinstance(file, (str, os.PathLike)):
            p = os.path.abspath(os.fspath(file))
            if p in full and "r" in mode and "+" not in mode:
                if "b" in mode:
                    return io.BytesIO(full[p].encode("utf-8"))
                return io.StringIO(full[p])
        return orig_open(file, mode, *a, **k)

    builtins.open = _open
    io.open = _open

    # --- process pools run inline (as in the map build) ------------------
    import concurrent.futures as cf
    import concurrent.futures.process as cfp
    import pickle

    class InlineExecutor:
        def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
            if initializer is not None:
                initializer(*pickle.loads(pickle.dumps(initargs)))

        def submit(self, fn, *args, **kwargs):
            fut = cf.Future()
            i = _job_state["n"]
            _job_state["n"] += 1
            only = _job_state["only"].get(_job_state["test"])
            if only is not None and i not in only[0]:
                fut.set_result(None)  # job does not reach the change
                return fut
            try:
                a, k = pickle.loads(pickle.dumps((args, kwargs)))
                fut.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
            except BaseException as e:  # noqa: BLE001
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

    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path.insert(0, SRC)
    os.chdir(SRC)
    return recorder


def _state_child(patched, wfd, only=None):
    out = os.fdopen(wfd, "w")
    _install(patched)
    from .state import fingerprint, import_all

    try:
        status = import_all(only)
        fp = fingerprint()
    except BaseException as e:  # noqa: BLE001
        out.write(json.dumps(["error", repr(e)[:300]]))
        out.flush()
        return
    out.write(json.dumps(["ok", status, fp]))
    out.flush()


_job_state = {"test": None, "n": 0, "only": {}}


def _child(patched, plan, wfd, probe=False, jobs=None):
    out = os.fdopen(wfd, "w", buffering=1)
    recorder = _install(patched, probe)
    _job_state["only"] = jobs or {}

    import unittest

    def on_alarm(signum, frame):
        raise _Timeout()

    signal.signal(signal.SIGPROF, on_alarm)

    by_id = {}
    suite = unittest.TestSuite()
    for n, tid, limit in plan:
        path, cls, meth = tid.split("::")
        dotted = path[:-3].replace("/", ".") + "." + cls + "." + meth
        try:
            loaded = unittest.defaultTestLoader.loadTestsFromName(dotted)
        except BaseException:  # noqa: BLE001
            out.write(json.dumps([n, False]) + "\n")
            return
        for t in _flatten(loaded):
            by_id[t.id()] = (n, limit)
            suite.addTest(t)
        if not by_id or dotted not in by_id:
            out.write(json.dumps([n, False]) + "\n")
            return

    class Result(unittest.TestResult):
        def startTest(self, test):
            super().startTest(test)
            _job_state["n"] = 0
            _job_state["test"] = None
            for tid in _job_state["only"]:
                path, cls, meth = tid.split("::")
                if test.id() == path[:-3].replace("/", ".") + "." + cls + "." + meth:
                    _job_state["test"] = tid
            self._bad = False
            _, limit = by_id.get(test.id(), (None, 2.0))
            signal.setitimer(signal.ITIMER_PROF, limit)

        def _fail(self):
            self._bad = True
            self.shouldStop = True

        def addError(self, test, err):
            if test.id() not in by_id:  # setUpClass / module fixture error
                out.write(json.dumps([-1, False]) + "\n")
                self.shouldStop = True
                return
            self._fail()

        def addFailure(self, test, err):
            self._fail()

        def addSkip(self, test, reason):
            self._fail()

        def addExpectedFailure(self, test, err):
            self._fail()

        def addUnexpectedSuccess(self, test):
            self._fail()

        def addSubTest(self, test, subtest, err):
            if err is not None:
                self._fail()

        def stopTest(self, test):
            signal.setitimer(signal.ITIMER_PROF, 0)
            super().stopTest(test)
            tid = _job_state["test"]
            if tid is not None and _job_state["n"] != _job_state["only"][tid][1]:
                self._bad = True  # job numbering does not match the map
            if probe and recorder.infected + recorder.unprobed:
                self.shouldStop = True  # the expensive-test rule can no longer apply
            if test.id() in by_id:
                n, _ = by_id[test.id()]
                out.write(json.dumps([n, not self._bad]) + "\n")

    try:
        suite.run(Result())
    except _Timeout:
        pass
    if probe:
        # a module that could not be instrumented counts as infected
        out.write(json.dumps(["probe", recorder.runs, recorder.infected + recorder.unprobed]) + "\n")


def _flatten(s):
    import unittest

    if isinstance(s, unittest.TestSuite):
        for x in s:
            yield from _flatten(x)
    else:
        yield s
