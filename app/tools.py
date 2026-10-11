"""Tools the agent can call, plus the code that executes them.

Each tool returns a JSON string for the model. Failures come back as
`ToolOutput(is_error=True)` with a message the model can act on (for example a
SQL compile error), not as exceptions. The exception is `WarehouseUnavailable`,
which ends the turn because retrying won't help.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .catalog import Catalog
from .geography import Geography
from .sql_guard import SqlRejected, validate
from .warehouse import Warehouse, WarehouseError, WarehouseUnavailable

log = logging.getLogger(__name__)


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


TOOL_DEFS: list[dict] = [
    _tool(
        "search_fields",
        "Keyword search over every ACS field in the dataset (labels, table titles, universes). Returns field ids, "
        "labels and the physical table holding each. Use this to find the right column before writing SQL.",
        {
            "query": {
                "type": "string",
                "description": "What you are looking for, e.g. 'median household income' or 'bachelor degree women'.",
            },
            "include_margin_of_error": {
                "type": "boolean",
                "description": "Also return margin-of-error (m) fields. Default false.",
            },
        },
        ["query"],
    ),
    _tool(
        "describe_table",
        "List every estimate field in one ACS table (e.g. 'B01001'), with labels and universe. Use this when you need the "
        "full breakdown of a table, e.g. all age brackets.",
        {"table_number": {"type": "string", "description": "ACS table number such as B01001 or B19013."}},
        ["table_number"],
    ),
    _tool(
        "resolve_geography",
        "Resolve a US state or county name to its GEOID prefix and a ready-to-use SQL filter on CENSUS_BLOCK_GROUP. "
        "Returns several candidates if the name is ambiguous. Cities, ZIP codes and neighborhoods are not available. "
        "For one or a few named places only; to put names on many result rows, join the FIPS codes table in SQL.",
        {"place": {"type": "string", "description": "e.g. 'Texas', 'Cook County, IL', 'King County WA', 'United States'."}},
        ["place"],
    ),
    _tool(
        "run_sql",
        "Run one read-only Snowflake SELECT against the census tables and return the rows. Double-quote all identifiers.",
        {
            "sql": {"type": "string", "description": "A single SELECT statement."},
            "purpose": {
                "type": "string",
                "description": "One short phrase describing what this query computes, shown to the user.",
            },
        },
        ["sql", "purpose"],
    ),
]


@dataclass
class ToolOutput:
    content: str
    is_error: bool = False
    # Extra info for the UI / logs, never sent to the model.
    meta: dict[str, Any] = field(default_factory=dict)


class ToolExecutor:
    def __init__(self, catalog: Catalog, geography: Geography, warehouse: Warehouse, *, max_rows: int):
        self.catalog = catalog
        self.geo = geography
        self.wh = warehouse
        self.max_rows = max_rows

    async def run(self, name: str, args: Any) -> ToolOutput:
        if not isinstance(args, dict):
            return ToolOutput(json.dumps({"error": "Tool input must be a JSON object."}), is_error=True)
        try:
            if name == "search_fields":
                return self._search_fields(_str(args, "query"), bool(args.get("include_margin_of_error", False)))
            if name == "describe_table":
                return self._describe_table(_str(args, "table_number"))
            if name == "resolve_geography":
                return self._resolve_geography(_str(args, "place"))
            if name == "run_sql":
                return await self._run_sql(_str(args, "sql"), str(args.get("purpose", "")))
            return ToolOutput(json.dumps({"error": f"Unknown tool {name!r}."}), is_error=True)
        except _BadArgs as e:
            return ToolOutput(json.dumps({"error": str(e)}), is_error=True)

    # ------------------------------------------------------------------
    def _search_fields(self, query: str, include_moe: bool) -> ToolOutput:
        hits = self.catalog.search_fields(query, limit=15, include_moe=include_moe)
        if not hits:
            return ToolOutput(
                json.dumps({"results": [], "note": "No matching fields. Try different words or check the table list."})
            )
        out = []
        for f in hits:
            t = self.catalog.tables[f.table_number]
            out.append(
                {
                    "field": f.id,
                    "label": f.label,
                    "table": f"{t.number} {t.title}",
                    "universe": t.universe,
                    "physical_tables": t.physical_tables,
                    "years": f.years,
                }
            )
        return ToolOutput(json.dumps({"results": out}))

    def _describe_table(self, number: str) -> ToolOutput:
        t = self.catalog.tables.get(number.strip().upper())
        if t is None:
            return ToolOutput(json.dumps({"error": f"No ACS table {number!r} in this dataset."}), is_error=True)
        fields = [
            {"field": fid, "label": self.catalog.fields[fid].label}
            for fid in t.field_ids
            if self.catalog.fields[fid].kind == "estimate"
        ]
        payload = {
            "table": t.number,
            "title": t.title,
            "universe": t.universe,
            "physical_tables": t.physical_tables,
            "fields": fields[:150],
        }
        if len(fields) > 150:
            payload["note"] = f"Showing 150 of {len(fields)} fields; use search_fields to narrow down."
        return ToolOutput(json.dumps(payload))

    def _resolve_geography(self, place: str) -> ToolOutput:
        matches = self.geo.resolve(place)
        if not matches:
            return ToolOutput(
                json.dumps(
                    {
                        "matches": [],
                        "note": "No state or county by that name. The dataset has no city/ZIP/neighborhood geography; "
                        "if this is a city, ask the user which county they mean or offer the county that contains it.",
                    }
                )
            )
        payload: dict[str, Any] = {"matches": [m.as_dict() for m in matches[:10]]}
        if len(matches) > 1:
            payload["note"] = f"{len(matches)} candidates; ask the user which one they mean unless context makes it clear."
        return ToolOutput(json.dumps(payload))

    async def _run_sql(self, sql: str, purpose: str) -> ToolOutput:
        try:
            v = validate(
                sql,
                allowed_tables=self.catalog.allowed_tables,
                database=self.catalog.database,
                schema=self.catalog.schema,
                max_rows=self.max_rows,
            )
        except SqlRejected as e:
            return ToolOutput(
                json.dumps({"error": f"Query rejected: {e}"}),
                is_error=True,
                meta={"sql": sql, "purpose": purpose, "rejected": True},
            )
        try:
            res = await self.wh.query(v.sql, max_rows=self.max_rows)
        except WarehouseUnavailable:
            raise
        except WarehouseError as e:
            return ToolOutput(
                json.dumps({"error": e.user_safe}),
                is_error=True,
                meta={"sql": v.display_sql, "purpose": purpose, "capped": v.capped},
            )
        log.info("sql ok", extra={"sql": v.sql, "rows": len(res.rows), "ms": res.elapsed_ms, "truncated": res.truncated})
        payload: dict[str, Any] = {"columns": res.columns, "rows": res.rows, "row_count": len(res.rows)}
        if res.truncated:
            payload["note"] = f"Result truncated to {self.max_rows} rows. Aggregate further if you need the full picture."
        if not res.rows:
            payload["note"] = "Query returned no rows. Check the geography filter and field names."
        return ToolOutput(
            json.dumps(payload, default=str),
            meta={"sql": v.display_sql, "purpose": purpose, "rows": len(res.rows), "ms": res.elapsed_ms, "capped": v.capped},
        )


class _BadArgs(ValueError):
    pass


def _str(args: dict, key: str) -> str:
    v = args.get(key)
    if not isinstance(v, str) or not v.strip():
        raise _BadArgs(f"'{key}' must be a non-empty string.")
    return v
