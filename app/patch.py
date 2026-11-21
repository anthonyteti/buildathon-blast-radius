"""Reading unified diffs (git style) and applying them to the pinned sources."""

import re
from dataclasses import dataclass, field

HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list = field(default_factory=list)  # (tag, text) with tag in " -+"


@dataclass
class FileDiff:
    old_path: str | None = None  # None for a new file
    new_path: str | None = None  # None for a deleted file
    binary: bool = False
    hunks: list = field(default_factory=list)

    @property
    def path(self):
        return self.new_path or self.old_path

    @property
    def status(self):
        if self.old_path is None:
            return "add"
        if self.new_path is None:
            return "delete"
        if self.old_path != self.new_path:
            return "rename"
        return "modify"


def _strip_prefix(p):
    p = p.strip()
    if "\t" in p:
        p = p.split("\t", 1)[0]
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p == "/dev/null":
        return None
    if p[:2] in ("a/", "b/"):
        p = p[2:]
    return p


def parse(text: str) -> list[FileDiff]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    lines = [l[:-1] if l.endswith("\r") else l for l in lines]
    out: list[FileDiff] = []
    cur: FileDiff | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            cur = FileDiff()
            out.append(cur)
            m = re.match(r"diff --git (\"?a/.*?\"?) (\"?b/.*\"?)$", line)
            if m:
                cur.old_path = _strip_prefix(m.group(1))
                cur.new_path = _strip_prefix(m.group(2))
        elif line.startswith("new file mode") and cur is not None:
            cur.old_path = None
        elif line.startswith("deleted file mode") and cur is not None:
            cur.new_path = None
        elif line.startswith("rename from ") and cur is not None:
            cur.old_path = line[len("rename from ") :]
        elif line.startswith("rename to ") and cur is not None:
            cur.new_path = line[len("rename to ") :]
        elif line.startswith(("Binary files ", "GIT binary patch")) and cur is not None:
            cur.binary = True
        elif line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old = _strip_prefix(line[4:])
            new = _strip_prefix(lines[i + 1][4:])
            if cur is None or cur.hunks:
                cur = FileDiff()
                out.append(cur)
            cur.old_path, cur.new_path = old, new
            i += 1
        elif line.startswith("@@") and cur is not None:
            m = HUNK.match(line)
            if not m:
                raise ValueError("bad hunk header")
            h = Hunk(
                int(m.group(1)),
                int(m.group(2)) if m.group(2) is not None else 1,
                int(m.group(3)),
                int(m.group(4)) if m.group(4) is not None else 1,
            )
            cur.hunks.append(h)
            old_left, new_left = h.old_len, h.new_len
            while (old_left > 0 or new_left > 0) and i + 1 < len(lines):
                nxt = lines[i + 1]
                if nxt.startswith("\\"):
                    i += 1
                    continue
                tag = nxt[:1] or " "
                if tag not in " -+":
                    break
                i += 1
                h.lines.append((tag, nxt[1:]))
                if tag != "+":
                    old_left -= 1
                if tag != "-":
                    new_left -= 1
            while i + 1 < len(lines) and lines[i + 1].startswith("\\"):
                i += 1
        i += 1
    return [fd for fd in out if fd.path]


@dataclass
class Region:
    """One run of consecutive changed lines in a hunk."""

    removed: list  # old line numbers (1-based)
    added: list  # new line numbers (1-based)
    before: int  # old line number just before the region (0 if none)


def apply(old: list[str], hunks: list[Hunk]):
    """Apply hunks to old lines (no newlines). Returns (new lines, regions)."""
    new: list[str] = []
    regions: list[Region] = []
    pos = 0  # index into old
    for h in hunks:
        start = max(h.old_start - 1, 0) if h.old_len else h.old_start
        expect = [t for tag, t in h.lines if tag != "+"]
        start = _locate(old, expect, start)
        if start < pos:
            raise ValueError("overlapping hunks")
        new.extend(old[pos:start])
        pos = start
        reg = None
        for tag, text in h.lines:
            if tag == " ":
                reg = None
                new.append(old[pos])
                pos += 1
                continue
            if reg is None:
                reg = Region([], [], pos)
                regions.append(reg)
            if tag == "-":
                reg.removed.append(pos + 1)
                pos += 1
            else:
                new.append(text)
                reg.added.append(len(new))
    new.extend(old[pos:])
    return new, regions


def _locate(old, expect, start):
    def fits(s):
        return s >= 0 and s + len(expect) <= len(old) and old[s : s + len(expect)] == expect

    if fits(start):
        return start
    norm = [e.rstrip() for e in expect]
    for d in range(1, 400):
        for s in (start - d, start + d):
            if fits(s):
                return s
            if 0 <= s and s + len(norm) <= len(old) and [o.rstrip() for o in old[s : s + len(norm)]] == norm:
                return s
    raise ValueError("hunk does not apply")
