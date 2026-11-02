{{ config(materialized='table') }}

-- GRAIN: one row per (top-level parent, component, path through the BOM).
--
-- Explodes the parent-child BOM into total component requirements (ADR-025).
-- This is the transformation that decides every component requirement figure
-- downstream, which is exactly why it lives in reviewable SQL rather than in
-- the generator.
--
-- Quantities multiply down each path: a rack needing 3 trays, each needing 7
-- GPUs, requires 21 GPUs via that path. A component reachable by more than one
-- path -- which happens, because the same tray goes into several rack
-- configurations -- contributes once per path, and `mart` consumers sum them.
--
-- TERMINATION. A BOM that contained itself would recurse until the server
-- stopped it, taking a nightly run down with it. Two guards:
--
--   1. `depth < {{ var('max_bom_depth') }}` bounds the recursion, so a cyclic
--      extract fails fast with visibly truncated output instead of hanging.
--   2. `path` records the materials already visited, and the recursive term
--      refuses to revisit one. That stops a cycle contributing bogus
--      quantities even within the depth bound.
--
-- The generator builds a strict hierarchy so no cycle can exist today. The
-- guards are here because trusting the generator is precisely the assumption a
-- planted defect would break -- and `assert_bom_has_no_cycles` tests the raw
-- data rather than this model's output.

-- WITH RECURSIVE, not WITH. In Postgres the keyword applies to the whole
-- clause, so `edges` need not be recursive itself -- but without it the
-- recursive term cannot reference `explode` and the error ("there is a WITH
-- item named explode, but it cannot be referenced from this part of the
-- query") points at the symptom rather than the cause.
with recursive edges as (
    select
        h.parent_material_number,
        i.component_material_number,
        i.quantity_per
    from {{ ref('stg_bom_headers') }} as h
    inner join {{ ref('stg_bom_items') }} as i on h.bom_number = i.bom_number
    -- An unreleased or expired BOM must not drive requirements.
    where
        h.is_released
        and h.is_current
),

explode as (
    -- Anchor: every direct parent-child edge.
    select
        parent_material_number                                   as top_material_number,
        parent_material_number,
        component_material_number,
        quantity_per                                             as total_quantity_per,
        1                                                        as bom_level,
        array[parent_material_number, component_material_number] as bom_path
    from edges

    union all

    -- Recursive term: extend each path by one level, multiplying quantities.
    select
        e.top_material_number,
        child.parent_material_number,
        child.component_material_number,
        e.total_quantity_per * child.quantity_per     as total_quantity_per,
        e.bom_level + 1                               as bom_level,
        e.bom_path || child.component_material_number as bom_path
    from explode as e
    inner join edges as child
        on e.component_material_number = child.parent_material_number
    where e.bom_level < {{ var('max_bom_depth') }}
    -- Refuse to revisit a material already on this path.
    and not (child.component_material_number = any(e.bom_path))
)

select
    {{ dbt_utils.generate_surrogate_key([
        'top_material_number', 'component_material_number', 'bom_path'
    ]) }}                  as bom_path_key,
    top_material_number,
    parent_material_number as immediate_parent_material_number,
    component_material_number,
    total_quantity_per,
    bom_level,
    bom_path,
    -- True when the component has no BOM of its own: a leaf, and therefore a
    -- part that is bought rather than built.
    not exists (
        select 1 from edges as e2
        where e2.parent_material_number = explode.component_material_number
    )                      as is_leaf
from explode
