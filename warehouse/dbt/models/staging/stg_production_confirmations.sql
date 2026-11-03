-- Production confirmations. One row per confirmation.
with source as (
    select * from {{ source('raw', 'production_confirmations') }}
)

select
    order_number,
    confirmation_number,
    confirmed_quantity,
    scrap_quantity,
    -- Units the order accounted for. A unit that was neither confirmed nor
    -- scrapped has vanished, and CTRL-005 is what notices.
    confirmed_quantity + scrap_quantity as accounted_quantity,
    upper(trim(uom))                    as unit_of_measure,
    confirmation_date,
    posting_date,
    to_char(posting_date, 'YYYY-MM')    as posting_period,
    entered_by,
    _batch_id                           as batch_id,
    _source_file                        as source_file,
    _file_sha256                        as source_file_sha256,
    _loaded_at                          as loaded_at
from source
