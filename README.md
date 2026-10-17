# TimescaleDB compression on bank transaction data

A Postgres 18 + TimescaleDB 2.30 container seeded with synthetic multi-client,
multi-year checking and Visa transactions, four canonical "statement" queries,
and an ablation that measures latency and storage for plain Postgres vs a
hypertable vs a compressed hypertable as the number of clients grows.

Findings and the run history are in [EXPERIMENT_LOG.md](EXPERIMENT_LOG.md); the
latest numbers are in [results/RESULTS.md](results/RESULTS.md).

## Quick start

```bash
# 100 clients, 1 year, 3 purchases per client per day (all defaults)
docker compose up -d
docker compose logs -f db        # wait for "PostgreSQL init process complete"

# bigger seed: reseed from scratch with different parameters
docker compose down -v
SEED_CLIENTS=1000 SEED_YEARS=2 docker compose up -d

psql postgresql://postgres:postgres@localhost:5432/bank
```

Reseed in place without recreating the container:

```sql
CALL bench.load_all(1000, 2, 3, '2024-01-01');  -- clients, years, purchases/day, start date
VACUUM ANALYZE;
```

| env var | default | meaning |
|---|---|---|
| `SEED_CLIENTS` | 100 | number of clients (each gets one checking + one Visa account) |
| `SEED_YEARS` | 1 | years of history |
| `SEED_TXNS_PER_DAY` | 3 | card/debit purchases per client per day (plus payroll and card payments) |
| `SEED_START_DATE` | 2025-01-01 | first day of history |

## Schema

```
clients 1──* accounts (checking, visa)        categories 1──* vendors
                 │                                              │
                 └──────────────* transactions *────────────────┘
```

- `clients`, `accounts`, `categories`, `vendors` live in `public` ([db/init/100_schema.sql](db/init/100_schema.sql)).
- `transactions(txn_id, txn_ts, client_id, account_id, vendor_id, txn_type, amount, description)`
  exists once per ablation variant, each in its own schema
  ([db/init/110_bench_functions.sql](db/init/110_bench_functions.sql)):

| schema | storage |
|---|---|
| `plain` | ordinary heap table, rows in arrival (time) order |
| `plain_clustered` | heap table `CLUSTER`ed on `(client_id, txn_ts)` once after load (reference point, not a steady state) |
| `hyper` | hypertable, 30-day chunks, rowstore |
| `cmp_7d`, `cmp_30d`, `cmp_90d` | hypertable with every chunk compressed (`segmentby client_id`, `orderby txn_ts`), 7/30/90-day chunks |

`client_id` is deliberately denormalised onto `transactions` (kept honest by a
composite FK to `accounts(account_id, client_id)`) because it is the leading key
of the access path and the compression `segmentby` column.

Every variant has the same indexes: `PRIMARY KEY (txn_id, txn_ts)` and
`transactions_client_ts_idx (client_id, txn_ts)`.

Each transaction has a short text description such as
`VISA PURCHASE WHOLE FOODS MARKET #0412 SEATTLE WA` or
`ACH CREDIT ACME CORP PAYROLL DIRECT DEP`.

## Canonical queries

In [queries/](queries/), written as prepared statements:

| query | filter |
|---|---|
| Q1 `q1_client_month.sql` | `client_id = $1 AND txn_ts >= $2 AND txn_ts < $3` (one month) |
| Q2 `q2_client_range.sql` | same, 1-12 month range |
| Q3 `q3_clients_month.sql` | `client_id = ANY($1::int[])` + one month |
| Q4 `q4_clients_range.sql` | `client_id = ANY($1::int[])` + 1-12 month range |

All four join `accounts`, `vendors` and `categories` and filter on the raw
`txn_ts` column with a half-open range, so they use `(client_id, txn_ts)`, get
chunk exclusion on hypertables, and filter compressed batches by segment
metadata. Pick the variant with `SET search_path = cmp_30d, public;`.

## Running the ablation

```bash
docker compose up -d
pip install "psycopg[binary]" matplotlib
sudo python3 bench/run_ablation.py --clients 100,1000,10000   # root: cold runs drop the page cache (or pass --no-cold)
python3 bench/report.py                                       # -> results/RESULTS.md
python3 bench/plots.py                                        # -> results/figures/*.png
```

The script reseeds the database for each client count, so it destroys whatever
is in it. Outputs in `results/`: `latency_raw.csv`, `latency_summary.csv`,
`sizes.csv`, `build.csv`, `run_meta.json`, and one `EXPLAIN (ANALYZE, BUFFERS)`
per query/variant/scale in `results/plans/`.

Cold measurements empty shared_buffers (`pg_buffercache_evict_all()`) and the
host page cache before every execution; the script aborts if it can't.
