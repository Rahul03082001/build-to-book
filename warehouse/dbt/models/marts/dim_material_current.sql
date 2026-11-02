-- GRAIN: one row per material.
--
-- The current view of the material master, for consumers that do not need
-- history. Exists so an analyst who joins on material_number alone gets one
-- row instead of silently multiplying their numbers against dim_material's
-- Type 2 versions.
select
    material_number,
    material_description,
    material_group,
    base_unit_of_measure,
    plant_code,
    created_date,
    standard_cost_usd as current_standard_cost_usd,
    currency_code,
    approved_by       as current_cost_approved_by,
    valid_from        as current_cost_valid_from
from {{ ref('dim_material') }}
where is_current
