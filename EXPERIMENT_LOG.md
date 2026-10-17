# Experiment log: TimescaleDB compression on bank transactions

Question: for per-client "statement" queries over checking + Visa transactions,
what does TimescaleDB (hypertables, then compression) buy in latency and storage
compared with plain Postgres, and how does that change as the number of clients
grows?

Full tables: [results/RESULTS.md](results/RESULTS.md). Raw data:
`results/latency_raw.csv`. Plans: `results/plans/`.

## Setup (all runs)

- **Host:** 4 vCPU, 16 GB RAM, cloud VM, Docker 29.8.
- **Image:** `timescale/timescaledb:2.30.2-pg18` (PostgreSQL 18.6, TimescaleDB 2.30.2). This was the newest tag on 2026-10-17.
- **Tuning:** `timescaledb-tune` pinned to 4 GB / 4 CPUs, which gives shared_buffers = 1.25 GB, effective_cache_size = 3.75 GB and work_mem = 16 MB. `jit=off` and `max_parallel_workers_per_gather=0` keep the latencies stable.
- **Data:** each client gets 3 purchases per day (60% Visa, 40% checking), payroll on the 1st and 15th, and a Visa payment from checking on the 25th. History is 1 year starting 2025-01-01, which works out to 1,143 rows per client per year. The data is deterministic (`setseed(0.42)`).
- **Load order:** rows are loaded in arrival (txn_ts) order. That is how a live system fills the table, so one client's rows end up scattered across the heap.
- **Indexes (identical on every variant):** `PRIMARY KEY (txn_id, txn_ts)` and `(client_id, txn_ts)`.
- **Compression settings:** `segmentby = client_id`, `orderby = txn_ts`.
- **Query parameters:**
  - Q1 and Q3 cover one random calendar month.
  - Q2 and Q4 cover a random range of 1 to 12 months, uniformly distributed.
  - Q3 and Q4 use 10 random clients.
  - Every variant gets the same parameter sequence.

### Variants

| variant | what |
|---|---|
| plain | heap table |
| plain_clustered | heap table `CLUSTER`ed on (client_id, txn_ts) after load. This is an upper bound for "rows of a client are adjacent" without TimescaleDB. It is not a steady state, because new rows arrive unclustered. |
| hyper | hypertable, 30-day chunks |
| cmp_7d / cmp_30d / cmp_90d | hypertable, all chunks compressed. 7 days is TimescaleDB's default chunk interval. |

### Measurements

- **warm:** 20 warm-up runs, then 200 measured runs per query, variant and scale. Each run is measured two ways:
  - **e2e:** a prepared statement executed from psycopg 3, with all rows fetched into Python.
  - **exec:** server execution time from `EXPLAIN (ANALYZE, TIMING OFF, BUFFERS)`. This excludes planning time and sending rows to the client.
- **cold:** before each variant, `docker restart` plus `echo 3 > /proc/sys/vm/drop_caches`. Then 25 runs per query, each with different random parameters, measuring exec time and the 8 KB blocks read by the scans on transactions. The first run is discarded because it loads catalog caches.
- **storage:** `hypertable_detailed_size()`, or `pg_total_relation_size()` for plain tables. Both include heap, toast and indexes.

---

## Run 0: smoke test (100 clients, 20 iterations)

- I had mounted `./db/init` over `/docker-entrypoint-initdb.d`, which hid the image's own `000_install_timescaledb.sh` and `001_timescaledb_tune.sh`. I switched to mounting the files one by one.
- PG18 images keep PGDATA at `/var/lib/postgresql/18/docker`, so the volume now mounts at `/var/lib/postgresql`.
- `round(double precision, int)` doesn't exist, so I cast random() to numeric before rounding.
- Planner check: every variant uses the `(client_id, txn_ts)` index. On compressed chunks, the plan is `ColumnarScan` over an index scan on `(client_id, _ts_meta_v2_first_txn_ts, _ts_meta_v2_last_txn_ts)`, so the client and time filters are applied before anything is decompressed.
- Docker Hub returned HTTP 429 (rate limit) on the first pull. Pulling through `mirror.gcr.io` worked, and a plain pull worked a few seconds later.

## Run 1: 100 / 1k / 10k (aborted at 10k)

- At 10k clients the masked `account_number` (last 4 digits) collided with its UNIQUE constraint. The constraint was wrong, because masked numbers aren't unique, so I dropped it. I also made the harness write its CSVs after each scale, so a crash no longer loses the finished scales.
- Buffer counts in this run were misleading. The top-level `Shared Hit Blocks` included the nested-loop lookups into `accounts`, about 3 buffers per returned row, and those swamped the transactions access (6 buffers for Q1 on plain_clustered). From run 2 on, buffers are summed only over scans of `transactions` and its chunks.

