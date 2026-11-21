# Base image digest is the one the commits were labelled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS src
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# sqlglot at the pinned commit (shallow fetch of exactly that commit).
RUN git init -q /opt/sqlglot \
    && git -C /opt/sqlglot fetch -q --depth 1 https://github.com/tobymao/sqlglot.git eb11aec4be8cb2122afe713a4816662c3f0fb2fa \
    && git -C /opt/sqlglot checkout -q FETCH_HEAD \
    && rm -rf /opt/sqlglot/.git

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock
COPY --from=src /opt/sqlglot /opt/sqlglot
RUN python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests; chmod -R a+rX /opt/sqlglot
COPY maps /app/maps
COPY app /app/app
COPY selector.py /app/selector.py
# Fingerprint of sqlglot's import-time state, unpatched (see app/state.py).
RUN cd /app && PYTHONPATH=/opt/sqlglot:/app SKIP_INTEGRATION=0 python -m app.state /app/maps/baseline_state.json
# Unpatched results of the executor tests' duckdb queries and pool jobs (see
# app/memo.py). Optional: if recording fails, the selector does without it.
COPY tools/build_memo.py /app/tools/build_memo.py
RUN cd /app && (python tools/build_memo.py /opt/sqlglot /app/maps/memo.pkl || rm -f /app/maps/memo.pkl)
RUN python -m compileall -q /app && chmod -R a+rX /app
ENV PYTHONDONTWRITEBYTECODE=1 SQLGLOT_SRC=/opt/sqlglot SKIP_INTEGRATION=0
CMD ["python", "/app/selector.py"]
