"""Stubs for dbt_utils macros, so sqlfluff can parse models that call them.

sqlfluff's jinja templater does not load dbt packages, so a model calling
`dbt_utils.generate_surrogate_key(...)` fails to template and sqlfluff reports
TMP/PRS and lints **nothing** in that file. Five of this project's marts were
being silently skipped for exactly that reason.

The alternative is sqlfluff's `dbt` templater, which compiles through dbt and
resolves every macro properly. It was rejected here because it needs a live
database connection at lint time: the CI lint job has no Postgres service, and
adding one to lint SQL style would make a fast check slow for no extra
coverage.

These stubs only need to produce *syntactically valid SQL of the right shape*.
They are never executed against the warehouse -- dbt uses the real macros --
so the returned expression just has to parse and be a single column.
"""

from __future__ import annotations

from collections.abc import Sequence


def generate_surrogate_key(field_list: Sequence[str]) -> str:
    """Stand in for dbt_utils.generate_surrogate_key.

    The real macro hashes the concatenated fields. This produces an md5 over
    the same columns, which parses identically and keeps the column count and
    type right for sqlfluff's purposes.
    """
    columns = ", ".join(str(field) for field in field_list)
    return f"md5(concat_ws('-', {columns}))"


def star(from_table: str = "", except_: Sequence[str] = ()) -> str:
    """Stand in for dbt_utils.star. Unused today; here so adding it later does
    not silently stop sqlfluff parsing a model."""
    del from_table, except_
    return "1 as placeholder"
