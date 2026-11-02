-- GRAIN: one row per calendar date.
--
-- Spans the posting dates actually present in the data, padded to whole
-- months so a period join never half-covers a month. Built from the data
-- rather than a hardcoded range, so it cannot silently fail to cover a
-- posting date -- a missing date row drops a fact from any inner join to it.
with bounds as (
    select
        date_trunc('month', min(posting_date))::date                        as first_day,
        (date_trunc('month', max(posting_date)) + interval '1 month - 1 day')::date as last_day
    from {{ ref('stg_goods_movements') }}
),

spine as (
    select generate_series(
        (select first_day from bounds),
        (select last_day from bounds),
        interval '1 day'
    )::date as date_day
)

select
    date_day,
    to_char(date_day, 'YYYY-MM')              as period,
    extract(year from date_day)::int          as calendar_year,
    extract(month from date_day)::int         as calendar_month,
    extract(day from date_day)::int           as day_of_month,
    extract(isodow from date_day)::int        as iso_day_of_week,
    extract(isodow from date_day) <= 5        as is_weekday,
    date_trunc('month', date_day)::date       as first_day_of_month,
    (date_trunc('month', date_day) + interval '1 month - 1 day')::date as last_day_of_month,
    date_day = (date_trunc('month', date_day) + interval '1 month - 1 day')::date
        as is_last_day_of_month
from spine
