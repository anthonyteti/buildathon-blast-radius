"""Split selected tests over workers to minimise  max worker load + 2 s x workers.

Integer milliseconds throughout, like the scorer. For every worker count
1..8: longest-processing-time first, then a local search moving/swapping tests
off the busiest worker; exact search when there are only a few tests.
"""

WORKER_MS = 2000
MAX_WORKERS = 8


def cost(workers, ms):
    loads = [sum(ms[t] for t in w) for w in workers if w]
    return (max(loads) + WORKER_MS * len(loads)) if loads else 0


def pack(tests, ms):
    tests = sorted(set(tests), key=lambda t: (-ms[t], t))
    if not tests:
        return 0, []
    total = sum(ms[t] for t in tests)
    best = None
    for k in range(1, min(MAX_WORKERS, len(tests)) + 1):
        lower = max(ms[tests[0]], -(-total // k)) + WORKER_MS * k
        if best is not None and lower >= best[0]:
            continue
        ws = _exact(tests, ms, k) if len(tests) <= 10 else _improve(_lpt(tests, ms, k), ms)
        c = cost(ws, ms)
        if best is None or c < best[0]:
            best = (c, [w for w in ws if w])
    return best


def _lpt(tests, ms, k):
    ws = [[] for _ in range(k)]
    loads = [0] * k
    for t in tests:
        i = loads.index(min(loads))
        ws[i].append(t)
        loads[i] += ms[t]
    return ws


def _improve(ws, ms):
    loads = [sum(ms[t] for t in w) for w in ws]
    for _ in range(200):
        hi = loads.index(max(loads))
        moved = False
        for lo in sorted(range(len(ws)), key=lambda i: loads[i]):
            if lo == hi:
                continue
            gap = loads[hi] - loads[lo]
            # move one test that lowers the max
            for t in ws[hi]:
                if 0 < ms[t] < gap:
                    ws[hi].remove(t)
                    ws[lo].append(t)
                    loads[hi] -= ms[t]
                    loads[lo] += ms[t]
                    moved = True
                    break
            if moved:
                break
            # swap a pair that lowers the max
            for a in ws[hi]:
                for b in ws[lo]:
                    d = ms[a] - ms[b]
                    if 0 < d < gap:
                        ws[hi].remove(a)
                        ws[lo].remove(b)
                        ws[hi].append(b)
                        ws[lo].append(a)
                        loads[hi] -= d
                        loads[lo] += d
                        moved = True
                        break
                if moved:
                    break
            if moved:
                break
        if not moved:
            break
    return ws


def _exact(tests, ms, k):
    """Branch and bound over assignments (tests sorted longest first)."""
    best = [None, _lpt(tests, ms, k)]
    best[0] = max(sum(ms[t] for t in w) for w in best[1])
    loads = [0] * k
    assign = [[] for _ in range(k)]

    def rec(i, cur_max):
        if cur_max >= best[0]:
            return
        if i == len(tests):
            best[0] = cur_max
            best[1] = [list(w) for w in assign]
            return
        t = tests[i]
        seen = set()
        for w in range(k):
            if loads[w] in seen:
                continue
            seen.add(loads[w])
            loads[w] += ms[t]
            assign[w].append(t)
            rec(i + 1, max(cur_max, loads[w]))
            assign[w].pop()
            loads[w] -= ms[t]

    rec(0, 0)
    return best[1]
