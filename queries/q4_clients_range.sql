-- Q4: all transactions for a set of clients over a date range (1-12 months).
--   $1 client_ids integer[]
--   $2 range_start timestamptz (inclusive)
--   $3 range_end   timestamptz (exclusive)
--
-- Index-friendly by construction: client_id = ANY($1) (one index range scan per client) plus a half-open range
-- on the raw txn_ts column (no date_trunc()/extract() on the column), which
-- matches transactions_client_ts_idx (client_id, txn_ts), lets TimescaleDB
-- exclude chunks, and lets compressed chunks filter on segment metadata
-- (segmentby client_id, min/max txn_ts per batch).
SELECT t.txn_id, t.txn_ts, a.account_type, a.account_number,
       v.name AS vendor, c.name AS category, t.txn_type, t.description, t.amount
FROM transactions t
JOIN accounts   a ON a.account_id  = t.account_id
JOIN vendors    v ON v.vendor_id   = t.vendor_id
JOIN categories c ON c.category_id = v.category_id
WHERE t.client_id = ANY ($1::integer[])
  AND t.txn_ts >= $2::timestamptz AND t.txn_ts < $3::timestamptz
ORDER BY t.client_id, t.txn_ts;  -- per-client statement order, matches the index