## Run 2: 100 / 1k / 10k (complete)

- Storage and exec-time numbers matched run 3 within noise.
- **Finding: parameter types matter for prepared statements on compressed chunks.**
  - The e2e latency of Q4 on cmp_30d/cmp_90d at 100 clients was about 55 ms, against roughly 9 ms of server exec time.
  - Cause: psycopg sends small Python ints as `int2`, so the statement received an `int2[]` parameter. In the **generic plan**, `client_id = ANY(($1)::integer[])` was not pushed into the compressed chunk's segment index. It became a plain `Filter` above the `ColumnarScan`, so every client's batches in the time range were decompressed: 14,931 buffers and 41 ms, against about 10 ms with `int4[]`.
  - Plain and uncompressed hypertables were not affected, because a btree can still use the condition.
  - At larger scales Postgres kept using custom plans, so it hid the problem. Production code could hit it at any scale once a generic plan is cached.
  - **Fix:** bind client ids as `int4` (`psycopg.types.numeric.Int4`) or declare parameter types in `PREPARE`. The harness now does the former.

## Run 3: 100 / 1k / 10k (final, reported below)

`python3 bench/run_ablation.py --clients 100,1000,10000`. The run took about 30 minutes, about 10 of them building the 10k-client variants (11.43M rows each).

### Storage

| variant | 100 clients | 1k clients | 10k clients | bytes/row @10k |
|---|---:|---:|---:|---:|
| plain | 19.2 MB | 191 MB | 1,912 MB | 175 |
| hyper | 20.2 MB | 192 MB | 1,912 MB | 175 |
| cmp_7d | 14.4 MB (1.3x) | 111 MB (1.7x) | 1,049 MB (1.8x) | 96 |
| cmp_30d | 7.4 MB (2.6x) | 60 MB (3.2x) | 586 MB (3.3x) | 54 |
| cmp_90d | 5.4 MB (3.6x) | 48 MB (4.0x) | 476 MB (4.0x) | 44 |

- Storage scales linearly with client count for every variant. The compression ratio stays roughly flat above 1k clients. At 100 clients, per-chunk fixed overhead lowers it.
- Of plain's 175 bytes per row, 107 are heap and 60 are the two btree indexes. Compressed chunks keep only a small segment index, about 0.2 to 2 bytes per row.
- **The chunk interval sets the ratio.** With `segmentby client_id`, a batch holds one client's rows for one chunk: 22 rows at 7 days, 94 at 30 days and 282 at 90 days, against a maximum of 1,000. Small batches pay per-batch header and toast overhead and give the codecs little to work with. The 7-day default is a poor fit for about 3 rows per client per day.
- Compressed bytes per row by column, measured on one chunk at 10k clients:

  | chunk | txn_id | txn_ts | account_id | vendor_id | txn_type | amount | description |
  |---|---:|---:|---:|---:|---:|---:|---:|
  | 7d | 3.9 | 10.2 | 2.4 | 3.2 | 3.1 | 8.9 | 43.6 |
  | 30d | 2.6 | 8.8 | 0.9 | 1.9 | 0.7 | 6.3 | 19.5 |
  | 90d | 2.3 | 8.6 | 0.6 | 1.7 | 0.4 | 5.2 | 15.4 |

  The free-text `description` (41 bytes raw, partly unique because of store numbers) and `txn_ts` dominate. `txn_ts` stays at about 8.6 bytes because purchase times are random within each day, so delta-of-delta encoding has little to remove.
- Compression time at 10k clients was 34 to 61 seconds for the whole year, on top of about 37 seconds to load and index.

### Latency at 10k clients (ms, p50)

| query | rows | plain warm | hyper warm | cmp_30d warm | cmp_90d warm | plain cold | hyper cold | cmp_30d cold | cmp_90d cold | plain_clustered cold |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Q1 client, month | 95 | 0.55 | 0.59 | 0.68 | 0.72 | 6.6 | 6.4 | 2.1 | 1.9 | 1.1 |
| Q2 client, 1-12 mo | ~630 | 2.3 | 2.7 | 2.5 | 2.0 | 34.9 | 29.9 | 4.9 | 3.7 | 2.2 |
| Q3 10 clients, month | ~950 | 3.1 | 4.0 | 3.1 | 3.4 | 38.1 | 33.2 | 9.9 | 10.0 | 5.9 |
| Q4 10 clients, 1-12 mo | ~6,500 | 19.8 | 25.3 | 15.1 | 14.2 | 91.6 | 104.6 | 28.5 | 22.6 | 16.2 |

