"""pytest plugin used only to build the maps (tools/build_maps.sh).

Coverage contexts:
  "preimport"           every sqlglot module imported before any test (import-time lines)
  "collect|<test file>" whatever the test file's import / collection executes
  "<nodeid>|setup|call|teardown" per test phase
File reads and directory listings under the checkout are written as JSONL to $COVMAP_OPENS.
"""
import builtins
import io
import json
import os

import coverage

ROOT = os.environ.get("COVMAP_ROOT", os.getcwd())
_current = {"ctx": "preimport"}
_reads = set()
_orig_open = builtins.open


def _switch(ctx):
    _current["ctx"] = ctx
    cov = coverage.Coverage.current()
    if cov is not None:
        cov.switch_context(ctx)


def _record(path, kind):
    try:
        p = os.path.abspath(os.fspath(path))
    except Exception:
        return
    if isinstance(p, bytes):
        p = p.decode("utf-8", "replace")
    if p == ROOT or p.startswith(ROOT + "/"):
        _reads.add((_current["ctx"], kind, os.path.relpath(p, ROOT)))


def _open(file, *a, **k):
    if isinstance(file, (str, bytes, os.PathLike)):
        _record(file, "open")
    return _orig_open(file, *a, **k)


builtins.open = _open
io.open = _open

_orig_listdir, _orig_scandir = os.listdir, os.scandir


def _listdir(path="."):
    _record(path, "list")
    return _orig_listdir(path)


def _scandir(path="."):
    _record(path, "list")
    return _orig_scandir(path)


os.listdir = _listdir
os.scandir = _scandir


def pytest_runtest_setup(item):
    _switch(item.nodeid + "|setup")


def pytest_runtest_call(item):
    _switch(item.nodeid + "|call")


def pytest_runtest_teardown(item):
    _switch(item.nodeid + "|teardown")


def pytest_sessionfinish(session):
    out = os.environ.get("COVMAP_OPENS")
    if out:
        with _orig_open(out, "a") as f:
            for ctx, kind, p in sorted(_reads):
                f.write(json.dumps([ctx, kind, p]) + "\n")


# Process pools run inline so work done in pool workers is traced under the
# calling test. Arguments and results go through pickle like a real pool.
import concurrent.futures as _cf
import pickle as _pickle


class InlineExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*_pickle.loads(_pickle.dumps(initargs)))

    def submit(self, fn, *args, **kwargs):
        fut = _cf.Future()
        try:
            a, k = _pickle.loads(_pickle.dumps((args, kwargs)))
            fut.set_result(_pickle.loads(_pickle.dumps(fn(*a, **k))))
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


_cf.ProcessPoolExecutor = InlineExecutor
import concurrent.futures.process as _cfp  # noqa: E402

_cfp.ProcessPoolExecutor = InlineExecutor

# Import every sqlglot module up front so module and class bodies are recorded
# under "preimport" instead of being charged to whichever test loads a dialect first.
import importlib  # noqa: E402
import pkgutil  # noqa: E402

import sqlglot  # noqa: E402

for _m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    try:
        importlib.import_module(_m.name)
    except Exception:
        pass

_switch("collect|" + os.environ.get("COVMAP_FILE", "?"))
