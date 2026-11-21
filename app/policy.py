"""Decide, per commit, whether running some candidates now is cheaper than
selecting them, and which ones.

Costs are in milliseconds, comparable to the scorer: the selection costs
pack(), verification costs the CPU it burns. Numbers below are generic
calibration constants (documented in the README), not per-patch knowledge.
"""

import os

from .pack import pack

# CPU ms to fork a child and import sqlglot plus the test modules.
FIXED_MS = int(os.environ.get("BR_FIXED_MS", "250"))
# CPU ms the first time pandas/numpy/duckdb are needed in a run.
HEAVY_IMPORT_MS = int(os.environ.get("BR_HEAVY_IMPORT_MS", "2200"))
# Multiplier on each test's measured in-process CPU (maps/cpu.json).
ALPHA = float(os.environ.get("BR_ALPHA", "1.4"))
# Prior that a commit breaks none of its candidates, by what changed.
P_NONE_CODE = float(os.environ.get("BR_P_NONE_CODE", "0.5"))
P_NONE_DATA = float(os.environ.get("BR_P_NONE_DATA", "0.1"))
# Verify only when the expected saving is at least this.
MIN_GAIN_MS = int(os.environ.get("BR_MIN_GAIN_MS", "250"))
# Only tests at most this long are worth running here.
MAX_VERIFY_MS = int(os.environ.get("BR_MAX_VERIFY_MS", "3000"))


def plan(cands, ms, cpu, verifiable, p_none, setup_ms):
    """-> (expected gain in ms, tests to verify in run order); ([], 0) = stay static.
    setup_ms(tests): CPU ms to start a child that runs those tests;
    cpu: measured in-process CPU ms per test."""
    static = pack(cands, ms)[0]
    pool = sorted((t for t in cands if t in verifiable and cpu[t] <= MAX_VERIFY_MS), key=lambda t: (cpu[t], t))
    if not pool or static == 0:
        return 0.0, []
    best = (static, [])
    # candidate subsets: cheapest-first prefixes cut at a few thresholds
    cuts = sorted({len(pool)} | {sum(1 for t in pool if cpu[t] <= th) for th in (50, 200, 500, 1000, 2000)})
    for k in cuts:
        if k == 0:
            continue
        v = pool[:k]
        vs = set(v)
        rest = [t for t in cands if t not in vs]
        run_ms = setup_ms(v) + ALPHA * sum(cpu[t] for t in v)
        if_pass = run_ms + pack(rest, ms)[0]
        # on a failure we stop early; assume half the run was spent
        if_fail = 0.5 * run_ms + static
        expected = p_none * if_pass + (1 - p_none) * if_fail
        if expected < best[0]:
            best = (expected, v)
    gain = static - best[0]
    if gain < MIN_GAIN_MS:
        return 0.0, []
    return gain, best[1]


# ---- skip expensive tests on evidence (on; BR_SKIP_HEAVY=0 turns it off) ----
SKIP_HEAVY = os.environ.get("BR_SKIP_HEAVY", "1") == "1"
# Tests at least this long are "expensive".
HEAVY_MS = int(os.environ.get("BR_HEAVY_MS", "3000"))
# CPU ms of cheaper candidates run (with probes) as evidence for the rule.
SKIP_BUDGET_MS = int(os.environ.get("BR_SKIP_BUDGET_MS", "3000"))
# Probe executions required before the rule may fire.
SKIP_MIN_RUNS = int(os.environ.get("BR_SKIP_MIN_RUNS", "20"))


def plan_skip(cands, ms, cpu, verifiable, p_none, setup_ms):
    """Expensive-test rule: run the cheaper candidates with infection probes;
    if all pass and the changed expressions never computed a different value,
    the expensive candidates are left out. Candidates too costly to run here
    stay selected.
    -> (expected gain ms, tests to verify) or (0, [])."""
    heavy = {t for t in cands if ms[t] >= HEAVY_MS}
    if not heavy:
        return 0.0, []
    # cheaper candidates, cheapest first, within a CPU budget; the rest stay selected
    pool = sorted((t for t in cands if t in verifiable and t not in heavy and cpu[t] <= MAX_VERIFY_MS),
                  key=lambda t: (cpu[t], t))
    # setUpClass costs count against the budget too (TestExecutor's is 1.5 s)
    v = []
    for t in pool:
        if v and setup_ms(v + [t]) + sum(cpu[x] for x in v) + cpu[t] > SKIP_BUDGET_MS + setup_ms([]):
            continue
        v.append(t)
    if not v:
        return 0.0, []
    vs = set(v)
    rest = [t for t in cands if t not in vs and t not in heavy]
    static = pack(cands, ms)[0]
    run_ms = setup_ms(v) + ALPHA * sum(cpu[t] for t in v)
    if_pass = run_ms + pack(rest, ms)[0]
    if_fail = 0.5 * run_ms + static
    gain = static - (p_none * if_pass + (1 - p_none) * if_fail)
    if gain < MIN_GAIN_MS:
        return 0.0, []
    return gain, v
