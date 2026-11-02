-- BOM headers. One row per BOM.
with source as (
    select * from {{ source('raw', 'bom_headers') }}
)

select
    bom_number,
    material_number              as parent_material_number,
    plant                        as plant_code,
    bom_usage,
    base_quantity,
    upper(trim(uom))             as unit_of_measure,
    valid_from,
    valid_to,
    status,
    -- Status 01 is released for production. An unreleased BOM must not drive
    -- component requirements, so the flag is derived once here.
    status = '01'                as is_released,
    valid_to = date '9999-12-31' as is_current,
    created_by,
    _batch_id                    as batch_id,
    _source_file                 as source_file,
    _file_sha256                 as source_file_sha256,
    _loaded_at                   as loaded_at
from source
