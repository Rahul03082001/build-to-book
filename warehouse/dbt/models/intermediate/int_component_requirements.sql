{{ config(materialized='table') }}

-- GRAIN: one row per (top-level material, leaf component).
--
-- Total quantity of each bought part needed to build one of a top-level
-- material, summed across every path that reaches it. Built on
-- int_bom_exploded, which holds one row per path.
--
-- Only leaves are counted. Including sub-assemblies would double-count: a rack
-- needs 3 trays AND the 21 GPUs inside them, so summing both would claim the
-- rack needs 24 "things" of which 3 are containers for the other 21.
select
    {{ dbt_utils.generate_surrogate_key([
        'top_material_number', 'component_material_number'
    ]) }}                   as component_requirement_key,
    top_material_number,
    component_material_number,
    sum(total_quantity_per) as quantity_required,
    count(*)                as path_count,
    max(bom_level)          as deepest_level
from {{ ref('int_bom_exploded') }}
where is_leaf
group by top_material_number, component_material_number
