-- Vendor master, renamed and typed. No joins.
with source as (
    select * from {{ source('raw', 'suppliers') }}
)

select
    supplier_number,
    supplier_name,
    country                as country_code,
    payment_terms,
    created_on             as created_date,
    _batch_id              as batch_id,
    _source_file           as source_file,
    _file_sha256           as source_file_sha256,
    _loaded_at             as loaded_at
from source
