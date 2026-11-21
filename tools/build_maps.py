"""Compact the coverage run (tools/run_coverage.sh) into maps/coverage.json.gz.

usage: build_maps.py <cov_dir>[,<lazy_cov_dir>] <sqlglot_checkout> <maps_dir>

The optional second coverage run is made without pre-importing sqlglot, the
way the real suite runs: from it only lines run inside a test are added, which
shows the lazy-loading paths (a dialect imported on first use) that the first
run hides.

Everything in the map comes from running sqlglot's own suite at the pinned
commit. Layout:

  tests      list of pytest node ids; a test's index in this list is its "slot"
  sets       distinct sets of tests, each a hex bitmask over slots
  lines      {file: {line: set index}}  tests that executed the line.
             setup-phase lines count for the whole class (setUpClass runs in
             the first test's setup); lines run while a test file is imported
             or collected count for every test in that file.
  preimport  {file: [lines]} executed only while importing sqlglot (module and
             class bodies); the selector handles those by name references.
  imports    {file: files it imports anywhere}; imports_top: only imports that
             run while the module is imported (no function bodies, no
             `if TYPE_CHECKING:` blocks)
  opens      {path: set index} tests that open()ed the file
  lists      {dir: set index}  tests that listed the directory
  subprocess [set index]  tests that start subprocesses
  (jobs.json.gz) {test: {count, lines: {file: {line: hex mask of jobs}}, main: {file:
             [lines run outside jobs]}}} for tests that submit work to a
             process pool (job i = i-th submission; a subTest entered after
             reading job i's result counts as job i)
  expr_classes {"module:Class": set index}  tests that created an expression
             of that class or a subclass
  uses       {module file: set index}  tests that created a Dialect, Tokenizer,
             Parser or Generator instance of a class defined in that module
"""

import ast
import glob
import gzip
import json
import os
import sqlite3
import sys
from collections import defaultdict

from coverage.numbits import numbits_to_nums

cov_arg, src, maps_dir = sys.argv[1:4]
cov_dir, lazy_dir = (cov_arg.split(",") + [None])[:2]

db = sqlite3.connect(os.path.join(cov_dir, ".coverage"))
files = dict(db.execute("select id, path from file"))
contexts = dict(db.execute("select id, context from context"))


def rel(path):
    return os.path.relpath(path, src)


# Every test that ran, by the contexts it produced.
tests = sorted(
    {c.rsplit("|", 1)[0] for c in contexts.values() if c.endswith(("|setup", "|call", "|teardown"))}
)
slot = {t: i for i, t in enumerate(tests)}
by_class = defaultdict(set)
by_file = defaultdict(set)
for t, i in slot.items():
    by_class[t.rsplit("::", 1)[0]].add(i)
    by_file[t.split("::", 1)[0]].add(i)


def slots_of(ctx):
    """Tests a coverage context stands for, or None for import-time work."""
    if ctx.startswith("collect|"):
        return by_file.get(ctx[len("collect|") :], set())
    if "|" not in ctx:
        return None
    nodeid, phase = ctx.rsplit("|", 1)
    if nodeid not in slot:
        return set()
    if phase == "setup":
        return by_class[nodeid.rsplit("::", 1)[0]]
    return {slot[nodeid]}


ctx_slots = {cid: slots_of(c) for cid, c in contexts.items()}

line_tests = defaultdict(lambda: defaultdict(set))
# per pool-based test: which jobs ran each line, and the lines its main body ran
job_lines = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
job_count = defaultdict(int)
call_lines = defaultdict(lambda: defaultdict(set))
preimport = defaultdict(set)
for fid, cid, bits in db.execute("select file_id, context_id, numbits from line_bits"):
    f = rel(files[fid])
    ctx = contexts[cid]
    if "|job" in ctx:
        nodeid, job = ctx.rsplit("|job", 1)
        if job.isdigit():
            j = int(job)
            job_count[nodeid] = max(job_count[nodeid], j + 1)
            for ln in numbits_to_nums(bits):
                job_lines[nodeid][f][ln] |= 1 << j
    elif ctx.endswith("|call"):
        call_lines[ctx[: -len("|call")]][f].update(numbits_to_nums(bits))
    s = ctx_slots[cid]
    for ln in numbits_to_nums(bits):
        if s is None:
            preimport[f].add(ln)
        else:
            line_tests[f][ln] |= s

def function_lines(path):
    """Lines inside function or lambda bodies (not module/class-level code)."""
    try:
        tree = ast.parse(open(os.path.join(src, path)).read())
    except (OSError, SyntaxError, ValueError):
        return set()
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.update(range(node.body[0].lineno, node.end_lineno + 1))
        elif isinstance(node, ast.Lambda):
            out.update(range(node.body.lineno, node.end_lineno + 1))
    return out


_fn_lines = {}
lazy_added = 0
if lazy_dir:
    db2 = sqlite3.connect(os.path.join(lazy_dir, ".coverage"))
    files2 = dict(db2.execute("select id, path from file"))
    contexts2 = dict(db2.execute("select id, context from context"))
    for fid, cid, bits in db2.execute("select file_id, context_id, numbits from line_bits"):
        ctx = contexts2[cid]
        if not ctx.endswith(("|setup", "|call", "|teardown")):
            continue
        s = slots_of(ctx)
        if not s:
            continue
        f = rel(files2[fid])
        if f not in _fn_lines:
            _fn_lines[f] = function_lines(f)
        for ln in numbits_to_nums(bits):
            if ln not in _fn_lines[f] or ln in preimport.get(f, ()):
                # import-time code (module bodies, or functions the first run saw
                # running during import): charged to whichever test loaded the
                # module first. Its effects are found by the import check.
                continue
            if not s <= line_tests[f][ln]:
                lazy_added += 1
                line_tests[f][ln] |= s

