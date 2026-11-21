"""Fingerprint of everything sqlglot builds while it is imported.

Module- and class-level code (dict/set tables, tries built by metaclasses,
registries) runs once, at import. Coverage cannot say which tests read that
data later, so for a change that runs at import time the selector imports
sqlglot with the patch applied and compares this fingerprint with the
unpatched one:

- the import raises            -> every test can fail
- nothing differs              -> the import-time part of the change is inert
- some attributes differ       -> tests that run code naming them

Values are digested structurally and deterministically (sets and dicts are
sorted, classes and functions are named, not hashed by identity, closures and
defaults are included), so two processes agree on unchanged state. Function
bodies are not digested: changes inside functions are covered by line
coverage.
"""

import enum
import functools
import logging
import hashlib
import importlib
import pkgutil
import sys
import types

MAX_DEPTH = 5
MAX_ITEMS = 5000


def _d(v, depth, seen):
    if v is None or isinstance(v, (bool, int, float, complex, str, bytes)):
        return repr(v)
    if isinstance(v, enum.Enum):
        return f"{type(v).__qualname__}.{v.name}"
    if isinstance(v, type):
        return f"<class {v.__module__}.{v.__qualname__}>"
    if isinstance(v, types.ModuleType):
        return f"<module {v.__name__}>"
    if isinstance(v, (logging.Logger, logging.Handler)):
        return f"<{type(v).__qualname__} {getattr(v, 'name', '')}>"
    if depth > MAX_DEPTH:
        return f"<{type(v).__qualname__}>"
    if id(v) in seen:
        return "<cycle>"
    seen = seen | {id(v)}
    if isinstance(v, (types.FunctionType, types.MethodType)):
        f = getattr(v, "__func__", v)
        parts = [f"<fn {getattr(f, '__module__', '')}.{getattr(f, '__qualname__', '?')}"]
        if f.__defaults__:
            parts.append(_d(f.__defaults__, depth + 1, seen))
        if f.__kwdefaults__:
            parts.append(_d(f.__kwdefaults__, depth + 1, seen))
        for cell in f.__closure__ or ():
            try:
                parts.append(_d(cell.cell_contents, depth + 1, seen))
            except ValueError:
                parts.append("<empty>")
        return " ".join(parts) + ">"
    if isinstance(v, (staticmethod, classmethod)):
        return f"<{type(v).__name__} {_d(v.__func__, depth + 1, seen)}>"
    if isinstance(v, property):
        return f"<property {_d(v.fget, depth + 1, seen)}>"
    if isinstance(v, functools.partial):
        return f"<partial {_d(v.func, depth + 1, seen)} {_d(v.args, depth + 1, seen)} {_d(v.keywords, depth + 1, seen)}>"
    if isinstance(v, (list, tuple)):
        items = [_d(x, depth + 1, seen) for x in list(v)[:MAX_ITEMS]]
        if v and all(isinstance(x, type) for x in v):
            items.sort()  # often built from a set of classes: order follows addresses
        return type(v).__name__ + "[" + ",".join(items) + "]"
    if isinstance(v, (set, frozenset)):
        items = sorted(_d(x, depth + 1, seen) for x in v)
        return "set{" + ",".join(items[:MAX_ITEMS]) + "}"
    if isinstance(v, dict):
        items = sorted(_d(k, depth + 1, seen) + ":" + _d(x, depth + 1, seen) for k, x in v.items())
        return "dict{" + ",".join(items[:MAX_ITEMS]) + "}"
    if isinstance(v, (types.MappingProxyType,)):
        return _d(dict(v), depth, seen)
    if type(v).__name__ in ("dict_values", "dict_keys", "dict_items"):
        return _d(list(v), depth, seen)
    d = getattr(v, "__dict__", None)
    if isinstance(d, dict):
        return f"<{type(v).__qualname__} {_d(d, depth + 1, seen)}>"
    try:
        r = repr(v)
    except Exception:  # noqa: BLE001
        r = "?"
    if " at 0x" in r:
        r = r.split(" at 0x")[0]
    return f"<{type(v).__qualname__} {r[:200]}>"


