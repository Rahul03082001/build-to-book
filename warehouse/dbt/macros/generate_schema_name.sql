{#
    Build each layer into its own fixed schema, optionally behind a prefix.

    Two requirements pull against each other.

    The layer schemas must be exact. `roles.sql` grants the transformer CREATE
    on `staging`, `intermediate` and `marts` by name, and dbt's default would
    turn `+schema: staging` into `publish_candidate_staging` -- a name outside
    every grant, which the transformer could not write to.

    But the Phase 6 failure demo has to run without touching live data, and
    exact schema names mean a sandbox run would rebuild the production
    `staging` and `marts`. That was not a theoretical risk: with
    BTB_TARGET_SCHEMA=sandbox, dbt still put 14 models in `staging` and 8 in
    `marts` and none in `sandbox`, while demo_failure.yml pinned the target
    schema and claimed the live data was safe.

    So the prefix is explicit rather than derived from the target. A normal run
    sets nothing and gets `staging`; the failure demo passes
    `--vars '{schema_prefix: sandbox_}'` and gets `sandbox_staging`. Making it
    a deliberate argument means a run cannot land in the wrong place by
    inheriting a target name, and a reviewer can see from the command which
    schemas a run will write.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- set prefix = var('schema_prefix', '') -%}
    {%- if custom_schema_name is none -%}
        {{ prefix }}{{ target.schema }}
    {%- else -%}
        {{ prefix }}{{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
