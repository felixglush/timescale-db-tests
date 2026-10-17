#!/bin/bash
# Seeds the database on first container init using SEED_* env vars from docker-compose.yml.
set -euo pipefail

echo "Seeding: clients=${SEED_CLIENTS} years=${SEED_YEARS} txns/day=${SEED_TXNS_PER_DAY} start=${SEED_START_DATE}"
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<SQL
CALL bench.load_all(${SEED_CLIENTS}, ${SEED_YEARS}, ${SEED_TXNS_PER_DAY}, '${SEED_START_DATE}');
VACUUM ANALYZE;
SELECT variant, kind, chunk_interval, n_rows, load_s, index_s, compress_s FROM bench.build_log ORDER BY built_at;
SQL
