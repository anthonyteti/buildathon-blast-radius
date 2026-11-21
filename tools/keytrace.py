"""pytest plugin used only to build maps/keys.json.gz (tools/build_keys.sh).

Which tests look up which keys of sqlglot's class-level dict tables
(FUNCTIONS, TRANSFORMS, KEYWORDS, NUMERIC_ESCAPES, ...). Every dict defined in
a sqlglot class body is swapped, after every sqlglot module is imported, for a
subclass that records per test:
  - each key looked up ([], get, in);
  - "*" when the whole table is read (iterated, copied, merged, compared,
    pickled, printed);
  - "#size" when its size or emptiness is read (len, bool).

Each test runs in its own forked child of a process that has imported sqlglot
but run no test, so nothing a test caches (e.g. a dispatch table built from
TRANSFORMS on first use) can hide a lookup from a later test. Lookups made
while pytest imports the test files happen in the parent; tables touched then
are written out as "collect" and never narrowed by key.

Output, one JSON file per test in $KT_OUT: {"test": nodeid, "ok": bool,
"keys": [[table, key digest], ...]}, plus collect.<pid>.json and
aliases.json ({fingerprint path: table} for app/state.py paths that reach a
traced table under another name, e.g. Dialect.tokenizer_class.X).
"""

import concurrent.futures as _cf
import concurrent.futures.process as _cfp
import importlib
import json
import os
import pickle
import pkgutil
import sys
import types

sys.path.insert(0, os.environ.get("KT_APP", "/app"))
from app.state import key_digest  # noqa: E402  (same key digests as the selector)

import sqlglot  # noqa: E402

for _m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    try:
        importlib.import_module(_m.name)
    except Exception:  # noqa: BLE001
        pass

OUT = os.environ.get("KT_OUT", "/out/keys")
ALL, SIZE = object(), object()  # sentinels: "*" whole table, "#size" len/bool
_ctx = {"name": "collect"}
_raw = {}  # ctx -> {(path, key)} already recorded
_seen = {}  # ctx -> {(path, key digest)}


def _rec(path, k):
    ctx = _ctx["name"]
    raw = _raw.setdefault(ctx, set())
    try:
        if (path, k) in raw:
            return
        raw.add((path, k))
    except TypeError:
        pass
    kd = "*" if k is ALL else "#size" if k is SIZE else key_digest(k)
    _seen.setdefault(ctx, set()).add((path, kd))


class TDict(dict):
    __slots__ = ("_kt_path",)

    def __getitem__(self, k):
        _rec(self._kt_path, k)
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        _rec(self._kt_path, k)
        return dict.get(self, k, default)

    def __contains__(self, k):
        _rec(self._kt_path, k)
        return dict.__contains__(self, k)

    def __len__(self):
        _rec(self._kt_path, SIZE)
        return dict.__len__(self)


def _whole(name):
    base = getattr(dict, name)

    def f(self, *a, **k):
        _rec(self._kt_path, ALL)
        return base(self, *a, **k)

    f.__name__ = name
    return f


for _n in ("__iter__", "__reversed__", "keys", "values", "items", "copy", "__or__", "__ror__", "__eq__", "__ne__",
           "__reduce_ex__", "__reduce__", "__repr__", "__str__", "__format__", "__sizeof__", "__getstate__",
           "__deepcopy__", "__copy__", "pop", "popitem", "setdefault", "update", "clear", "__setitem__",
           "__delitem__", "__ior__"):
    if hasattr(dict, _n):  # (copy.copy/deepcopy of a dict subclass use __reduce_ex__)
        setattr(TDict, _n, _whole(_n))

# dicts also bound at module level (or anywhere outside a class body) could be
# read without going through the class attribute: leave those alone
_module_objs = set()
for _name, _mod in list(sys.modules.items()):
    if (_name == "sqlglot" or _name.startswith("sqlglot.")) and _mod is not None:
        for _v in vars(_mod).values():
            if isinstance(_v, dict):
                _module_objs.add(id(_v))

_traced = {}  # id(original dict) -> table path (module:qualname.attr)


def _trace_class(cls):
    for attr, val in list(vars(cls).items()):
        if attr.startswith("__") or type(val) is not dict or not val or id(val) in _module_objs or id(val) in _bad:
            continue
        if id(val) in _traced:
            # the same dict object in two class bodies: reads via either name
            # are one table; keep the first name and alias the second
            path = _traced[id(val)]
        else:
            path = f"{cls.__module__}:{cls.__qualname__}.{attr}"
            _traced[id(val)] = path
        new = TDict(val)
        new._kt_path = path
        try:
            setattr(cls, attr, new)
        except (TypeError, AttributeError):
            pass


