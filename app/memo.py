"""Replay of results that cannot depend on a patch, for running expensive tests.

Two kinds, both recorded by tools/build_memo.py while sqlglot's tests run
unpatched at the pinned commit (when the image is built):

- duckdb queries. A duckdb connection only sees SQL text: the result of a
  read query is a function of every statement run on that connection before
  it (which created its views over the test data files) and of the query
  text. A query whose connection history and text match a recorded one gets
  the recorded DataFrame instead of being run again. Anything else (another
  text, statements with parameters, writes, other fetch methods) runs in
  duckdb as usual.
- process-pool jobs of a test (test_execute_tpcds, ...). A job that ran none
  of the changed lines in the recorded run (per-job coverage, maps/jobs.json.gz)
  and receives byte-identical pickled arguments, in a pool whose initializer
  got byte-identical pickled arguments, runs the same code on the same input,
  so it returns the recorded result. Its result is still
  unpickled and checked by the test as usual.

Recorded data is used only for exact matches; the selector never treats a
test as passing without running it.
"""

import hashlib
import os
import pickle

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "maps", "memo.pkl")
_READ = ("SELECT", "WITH", "(", "FROM", "VALUES", "TABLE")


def args_key(args, kwargs):
    """Digest of a job's arguments as a process pool would send them."""
    return blob_key(pickle.dumps((args, kwargs)))


def blob_key(blob):
    return hashlib.sha256(blob).hexdigest()


def is_read(sql):
    return isinstance(sql, str) and sql.lstrip().upper().startswith(_READ)


class _Replayed:
    """Result of a replayed query: fetchdf() returns the recorded frame; any
    other use runs the query in duckdb first."""

    def __init__(self, conn, sql, blob):
        self._conn, self._sql, self._blob, self._real = conn, sql, blob, None

    def fetchdf(self):
        return pickle.loads(self._blob)

    df = fetch_df = fetchdf

    def __getattr__(self, name):
        if self._real is None:
            self._real = self._conn.execute(self._sql)
        return getattr(self._real, name)


class _Recording:
    def __init__(self, real, key, store):
        self._real, self._key, self._store = real, key, store

    def fetchdf(self):
        df = self._real.fetchdf()
        self._store[self._key] = pickle.dumps(df, protocol=4)
        return df

    df = fetch_df = fetchdf

    def __getattr__(self, name):
        return getattr(self._real, name)


class Connection:
    """Wraps a duckdb connection; mode "record" fills `store`, "replay" reads it."""

    def __init__(self, real, store, mode):
        self._real, self._store, self._mode = real, store, mode
        self._h = hashlib.sha256()

    def execute(self, sql, *a, **k):
        self._h.update(repr((sql, a, sorted(k.items()))).encode("utf-8", "replace") + b"\0")
        key = self._h.hexdigest()
        if a or k or not is_read(sql):
            return self._real.execute(sql, *a, **k)
        if self._mode == "replay":
            blob = self._store.get(key)
            if blob is not None:
                return _Replayed(self._real, sql, blob)
            return self._real.execute(sql)
        return _Recording(self._real.execute(sql), key, self._store)

    def sql(self, *a, **k):
        # relation API: not recorded; mark the history so later keys differ
        self._h.update(b"<sql>\0")
        return self._real.sql(*a, **k)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)


def install_duckdb(store, mode):
    """Route duckdb.connect() through Connection."""
    import duckdb

    real_connect = duckdb.connect
    if getattr(real_connect, "_br_memo", False):
        return

    def connect(*a, **k):
        return Connection(real_connect(*a, **k), store, mode)

    connect._br_memo = True
    duckdb.connect = connect


_loaded = {}


def load():
    """-> {"duckdb": {key: frame bytes}, "jobs": {test id: [(args key, result
    bytes, cpu ms) or None]}, "inits": {test id: initializer args key}, "cpu_ms": {test
    id: {"record"|"replay"|"replay_all": ms}}} or None."""
    if "m" not in _loaded:
        try:
            with open(PATH, "rb") as fh:
                _loaded["m"] = pickle.load(fh)
        except Exception:  # noqa: BLE001
            _loaded["m"] = None
    return _loaded["m"]
