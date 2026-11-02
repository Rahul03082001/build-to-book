-- GRAIN: one row per material, per standard cost validity window.
--
-- SCD Type 2 (ADR-007): a material with two cost windows has two rows. That
-- is deliberate -- it lets a past period be valued at the cost actually in
-- force rather than today's -- but it makes this dimension unsafe to join on
-- material_number alone. Any join must be as-of:
--
--     on  d.material_number = f.material_number
--     and f.posting_date between d.valid_from and d.valid_to
--
-- A plain equality join multiplies fact rows by the number of windows, which
-- is the classic Type 2 bug. `dim_material_current` exists for consumers that
-- only want today's view and should not have to get the as-of join right.
with materials as (
    select * from {{ ref('stg_materials') }}
),

costs as (
    select * from {{ ref('stg_standard_costs') }}
)

select
    {{ dbt_utils.generate_surrogate_key(['materials.material_number', 'costs.valid_from']) }}
        as material_version_key,
    materials.material_number,
    materials.material_description,
    materials.material_group,
    materials.base_unit_of_measure,
    materials.plant_code,
    materials.created_date,
    costs.valid_from,
    costs.valid_to,
    costs.standard_cost_usd,
    costs.currency_code,
    costs.approved_by,
    costs.approved_date,
    costs.is_current,
    materials.batch_id,
    materials.source_file,
    materials.loaded_at
from materials
inner join costs
    on materials.material_number = costs.material_number
