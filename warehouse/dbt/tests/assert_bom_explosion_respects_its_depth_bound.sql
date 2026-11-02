-- The recursion must not reach its own limit.
--
-- If the deepest path equals max_bom_depth, the explosion was truncated and
-- the component requirements are understated -- silently, because a truncated
-- recursion returns rows rather than raising. Catching it here means a product
-- structure that grows deeper than the bound fails the build instead of
-- quietly producing short BOMs.
select
    max(bom_level)             as deepest_level,
    {{ var('max_bom_depth') }} as depth_bound
from {{ ref('int_bom_exploded') }}
having max(bom_level) >= {{ var('max_bom_depth') }}
