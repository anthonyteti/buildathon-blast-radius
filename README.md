# Blast Radius selector

Picks which sqlglot tests to run for each patch (sqlglot pinned at
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`) and packs them onto workers.

## How it decides

All knowledge comes from maps built by running sqlglot's own test suite at the
pinned commit (`maps/coverage.json.gz`, rebuilt with `tools/build_maps.sh`):

- **line coverage per test** (coverage.py, C tracer). Process pools run inline
  so their work is charged to the calling test; every sqlglot module is
  imported first so import-time lines are kept apart; `setUp`/`setUpClass` lines
  count for the whole class; lines run while a test file is imported count for
  every test in that file.
- **file reads and directory listings** per test (fixtures, data files).
- **static import graph** of `sqlglot/` and `tests/`.

For each changed file in a patch (`app/analyze.py`):

| change | tests selected |
|---|---|
| Python, same AST after the patch (comments, formatting) or docstring only | none |
| line inside a function body | tests that executed that statement (header only for `if`/`for`/...; the body of a lambda when inside one) |
| `def` line, signature, decorator | tests that executed the function body |
| module/class-level line in `sqlglot/` | tests executing lines that mention the names it defines (the class name when the name is generic), restricted to the same dialect's modules |
| module/class-level line in `tests/` | tests in every test file whose import ran it |
| fixture / data file | tests that opened it (plus tests that listed its directory when added/removed) |
| source file read as text by a test | those tests too |
| syntax error, deleted module, `conftest.py`, pytest config, anything that fails to analyse | every test |

Selected tests are packed (`app/pack.py`) to minimise
`max worker load + 2 s x workers` over 1..8 workers: exact search for small
selections, otherwise longest-first plus a move/swap local search. Everything
is in whole milliseconds like the scorer.

Nothing in this repository is derived from the sample answers: there is no
table from patches or kinds of edit to failing tests.

## Layout

- `selector.py`: entry point; reads `/data`, prints one JSON line per commit, logs to stderr.
- `app/`: patch parsing, analysis, maps, packing.
- `maps/coverage.json.gz`: the maps (test sets stored as hex bitmasks over the map's test list).
- `tools/`: how the maps are built (`build_maps.sh` runs `run_coverage.sh` with
  `covplugin.py` inside the submission image, then `build_maps.py`), and
  `sample.sh` to run the image on the sample with the scorer's docker flags.

## Open-source code used

- [sqlglot](https://github.com/tobymao/sqlglot) (MIT), cloned at the pinned commit during the build; its test suite produces the maps.
- [coverage.py](https://github.com/nedbat/coveragepy) (Apache-2.0), used only offline by `tools/` to build the maps.
- The Python packages in `requirements.lock` (pytest and sqlglot's test dependencies), installed in the image.
