-- Seeding and ablation helpers.
--
--   CALL bench.seed(clients, years, txns_per_day, start_date)  -- reference data + bench.txn_src
--   CALL bench.build_variant(name, kind, chunk_interval)         -- one transactions table
--   CALL bench.load_all(clients, years, txns_per_day)            -- seed + every default variant
--
-- Every variant is loaded from the same bench.txn_src rows, in arrival (txn_ts)
-- order, so the variants differ only in physical storage.

CREATE TYPE txn_type AS ENUM ('purchase', 'payroll', 'card_payment');

-- One row per variant build: how long it took to load / index / compress.
CREATE TABLE bench.build_log (
    variant        text        NOT NULL,
    kind           text        NOT NULL,
    chunk_interval interval,
    n_clients      integer     NOT NULL,
    n_rows         bigint      NOT NULL,
    load_s         numeric     NOT NULL,
    index_s        numeric     NOT NULL,
    compress_s     numeric     NOT NULL,
    built_at       timestamptz NOT NULL DEFAULT now()
);

------------------------------------------------------------------------------
-- Reference data: categories, vendors, clients, accounts.
------------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE bench.seed_reference(p_clients integer, p_start date)
LANGUAGE plpgsql AS $$
DECLARE
    first_names text[] := ARRAY['James','Mary','Robert','Patricia','John','Jennifer','Michael','Linda',
        'David','Elizabeth','William','Barbara','Richard','Susan','Joseph','Jessica','Thomas','Sarah',
        'Charles','Karen','Daniel','Lisa','Matthew','Nancy','Anthony','Sandra','Mark','Betty','Wei','Priya',
        'Carlos','Sofia','Ahmed','Fatima','Hiroshi','Yuki','Olu','Amara','Ivan','Elena'];
    last_names text[] := ARRAY['Smith','Johnson','Williams','Brown','Jones','Garcia','Miller','Davis',
        'Rodriguez','Martinez','Hernandez','Lopez','Gonzalez','Wilson','Anderson','Thomas','Taylor','Moore',
        'Jackson','Martin','Lee','Perez','Thompson','White','Harris','Sanchez','Clark','Ramirez','Lewis',
        'Robinson','Chen','Patel','Nguyen','Kim','Singh','Okafor','Ivanova','Tanaka','Cohen','Murphy'];
    cities text[][] := ARRAY[['New York','NY'],['Los Angeles','CA'],['Chicago','IL'],['Houston','TX'],
        ['Phoenix','AZ'],['Philadelphia','PA'],['San Antonio','TX'],['San Diego','CA'],['Dallas','TX'],
        ['Austin','TX'],['Seattle','WA'],['Denver','CO'],['Boston','MA'],['Atlanta','GA'],['Miami','FL'],
        ['Portland','OR'],['Nashville','TN'],['Detroit','MI'],['Minneapolis','MN'],['Charlotte','NC']];