opens = defaultdict(set)
lists = defaultdict(set)
spawners = set()
uses = defaultdict(set)
exprs = defaultdict(set)
for p in glob.glob(os.path.join(cov_dir, "opens.*.jsonl")) + (
    glob.glob(os.path.join(lazy_dir, "opens.*.jsonl")) if lazy_dir else []
):
    for row in open(p):
        ctx, kind, path = json.loads(row)
        if "__pycache__" in path:
            continue
        if ctx.startswith("collect|") and (kind == "list" or path.endswith(".py")):
            continue  # pytest itself collecting / rewriting the test module
        s = slots_of(ctx)
        if not s:
            continue
        if kind == "subprocess":
            spawners |= s
            continue
        if kind == "expr":
            exprs[path] |= s
            continue
        if kind == "use":
            f = path.replace(".", "/") + ".py"
            if not os.path.exists(os.path.join(src, f)):
                f = path.replace(".", "/") + "/__init__.py"
            uses[f] |= s
            continue
        (opens if kind == "open" else lists)[path] |= s

set_index = {}
sets = []


def intern(s):
    mask = 0
    for i in s:
        mask |= 1 << i
    if mask not in set_index:
        set_index[mask] = len(sets)
        sets.append(format(mask, "x"))
    return set_index[mask]


out = {
    "commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
    "tests": tests,
    "lines": {
        f: {str(ln): intern(s) for ln, s in sorted(lm.items()) if s}
        for f, lm in sorted(line_tests.items())
    },
    "opens": {p: intern(s) for p, s in sorted(opens.items())},
    "lists": {p: intern(s) for p, s in sorted(lists.items())},
    "preimport": {f: sorted(ls) for f, ls in sorted(preimport.items())},
    "subprocess": [intern(spawners)] if spawners else [],
    "uses": {f: intern(s) for f, s in sorted(uses.items())},
    "expr_classes": {c: intern(s) for c, s in sorted(exprs.items())},
}
# per-job coverage of pool-based tests: a separate file, loaded only when needed
jobs = {
    t: {
        "count": job_count[t],
        "lines": {f: {str(ln): format(m, "x") for ln, m in sorted(lm.items())} for f, lm in sorted(fl.items())},
        "main": {f: sorted(ls) for f, ls in sorted(call_lines[t].items())},
    }
    for t, fl in sorted(job_lines.items())
}


out["sets"] = sets


# Static import graph over sqlglot/ and tests/: file -> files it imports
# (anywhere in the module, including imports inside functions).
def module_file(mod):
    base = os.path.join(src, *mod.split("."))
    for cand in (base + ".py", os.path.join(base, "__init__.py")):
        if os.path.exists(cand):
            return rel(cand)
    return None


imports = {}
imports_top = {}
for top_dir in ("sqlglot", "tests"):
    for root, _, names in os.walk(os.path.join(src, top_dir)):
        for name in names:
            if not name.endswith(".py"):
                continue
            path = rel(os.path.join(root, name))
            pkg = os.path.dirname(path).replace("/", ".")
            pkg_parts = pkg.split(".")
            try:
                tree = ast.parse(open(os.path.join(src, path)).read())
            except SyntaxError:
                continue
            deps = set()
            top = set()
            # imports executed while the module is imported: module and class
            # bodies, not function bodies, not `if TYPE_CHECKING:` blocks
            runs_at_import = set()

            def mark(body):
                for st in body:
                    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    if isinstance(st, ast.If) and "TYPE_CHECKING" in ast.unparse(st.test):
                        mark(st.orelse)
                        continue
                    runs_at_import.add(id(st))
                    for field in ("body", "orelse", "handlers", "finalbody"):
                        sub = getattr(st, field, None)
                        if isinstance(sub, list):
                            mark([x for x in sub if isinstance(x, ast.stmt)] if field != "handlers" else
                                 [y for h in sub for y in h.body])

            mark(tree.body)
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    base = node.module or ""
                    if node.level:
                        parts = pkg_parts[: len(pkg_parts) - node.level + 1]
                        base = ".".join(parts + ([base] if base else []))
                    mods = [base] + [base + "." + a.name for a in node.names]
                for mod in mods:
                    parts = mod.split(".")
                    for k in range(1, len(parts) + 1):  # importing a.b.c runs a, a.b, a.b.c
                        f = module_file(".".join(parts[:k]))
                        if f and f != path:
                            deps.add(f)
                            if id(node) in runs_at_import:
                                top.add(f)
            imports[path] = sorted(deps)
            imports_top[path] = sorted(top)
out["imports"] = imports
out["imports_top"] = imports_top
os.makedirs(maps_dir, exist_ok=True)
data = json.dumps(out, separators=(",", ":")).encode()
with open(os.path.join(maps_dir, "coverage.json.gz"), "wb") as fh:
    fh.write(gzip.compress(data, compresslevel=9, mtime=0))
with open(os.path.join(maps_dir, "jobs.json.gz"), "wb") as fh:
    fh.write(gzip.compress(json.dumps(jobs, separators=(",", ":")).encode(), compresslevel=9, mtime=0))
print(
    f"{len(tests)} tests, {len(sets)} sets, {sum(len(v) for v in out['lines'].values())} lines in"
    f" {len(out['lines'])} files, {len(opens)} opened paths, {len(lists)} listed dirs,"
    f" {len(data) // 1024} KB raw; {lazy_added} line attributions from the lazy run; {len(spawners)} subprocess tests",
    file=sys.stderr,
)
