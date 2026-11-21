"""Static analysis of one changed file -> the tests that could notice it.

Every rule answers from maps built at the pinned commit (line coverage, file
reads, directory listings, imports) plus the pinned source itself. Nothing
here knows about particular patches or which tests they break.
"""

import ast
import io
import os
import re
import tokenize

from .patch import FileDiff, apply

SRC = os.environ.get("SQLGLOT_SRC", "/opt/sqlglot")
ALL = "ALL"  # sentinel: select every test

# Directories whose modules belong to one dialect each (same file stem = same dialect).
DIALECT_DIRS = (
    "sqlglot/dialects/",
    "sqlglot/parsers/",
    "sqlglot/generators/",
    "sqlglot/tokenizers/",
    "sqlglot/typing/",
)
# A name on more source lines than this is too generic to search for.
GENERIC_REFS = 60
# An attribute name whose referring lines reach more tests than this is too
# common to stand for one class's attribute.
BROAD_TESTS = 300
PYTEST_CONFIG = {"pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"}


class Analyzer:
    def __init__(self, maps):
        self.maps = maps
        self._text = {}
        self._refcount = {}
        self._importers = None
        self._trees = {}
        self._ref_cache = {}
        self._preimport_sets = {}
        self.import_time = False  # set when the last analyze() touched import-time code

    # ---------- sources ----------

    def text(self, path):
        if path not in self._text:
            try:
                with open(os.path.join(SRC, path), encoding="utf-8") as fh:
                    self._text[path] = fh.read().split("\n")
            except (OSError, UnicodeDecodeError):
                self._text[path] = None
        return self._text[path]

    # ---------- entry point ----------

    def analyze(self, fd: FileDiff):
        """-> (set of test numbers or ALL, reason string). Sets self.import_time
        when the change touches code that runs while sqlglot is imported."""
        self.import_time = False
        self.names_part = set()  # import-time guesses by names (replaced by app/state.py when it can run)
        self.runtime_part = set()  # everything else
        t, w = self._analyze(fd)
        if t is not ALL and not self.is_python(fd):
            self.runtime_part |= t
        return t, w

    @staticmethod
    def is_python(fd):
        return fd.path.endswith(".py") and fd.path.startswith(("sqlglot/", "tests/", "sqlglotc/"))

    def _analyze(self, fd: FileDiff):
        path = fd.path
        name = os.path.basename(path)
        if fd.status == "rename":
            a, ra = self._analyze(FileDiff(fd.old_path, None, fd.binary))
            b, rb = self._analyze(FileDiff(None, fd.new_path, fd.binary))
            if ALL in (a, b):
                return ALL, f"rename({ra},{rb})"
            return a | b, f"rename({ra},{rb})"
        if name == "conftest.py":
            return ALL, "conftest"
        if name in PYTEST_CONFIG and "/" not in path:
            body = "\n".join(t for h in fd.hunks for tag, t in h.lines)
            if "pytest" in body:
                return ALL, "pytest-config"
        if path.endswith(".py") and path.startswith(("sqlglot/", "tests/", "sqlglotc/")):
            return self.python(fd)
        # data files, fixtures, anything else: who reads or lists it
        tests = self.maps.opened(path)
        if fd.status != "modify" or fd.binary:
            tests |= self.maps.listed(path)
        return tests, f"reads->{len(tests)}"

    # ---------- python ----------

    def python(self, fd: FileDiff):
        """Python source: tests that read it as text, plus what its code reaches."""
        t, why = self._python(fd)
        reads = self.maps.opened(fd.path)
        self.runtime_part |= reads
        if reads and t is not ALL:
            t = t | reads
            why += f" +read->{len(reads)}"
        return t, why

    def _python(self, fd: FileDiff):
        path = fd.path
        is_test = path.startswith("tests/")
        if fd.status == "add":
            if is_test and os.path.basename(path).startswith("test_"):
                return set(), "new-test-file"
            t = self.maps.listed(path)
            return t, f"new-module->{len(t)}"
        if fd.status == "delete":
            if is_test:
                t = set(self.maps.by_file.get(path, set())) | self.importer_tests(path)
                return t, f"deleted-test-module->{len(t)}"
            return ALL, "deleted-module"
        old = self.text(path)
        if old is None:
            return ALL, "unknown-file"
        new, regions = apply(old, fd.hunks)
        try:
            t0 = ast.parse("\n".join(old))
        except SyntaxError:
            return ALL, "old-unparsable"
        try:
            t1 = ast.parse("\n".join(new))
        except SyntaxError:
            if is_test:
                t = set(self.maps.by_file.get(path, set())) | self.importer_tests(path)
                return t, f"syntax-error->{len(t)}"
            return ALL, "syntax-error"
        if ast.dump(t0) == ast.dump(t1):
            return set(), "same-ast"
        if self.docstring_only(t0, t1):
            return set(), "docstring"
        ctx = FileCtx(path, old, t0)
        nctx = FileCtx(path, new, t1)
        tests = set()
        why = []
        for reg in regions:
            for line in ctx.touched(reg, nctx.code_lines):
                t, w = self.line(ctx, line)
                if t is ALL:
                    return ALL, w
                tests |= t
                why.append(f"L{line}:{w}->{len(t)}")
            if not reg.removed or not any(l in ctx.code_lines for l in reg.removed):
                # code inserted at module/class level runs at import time
                for nl in reg.added:
                    if nl in nctx.code_lines and nctx.function_at(nl)[0] is None and not path.startswith("tests/"):
                        self.import_time = True
                        t, w = self.names(nctx, nl)
                        self.names_part |= t
                        tests |= t
                        why.append(f"+{nl}:{w}->{len(t)}")
                        break
        return tests, " ".join(why) or "no-code-lines"

    def docstring_only(self, t0, t1):
        def strip(tree):
            tree = ast.parse(ast.unparse(tree))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    b = node.body
                    if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) and isinstance(b[0].value.value, str):
                        b[0].value.value = ""
            return ast.dump(tree)

        try:
            return strip(t0) == strip(t1)
        except Exception:
            return False

    def line(self, ctx, line):
        """Tests that can notice a change on old line `line`."""
        path = ctx.path
        fn, in_header = ctx.function_at(line)
        if fn is not None and in_header:
            # signature, defaults or decorators: everything that called it ran its body.
            # Defaults and decorators are evaluated at import time.
            body = fn.body
            t = self.maps.line_tests(path, range(body[0].lineno, fn.end_lineno + 1))
            self.runtime_part |= t
            if path.startswith("sqlglot/"):
                self.import_time = True
            return t, "def"
        if fn is not None:
            t = self.maps.line_tests(path, ctx.stmt_lines(line))
            self.runtime_part |= t
            if path.startswith("sqlglot/") and line in self.preimport_lines(path) and isinstance(fn, ast.FunctionDef):
                # a function that also runs while sqlglot is imported (metaclasses,
                # tables built at import)
                self.import_time = True
                return t, "cov+import-time"
            return t, "cov"
        if path.startswith("tests/"):
            # module/class level in a test file runs at collection: the map
            # charges those lines to every test of every file importing it
            t = self.maps.line_tests(path, ctx.stmt_lines(line))
            self.runtime_part |= t
            return t, "cov-collect"
        # import-time code: by names, plus tests that ran it in a fresh
        # subprocess (the map charges those runs to the test that started it)
        self.import_time = True
        t, w = self.names(ctx, line)
        self.names_part |= t
        fresh = self.maps.line_tests(path, ctx.stmt_lines(line))
        self.runtime_part |= fresh
        return t | fresh, w + (f"+import->{len(fresh)}" if fresh else "")

    def state_tests(self, paths):
        """Tests that run code naming attributes whose import-time value
        changed (paths "module:Class.ATTR" from app/state.py). An attribute
        name read almost everywhere (e.g. one a metaclass derives for every
        expression class) is replaced by its class name, like names() does.
        Dialect modules are limited to the tests using that dialect."""
        by_file = {}
        for p in paths:
            mod, _, attr = p.partition(":")
            parts = attr.split(".")
            leaf, classes = parts[-1], parts[:-1]
            f = mod.replace(".", "/") + ".py"
            if self.text(f) is None:
                f = mod.replace(".", "/") + "/__init__.py"
            refs = by_file.setdefault(f, set())
            broad = self.refcount(leaf) > GENERIC_REFS
            if not broad and self.family(f) is None:
                # outside dialect modules nothing else narrows a hot attribute name
                broad = len(self.referring_cached(leaf)) > BROAD_TESTS
            if classes and broad:
                refs |= {classes[-1], classes[-1].lower() + "_sql"}
            else:
                refs.add(leaf)
        out = set()
        for f, refs in by_file.items():
            hits = self.referring(refs)
            fam = self.family(f)
            if fam:
                hits &= self.family_tests(fam)
            out |= hits
        return out

    def referring_cached(self, name):
        if name not in self._ref_cache:
            self._ref_cache[name] = self.referring({name})
        return self._ref_cache[name]

    # ---------- module / class level code ----------

    def names(self, ctx, line):
        """A module- or class-level statement: tests that run code referring to
        what it defines, limited to the dialect it belongs to."""
        stmt = ctx.top_stmt(line)
        cls = ctx.class_at(line)
        names = set()
        if stmt is not None:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                for a in stmt.names:
                    names.add((a.asname or a.name).split(".")[0])
            elif isinstance(stmt, ast.ClassDef):
                names.add(stmt.name)
            else:
                targets = []
                if isinstance(stmt, ast.Assign):
                    targets = stmt.targets
                elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
                    targets = [stmt.target]
                for t in targets:
                    for x in ast.walk(t):
                        if isinstance(x, ast.Name):
                            names.add(x.id)
                        elif isinstance(x, ast.Attribute):
                            names.add(x.attr)
        refs = set()
        for n in names:
            if self.refcount(n) > GENERIC_REFS and cls is not None:
                refs |= {cls.name, cls.name.lower() + "_sql"}
            else:
                refs.add(n)
        if not refs and cls is not None:
            refs = {cls.name, cls.name.lower() + "_sql"}
        if not refs:
            t = self.maps.file_tests(ctx.path)
            return t, "module-any"
        hits = self.referring(refs)
        fam = self.family(ctx.path)
        if fam:
            fam_tests = self.family_tests(fam)
            hits &= fam_tests
        return hits, "names[" + ",".join(sorted(refs)) + "]"

    def preimport_lines(self, path):
        if path not in self._preimport_sets:
            self._preimport_sets[path] = set(self.maps.preimport.get(path, ()))
        return self._preimport_sets[path]

    def tree(self, path):
        if path not in self._trees:
            text = self.text(path)
            try:
                self._trees[path] = FileCtx(path, text, ast.parse("\n".join(text))) if text else None
            except SyntaxError:
                self._trees[path] = None
        return self._trees[path]

    def refcount(self, name):
        """Source lines (covered files, any line) mentioning name."""
        if name not in self._refcount:
            pat = re.compile(r"\b" + re.escape(name) + r"\b")
            c = 0
            for f in self.maps.lines:
                for l in self.text(f) or ():
                    if name in l and pat.search(l):
                        c += 1
            self._refcount[name] = c
        return self._refcount[name]

    def referring(self, refs):
        pat = re.compile(r"\b(?:" + "|".join(re.escape(r) for r in sorted(refs)) + r")\b")
        out = set()
        for f, lm in self.maps.lines.items():
            text = self.text(f)
            if not text:
                continue
            for ln, idx in lm.items():
                i = int(ln) - 1
                if i < len(text) and pat.search(text[i]):
                    out |= self.maps.set(idx)
        return out

    def family(self, path):
        """Modules of the same dialect, plus dialect modules importing them."""
        if not path.startswith(DIALECT_DIRS) or path.endswith(("/dialect.py", "/__init__.py")):
            return None
        importers = self.importers()

        def same_stem(p):
            stem = os.path.basename(p)
            return {d + stem for d in DIALECT_DIRS if self.text(d + stem) is not None}

        fam = same_stem(path) | {path}
        todo = list(fam)
        while todo:
            m = todo.pop()
            for imp in importers.get(m, ()):
                if imp.startswith(DIALECT_DIRS) and not imp.endswith(("/dialect.py", "/__init__.py")):
                    for q in same_stem(imp) | {imp}:
                        if q not in fam:
                            fam.add(q)
                            todo.append(q)
        return fam

    def family_tests(self, fam):
        """Tests that use a dialect: ran a line of its modules or created one
        of its dialect/tokenizer/parser/generator classes."""
        out = set()
        for f in fam:
            out |= self.maps.file_tests(f) | self.maps.used_by(f)
        return out

    def importers(self):
        if self._importers is None:
            rev = {}
            for f, deps in self.maps.imports.items():
                for d in deps:
                    rev.setdefault(d, set()).add(f)
            self._importers = rev
        return self._importers

    def importer_tests(self, path):
        """Tests in test files that import the module, directly or not."""
        rev = self.importers()
        seen = {path}
        todo = [path]
        while todo:
            m = todo.pop()
            for imp in rev.get(m, ()):
                if imp not in seen:
                    seen.add(imp)
                    todo.append(imp)
        out = set()
        for f in seen:
            out |= self.maps.by_file.get(f, set())
        return out