BEGIN
    INSERT INTO categories (category_id, name, min_amount, max_amount, used_by) VALUES
        ( 1, 'Groceries',         12,   260, '{checking,visa}'),
        ( 2, 'Restaurants',        8,   140, '{checking,visa}'),
        ( 3, 'Coffee',             3,    16, '{checking,visa}'),
        ( 4, 'Gas',               25,    95, '{checking,visa}'),
        ( 5, 'Pharmacy',           5,    90, '{checking,visa}'),
        ( 6, 'Online Shopping',   10,   450, '{visa}'),
        ( 7, 'Travel',            60,  1400, '{visa}'),
        ( 8, 'Entertainment',      9,   180, '{visa}'),
        ( 9, 'Subscriptions',      5,    25, '{visa}'),
        (10, 'Utilities',         40,   320, '{checking}'),
        (11, 'ATM Withdrawal',    20,   400, '{checking}'),
        (12, 'Payroll',         1500,  4800, '{}'),   -- recurring only
        (13, 'Card Payment',     300,  3500, '{}');   -- recurring only

    -- Vendors: each brand gets several locations (distinct vendor rows).
    INSERT INTO vendors (vendor_id, name, category_id, city, state)
    SELECT row_number() OVER (ORDER BY b.category_id, b.brand, loc.i),
           b.brand, b.category_id, cities[loc.i][1], cities[loc.i][2]
    FROM (VALUES
        (1,'Whole Foods Market'),(1,'Trader Joe''s'),(1,'Kroger'),(1,'Safeway'),(1,'Costco Wholesale'),(1,'Aldi'),
        (2,'Chipotle'),(2,'Olive Garden'),(2,'Shake Shack'),(2,'Panera Bread'),(2,'Sweetgreen'),(2,'Local Taqueria'),
        (3,'Starbucks'),(3,'Dunkin'),(3,'Blue Bottle Coffee'),(3,'Peet''s Coffee'),
        (4,'Shell'),(4,'Chevron'),(4,'ExxonMobil'),(4,'BP'),
        (5,'CVS Pharmacy'),(5,'Walgreens'),(5,'Rite Aid'),
        (6,'Amazon Marketplace'),(6,'Etsy'),(6,'Best Buy Online'),(6,'Target.com'),(6,'Wayfair'),
        (7,'Delta Air Lines'),(7,'United Airlines'),(7,'Marriott'),(7,'Hilton'),(7,'Airbnb'),(7,'Uber'),
        (8,'AMC Theatres'),(8,'Ticketmaster'),(8,'Steam Games'),(8,'Bowlero'),
        (9,'Netflix'),(9,'Spotify'),(9,'Apple Services'),(9,'NYTimes Digital'),
        (10,'Con Edison'),(10,'PG&E'),(10,'Comcast Xfinity'),(10,'Verizon Wireless'),(10,'City Water Dept'),
        (11,'ATM'),
        (12,'Acme Corp Payroll'),(12,'Globex Inc Payroll'),(12,'Initech Payroll'),(12,'Umbrella Health Payroll'),
        (13,'First National Card Services')
    ) AS b(category_id, brand)
    CROSS JOIN LATERAL generate_series(1, CASE WHEN b.category_id IN (9, 12, 13) THEN 1 ELSE 10 END) AS loc(i);

    INSERT INTO clients (client_id, first_name, last_name, email, city, state, created_at)
    SELECT g,
           first_names[1 + (g * 7) % cardinality(first_names)],
           last_names[1 + (g * 13) % cardinality(last_names)],
           format('client%s@example.com', g),
           cities[1 + g % 20][1], cities[1 + g % 20][2],
           p_start - (365 + g % 2000)
    FROM generate_series(1, p_clients) g;

    -- Checking = 2*client_id - 1, Visa = 2*client_id.
    INSERT INTO accounts (account_id, client_id, account_type, account_number, opened_on)
    SELECT 2 * c.client_id - (t.v = 'checking')::int, c.client_id, t.v,
           '****' || lpad(((c.client_id * 7919 + (t.v = 'visa')::int * 31) % 10000)::text, 4, '0'),
           c.created_at::date
    FROM clients c CROSS JOIN unnest(enum_range(NULL::account_type)) t(v);
END $$;

------------------------------------------------------------------------------
-- Transactions source: bench.txn_src, ordered by txn_ts (arrival order).
------------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE bench.seed(
    p_clients      integer,
    p_years        integer DEFAULT 1,
    p_txns_per_day integer DEFAULT 3,
    p_start        date    DEFAULT '2025-01-01',
    p_seed         float8  DEFAULT 0.42)
LANGUAGE plpgsql AS $$
DECLARE
    v_end date := (p_start + make_interval(years => p_years))::date;
    v     text;
