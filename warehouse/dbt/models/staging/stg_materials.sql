-- Material master, renamed and typed. No joins.
with source as (
    select * from {{ source('raw', 'materials') }}
)

select
    material_number,
    material_description,
    material_group,
    -- Units of measure are standardised here so every downstream quantity is
    -- comparable. A unit-of-measure mismatch on one component is a planted
    -- defect (CTRL-011), and it is only detectable if staging has one rule.
    upper(trim(base_uom))      as base_unit_of_measure,
    plant                      as plant_code,
    created_on                 as created_date,
    _batch_id                  as batch_id,
    _source_file               as source_file,
    _file_sha256               as source_file_sha256,
    _loaded_at                 as loaded_at
from source
