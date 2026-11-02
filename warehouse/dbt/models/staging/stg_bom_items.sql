-- BOM items. One row per BOM line.
--
-- A parent-child edge: this BOM's parent requires `quantity_per` of
-- `component_material_number`. Nothing here resolves the hierarchy -- that is
-- int_bom_exploded's job, in reviewable SQL (ADR-025).
with source as (
    select * from {{ source('raw', 'bom_items') }}
)

select
    bom_number,
    bom_item,
    component_material_number,
    quantity_per,
    upper(trim(uom)) as unit_of_measure,
    is_phantom,
    _batch_id        as batch_id,
    _source_file     as source_file,
    _file_sha256     as source_file_sha256,
    _loaded_at       as loaded_at
from source