BEGIN
    -- Drop every variant (they hold FKs to the reference tables).
    FOR v IN SELECT DISTINCT variant FROM bench.build_log LOOP
        EXECUTE format('DROP SCHEMA IF EXISTS %I CASCADE', v);
    END LOOP;
    TRUNCATE bench.build_log;
    DROP TABLE IF EXISTS bench.txn_staging, bench.txn_src;
    TRUNCATE accounts, clients, vendors, categories CASCADE;

    PERFORM setseed(p_seed);
    CALL bench.seed_reference(p_clients, p_start);

    CREATE UNLOGGED TABLE bench.txn_staging (
        txn_ts      timestamptz   NOT NULL,
        client_id   integer       NOT NULL,
        account_id  integer       NOT NULL,
        vendor_id   integer       NOT NULL,
        txn_type    txn_type      NOT NULL,
        amount      numeric(12,2) NOT NULL,
        description text          NOT NULL
    );

    -- Daily card / debit purchases: p_txns_per_day per client per day,
    -- 60% on the Visa, 40% on checking.
    INSERT INTO bench.txn_staging
    WITH vend AS (
        SELECT array_agg(v.vendor_id ORDER BY v.vendor_id) FILTER (WHERE 'checking' = ANY (c.used_by)) AS chk,
               array_agg(v.vendor_id ORDER BY v.vendor_id) FILTER (WHERE 'visa'     = ANY (c.used_by)) AS visa
        FROM vendors v JOIN categories c USING (category_id)
    ), raw AS MATERIALIZED (
        SELECT d::date AS day, cl.client_id,
               random() < 0.6 AS is_visa,
               random() AS r_vendor, random() AS r_amount, random() AS r_time,
               (random() * 9999)::int AS store_no
        FROM generate_series(p_start, v_end - 1, interval '1 day') d
        CROSS JOIN clients cl
        CROSS JOIN generate_series(1, p_txns_per_day) k
    ), picked AS (
        SELECT r.*,
               CASE WHEN r.is_visa THEN vend.visa[1 + floor(r.r_vendor * cardinality(vend.visa))::int]
                    ELSE vend.chk[1 + floor(r.r_vendor * cardinality(vend.chk))::int] END AS vendor_id
        FROM raw r CROSS JOIN vend
    )
    SELECT p.day + p.r_time * interval '1 day',
           p.client_id,
           2 * p.client_id - (NOT p.is_visa)::int,
           p.vendor_id,
           'purchase',
           -round(c.min_amount + p.r_amount::numeric * (c.max_amount - c.min_amount), 2),
           CASE
               WHEN c.name = 'ATM Withdrawal' THEN format('ATM WITHDRAWAL #%s %s %s', lpad(p.store_no::text, 4, '0'), upper(v.city), v.state)
               WHEN c.name = 'Utilities'      THEN format('ACH DEBIT %s AUTOPAY', upper(v.name))
               WHEN p.is_visa                 THEN format('VISA PURCHASE %s #%s %s %s', upper(v.name), lpad(p.store_no::text, 4, '0'), upper(v.city), v.state)
               ELSE                                format('POS DEBIT %s #%s %s %s', upper(v.name), lpad(p.store_no::text, 4, '0'), upper(v.city), v.state)
           END
    FROM picked p
    JOIN vendors v    ON v.vendor_id = p.vendor_id
    JOIN categories c ON c.category_id = v.category_id;

    -- Payroll into checking on the 1st and 15th of each month.
    INSERT INTO bench.txn_staging
    SELECT d + interval '9 hours' + random() * interval '1 hour',
           cl.client_id,
           2 * cl.client_id - 1,
           emp.vendor_id,
           'payroll',
           round(1500 + (cl.client_id * 37 % 3300) + random()::numeric * 50, 2),
           format('ACH CREDIT %s DIRECT DEP', upper(emp.name))
    FROM generate_series(p_start, v_end - 1, interval '1 day') d
    CROSS JOIN clients cl
    JOIN LATERAL (
        SELECT v.vendor_id, v.name FROM vendors v WHERE v.category_id = 12
        ORDER BY v.vendor_id OFFSET cl.client_id % 4 LIMIT 1
    ) emp ON true
    WHERE extract(day FROM d) IN (1, 15);

    -- Visa statement paid from checking on the 25th: debit on checking, credit on Visa.
    INSERT INTO bench.txn_staging
    SELECT pay.ts, pay.client_id, acct.account_id, cs.vendor_id, 'card_payment',
           CASE WHEN acct.is_visa THEN pay.amount ELSE -pay.amount END,
           CASE WHEN acct.is_visa THEN 'PAYMENT RECEIVED - THANK YOU'
                ELSE format('ONLINE PAYMENT TO VISA %s', a_visa.account_number) END
    FROM (
        SELECT d + interval '7 hours' + random() * interval '10 hours' AS ts, cl.client_id,
               round(300 + random()::numeric * 3200, 2) AS amount
        FROM generate_series(p_start, v_end - 1, interval '1 day') d
        CROSS JOIN clients cl
        WHERE extract(day FROM d) = 25
    ) pay
    CROSS JOIN LATERAL (VALUES (2 * pay.client_id - 1, false), (2 * pay.client_id, true)) acct(account_id, is_visa)
    JOIN accounts a_visa ON a_visa.account_id = 2 * pay.client_id
    CROSS JOIN (SELECT vendor_id FROM vendors WHERE category_id = 13) cs;

    -- Final source in arrival order with a monotonically increasing id.
    CREATE UNLOGGED TABLE bench.txn_src AS
    SELECT row_number() OVER (ORDER BY txn_ts, account_id) AS txn_id, s.*
    FROM bench.txn_staging s
    ORDER BY txn_ts, account_id;
    DROP TABLE bench.txn_staging;
    ANALYZE bench.txn_src;