def _h(s):
    return hashlib.md5(s.encode("utf-8", "replace")).hexdigest()[:16]


def import_all(only=None):
    """Import sqlglot and every submodule (or just `only`, a set of module
    names). -> {module: error name or None}. Some modules fail even unpatched
    (e.g. sqlglot.__main__); what matters is a change in that status."""
    status = {}
    try:
        import sqlglot
    except BaseException as e:  # noqa: BLE001
        return {"sqlglot": type(e).__name__}
    status["sqlglot"] = None
    if only is None:
        names = [m.name for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot.", onerror=lambda name: None)]
    else:
        names = sorted(only)
    for name in names:
        try:
            importlib.import_module(name)
            status[name] = None
        except BaseException as e:  # noqa: BLE001
            status[name] = type(e).__name__
    return status


def fingerprint():
    """{"module:attr.path": digest} for module- and class-level state."""
    out = {}
    for name, mod in sorted(sys.modules.items()):
        if not (name == "sqlglot" or name.startswith("sqlglot.")) or mod is None:
            continue
        for attr, v in sorted(vars(mod).items(), key=lambda kv: kv[0]):
            if attr.startswith("__") and attr not in ("__all__",):
                continue
            _walk(out, f"{name}:{attr}", v, name, 0)
    return out


def _walk(out, path, v, modname, depth):
    if isinstance(v, type) and v.__module__ == modname and depth < 4:
        for attr, x in sorted(vars(v).items(), key=lambda kv: kv[0]):
            if attr in ("__dict__", "__weakref__", "__doc__", "__module__", "__qualname__", "__firstlineno__",
                        "__static_attributes__"):
                continue
            if isinstance(x, (types.FunctionType,)) and x.__qualname__.startswith(v.__qualname__ + "."):
                # methods: bodies are covered by line coverage; keep defaults/closures
                out[f"{path}.{attr}"] = _h(_d(x, 0, frozenset()))
                continue
            _walk(out, f"{path}.{attr}", x, modname, depth + 1)
        out[path] = _h(f"<class bases {[b.__qualname__ for b in v.__bases__]}>")
        return
    if isinstance(v, types.ModuleType):
        return
    out[path] = _h(_d(v, 0, frozenset()))


def crashed(base_status, status):
    """Modules that import unpatched but not patched."""
    return sorted(m for m, err in status.items() if err and not base_status.get(m))


def diff(a, b, modules=None):
    """Paths whose digest differs or that exist on one side only, optionally
    limited to the given modules."""
    keys = set(a) | set(b)
    if modules is not None:
        keys = {k for k in keys if k.split(":", 1)[0] in modules}
    return sorted(k for k in keys if a.get(k) != b.get(k))


def build_baseline(out_path, runs=3):
    """Run at image build time: the paths whose digest varies between processes
    (address-ordered data), which are ignored, and the modules a plain
    `import sqlglot` loads."""
    import json
    import subprocess

    results = []
    for _ in range(runs):
        r = subprocess.run([sys.executable, "-c", "import json; from app.state import import_all, fingerprint; "
                            "s = import_all(); print(json.dumps([s, fingerprint()]))"],
                           capture_output=True, text=True, check=True)
        results.append(json.loads(r.stdout))
    status, fp = results[0]
    unstable = set()
    for _, other in results[1:]:
        unstable |= set(diff(fp, other))
    eager = json.loads(subprocess.run(
        [sys.executable, "-c", "import json, sys, sqlglot; print(json.dumps(sorted(m for m in sys.modules if m.startswith('sqlglot'))))"],
        capture_output=True, text=True, check=True).stdout)
    with open(out_path, "w") as fh:
        json.dump({"attributes": len(fp), "unstable": sorted(unstable), "eager": eager}, fh)
    print(f"baseline: {len(fp)} attributes, {len(unstable)} unstable", file=sys.stderr)


if __name__ == "__main__":
    build_baseline(sys.argv[1])