class FileCtx:
    """The pinned version of one python file, parsed."""

    def __init__(self, path, lines, tree):
        self.path = path
        self.lines = lines
        self.tree = tree
        self.nodes = list(ast.walk(tree))
        self.code_lines = _code_lines("\n".join(lines))

    def touched(self, reg, new_code):
        """Old code lines a region changes. When it removes no code but adds
        some (an insertion), the nearest code lines around the insertion point."""
        removed = [l for l in reg.removed if l in self.code_lines]
        if removed:
            return removed
        if not any(a in new_code for a in reg.added):
            return []
        out = []
        b = reg.before
        while b >= 1 and b not in self.code_lines:
            b -= 1
        a = (reg.removed[-1] if reg.removed else reg.before) + 1
        while a <= len(self.lines) and a not in self.code_lines:
            a += 1
        for l in (b, a):
            if 1 <= l <= len(self.lines):
                out.append(l)
        return out

    def function_at(self, line):
        """Innermost def/lambda containing line -> (node, line is in its header)."""
        best = None
        for node in self.nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([d.lineno for d in node.decorator_list] + [node.lineno])
                if start <= line <= node.end_lineno and (best is None or start >= best[1]):
                    best = (node, start)
            elif isinstance(node, ast.Lambda):
                if node.lineno <= line <= node.end_lineno and (best is None or node.lineno >= best[1]):
                    best = (node, node.lineno)
        if best is None:
            return None, False
        node = best[0]
        if isinstance(node, ast.Lambda):
            return node, False
        return node, line < node.body[0].lineno

    def stmt_lines(self, line):
        """Lines of the innermost statement containing line: the whole statement
        for simple ones, the header for compound ones; a lambda's body when the
        line is inside a lambda nested in that statement."""
        lam = None
        for node in self.nodes:
            if isinstance(node, ast.Lambda) and node.body.lineno <= line <= node.end_lineno:
                if lam is None or node.lineno >= lam.lineno:
                    lam = node
        best = None
        for node in self.nodes:
            if isinstance(node, ast.stmt) and node.lineno <= line <= (node.end_lineno or node.lineno):
                if best is None or (node.end_lineno - node.lineno) <= (best.end_lineno - best.lineno):
                    best = node
        if lam is not None and (best is None or best.lineno < lam.lineno):
            return list(range(lam.body.lineno, lam.end_lineno + 1))
        if best is None:
            return [line]
        end = best.end_lineno
        body = getattr(best, "body", None)
        if isinstance(body, list) and body and body[0].lineno > best.lineno:
            end = body[0].lineno - 1
        if line > end:
            # else/except/finally header: the clause's own first statement
            nxt = [n for n in self.nodes if isinstance(n, ast.stmt) and n.lineno > line]
            if nxt:
                first = min(nxt, key=lambda n: n.lineno)
                return [line, first.lineno]
            return [line]
        return list(range(best.lineno, end + 1))

    def top_stmt(self, line):
        """Innermost statement containing line that is not inside a function."""
        best = None
        for node in self.nodes:
            if isinstance(node, ast.stmt) and node.lineno <= line <= node.end_lineno:
                if best is None or node.lineno >= best.lineno and node.end_lineno <= best.end_lineno:
                    best = node
        return best

    def assigned_at(self, line):
        """Names the innermost statement containing line binds: assignment
        targets (names, attributes, subscripted names), or the def/class name."""
        stmt = None
        for node in self.nodes:
            if isinstance(node, ast.stmt) and node.lineno <= line <= node.end_lineno:
                if stmt is None or (node.lineno >= stmt.lineno and node.end_lineno <= stmt.end_lineno):
                    stmt = node
        out = set()
        if stmt is None:
            return out
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(stmt.name)
            return out
        targets = []
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
            targets = [stmt.target]
        elif isinstance(stmt, ast.Return):
            fn = None
            for node in self.nodes:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno <= line <= node.end_lineno:
                    if fn is None or node.lineno >= fn.lineno:
                        fn = node
            if fn is not None:
                out.add(fn.name)
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            f = stmt.value.func  # e.g. trie.update(...), setattr(...)
            if isinstance(f, ast.Attribute):
                for x in ast.walk(f.value):
                    if isinstance(x, ast.Name):
                        out.add(x.id)
                    elif isinstance(x, ast.Attribute):
                        out.add(x.attr)
        for t in targets:
            for x in ast.walk(t):
                if isinstance(x, ast.Name):
                    out.add(x.id)
                elif isinstance(x, ast.Attribute):
                    out.add(x.attr)
        out.discard("self")
        out.discard("cls")
        out.discard("klass")
        return out

    def class_at(self, line):
        best = None
        for node in self.nodes:
            if isinstance(node, ast.ClassDef) and node.lineno <= line <= node.end_lineno:
                if best is None or node.lineno > best.lineno:
                    best = node
        return best


def _code_lines(source):
    """Line numbers holding code: not blank, not only a comment. Lines inside
    multi-line strings count as code."""
    out = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                            tokenize.DEDENT, tokenize.ENDMARKER):
                continue
            out.update(range(tok.start[0], tok.end[0] + 1))
    except (tokenize.TokenError, SyntaxError):
        out = {i + 1 for i, l in enumerate(source.split("\n")) if l.strip() and not l.strip().startswith("#")}
    return out
