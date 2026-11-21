"""Measure what verification costs: every test's CPU when run in one process
the way app/verify.py runs it (unittest, process pools inline), plus each
class's setUpClass/tearDownClass CPU. Writes maps/cpu.json.

usage: measure_cpu.py <sqlglot_checkout> <tests.json> <out.json>

Times are scaled to the machine that recorded tests.json: the scale is the
median of recorded/measured over tests that do not use process pools.
"""
import concurrent.futures as cf
import concurrent.futures.process as cfp
import json
import os
import pickle
import resource
import statistics
import sys
import unittest

src, tests_path, out_path = (os.path.abspath(p) for p in sys.argv[1:4])
os.environ["SKIP_INTEGRATION"] = "0"
os.environ.setdefault("OMP_NUM_THREADS", "1")
sys.path.insert(0, src)
os.chdir(src)

POOL_USED = set()
CURRENT = [None]


class InlineExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *args, **kwargs):
        POOL_USED.add(CURRENT[0])
        fut = cf.Future()
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


def cpu():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


tests = json.load(open(tests_path))["tests"]
recorded = {t["id"]: t["seconds"] for t in tests}
dotted = {}
suite = unittest.TestSuite()
for t in tests:
    path, cls, meth = t["id"].split("::")
    name = path[:-3].replace("/", ".") + "." + cls + "." + meth
    dotted[name] = t["id"]
    suite.addTest(unittest.defaultTestLoader.loadTestsFromName(name))

test_cpu = {}
gaps = {}
state = {"last": cpu(), "start": None, "cls": None}


class Result(unittest.TestResult):
    def startTest(self, test):
        now = cpu()
        cls = test.id().rsplit(".", 1)[0]
        if cls != state["cls"]:
            gaps[cls] = now - state["last"]  # setUpClass (+ previous tearDownClass)
            state["cls"] = cls
        CURRENT[0] = test.id()
        state["start"] = now
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        now = cpu()
        test_cpu[test.id()] = now - state["start"]
        state["last"] = now


res = Result()
suite.run(res)
print("errors", len(res.errors), "failures", len(res.failures), file=sys.stderr)

ratios = [recorded[dotted[d]] / c for d, c in test_cpu.items()
          if d in dotted and d not in POOL_USED and c > 0.02 and recorded[dotted[d]] > 0.02]
scale = statistics.median(ratios) if ratios else 1.0
out = {
    "scale": round(scale, 3),
    "test_ms": {dotted[d]: round(c * scale * 1000) for d, c in test_cpu.items() if d in dotted},
    "class_setup_ms": {},
    "pool_tests": sorted(dotted[d] for d in POOL_USED if d in dotted),
}
for cls, g in gaps.items():
    path_cls = None
    for d, tid in dotted.items():
        if d.rsplit(".", 1)[0] == cls:
            path_cls = tid.rsplit("::", 1)[0]
            break
    if path_cls and g * scale >= 0.01:
        out["class_setup_ms"][path_cls] = round(g * scale * 1000)
json.dump(out, open(out_path, "w"), indent=0, sort_keys=True)
print("scale", scale, "pool tests", len(out["pool_tests"]),
      "top setup", sorted(out["class_setup_ms"].items(), key=lambda x: -x[1])[:5], file=sys.stderr)
