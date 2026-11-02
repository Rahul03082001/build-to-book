-- GRAIN: one row per material document LINE (material_document, fiscal_year,
-- material_document_item).
--
-- Not one row per document. A material document can carry several items, so
-- a count of documents and a count of lines are different numbers -- which is
-- why the grain is stated here rather than left for a consumer to infer.
--
-- The fiscal year is part of the key because document numbers legitimately
-- repeat across years (ADR-020).
with movements as (
    select * from {{ ref('stg_goods_movements') }}
)

select
    {{ dbt_utils.generate_surrogate_key([
        'material_document', 'fiscal_year', 'material_document_item'
    ]) }}                                  as goods_movement_key,

    -- Degenerate dimensions: the document identity itself.
    material_document,
    material_document_item,
    fiscal_year,

    movement_type,
    movement_direction,

    -- Foreign keys.
    material_number,
    plant_code,
    po_number,
    po_item,

    -- Measures. Quantity and amount are already signed by movement type, so
    -- a period's net movement is sum(), not a conditional.
    quantity,
    amount_usd,
    unit_of_measure,

    posting_date,
    document_date,
    posting_period,
    entered_by,

    -- Provenance carried through, so any number on a dashboard traces to the
    -- file and run that produced it (ADR-008).
    batch_id,
    source_file,
    source_file_sha256,
    loaded_at
from movements
