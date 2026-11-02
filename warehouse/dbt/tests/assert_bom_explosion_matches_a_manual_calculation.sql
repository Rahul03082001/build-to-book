-- The explosion's arithmetic, checked against an independent calculation.
--
-- int_component_requirements is derived by recursion; this derives the same
-- figures by explicit two-level joins, which is how a person would work them
-- out by hand. If the recursion multiplies, dedupes or filters wrongly, the
-- two disagree.
--
-- Deliberately NOT written in terms of the model under test. A check that
-- reused the recursive CTE would pass whatever the recursion did -- the same
-- trap as a reconciliation whose two sides share a common error (ADR-023).
--
-- Only covers depth <= 2, which is the generator's product structure today. A
-- deeper BOM would not be verified here, which is why the depth bound and the
-- cycle test exist alongside it.
with edges as (
    select
        h.parent_material_number    as parent,
        i.component_material_number as child,
        i.quantity_per              as qty
    from {{ ref('stg_bom_headers') }} as h
    inner join {{ ref('stg_bom_items') }} as i on h.bom_number = i.bom_number
    where h.is_released and h.is_current
),

tops as (
    select distinct top_material_number from {{ ref('int_bom_exploded') }}
),

-- A rack child with no BOM of its own is a bought part: take it as-is.
direct as (
    select
        t.top_material_number,
        e.child as component,
        e.qty   as expected
    from tops as t
    inner join edges as e on t.top_material_number = e.parent
    where not exists (
        select 1 from edges as sub
        where sub.parent = e.child
    )
),

-- A rack child that has a BOM is a sub-assembly: multiply through it.
nested as (
    select
        t.top_material_number,
        inner_edge.child                     as component,
        sum(outer_edge.qty * inner_edge.qty) as expected
    from tops as t
    inner join edges as outer_edge on t.top_material_number = outer_edge.parent
    inner join edges as inner_edge on outer_edge.child = inner_edge.parent
    group by t.top_material_number, inner_edge.child
),

expected as (
    select
        top_material_number,
        component,
        sum(expected) as expected_quantity
    from (
        select * from direct
        union all
        select * from nested
    ) as combined
    group by top_material_number, component
)

select
    coalesce(e.top_material_number, m.top_material_number) as top_material_number,
    coalesce(e.component, m.component_material_number)     as component_material_number,
    e.expected_quantity,
    m.quantity_required                                    as model_quantity,
    e.expected_quantity - m.quantity_required              as difference
from expected as e
full outer join {{ ref('int_component_requirements') }} as m
    on
        e.top_material_number = m.top_material_number
        and e.component = m.component_material_number
where e.expected_quantity is distinct from m.quantity_required