_classes = []
for _name, _mod in sorted(sys.modules.items()):
    if not (_name == "sqlglot" or _name.startswith("sqlglot.")) or _mod is None:
        continue
    todo = [v for v in vars(_mod).values() if isinstance(v, type) and v.__module__ == _name]
    while todo:
        c = todo.pop()
        if c in _classes:
            continue
        _classes.append(c)
        todo.extend(v for v in vars(c).values() if isinstance(v, type) and v.__module__ == _name)


def _untrusted():
    """ids of candidate dicts referenced from anywhere but class bodies (an
    instance, closure, default, tuple, cache built while importing): code
    holding such a reference reads the original, untraced dict."""
    import gc

    targets = set()
    namespaces = set()
    for c in _classes:
        ns = vars(c)
        for real in gc.get_referents(ns):
            namespaces.add(id(real))
        for attr, val in ns.items():
            if not attr.startswith("__") and type(val) is dict and val and id(val) not in _module_objs:
                targets.add(id(val))
    bad = set()
    for o in gc.get_objects():
        if id(o) in namespaces:
            continue
        for r in gc.get_referents(o):
            if id(r) in targets:
                bad.add(id(r))
    return bad


_bad = _untrusted()
_untraced = []
for _c in _classes:
    for _attr, _val in list(vars(_c).items()):
        if id(_val) in _bad:
            _untraced.append(f"{_c.__module__}:{_c.__qualname__}.{_attr}")
    _trace_class(_c)


def _aliases():
    """Every app/state.py fingerprint path (module:Attr.attr...) that reaches a
    traced table, mapped to the table's own path."""
    out = {}

    def walk(path, v, modname, depth):
        if isinstance(v, type) and v.__module__ == modname and depth < 4:
            for attr, x in sorted(vars(v).items(), key=lambda kv: kv[0]):
                walk(f"{path}.{attr}", x, modname, depth + 1)
            return
        if isinstance(v, TDict):
            out[path] = v._kt_path

    for name, mod in sorted(sys.modules.items()):
        if not (name == "sqlglot" or name.startswith("sqlglot.")) or mod is None:
            continue
        for attr, v in sorted(vars(mod).items(), key=lambda kv: kv[0]):
            walk(f"{name}:{attr}", v, name, 0)
    return out


# process pools run inline, so their lookups are charged to the calling test
class InlineExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *args, **kwargs):
        fut = _cf.Future()
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


_cf.ProcessPoolExecutor = InlineExecutor
_cfp.ProcessPoolExecutor = InlineExecutor

os.makedirs(OUT, exist_ok=True)
if os.environ.get("KT_ALIASES") == "1":
    with open(os.path.join(OUT, "aliases.json"), "w") as fh:
        json.dump({"aliases": _aliases(), "untraced": sorted(_untraced)}, fh, sort_keys=True)


def _dump(path, ctx, extra):
    keys = sorted([p, kd] for p, kd in _seen.get(ctx, ()))
    with open(path, "w") as fh:
        json.dump(dict(extra, keys=keys), fh)


def pytest_runtest_protocol(item, nextitem):
    from _pytest.runner import runtestprotocol

    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            _ctx["name"] = item.nodeid
            reports = runtestprotocol(item, nextitem=None, log=False)
            ok = not any(r.failed for r in reports)
            safe = "".join(ch if ch.isalnum() else "_" for ch in item.nodeid)[-150:]
            _dump(os.path.join(OUT, f"t.{safe}.{abs(hash(item.nodeid))}.json"), item.nodeid,
                  {"test": item.nodeid, "ok": ok, "outcomes": [r.outcome for r in reports]})
        except BaseException:  # noqa: BLE001
            code = 3
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    if status != 0:
        with open(os.path.join(OUT, f"err.{os.getpid()}.txt"), "a") as fh:
            fh.write(f"{item.nodeid} status {status}\n")
    return True


def pytest_sessionfinish(session):
    # the test files this process collected: a table read then may feed a
    # cache that every test of these files (and only these: one pytest
    # process per file) sees without a lookup
    files = sorted({item.nodeid.split("::", 1)[0] for item in session.items})
    _dump(os.path.join(OUT, f"collect.{os.getpid()}.json"), "collect", {"test": "collect", "files": files})
