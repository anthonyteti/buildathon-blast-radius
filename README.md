# Blast Radius selector

Picks which sqlglot tests to run for each patch (sqlglot pinned at
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`) and packs them onto workers.

## How it decides

All knowledge comes from maps built by running sqlglot's own test suite at the
pinned commit (`maps/coverage.json.gz`, rebuilt with `tools/build_maps.sh`):

- **line coverage per test** (coverage.py, C tracer), from two runs:
  - one that imports every sqlglot module before any test, so module and
    class bodies are kept apart from what tests execute;
  - one that does not, like the real suite, so lazy loading (a dialect
    imported on first use) is charged to the tests that trigger it. Only
    lines inside functions are taken from this run.

  Process pools run inline so their work is charged to the calling test;
  `setUp`/`setUpClass` lines count for the whole class; lines run while a test
  file is imported count for every test in that file; subprocesses are
  measured under the test that started them.
- **file reads and directory listings** per test (fixtures, data files).
- **dialect use** per test: which modules' Dialect/Tokenizer/Parser/Generator
  classes each test instantiated.
- **expression classes** per test: which `sqlglot.expressions` classes (and
  their bases) each test created. Functions are dispatched by name (parser
  `FUNCTIONS` tables, `<key>_sql` generator methods), so a test can depend on a
  class without running a line that names it.
- **process-pool jobs** of tests that submit work to a pool
  (`maps/jobs.json.gz`): which lines each job ran (job i = i-th submission;
  a subTest entered after reading job i's result counts as job i), and which
  lines the test ran outside its jobs.
- **static import graph** of `sqlglot/` and `tests/`.
- **CPU cost of running each test in-process** (`maps/cpu.json`,
  `tools/measure_cpu.py`), scaled to the recorded seconds.

For each changed file in a patch (`app/analyze.py`):

| change | tests selected |
|---|---|
| Python, same AST after the patch (comments, formatting) or docstring only | none |
| line inside a function body | tests that executed that statement (header only for `if`/`for`/...; the body of a lambda when inside one) |
| `def` line, signature, decorator | tests that executed the function body |
| module/class-level line in `sqlglot/`, or a line of a function that runs during import | the import check below |
| module/class-level line in `tests/` | tests in every test file whose import ran it |
| fixture / data file | tests that opened it (plus tests that listed its directory when added/removed) |
| source file read as text by a test | those tests too |
| syntax error, deleted module, `conftest.py`, pytest config, anything that fails to analyse | every test |

### Changes that run at import time (`app/state.py`)

Module and class bodies (tables, flags, registries) and functions called while
importing (metaclasses, trie builders) run once, at import, so line coverage
cannot tell which tests read what they build. For such a change the selector
imports all of sqlglot with the patch applied, in a forked child, and compares
a structural fingerprint of every module- and class-level attribute with the
unpatched one (sets and dicts sorted, classes and functions named, closures
and defaults included). The unpatched fingerprint is made when the image is
built (`maps/baseline_state.json`, from three runs: the few attributes that
vary between processes are ignored). Only the patched modules and the modules
whose import runs them are imported and compared:

- a module that imported before now fails to import: every test;
- no attribute changed: nothing beyond the coverage of the changed lines;
- a dict table defined in a class body (`KEYWORDS`, `NUMERIC_ESCAPES`,
  `FUNCTIONS`, ...): the tests that looked up a changed key, read the whole
  table (iterated, copied, merged, compared), or read its size when keys were
  added or removed (`maps/keys.json.gz`, `tools/keytrace.py`). The trace runs
  every test in its own forked child of a process that has imported sqlglot
  but run nothing, so no cache one test builds (e.g. the generator's dispatch
  table from `TRANSFORMS`) hides a lookup from another. Keys equal under dict
  lookup share a digest (a str-valued enum and its string, `True` and `1`).
  Tables also bound at module level or referenced from anywhere but class
  bodies when the trace starts are not narrowed this way; a table read while
  pytest imports a test file counts as read whole by every test of that file;
- a dict table (`TRANSFORMS`, `FUNCTIONS`, ...) whose changed keys all name
  expression classes (the key itself, or the class a parser entry builds):
  tests that created those expressions, within the dialect's tests (and
  within the key trace above, when there is one). The per-key digests are
  compared only for tables whose digest changed;
- otherwise: tests that created an expression of a class whose attribute
  changed, plus tests running code that names a changed attribute. In a
  dialect's modules those are limited to tests that use the dialect (all of
  them for `TRANSFORMS`, which the generator reads once into a cached dispatch
  table); elsewhere an attribute name read almost everywhere is replaced by
  its class's name.
  Tests that import sqlglot in a subprocess are added when an eagerly
  imported module changed.

When that check cannot run, the static fallback selects by the names the
statement defines.

### Run-time verification (`app/verify.py`, `app/policy.py`)

Running sqlglot's tests while deciding is allowed and charged as CPU time. For
each commit the selector compares two costs in milliseconds:

- **static:** the packed cost of the candidate tests;
- **verify:** the CPU to run the cheaper candidates now (fork, imports,
  measured `setUpClass` costs, each test's measured in-process CPU), plus the packed
  cost of whatever is left. A run stops at the first test that does not pass.

It verifies only when the expected saving is at least 250 ms, using a prior
that a code change breaks none of its candidates (0.5) and a data/fixture
change (0.1). pandas/numpy/duckdb (about 1 s to import) are imported once per
run, and only if the commits that need them save more than that together.

The patch is applied in memory, because the disk is read-only: an import hook
serves patched modules, an `open()` hook serves patched data files, and process
pools run inline. Each verification is a forked child of the selector, which
never imports sqlglot itself. Anything that is not a clean pass (failure,
error, skip, subtest failure, a timeout of 4x the recorded time, a crash)
keeps the test selected. Tests that start subprocesses, or read source files
as text, are never verified because an in-memory patch cannot reach them.

### Expensive tests (on; `BR_SKIP_HEAVY=0` turns it off)

Tests of 3 s or more (`test_max_nodes`, `test_execute_tpcds`, ...) dominate the
cost. When a change only replaces side-effect-free expressions inside
functions, the verification child compiles the patched module with
*infection probes* (`app/probe.py`): each replaced expression computes both its
old and new value, returns the new one, and records whether they ever
differed. The cheapest other candidates are run that way, within a CPU budget,
stopping at the first difference. If they all pass, the changed expressions
ran at least 20 times, and the old and new values never differed, the
expensive candidates are left out; everything not run stays selected. This is
weak-mutation evidence about the change itself, not a table of edits. It was
enabled after showing no miss on 300+ self-generated, fully labelled changes.

### Expensive executor tests: run them here, replaying what cannot change

`test_execute_tpcds` (21.5 s) compares sqlglot's Python executor with duckdb
on 99 queries: each query runs as a process-pool job, and duckdb runs the
query transpiled by sqlglot. Most of its time is duckdb and jobs whose inputs
and code a patch does not touch. When the image is built, `tools/build_memo.py`
runs the executor tests unpatched and records (`maps/memo.pkl`, `app/memo.py`):

- the DataFrame of every duckdb read query, keyed by the text of every
  statement run on that connection before it (the views over the data files)
  and the query text;
- each pool job's pickled arguments (digest), pickled result and CPU, and
  the digest of the pool initializer's pickled arguments.

For a still-selected expensive test with a recording, the verification child
runs the whole test with the patch applied, except that

- a duckdb query whose connection history and text match the recording
  returns the recorded frame (duckdb sees only SQL text and the data files);
- a job that ran none of the changed lines in the per-job coverage
  (`maps/jobs.json.gz`, not used for changes that run at import time or in
  test files), whose arguments and pool initializer arguments pickle to the
  recorded bytes, returns its recorded result: same code, same input.

Everything else, including every sqlglot call in the test process (parsing,
transpiling, unpickling and comparing results), runs for real on the patched
code, and jobs run when the test asks for their result, so a failing test
stops early. A pass means the test passes on this change; the test is then
left out. Without a recording, pool-based tests whose changed lines ran only
inside jobs run just those jobs (the others return `None`, which these tests
skip). It is used only when the expected saving beats the estimated CPU
(`BR_JOB_SUBSETS=0` / `BR_MEMO=0` turn it off).

Selected tests are packed (`app/pack.py`) to minimise
`max worker load + 2 s x workers` over 1..8 workers: exact search for small
selections, otherwise longest-first plus a move/swap local search. Everything
is in whole milliseconds like the scorer.

Nothing in this repository is derived from the sample answers: there is no
table from patches or kinds of edit to failing tests.

## Layout

- `selector.py`: entry point; reads `/data`, prints one JSON line per commit, logs to stderr.
- `app/`: patch parsing (`patch.py`), static analysis (`analyze.py`), maps
  (`maps.py`), verification (`verify.py`, `probe.py`), cost policy
  (`policy.py`), packing (`pack.py`).
- `maps/coverage.json.gz`: the maps (test sets stored as hex bitmasks over the map's test list).
- `maps/cpu.json`: in-process CPU per test and per `setUpClass`.
- `maps/jobs.json.gz`: per-job coverage of pool-based tests (loaded on demand).
- `maps/memo.pkl` (made when the image is built, `tools/build_memo.py`):
  recorded duckdb results and pool jobs of the executor tests.
- `maps/keys.json.gz`: which tests looked up which keys of class-level dict
  tables (loaded on demand; `tools/build_keys.sh` runs `run_keytrace.sh` with
  `keytrace.py` inside the submission image, then `build_keys.py`).
- `tools/`: how the maps are built (`build_maps.sh` runs `run_coverage.sh` twice
  with `covplugin.py` inside the submission image, then `build_maps.py`;
  `measure_cpu.py` writes `maps/cpu.json`), and
  `sample.sh` to run the image on the sample with the scorer's docker flags.

## Open-source code used

- [sqlglot](https://github.com/tobymao/sqlglot) (MIT), cloned at the pinned commit during the build; its test suite produces the maps.
- [coverage.py](https://github.com/nedbat/coveragepy) (Apache-2.0), used only offline by `tools/` to build the maps.
- The Python packages in `requirements.lock` (pytest and sqlglot's test dependencies), installed in the image.