END $$;

------------------------------------------------------------------------------
-- One ablation variant: <p_variant>.transactions
--   kind = 'plain'           ordinary heap table
--          'plain_clustered' heap table CLUSTERed on (client_id, txn_ts) after load
--          'hypertable'      TimescaleDB hypertable, rowstore only
--          'compressed'      hypertable with every chunk compressed
--                            (segmentby client_id, orderby txn_ts)
------------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE bench.build_variant(
    p_variant text,
    p_kind    text,
    p_chunk   interval DEFAULT interval '30 days')
LANGUAGE plpgsql AS $$
DECLARE
    tbl     text := format('%I.transactions', p_variant);
    t0      timestamptz;
    load_s  numeric;
    index_s numeric;
    comp_s  numeric := 0;
BEGIN
    IF p_kind NOT IN ('plain', 'plain_clustered', 'hypertable', 'compressed') THEN
        RAISE EXCEPTION 'unknown kind %', p_kind;
    END IF;
    IF p_kind LIKE 'plain%' THEN
        p_chunk := NULL;
    END IF;

    EXECUTE format('DROP SCHEMA IF EXISTS %I CASCADE', p_variant);
    EXECUTE format('CREATE SCHEMA %I', p_variant);
    EXECUTE format($f$
        CREATE TABLE %s (
            txn_id      bigint        NOT NULL,
            txn_ts      timestamptz   NOT NULL,
            client_id   integer       NOT NULL,  -- denormalised from accounts: drives the access path
            account_id  integer       NOT NULL,
            vendor_id   integer       NOT NULL,
            txn_type    txn_type      NOT NULL,
            amount      numeric(12,2) NOT NULL,
            description text          NOT NULL
        )$f$, tbl);

    IF p_kind IN ('hypertable', 'compressed') THEN
        PERFORM create_hypertable(tbl::regclass, by_range('txn_ts', p_chunk),
                                  create_default_indexes => false);
    END IF;

    t0 := clock_timestamp();
    EXECUTE format('INSERT INTO %s SELECT * FROM bench.txn_src', tbl);
    load_s := extract(epoch FROM clock_timestamp() - t0);

    t0 := clock_timestamp();
    -- Hypertable unique constraints must include the time column; use the same
    -- PK on every variant so index sizes are comparable.
    EXECUTE format('ALTER TABLE %s ADD PRIMARY KEY (txn_id, txn_ts)', tbl);
    -- The access path for all four canonical queries:
    --   WHERE client_id = $1 / = ANY($1) AND txn_ts >= $2 AND txn_ts < $3
    EXECUTE format('CREATE INDEX transactions_client_ts_idx ON %s (client_id, txn_ts)', tbl);
    EXECUTE format('ALTER TABLE %s ADD FOREIGN KEY (account_id, client_id) REFERENCES accounts (account_id, client_id)', tbl);
    EXECUTE format('ALTER TABLE %s ADD FOREIGN KEY (vendor_id) REFERENCES vendors', tbl);
    IF p_kind = 'plain_clustered' THEN
        EXECUTE format('CLUSTER %s USING transactions_client_ts_idx', tbl);
    END IF;
    index_s := extract(epoch FROM clock_timestamp() - t0);

    IF p_kind = 'compressed' THEN
        t0 := clock_timestamp();
        EXECUTE format($f$ALTER TABLE %s SET (timescaledb.compress,
                          timescaledb.compress_segmentby = 'client_id',
                          timescaledb.compress_orderby   = 'txn_ts')$f$, tbl);
        PERFORM compress_chunk(c) FROM show_chunks(tbl::regclass) c;
        comp_s := extract(epoch FROM clock_timestamp() - t0);
    END IF;

    INSERT INTO bench.build_log (variant, kind, chunk_interval, n_clients, n_rows, load_s, index_s, compress_s)
    VALUES (p_variant, p_kind, p_chunk, (SELECT count(*) FROM clients),
            (SELECT count(*) FROM bench.txn_src),
            round(load_s, 2), round(index_s, 2), round(comp_s, 2));