Warm = server exec time. Cold = server exec time with the page cache dropped.

Blocks read from disk per query, cold, at 10k clients:

| query | plain | hyper | cmp_30d | cmp_90d | plain_clustered |
|---|---:|---:|---:|---:|---:|
| Q1 | 96 | 98 | 11 | 8 | 5 |
| Q2 | 582 | 590 | 29 | 15 | 14 |
| Q3 | 774 | 781 | 75 | 68 | 44 |
| Q4 | 2,845 | 2,927 | 219 | 107 | 137 |

### Findings

1. **A hypertable without compression gives these queries nothing.**
   - Latency and size match plain Postgres within noise. Warm, it is slightly slower: planning takes about 0.3 to 0.7 ms longer, and a range is spread over several chunk indexes.
   - Chunk exclusion has nothing to exclude, because the `(client_id, txn_ts)` btree already narrows the range.
   - Hypertables would pay off for retention (dropping old chunks) or for queries over the whole fleet by time, and neither is in this workload.
2. **Warm, all variants perform about the same.**
   - When the working set is in memory, every variant answers Q1 to Q3 in under about 4 ms. Q4 ranges from 14 to 25 ms, and compressed is slightly faster because it touches fewer buffers.
   - Latency barely grows with client count for any variant, because each query reads one client's rows through an index.
3. **Cold, compression wins by 3 to 8x, and the gap grows with client count.**
   - At 10k clients, plain and hypertable Q4 reads about 2,900 blocks (about 23 MB) for about 6,500 rows, because arrival-order loading scatters a client's rows over roughly one heap page per row. Compressed reads 107 to 219 blocks.
   - At 100 clients the whole table is about 19 MB and readahead absorbs the difference. At 10k clients it does not.
4. **Most of the cold win comes from locality, not compression itself.**
   - `plain_clustered` reads about as few blocks as cmp_90d and is the fastest variant cold. `segmentby client_id` is effectively a CLUSTER that stays in place, because each chunk is compressed once, after its time window closes.
   - Compression adds 3 to 4x less disk on top of that locality, and keeps it as new data arrives. A clustered plain table loses its ordering as new rows arrive.
5. **Chunk interval matters more than anything else that was tunable.**
   - The 7-day default doubles storage compared with 30 days, and it is the slowest compressed variant on Q1 and Q2, warm and cold. It has 53 chunks a year, so Q2/Q4 planning takes 4 to 5 ms, and its tiny batches add decompression overhead.
   - 90 days was best on both size and latency here. A month query decompresses up to 3x more rows than it returns, but that costs less than the extra batches and chunks.
6. **End-to-end costs.**
   - Client-side e2e is about 2 to 3x the server exec time. Fetching about 6,500 rows into Python costs around 10 ms.
   - Compressed variants also re-plan on every call, because Postgres keeps choosing custom plans, so planning time counts. That makes compressed e2e about 1 to 2 ms slower than plain on the small queries even when warm.
7. **Bind parameter types correctly** (see run 2). An `int2[]` parameter in a cached generic plan makes compressed chunks decompress every segment in the time range.

### Caveats

- 10k clients (11.4M rows, 1.9 GB uncompressed) is the largest scale that fit the session's disk next to six variants. Even plain fits in RAM at that size. The cold runs simulate a larger-than-memory dataset by dropping the page cache.
- Single client connection, no concurrent writes. A real system would also be inserting into the newest, still uncompressed chunk, and would compress chunks with a policy.
- The PK `(txn_id, txn_ts)` on the compressed variants triggers TimescaleDB's warning "column txn_id should be used for segmenting or ordering". It only matters for uniqueness checks when inserting into compressed chunks, which this workload does not do.
- 30-day chunks are aligned to the epoch, not to calendar months, so a month query usually touches 2 chunks.

### Next ideas

- Larger scales, such as 100k clients, on a bigger disk, and multi-year history to see chunk exclusion matter.
- A 1-year chunk interval, where batches approach the 1,000-row maximum.
- Making descriptions more compressible: drop the store number, or split it into its own integer column.
- Concurrent ingest with a compression policy, and query latency on the uncompressed "hot" chunk.
- A covering index `(client_id, txn_ts) INCLUDE (...)` on plain, for index-only scans.
