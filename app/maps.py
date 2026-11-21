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
        self._sets = m["sets"]
        self._cache = {}
        self.tests = tests_json["tests"]
        self.ms = {t["number"]: round(t["seconds"] * 1000) for t in self.tests}
        self.all = set(self.ms)
        number = {t["id"]: t["number"] for t in self.tests}
        # slot (index in the map's test list) -> number in this run's tests.json
        self.slot_number = [number.get(t) for t in m["tests"]]
        self.mapped = {n for n in self.slot_number if n is not None}
        self.by_file = {}
        for t in self.tests:
            self.by_file.setdefault(t["file"], set()).add(t["number"])

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

    def opened(self, path: str) -> set:
        idx = self.opens.get(path)
        return set(self.set(idx)) if idx is not None else set()

    def listed(self, path: str) -> set:
        """Tests that listed the directory holding path."""
        idx = self.lists.get(os.path.dirname(path) or ".")
        return set(self.set(idx)) if idx is not None else set()