END $$;

-- Default ablation set. Hypertables use 30-day chunks (a month query touches 1-2
-- chunks); compression is additionally tried at 7 days (the TimescaleDB default)
-- and 90 days because rows-per-segment, and so compression ratio, scales with
-- the chunk interval.
CREATE OR REPLACE VIEW bench.default_variants (variant, kind, chunk_interval) AS
VALUES ('plain',           'plain',           NULL::interval),
       ('plain_clustered', 'plain_clustered', NULL),
       ('hyper',           'hypertable',      interval '30 days'),
       ('cmp_7d',          'compressed',      interval '7 days'),
       ('cmp_30d',         'compressed',      interval '30 days'),
       ('cmp_90d',         'compressed',      interval '90 days');

CREATE OR REPLACE PROCEDURE bench.load_all(
    p_clients      integer,
    p_years        integer DEFAULT 1,
    p_txns_per_day integer DEFAULT 3,
    p_start        date    DEFAULT '2025-01-01')
LANGUAGE plpgsql AS $$
DECLARE r record;
BEGIN
    CALL bench.seed(p_clients, p_years, p_txns_per_day, p_start);
    COMMIT;
    FOR r IN SELECT * FROM bench.default_variants LOOP
        CALL bench.build_variant(r.variant, r.kind, r.chunk_interval);
        COMMIT;
    END LOOP;
END $$;

------------------------------------------------------------------------------
-- Storage footprint of one variant's transactions table (heap + toast + indexes;
-- for hypertables this includes every chunk, compressed or not).
------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION bench.variant_size(p_variant text)
RETURNS TABLE (table_bytes bigint, index_bytes bigint, toast_bytes bigint, total_bytes bigint)
LANGUAGE plpgsql AS $$
DECLARE rel regclass := format('%I.transactions', p_variant)::regclass;
BEGIN
    IF EXISTS (SELECT 1 FROM timescaledb_information.hypertables
               WHERE hypertable_schema = p_variant AND hypertable_name = 'transactions') THEN
        RETURN QUERY SELECT d.table_bytes, d.index_bytes, d.toast_bytes, d.total_bytes
                     FROM hypertable_detailed_size(rel) d;
    ELSE
        RETURN QUERY SELECT pg_table_size(rel) - coalesce(pg_total_relation_size(c.reltoastrelid), 0),
                            pg_indexes_size(rel),
                            coalesce(pg_total_relation_size(c.reltoastrelid), 0),
                            pg_total_relation_size(rel)
                     FROM pg_class c WHERE c.oid = rel;
    END IF;
END $$;
