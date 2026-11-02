-- Landed source extracts (ADR-002 shapes, ADR-016/ADR-020 loading).
--
-- Every table carries the same four provenance columns, so any row traces to
-- the file and run that produced it:
--
--   _batch_id      the run's batch
--   _source_file   the file it came from
--   _file_sha256   that file's checksum at load time
--   _loaded_at     when it landed
--
-- A UNIQUE index on each extract's declared natural key is a backstop, not
-- the loading mechanism. Idempotency comes from the anti-join (ADR-020); this
-- index means a bug in that logic fails loudly instead of duplicating
-- inventory quietly.
--
-- Types are explicit. Money is numeric, never float: a float column makes the
-- subledger-to-GL reconciliation fail on representation rather than on a real
-- difference.
--
-- Run with:  make raw-schema

\set ON_ERROR_STOP on

SET client_min_messages = warning;

CREATE TABLE IF NOT EXISTS raw.materials (
    material_number       text    NOT NULL,
    material_description  text    NOT NULL,
    material_group        text    NOT NULL,
    base_uom              text    NOT NULL,
    plant                 text    NOT NULL,
    created_on            date    NOT NULL,
    _batch_id             text    NOT NULL,
    _source_file          text    NOT NULL,
    _file_sha256          char(64) NOT NULL,
    _loaded_at            timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS materials_nk ON raw.materials (material_number);

CREATE TABLE IF NOT EXISTS raw.suppliers (
    supplier_number  text    NOT NULL,
    supplier_name    text    NOT NULL,
    country          text    NOT NULL,
    payment_terms    text    NOT NULL,
    created_on       date    NOT NULL,
    _batch_id        text    NOT NULL,
    _source_file     text    NOT NULL,
    _file_sha256     char(64) NOT NULL,
    _loaded_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS suppliers_nk ON raw.suppliers (supplier_number);

CREATE TABLE IF NOT EXISTS raw.standard_costs (
    material_number    text           NOT NULL,
    valid_from         date           NOT NULL,
    valid_to           date           NOT NULL,
    standard_cost_usd  numeric(18, 2) NOT NULL,
    currency           text           NOT NULL,
    approved_by        text           NOT NULL,
    approved_on        date           NOT NULL,
    _batch_id          text           NOT NULL,
    _source_file       text           NOT NULL,
    _file_sha256       char(64)       NOT NULL,
    _loaded_at         timestamptz    NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS standard_costs_nk
    ON raw.standard_costs (material_number, valid_from);

CREATE TABLE IF NOT EXISTS raw.purchase_order_headers (
    po_number        text    NOT NULL,
    supplier_number  text    NOT NULL,
    po_date          date    NOT NULL,
    currency         text    NOT NULL,
    created_by       text    NOT NULL,
    company_code     text    NOT NULL,
    _batch_id        text    NOT NULL,
    _source_file     text    NOT NULL,
    _file_sha256     char(64) NOT NULL,
    _loaded_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS purchase_order_headers_nk
    ON raw.purchase_order_headers (po_number);

CREATE TABLE IF NOT EXISTS raw.purchase_order_lines (
    po_number         text           NOT NULL,
    po_item           integer        NOT NULL,
    material_number   text           NOT NULL,
    quantity_ordered  integer        NOT NULL,
    unit_price_usd    numeric(18, 2) NOT NULL,
    net_value_usd     numeric(18, 2) NOT NULL,
    uom               text           NOT NULL,
    delivery_date     date           NOT NULL,
    plant             text           NOT NULL,
    _batch_id         text           NOT NULL,
    _source_file      text           NOT NULL,
    _file_sha256      char(64)       NOT NULL,
    _loaded_at        timestamptz    NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS purchase_order_lines_nk
    ON raw.purchase_order_lines (po_number, po_item);

CREATE TABLE IF NOT EXISTS raw.goods_movements (
    material_document       text           NOT NULL,
    material_document_item  integer        NOT NULL,
    -- Part of the natural key: material document numbers legitimately repeat
    -- across fiscal years (ADR-020).
    fiscal_year             integer        NOT NULL,
    movement_type           text           NOT NULL,
    material_number         text           NOT NULL,
    plant                   text           NOT NULL,
    quantity                integer        NOT NULL,
    uom                     text           NOT NULL,
    amount_usd              numeric(18, 2) NOT NULL,
    posting_date            date           NOT NULL,
    document_date           date           NOT NULL,
    -- Null on goods issues, which reference no purchase order.
    po_number               text,
    po_item                 integer,
    entered_by              text           NOT NULL,
    _batch_id               text           NOT NULL,
    _source_file            text           NOT NULL,
    _file_sha256            char(64)       NOT NULL,
    _loaded_at              timestamptz    NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS goods_movements_nk
    ON raw.goods_movements (material_document, fiscal_year, material_document_item);
CREATE INDEX IF NOT EXISTS goods_movements_posting_idx
    ON raw.goods_movements (posting_date);

CREATE TABLE IF NOT EXISTS raw.inventory_revaluations (
    revaluation_document   text           NOT NULL,
    fiscal_year            integer        NOT NULL,
    material_number        text           NOT NULL,
    posting_date           date           NOT NULL,
    old_standard_cost_usd  numeric(18, 2) NOT NULL,
    new_standard_cost_usd  numeric(18, 2) NOT NULL,
    quantity_on_hand       integer        NOT NULL,
    amount_usd             numeric(18, 2) NOT NULL,
    currency               text           NOT NULL,
    approved_by            text           NOT NULL,
    _batch_id              text           NOT NULL,
    _source_file           text           NOT NULL,
    _file_sha256           char(64)       NOT NULL,
    _loaded_at             timestamptz    NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS inventory_revaluations_nk
    ON raw.inventory_revaluations (revaluation_document, fiscal_year);

CREATE TABLE IF NOT EXISTS raw.gl_postings (
    accounting_document     text           NOT NULL,
    line_item               integer        NOT NULL,
    fiscal_year             integer        NOT NULL,
    company_code            text           NOT NULL,
    gl_account              text           NOT NULL,
    debit_credit_indicator  text           NOT NULL,
    amount_usd              numeric(18, 2) NOT NULL,
    signed_amount_usd       numeric(18, 2) NOT NULL,
    currency                text           NOT NULL,
    posting_date            date           NOT NULL,
    document_date           date           NOT NULL,
    reference_document      text           NOT NULL,
    -- Null on the offset line, which carries no material.
    material_number         text,
    entered_by              text           NOT NULL,
    _batch_id               text           NOT NULL,
    _source_file            text           NOT NULL,
    _file_sha256            char(64)       NOT NULL,
    _loaded_at              timestamptz    NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS gl_postings_nk
    ON raw.gl_postings (accounting_document, fiscal_year, line_item);
CREATE INDEX IF NOT EXISTS gl_postings_account_posting_idx
    ON raw.gl_postings (gl_account, posting_date);

-- The loader writes raw; the transformer reads it. Explicit grants, because
-- ALTER DEFAULT PRIVILEGES only covers tables created after it ran.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'btb_loader') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA raw TO btb_loader;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'btb_transformer') THEN
        GRANT SELECT ON ALL TABLES IN SCHEMA raw TO btb_transformer;
    END IF;
END
$$;
