"""Loads maps/coverage.json.gz (built by tools/build_maps.py) and answers
"which tests touched this?" as sets of test numbers from /data/tests.json."""

import gzip
import json
import os

MAPS = os.environ.get("BR_MAPS", os.path.join(os.path.dirname(os.path.dirname(__file__)), "maps"))


class Maps:
    def __init__(self, tests_json: dict):
        with gzip.open(os.path.join(MAPS, "coverage.json.gz"), "rb") as fh:
            m = json.loads(fh.read())
        self.lines = m["lines"]
        self.opens = m["opens"]
        self.lists = m["lists"]
        self.preimport = m["preimport"]
        self.imports = m.get("imports", {})
        self.imports_top = m.get("imports_top", self.imports)
        self.uses = m.get("uses", {})
        self.expr_classes = m.get("expr_classes", {})
        # measured in-process CPU (tools/measure_cpu.py), scaled to tests.json's machine
        self.class_setup = {}
        self.cpu_ms = {}
        cpu_path = os.path.join(MAPS, "cpu.json")
        if os.path.exists(cpu_path):
            with open(cpu_path) as fh:
                c = json.load(fh)
            self.class_setup = c.get("class_setup_ms", {})
            self._cpu_by_id = c.get("test_ms", {})
        else:
            self._cpu_by_id = {}
        self._sets = m["sets"]
        self._cache = {}
        self.tests = tests_json["tests"]
        self.ms = {t["number"]: round(t["seconds"] * 1000) for t in self.tests}
        self.all = set(self.ms)
        number = {t["id"]: t["number"] for t in self.tests}
        # slot (index in the map's test list) -> number in this run's tests.json
        self.slot_number = [number.get(t) for t in m["tests"]]
        self.mapped = {n for n in self.slot_number if n is not None}
        # tests whose outcome an in-process run cannot reproduce: they start
        # subprocesses (which would see the unpatched tree) or read sources as text
        self.unverifiable = set()
        self.subprocess_tests = set()
        for idx in m.get("subprocess", []):
            self.subprocess_tests |= self.set(idx)
        self.unverifiable |= self.subprocess_tests
        for p, idx in self.opens.items():
            if p.endswith(".py"):
                self.unverifiable |= self.set(idx)
        for t in self.tests:
            # never assume a test is cheaper to run here than recorded
            self.cpu_ms[t["number"]] = max(self.ms[t["number"]], self._cpu_by_id.get(t["id"], 0))
        self.by_file = {}
        for t in self.tests:
            self.by_file.setdefault(t["file"], set()).add(t["number"])

    def class_setup_ms(self, cls):
        """Measured CPU ms of a test class's setUpClass (0 if negligible)."""
        return self.class_setup.get(cls, 0)

    def set(self, idx) -> frozenset:
        s = self._cache.get(idx)
        if s is None:
            bits = bin(int(self._sets[idx], 16))[:1:-1]  # bit i = slot i
            nums = [self.slot_number[i] for i, b in enumerate(bits) if b == "1"]
            nums = [n for n in nums if n is not None]
            s = self._cache[idx] = frozenset(nums)
        return s

    def line_tests(self, path: str, lines) -> set:
        lm = self.lines.get(path)
        out = set()
        if not lm:
            return out
        for ln in lines:
            idx = lm.get(str(ln))
            if idx is not None:
                out |= self.set(idx)
        return out

    def file_tests(self, path: str) -> set:
        """Every test that executed any line of the file."""
        out = set()
        for idx in set((self.lines.get(path) or {}).values()):
            out |= self.set(idx)
        return out

    def used_by(self, path: str) -> set:
        """Tests that created a Dialect/Tokenizer/Parser/Generator from a class
        defined in this module."""
        idx = self.uses.get(path)
        return set(self.set(idx)) if idx is not None else set()

    def instantiated(self, cls_key: str) -> set:
        """Tests that created an expression of class "module:Qualname" or a subclass."""
        idx = self.expr_classes.get(cls_key)
        return set(self.set(idx)) if idx is not None else set()

    def opened(self, path: str) -> set:
        idx = self.opens.get(path)
        return set(self.set(idx)) if idx is not None else set()

    def listed(self, path: str) -> set:
        """Tests that listed the directory holding path."""
        idx = self.lists.get(os.path.dirname(path) or ".")
        return set(self.set(idx)) if idx is not None else set()
