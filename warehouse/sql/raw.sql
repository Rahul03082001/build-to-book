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

-- BOM header and item, shaped on SAP's STKO and STPO (ADR-002). Multi-level
-- structure is emergent: a component that is itself a parent has its own
-- header, so depth comes from the data rather than from a level column that
-- someone has to keep correct.
CREATE TABLE IF NOT EXISTS raw.bom_headers (
    bom_number       text        NOT NULL,
    material_number  text        NOT NULL,
    plant            text        NOT NULL,
    bom_usage        text        NOT NULL,
    base_quantity    integer     NOT NULL,
    uom              text        NOT NULL,
    valid_from       date        NOT NULL,
    valid_to         date        NOT NULL,
    status           text        NOT NULL,
    created_by       text        NOT NULL,
    _batch_id        text        NOT NULL,
    _source_file     text        NOT NULL,
    _file_sha256     char(64)    NOT NULL,
    _loaded_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS bom_headers_nk ON raw.bom_headers (bom_number);
CREATE INDEX IF NOT EXISTS bom_headers_material_idx ON raw.bom_headers (material_number);

CREATE TABLE IF NOT EXISTS raw.bom_items (
    bom_number                 text        NOT NULL,
    bom_item                   integer     NOT NULL,
    component_material_number  text        NOT NULL,
    -- Quantity of the component per base quantity of the parent. Not money,
    -- so no value total in the sidecar.
    quantity_per               integer     NOT NULL,
    uom                        text        NOT NULL,
    is_phantom                 boolean     NOT NULL,
    _batch_id                  text        NOT NULL,
    _source_file               text        NOT NULL,
    _file_sha256               char(64)    NOT NULL,
    _loaded_at                 timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS bom_items_nk ON raw.bom_items (bom_number, bom_item);
CREATE INDEX IF NOT EXISTS bom_items_component_idx
    ON raw.bom_items (component_material_number);

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
    -- The production order an issue was consumed by (ADR-027). Null on
    -- receipts. Issues used to reference nothing at all, which is why
    -- production consumption and inventory issues could drift 58-fold apart
    -- with no join able to compare them.
    order_number            text,
    entered_by              text           NOT NULL,
    -- The inbound delivery this receipt arrived on (ADR-026). Null on issues,
    -- which have no supplier shipment. The link is recorded here rather than
    -- inferred from the purchase order line and a date, because partial
    -- deliveries mean one line can have two shipments.
    inbound_delivery        text,
    _batch_id               text           NOT NULL,
    _source_file            text           NOT NULL,
    _file_sha256            char(64)       NOT NULL,
    _loaded_at              timestamptz    NOT NULL DEFAULT now()
);
-- CREATE TABLE IF NOT EXISTS does not alter an existing table, so a warehouse
-- created before inbound_delivery existed needs the column added -- otherwise
-- the index below fails on a column that is not there, which is how this was
-- found.
ALTER TABLE raw.goods_movements ADD COLUMN IF NOT EXISTS inbound_delivery text;
ALTER TABLE raw.goods_movements ADD COLUMN IF NOT EXISTS order_number text;

CREATE UNIQUE INDEX IF NOT EXISTS goods_movements_nk
    ON raw.goods_movements (material_document, fiscal_year, material_document_item);
CREATE INDEX IF NOT EXISTS goods_movements_posting_idx
    ON raw.goods_movements (posting_date);
-- The delivery-performance join reads every receipt by delivery.
CREATE INDEX IF NOT EXISTS goods_movements_delivery_idx
    ON raw.goods_movements (inbound_delivery);
-- Order costing and the consumption-to-issue reconciliation both read issues
-- by order.
CREATE INDEX IF NOT EXISTS goods_movements_order_idx
    ON raw.goods_movements (order_number);

-- The chart of accounts (BTB-35). Master data from finance: every account the
-- postings reference, with its type and the side that normally increases it.
-- Account numbers used to be constants in Python with the type in a comment,
-- so nothing could ask whether a posting was to an asset or an expense.
CREATE TABLE IF NOT EXISTS raw.chart_of_accounts (
    gl_account       text        NOT NULL,
    account_name     text        NOT NULL,
    account_type     text        NOT NULL,
    -- DEBIT or CREDIT: the side that increases this account.
    normal_balance   text        NOT NULL,
    -- Balance sheet or income statement. A trial balance needs both and needs
    -- to know which is which.
    is_balance_sheet boolean     NOT NULL,
    account_purpose  text        NOT NULL,
    _batch_id        text        NOT NULL,
    _source_file     text        NOT NULL,
    _file_sha256     char(64)    NOT NULL,
    _loaded_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS chart_of_accounts_nk
    ON raw.chart_of_accounts (gl_account);

-- Inbound deliveries, shaped on SAP's LIKP and LIPS. An advance ship notice
-- is a promise that arrives before the goods do, so every receipt records the
-- delivery it came in on (ADR-026).
CREATE TABLE IF NOT EXISTS raw.shipment_headers (
    inbound_delivery      text        NOT NULL,
    -- Nullable on purpose: the `missing_supplier_on_shipment` defect nulls it
    -- and CTRL-009 detects it. A NOT NULL here would reject the defect at the
    -- door and the control would have nothing to find.
    supplier_number       text,
    carrier_code          text        NOT NULL,
    carrier_name          text        NOT NULL,
    shipped_date          date        NOT NULL,
    -- The supplier's own commitment: ship date plus the carrier's planned
    -- transit. Distinct from the purchase order's delivery date, so on-time
    -- delivery can be measured against either (BTB-44).
    promised_arrival_date date        NOT NULL,
    -- Null while in transit. A date here would make an unreceived shipment
    -- look delivered and count towards on-time performance.
    arrived_date          date,
    planned_transit_days  integer     NOT NULL,
    status                text        NOT NULL,
    total_quantity_shipped integer    NOT NULL,
    _batch_id             text        NOT NULL,
    _source_file          text        NOT NULL,
    _file_sha256          char(64)    NOT NULL,
    _loaded_at            timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS shipment_headers_nk
    ON raw.shipment_headers (inbound_delivery);
CREATE INDEX IF NOT EXISTS shipment_headers_supplier_idx
    ON raw.shipment_headers (supplier_number);

CREATE TABLE IF NOT EXISTS raw.shipment_lines (
    inbound_delivery  text        NOT NULL,
    shipment_item     integer     NOT NULL,
    po_number         text        NOT NULL,
    po_item           integer     NOT NULL,
    material_number   text        NOT NULL,
    quantity_shipped  integer     NOT NULL,
    uom               text        NOT NULL,
    _batch_id         text        NOT NULL,
    _source_file      text        NOT NULL,
    _file_sha256      char(64)    NOT NULL,
    _loaded_at        timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS shipment_lines_nk
    ON raw.shipment_lines (inbound_delivery, shipment_item);
CREATE INDEX IF NOT EXISTS shipment_lines_po_idx
    ON raw.shipment_lines (po_number, po_item);

-- Supplier invoices, shaped on SAP's RBKP and RSEG. The third leg of the
-- three-way match (CTRL-004).
CREATE TABLE IF NOT EXISTS raw.supplier_invoice_headers (
    invoice_number    text           NOT NULL,
    fiscal_year       integer        NOT NULL,
    supplier_number   text           NOT NULL,
    invoice_date      date           NOT NULL,
    posting_date      date           NOT NULL,
    currency          text           NOT NULL,
    gross_amount_usd  numeric(18, 2) NOT NULL,
    payment_terms     text           NOT NULL,
    entered_by        text           NOT NULL,
    _batch_id         text           NOT NULL,
    _source_file      text           NOT NULL,
    _file_sha256      char(64)       NOT NULL,
    _loaded_at        timestamptz    NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS supplier_invoice_headers_nk
    ON raw.supplier_invoice_headers (invoice_number, fiscal_year);

CREATE TABLE IF NOT EXISTS raw.supplier_invoice_lines (
    invoice_number     text           NOT NULL,
    fiscal_year        integer        NOT NULL,
    invoice_item       integer        NOT NULL,
    po_number          text           NOT NULL,
    po_item            integer        NOT NULL,
    material_number    text           NOT NULL,
    quantity_invoiced  integer        NOT NULL,
    unit_price_usd     numeric(18, 2) NOT NULL,
    net_amount_usd     numeric(18, 2) NOT NULL,
    uom                text           NOT NULL,
    _batch_id          text           NOT NULL,
    _source_file       text           NOT NULL,
    _file_sha256       char(64)       NOT NULL,
    _loaded_at         timestamptz    NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS supplier_invoice_lines_nk
    ON raw.supplier_invoice_lines (invoice_number, fiscal_year, invoice_item);
CREATE INDEX IF NOT EXISTS supplier_invoice_lines_po_idx
    ON raw.supplier_invoice_lines (po_number, po_item);

-- Production orders and their component consumption.
CREATE TABLE IF NOT EXISTS raw.production_orders (
    order_number     text        NOT NULL,
    material_number  text        NOT NULL,
    plant            text        NOT NULL,
    target_quantity  integer     NOT NULL,
    uom              text        NOT NULL,
    order_date       date        NOT NULL,
    start_date       date        NOT NULL,
    finish_date      date        NOT NULL,
    status           text        NOT NULL,
    created_by       text        NOT NULL,
    _batch_id        text        NOT NULL,
    _source_file     text        NOT NULL,
    _file_sha256     char(64)    NOT NULL,
    _loaded_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS production_orders_nk
    ON raw.production_orders (order_number);

CREATE TABLE IF NOT EXISTS raw.component_consumption (
    order_number               text        NOT NULL,
    consumption_item           integer     NOT NULL,
    component_material_number  text        NOT NULL,
    quantity_consumed          integer     NOT NULL,
    uom                        text        NOT NULL,
    movement_type              text        NOT NULL,
    posting_date               date        NOT NULL,
    entered_by                 text        NOT NULL,
    _batch_id                  text        NOT NULL,
    _source_file               text        NOT NULL,
    _file_sha256               char(64)    NOT NULL,
    _loaded_at                 timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS component_consumption_nk
    ON raw.component_consumption (order_number, consumption_item);
CREATE INDEX IF NOT EXISTS component_consumption_material_idx
    ON raw.component_consumption (component_material_number);

-- Unit-level MES events. One row per serial per step attempt -- about five
-- million rows on the full profile, loaded in chunks (ADR-024).
CREATE TABLE IF NOT EXISTS raw.mes_unit_events (
    serial_number     text        NOT NULL,
    order_number      text        NOT NULL,
    material_number   text        NOT NULL,
    step_name         text        NOT NULL,
    step_sequence     integer     NOT NULL,
    attempt           integer     NOT NULL,
    station           text        NOT NULL,
    -- Timestamps, not dates: burn-in dwell is measured in hours and cycle
    -- time is a Phase 3 KPI.
    started_at        timestamp   NOT NULL,
    finished_at       timestamp   NOT NULL,
    result            text        NOT NULL,
    -- Null on a pass.
    failure_code      text,
    is_final_attempt  boolean     NOT NULL,
    operator          text        NOT NULL,
    _batch_id         text        NOT NULL,
    _source_file      text        NOT NULL,
    _file_sha256      char(64)    NOT NULL,
    _loaded_at        timestamptz NOT NULL DEFAULT now()
);
-- The loader's anti-join probes this on every chunk, so it is not merely a
-- uniqueness constraint: at five million rows a sequential scan per chunk
-- would dominate the load.
CREATE UNIQUE INDEX IF NOT EXISTS mes_unit_events_nk
    ON raw.mes_unit_events (serial_number, step_sequence, attempt);
-- CTRL-005 groups by order; the reconciliation reads every event.
CREATE INDEX IF NOT EXISTS mes_unit_events_order_idx
    ON raw.mes_unit_events (order_number);
CREATE INDEX IF NOT EXISTS mes_unit_events_step_result_idx
    ON raw.mes_unit_events (step_name, result);

-- Production confirmations, derived from the MES outcome so CTRL-005
-- reconciles exactly on a clean run.
CREATE TABLE IF NOT EXISTS raw.production_confirmations (
    order_number         text        NOT NULL,
    confirmation_number  text        NOT NULL,
    confirmed_quantity   integer     NOT NULL,
    scrap_quantity       integer     NOT NULL,
    uom                  text        NOT NULL,
    confirmation_date    date        NOT NULL,
    posting_date         date        NOT NULL,
    entered_by           text        NOT NULL,
    _batch_id            text        NOT NULL,
    _source_file         text        NOT NULL,
    _file_sha256         char(64)    NOT NULL,
    _loaded_at           timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS production_confirmations_nk
    ON raw.production_confirmations (order_number, confirmation_number);

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
-- CREATE TABLE IF NOT EXISTS does not alter an existing table, so a
-- warehouse created before entered_on existed needs the column added.
ALTER TABLE raw.gl_postings ADD COLUMN IF NOT EXISTS entered_on date;

CREATE UNIQUE INDEX IF NOT EXISTS gl_postings_nk
    ON raw.gl_postings (accounting_document, fiscal_year, line_item);
CREATE INDEX IF NOT EXISTS gl_postings_account_posting_idx
    ON raw.gl_postings (gl_account, posting_date);

-- The monthly trial balance (BTB-35, ADR-029). One row per account per
-- period, derived from the postings so it reconciles to the cent on a clean
-- run -- which is what makes a disagreement a finding rather than noise.
CREATE TABLE IF NOT EXISTS raw.trial_balance (
    company_code        text           NOT NULL,
    fiscal_year         integer        NOT NULL,
    posting_period      text           NOT NULL,
    gl_account          text           NOT NULL,
    -- Where the period opened: the prior period's closing balance. Without it
    -- the first period starts from nothing and a monthly trial balance is a
    -- snapshot rather than a statement about a going concern.
    opening_balance_usd numeric(18, 2) NOT NULL,
    debits_usd          numeric(18, 2) NOT NULL,
    credits_usd         numeric(18, 2) NOT NULL,
    -- Signed in the debit direction, so a period's closing balances sum to
    -- zero across every account. Storing each account on its own natural side
    -- would make every consumer learn each account's sign convention.
    closing_balance_usd numeric(18, 2) NOT NULL,
    _batch_id           text           NOT NULL,
    _source_file        text           NOT NULL,
    _file_sha256        char(64)       NOT NULL,
    _loaded_at          timestamptz    NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS trial_balance_nk
    ON raw.trial_balance (company_code, fiscal_year, posting_period, gl_account);

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
