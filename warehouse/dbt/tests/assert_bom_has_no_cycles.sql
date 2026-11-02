-- A BOM must not contain itself, directly or through sub-assemblies.
--
-- Tested against the RAW parent-child edges, not against int_bom_exploded's
-- output. The explosion model guards itself with a depth bound and a visited
-- path, so a cycle there produces truncated output rather than an error -- it
-- would survive silently. This looks at the data instead.
--
-- The generator builds a strict hierarchy, so this cannot fail today. It
-- exists because trusting the generator is exactly the assumption a future
-- source system, or a planted defect, would break (ADR-025).
with recursive edges as (
    select
        h.parent_material_number    as parent,
        i.component_material_number as child
    from {{ ref('stg_bom_headers') }} as h
    inner join {{ ref('stg_bom_items') }} as i on h.bom_number = i.bom_number
),

walk as (
    select
        parent               as root,
        child,
        -- `path` is a reserved word in Postgres (the geometric type), so the
        -- column is named bom_path throughout.
        array[parent, child] as bom_path,
        false                as is_cycle
    from edges

    union all

    select
        w.root,
        e.child,
        w.bom_path || e.child     as bom_path,
        e.child = any(w.bom_path) as is_cycle
    from walk as w
    inner join edges as e on w.child = e.parent
    -- Stop as soon as a cycle is seen, or the walk would not terminate.
    where
        not w.is_cycle
        and array_length(w.bom_path, 1) < {{ var('max_bom_depth') }} + 2
)

select
    root     as material_number,
    child    as revisited_material_number,
    bom_path as cycle_path
from walk
where is_cycle
