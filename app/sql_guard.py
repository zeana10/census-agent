"""Static validation of model-written SQL before it reaches Snowflake.

This is the first of three layers (see warehouse.py). It is deliberately strict:
a rejected query costs one extra model step, an accepted bad query can cost
money or leak data from outside the census share.

Rules:
  * exactly one statement, and it must be a read-only query (SELECT / WITH / UNION);
  * every referenced table must be in the catalog's allow-list (CTE names excepted)
    and may not be qualified with another database/schema;
  * no table functions, no SYSTEM$ / RESULT_SCAN style functions, no INFORMATION_SCHEMA;
  * a LIMIT of max_rows + 1 is added (or tightened) on the outermost query, so the
    caller can tell "exactly max_rows" from "truncated".
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError


class SqlRejected(Exception):
    pass


_BANNED_FUNC_PREFIXES = ("SYSTEM$", "RESULT_SCAN", "GENERATOR", "FLATTEN", "GET_DDL", "CURRENT_", "IDENTIFIER", "TABLE")
_READ_ONLY_ROOTS = (exp.Select, exp.Union, exp.Except, exp.Intersect)
_WRITE_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter, exp.Command, exp.Into)


@dataclass
class ValidatedSql:
    sql: str  # what runs: normalized, with the row cap applied
    tables: list[str]
    display_sql: str = ""  # what the query asked for, before the safety cap (shown to users)
    capped: bool = False  # True when the cap was added or tightened


def validate(sql: str, *, allowed_tables: set[str], database: str, schema: str, max_rows: int) -> ValidatedSql:
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        raise SqlRejected("Empty query.")
    if len(text) > 20_000:
        raise SqlRejected("Query is too long.")
    try:
        statements = [s for s in sqlglot.parse(text, read="snowflake") if s is not None]
    except ParseError as e:
        raise SqlRejected(f"Could not parse SQL: {str(e).splitlines()[0][:300]}") from e
    if len(statements) != 1:
        raise SqlRejected("Exactly one SQL statement is allowed.")
    tree = statements[0]

    if not isinstance(tree, _READ_ONLY_ROOTS):
        raise SqlRejected(f"Only SELECT queries are allowed (got {tree.key.upper()}).")
    if any(tree.find_all(*_WRITE_NODES)):
        raise SqlRejected("Only read-only SELECT queries are allowed.")

    cte_names = {c.alias_or_name.upper() for c in tree.find_all(exp.CTE)}
    allowed_upper = {t.upper(): t for t in allowed_tables}
    used: list[str] = []
    for t in tree.find_all(exp.Table):
        if isinstance(t.this, exp.Anonymous) or t.find(exp.Anonymous, exp.Func):
            raise SqlRejected("Table functions are not allowed.")
        name = t.name
        if not name:
            raise SqlRejected("Unsupported table expression.")
        if t.args.get("db") or t.args.get("catalog"):
            db = (t.text("catalog") or database).upper()
            sch = (t.text("db") or schema).upper()
            if db != database.upper() or sch != schema.upper():
                raise SqlRejected(f"Table {t.sql(dialect='snowflake')} is outside the census dataset.")
        if name.upper() in cte_names and not t.args.get("db"):
            continue
        if name.upper() not in allowed_upper:
            raise SqlRejected(
                f'Unknown table "{name}". Use only the census tables listed in the schema '
                '(remember to double-quote names that start with a digit, e.g. "2019_CBG_B01").'
            )
        used.append(allowed_upper[name.upper()])

    if not used:
        raise SqlRejected("The query must read from at least one census table.")

    for fn in tree.find_all(exp.Func):
        fname = (fn.name if isinstance(fn, exp.Anonymous) else fn.sql_name()).upper()
        if fname.startswith(_BANNED_FUNC_PREFIXES):
            raise SqlRejected(f"Function {fname} is not allowed.")

    # Enforce a row cap on the outermost query.
    limit = tree.args.get("limit")
    current = None
    if isinstance(limit, exp.Limit):
        try:
            current = int(limit.expression.name)
        except (TypeError, ValueError, AttributeError):
            current = None
    display_sql = tree.sql(dialect="snowflake", pretty=True)
    cap = max_rows + 1
    capped = current is None or current > cap
    if capped:
        tree = (
            tree.limit(cap, copy=False)
            if isinstance(tree, exp.Select)
            else (exp.select("*").from_(tree.subquery("q")).limit(cap))
        )

    return ValidatedSql(sql=tree.sql(dialect="snowflake"), tables=sorted(set(used)), display_sql=display_sql, capped=capped)
