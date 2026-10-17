-- Relational reference schema (shared by every ablation variant).
--
--   clients 1──* accounts (one checking + one Visa per client)
--   categories 1──* vendors
--   accounts / vendors 1──* <variant>.transactions
--
-- The transactions fact table is created once per ablation variant, in its own
-- schema (plain, hyper, cmp_7d, ...) by bench.build_variant() in
-- 110_bench_functions.sql. Queries are written against the unqualified name
-- `transactions` and the variant is picked with search_path.

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE clients (
    client_id   integer PRIMARY KEY,
    first_name  text        NOT NULL,
    last_name   text        NOT NULL,
    email       text        NOT NULL UNIQUE,
    city        text        NOT NULL,
    state       char(2)     NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TYPE account_type AS ENUM ('checking', 'visa');

CREATE TABLE accounts (
    account_id     integer PRIMARY KEY,
    client_id      integer      NOT NULL REFERENCES clients,
    account_type   account_type NOT NULL,
    account_number text         NOT NULL UNIQUE,  -- masked, e.g. ****1234
    opened_on      date         NOT NULL,
    UNIQUE (client_id, account_type),
    -- Target for the composite FK from transactions(account_id, client_id), which
    -- guarantees the denormalised transactions.client_id matches the account.
    UNIQUE (account_id, client_id)
);

CREATE TABLE categories (
    category_id smallint PRIMARY KEY,
    name        text          NOT NULL UNIQUE,
    min_amount  numeric(12,2) NOT NULL,
    max_amount  numeric(12,2) NOT NULL,
    -- Which account types draw purchases from this category ('checking','visa').
    used_by     account_type[] NOT NULL
);

CREATE TABLE vendors (
    vendor_id    integer  PRIMARY KEY,
    name         text     NOT NULL,
    category_id  smallint NOT NULL REFERENCES categories,
    city         text     NOT NULL,
    state        char(2)  NOT NULL
);
CREATE INDEX ON vendors (category_id);

-- Benchmark helpers live here.
CREATE SCHEMA bench;
