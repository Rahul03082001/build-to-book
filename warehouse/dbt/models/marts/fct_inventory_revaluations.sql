-- GRAIN: one row per revaluation document (revaluation_document, fiscal_year).
--
-- The second half of the inventory subledger (ADR-023). A revaluation moves
-- value without moving quantity, so it has an amount and no quantity measure.
-- CTRL-003 sums this alongside fct_goods_movements; omitting it lets the
-- inventory account drift away from quantity x standard cost while the
-- reconciliation still passes.
select
    {{ dbt_utils.generate_surrogate_key(['revaluation_document', 'fiscal_year']) }}
        as revaluation_key,
    revaluation_document,
    fiscal_year,
    material_number,
    old_standard_cost_usd,
    new_standard_cost_usd,
    cost_delta_usd,
    quantity_on_hand,
    amount_usd,
    posting_date,
    posting_period,
    approved_by,
    batch_id,
    source_file,
    source_file_sha256,
    loaded_at
from {{ ref('stg_inventory_revaluations') }}
