{#
    Use the configured schema name verbatim instead of prefixing it with the
    target schema.

    dbt's default would turn `+schema: staging` against a target schema of
    `publish_candidate` into `publish_candidate_staging`. This project's layers
    are fixed schemas that roles are granted on individually
    (warehouse/sql/roles.sql), so the names must be exactly `staging`,
    `intermediate` and `marts` -- a prefixed name would land outside every
    grant and the transformer could not write to it.

    A model with no `+schema` falls back to the target schema, which is what
    the publish gate switches between `publish_candidate` and `sandbox`
    (ADR-021).
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
