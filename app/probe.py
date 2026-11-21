"""Infection probes (weak mutation testing).

When a patch only replaces side-effect-free expressions inside functions,
the patched module can be compiled so that each replaced expression computes
both its old and its new value, returns the new one, and records whether they
ever differed. If the cheap tests run through the changed code many times and
the two values never differ, the program state those tests saw is identical
to the unpatched program's: the change did not "infect" anything they ran.

This is only evidence used by the optional expensive-test rule in
app/policy.py; it never removes a test on its own.
"""

import ast

PURE_CALLS = {"len", "isinstance", "issubclass", "type", "bool"}
PURE_METHODS = {"get"}
_OPS = (ast.cmpop, ast.boolop, ast.operator, ast.unaryop, ast.expr_context)


def _differences(a, b, out):
    """Collect minimal (old, new) differing node pairs. Returns False if the
    trees differ in shape (statements added/removed) where no probe fits."""
    if type(a) is not type(b):
        out.append((a, b))
        return True
    if isinstance(a, ast.AST):
        for field in a._fields:
            x, y = getattr(a, field, None), getattr(b, field, None)
            if isinstance(x, list) or isinstance(y, list):
                if not isinstance(x, list) or not isinstance(y, list) or len(x) != len(y):
                    out.append((a, b))
                    continue
                for i, j in zip(x, y):
                    if isinstance(i, _OPS) or isinstance(j, _OPS):
                        if type(i) is not type(j):
                            out.append((a, b))
                            break
                    elif not _differences(i, j, out):
                        return False
            elif isinstance(x, _OPS) or isinstance(y, _OPS):
                if type(x) is not type(y):
                    out.append((a, b))
            elif isinstance(x, ast.AST) or isinstance(y, ast.AST):
                if x is None or y is None:
                    out.append((a, b))
                elif not _differences(x, y, out):
                    return False
            elif x != y or type(x) is not type(y):
                out.append((a, b))
        return True
    if a != b:
        out.append((a, b))
    return True


def _pure(node):
    for n in ast.walk(node):
        if isinstance(n, (ast.Name, ast.Constant, ast.Compare, ast.BoolOp, ast.BinOp, ast.UnaryOp,
                          ast.Attribute, ast.Subscript, ast.Tuple, ast.List, ast.Set, ast.Slice,
                          ast.keyword, ast.IfExp) + _OPS):
            if isinstance(n, ast.Name) and n.id in ("super", "__class__"):
                return False
            continue
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name) and f.id in PURE_CALLS:
                continue
            if isinstance(f, ast.Attribute) and f.attr in PURE_METHODS:
                continue
        return False
    return True


def instrument(old_text, new_text, filename):
    """-> (code object for the new module with probes, number of probes), or
    (None, 0) when the change is not a set of side-effect-free expression
    replacements inside functions. Docstring edits count as unprobeable."""
    try:
        t0 = ast.parse(old_text)
        t1 = ast.parse(new_text)
    except SyntaxError:
        return None, 0
    pairs = []
    if not _differences(t0, t1, pairs):
        return None, 0
    if not pairs:  # same code (comments, formatting): nothing to probe
        return compile(t1, filename, "exec"), 0
    parents = {}
    for node in ast.walk(t1):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    targets = {}
    for old, new in pairs:
        if not isinstance(old, ast.expr) or not isinstance(new, ast.expr):
            return None, 0
        if not (_pure(old) and _pure(new)):
            return None, 0
        p = parents.get(new)
        inside = False
        while p is not None:
            if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                inside = True
                break
            if isinstance(p, ast.ClassDef):
                break
            p = parents.get(p)
        if not inside:
            return None, 0
        targets[id(new)] = (len(targets), old)

    class Rewrite(ast.NodeTransformer):
        def visit(self, node):
            hit = targets.get(id(node))
            if hit is not None:
                k, old = hit
                call = ast.Call(
                    func=ast.Name("__br_probe__", ast.Load()),
                    args=[ast.Constant(k), _thunk(old), _thunk(node)],
                    keywords=[],
                )
                return ast.copy_location(call, node)
            return self.generic_visit(node)

    t1 = Rewrite().visit(t1)
    ast.fix_missing_locations(t1)
    try:
        return compile(t1, filename, "exec"), len(targets)
    except (SyntaxError, ValueError):
        return None, 0


def _thunk(expr):
    lam = ast.Lambda(
        args=ast.arguments(posonlyargs=[], args=[], vararg=None, kwonlyargs=[], kw_defaults=[], kwarg=None, defaults=[]),
        body=expr,
    )
    return ast.copy_location(lam, expr)


class Recorder:
    """Installed as builtins.__br_probe__ in the verification child."""

    def __init__(self):
        self.runs = 0
        self.infected = 0

    def __call__(self, k, old, new):
        try:
            o = old()
            failed = False
        except BaseException:  # noqa: BLE001
            failed = True
        n = new()
        self.runs += 1
        if failed or not _same(o, n):
            self.infected += 1
        return n


def _same(a, b):
    if a is b:
        return True
    try:
        return type(a) is type(b) and bool(a == b)
    except BaseException:  # noqa: BLE001
        return False
