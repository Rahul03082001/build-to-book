-- Unit-level test events. One row per serial per step attempt.
--
-- The derived columns exist so the Phase 3 yield and cycle-time metrics do not
-- each reinvent them -- and so "first attempt" means one thing across the
-- warehouse rather than whatever each model decided.
with source as (
    select * from {{ source('raw', 'mes_unit_events') }}
)

select
    serial_number,
    order_number,
    material_number,
    step_name,
    step_sequence,
    attempt,
    station,
    started_at,
    finished_at,
    result,
    failure_code,
    is_final_attempt,
    operator,

    result = 'PASS'                                         as passed,
    attempt = 1                                             as is_first_attempt,
    -- First-pass yield counts only units that passed on their first attempt;
    -- a reworked unit that later passes is not a first-pass success.
    result = 'PASS' and attempt = 1                         as passed_first_time,
    step_name = '{{ var('mes_final_step') }}'               as is_final_step,

    -- Dwell on this attempt, in hours. Burn-in dominates the route, which is
    -- why cycle time is measured in hours rather than days.
    extract(epoch from (finished_at - started_at)) / 3600.0 as dwell_hours,

    started_at::date                                        as event_date,
    to_char(started_at, 'YYYY-MM')                          as event_period,
    _batch_id                                               as batch_id,
    _source_file                                            as source_file,
    _file_sha256                                            as source_file_sha256,
    _loaded_at                                              as loaded_at
from source
