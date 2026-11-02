-- CTRL-002 in miniature, for the inventory slice.
--
-- Raw-to-mart completeness by key: a row present in raw must appear exactly
-- once in the fact, and the fact must invent nothing. A join that changes
-- grain shows up here as a count mismatch rather than as a quietly inflated
-- metric three models downstream.
with raw_count as (
    select count(*) as n from {{ source('raw', 'goods_movements') }}
),

fact_count as (
    select count(*) as n from {{ ref('fct_goods_movements') }}
),

distinct_keys as (
    select count(distinct goods_movement_key) as n from {{ ref('fct_goods_movements') }}
)

select
    raw_count.n  as raw_rows,
    fact_count.n as fact_rows,
    distinct_keys.n as distinct_fact_keys
from raw_count, fact_count, distinct_keys
where raw_count.n <> fact_count.n
   or fact_count.n <> distinct_keys.n
