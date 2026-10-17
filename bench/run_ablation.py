#!/usr/bin/env python3
"""Ablation benchmark: plain Postgres vs TimescaleDB hypertable vs compressed hypertable.

For each client count it reseeds the database (CALL bench.load_all), records build
time and storage per variant, then measures latency of the four canonical queries
in queries/ against every variant:

  warm  - steady state: same randomised parameter sequence for every variant,
          after a warm-up pass (data is in shared_buffers / OS page cache).
  cold  - container restarted and the host page cache dropped before each
          variant, then distinct parameters so most data pages are first reads.

Usage (database from docker-compose.yml must be up):
  python3 bench/run_ablation.py --clients 100,1000,10000
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import random
import re
import statistics
import subprocess
import time
from pathlib import Path

import psycopg
from psycopg.types.numeric import Int4

ROOT = Path(__file__).resolve().parent.parent
QUERIES = {
    "Q1_client_month": "q1_client_month.sql",
    "Q2_client_range": "q2_client_range.sql",
    "Q3_clients_month": "q3_clients_month.sql",
    "Q4_clients_range": "q4_clients_range.sql",
}
CONTAINER = "tsdb-bench"


def load_query(name: str) -> str:
    sql = (ROOT / "queries" / name).read_text()
    lines = [l for l in sql.splitlines() if not l.lstrip().startswith("--")]
    sql = "\n".join(lines).strip().rstrip(";")
    # strip trailing "-- comment" after the final ORDER BY
    # psycopg uses %s placeholders; each $n appears once, in order.
    return re.sub(r"\$\d+", "%s", sql.split(";")[0])


def add_months(d: dt.date, n: int) -> dt.date:
    m = d.month - 1 + n
    return dt.date(d.year + m // 12, m % 12 + 1, 1)


def ts(d: dt.date) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc)


def make_params(query: str, rng: random.Random, n_clients: int, start: dt.date,
                months: int, set_size: int):
    """Random parameters for one execution. Month ranges stay inside the data."""
    if query.endswith("month"):
        length = 1
    else:
        length = rng.randint(1, min(12, months))
    first = add_months(start, rng.randint(0, months - length))
    last = add_months(first, length)
    # Int4: psycopg would otherwise send small ints as int2 / int2[]. With an
    # int2[] parameter the generic plan of a prepared statement cannot push
    # client_id = ANY($1::integer[]) into the compressed chunks' segment index,
    # and decompresses every client's batches in the time range (see EXPERIMENT_LOG.md).
    if query.startswith(("Q1", "Q2")):
        who = Int4(rng.randint(1, n_clients))
    else:
        who = [Int4(x) for x in sorted(rng.sample(range(1, n_clients + 1), min(set_size, n_clients)))]
    return (who, ts(first), ts(last)), length


def pct(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


SUMMARY_COLS = ["clients", "variant", "query", "mode", "n", "avg_rows",
                "e2e_p50_ms", "e2e_p95_ms", "exec_mean_ms", "exec_p50_ms", "exec_p95_ms", "exec_p99_ms",
                "plan_p50_ms", "avg_shared_hit", "avg_shared_read"]


def summarise(n_clients, variant, query, mode, recs):
    e2e = [r[0] for r in recs if r[0] is not None]
    ex = [r[1] for r in recs]
    f = lambda x: f"{x:.3f}"
    return [n_clients, variant, query, mode, len(recs),
            round(statistics.mean(r[5] for r in recs)),
            f(pct(e2e, 50)) if e2e else "", f(pct(e2e, 95)) if e2e else "",
            f(statistics.mean(ex)), f(pct(ex, 50)), f(pct(ex, 95)), f(pct(ex, 99)),
            f(pct([r[2] for r in recs], 50)),
            round(statistics.mean(r[3] for r in recs)), round(statistics.mean(r[4] for r in recs))]


def connect(dsn: str, variant: str | None = None) -> psycopg.Connection:
    conn = psycopg.connect(dsn, autocommit=True)
    if variant:
        conn.execute(f"SET search_path = {variant}, public")
    return conn


def wait_ready(dsn: str, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with psycopg.connect(dsn, connect_timeout=2) as c:
                c.execute("SELECT 1")
                return
        except psycopg.OperationalError:
            time.sleep(0.5)
    raise RuntimeError("database did not come back")


def restart_cold(dsn: str):
    subprocess.run(["docker", "restart", CONTAINER], check=True, capture_output=True)
    subprocess.run(["sync"], check=True)
    try:
        Path("/proc/sys/vm/drop_caches").write_text("3\n")
    except OSError as e:  # not root / not permitted: cold numbers then only cover shared_buffers
        print(f"  ! could not drop page cache: {e}")
    wait_ready(dsn)


def txn_buffers(node):
    """Shared hit/read blocks of the scans on transactions (or its chunks), so the
    joins to accounts/vendors/categories don't drown out the storage difference."""
    rel = node.get("Relation Name", "")
    if rel == "transactions" or rel.startswith("_hyper_"):
        return node.get("Shared Hit Blocks", 0), node.get("Shared Read Blocks", 0)
    hit = read = 0
    for child in node.get("Plans", []):
        h, r = txn_buffers(child)
        hit, read = hit + h, read + r
    return hit, read


