-- ADR-007's hazard, made into a test.
--
-- dim_material is SCD Type 2, so a material with two cost windows has two
-- rows. An as-of join must return exactly one of them per fact row. If the
-- windows overlap, or the join predicate is wrong, every affected fact row is
-- silently duplicated -- and the resulting inventory value is simply too big,
-- with nothing obviously wrong about it.
--
-- Counts fact rows before and after the as-of join and reports any row that
-- matched more than one dimension version.
with joined as (
    select
        f.goods_movement_key,
        count(*) as dimension_versions_matched
    from {{ ref('fct_goods_movements') }} as f
    inner join {{ ref('dim_material') }} as d
        on
            f.material_number = d.material_number
            and f.posting_date between d.valid_from and d.valid_to
    group by f.goods_movement_key
)

select
    goods_movement_key,
    dimension_versions_matched
from joined
where dimension_versions_matched <> 1