def server_timed(conn, sql, params):
    """Server-side execution via EXPLAIN ANALYZE (rows are produced but not sent):
    returns execution ms, planning ms, rows, and shared blocks hit / read by the
    transactions scans."""
    plan = conn.execute(f"EXPLAIN (ANALYZE, TIMING OFF, BUFFERS, FORMAT JSON) {sql}", params).fetchone()[0][0]
    top = plan["Plan"]
    hit, read = txn_buffers(top)
    return plan["Execution Time"], plan["Planning Time"], int(top["Actual Rows"]), hit, read


def timed(conn, sql, params):
    t0 = time.perf_counter()
    rows = conn.execute(sql, params, prepare=True).fetchall()
    return (time.perf_counter() - t0) * 1000.0, len(rows)


def write_outputs(out, summary_rows, size_rows, build_rows):
    with open(out / "latency_summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(SUMMARY_COLS)
        w.writerows(summary_rows)
    with open(out / "sizes.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["clients", "variant", "table_bytes", "index_bytes", "toast_bytes", "total_bytes", "chunks"])
        w.writerows(size_rows)
    with open(out / "build.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["clients", "variant", "kind", "chunk_interval", "rows", "load_s", "index_s", "compress_s"])
        w.writerows(build_rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default="postgresql://postgres:postgres@localhost:5432/bank")
    ap.add_argument("--clients", default="100,1000,10000")
    ap.add_argument("--years", type=int, default=1)
    ap.add_argument("--txns-per-day", type=int, default=3)
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--iterations", type=int, default=200)
    ap.add_argument("--warmup", type=int, default=20)
    ap.add_argument("--cold-iterations", type=int, default=25)
    ap.add_argument("--set-size", type=int, default=10, help="clients per Q3/Q4 call")
    ap.add_argument("--no-cold", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "results"))
    args = ap.parse_args()

    out = Path(args.out)
    (out / "plans").mkdir(parents=True, exist_ok=True)
    start = dt.date.fromisoformat(args.start)
    months = 12 * args.years
    queries = {k: load_query(v) for k, v in QUERIES.items()}

    raw_f = open(out / "latency_raw.csv", "w", newline="")
    raw = csv.writer(raw_f)
    raw.writerow(["clients", "variant", "query", "mode", "iter", "months", "rows",
                  "e2e_ms", "exec_ms", "plan_ms", "shared_hit", "shared_read"])
    summary_rows, size_rows, build_rows = [], [], []

    with connect(args.dsn) as c:
        server = c.execute("SELECT version(), extversion FROM pg_extension WHERE extname='timescaledb'").fetchone()
        settings = dict(c.execute(
            "SELECT name, setting || coalesce(unit, '') FROM pg_settings WHERE name = ANY(%s)",
            (["shared_buffers", "effective_cache_size", "work_mem", "jit",
              "max_parallel_workers_per_gather", "random_page_cost"],)).fetchall())
        variants = [r[0] for r in c.execute("SELECT variant FROM bench.default_variants").fetchall()]
    meta = {"postgres": server[0], "timescaledb": server[1], "settings": settings, "args": vars(args),
            "started": dt.datetime.now(dt.timezone.utc).isoformat()}
    print(json.dumps(meta, indent=2))
    (out / "run_meta.json").write_text(json.dumps(meta, indent=2, default=str))

    for n_clients in [int(x) for x in args.clients.split(",")]:
        print(f"\n=== {n_clients} clients: seeding + building variants")
        t0 = time.time()
        with connect(args.dsn) as c:
            c.execute("CALL bench.load_all(%s, %s, %s, %s)",
                      (n_clients, args.years, args.txns_per_day, start))
            c.execute("VACUUM ANALYZE")
            for r in c.execute("""SELECT variant, kind, chunk_interval::text, n_rows, load_s, index_s, compress_s
                                  FROM bench.build_log ORDER BY built_at""").fetchall():
                build_rows.append([n_clients, *r])
            for v in variants:
                tb, ib, xb, tot = c.execute("SELECT * FROM bench.variant_size(%s)", (v,)).fetchone()
                chunks = c.execute("""SELECT count(*) FROM timescaledb_information.chunks
                                      WHERE hypertable_schema = %s""", (v,)).fetchone()[0]
                size_rows.append([n_clients, v, tb, ib, xb, tot, chunks])
            c.execute("DROP TABLE bench.txn_src")
        print(f"  built in {time.time() - t0:.0f}s")

        # Warm: identical parameter stream per (scale, query) across variants.
        # Each execution is timed twice: end-to-end from the client (prepared
        # statement, rows fetched into Python) and server-side via EXPLAIN ANALYZE.
        for v in variants:
            with connect(args.dsn, v) as c:
                for q, sql in queries.items():
                    rng = random.Random(f"{n_clients}-{q}")
                    params = [make_params(q, rng, n_clients, start, months, args.set_size)
                              for _ in range(args.warmup + args.iterations)]
                    for p, _ in params[: args.warmup]:
                        timed(c, sql, p)
                    recs = []
                    for i, (p, length) in enumerate(params[args.warmup:]):
                        e2e, nrows = timed(c, sql, p)
                        ex, pl, _, hit, read = server_timed(c, sql, p)
                        recs.append((e2e, ex, pl, hit, read, nrows))
                        raw.writerow([n_clients, v, q, "warm", i, length, nrows,
                                      f"{e2e:.3f}", f"{ex:.3f}", f"{pl:.3f}", hit, read])
                    summary_rows.append(summarise(n_clients, v, q, "warm", recs))
                    # One representative plan per query/variant/scale.
                    p, _ = params[args.warmup]
                    plan = c.execute(f"EXPLAIN (ANALYZE, BUFFERS) {sql}", p).fetchall()
                    (out / "plans" / f"{n_clients}_{v}_{q}.txt").write_text(
                        f"-- params: {p}\n" + "\n".join(r[0] for r in plan) + "\n")
                print(f"  warm {v} done")

        # Cold: fresh server + empty page cache per variant, distinct parameters,
        # server-side timing only (so the read-block counts are first reads).
        if not args.no_cold:
            for v in variants:
                restart_cold(args.dsn)
                with connect(args.dsn, v) as c:
                    for q, sql in queries.items():
                        rng = random.Random(f"cold-{n_clients}-{q}")
                        recs = []
                        for i in range(args.cold_iterations + 1):
                            p, length = make_params(q, rng, n_clients, start, months, args.set_size)
                            ex, pl, nrows, hit, read = server_timed(c, sql, p)
                            if i == 0:
                                continue  # first execution of each query also loads catalog caches
                            recs.append((None, ex, pl, hit, read, nrows))
                            raw.writerow([n_clients, v, q, "cold", i, length, nrows,
                                          "", f"{ex:.3f}", f"{pl:.3f}", hit, read])
                        summary_rows.append(summarise(n_clients, v, q, "cold", recs))
                print(f"  cold {v} done")
        raw_f.flush()
        write_outputs(out, summary_rows, size_rows, build_rows)

    raw_f.close()
    write_outputs(out, summary_rows, size_rows, build_rows)
    meta["finished"] = dt.datetime.now(dt.timezone.utc).isoformat()
    (out / "run_meta.json").write_text(json.dumps(meta, indent=2, default=str))
    print("done")


if __name__ == "__main__":
    main()
