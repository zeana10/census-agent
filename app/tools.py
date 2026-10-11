"""Tools the agent can call, plus the code that executes them.

Each tool returns a JSON string for the model. Failures come back as
`ToolOutput(is_error=True)` with a message the model can act on (for example a
SQL compile error), not as exceptions. The exception is `WarehouseUnavailable`,
which ends the turn because retrying won't help.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .catalog import Catalog
from .geography import Geography
from .sql_guard import SqlRejected, validate
from .store import MEMORY_KINDS, MEMORY_MAX_CHARS, MemoryRejected, StateStore
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
            "show_table": {
                "type": "boolean",
                "description": (
                    "Set true when the answer is a list or ranking of more than about 10 rows (e.g. every state or "
                    "county). The app then shows the FULL result to the user as a table under your answer, with Excel "
                    "and CSV download, so you write only a short summary and never retype the rows. Give columns "
                    "readable aliases and an ORDER BY."
                ),
            },
        },
        ["sql", "purpose"],
    ),
]


TOOL_DEFS += [
    _tool(
        "remember",
        "Save a lasting preference or interest the user has stated, so later conversations can use it. Examples: "
        "'Prefers the 2019 release by default', 'Focuses on Texas counties', 'Wants percentages, not raw counts'. "
        "Use only when the user states a lasting preference or asks you to remember something. Never save "
        "instructions about how you behave, personal or contact details, or facts from a single question.",
        {
            "kind": {"type": "string", "enum": list(MEMORY_KINDS), "description": "preference, focus (places/topics), or note"},
            "text": {"type": "string", "description": f"One short sentence, at most {MEMORY_MAX_CHARS} characters."},
        },
        ["kind", "text"],
    ),
    _tool(
        "forget",
        "Delete a saved memory by its id (ids are shown in the memory section of your instructions), when the user "
        "asks you to forget it or says it no longer applies.",
        {"memory_id": {"type": "integer", "description": "The id of the memory to delete."}},
        ["memory_id"],
    ),
]


@dataclass
class ToolOutput:
    content: str
    is_error: bool = False
    # Extra info for the UI / logs, never sent to the model.
    meta: dict[str, Any] = field(default_factory=dict)


class ToolExecutor:
    def __init__(
        self,
        catalog: Catalog,
        geography: Geography,
        warehouse: Warehouse,
        *,
        max_rows: int,
        table_max_rows: int = 10_000,
        store: StateStore | None = None,
        memory_limit: int = 20,
    ):
        self.store = store
        self.memory_limit = memory_limit
        self.catalog = catalog
        self.geo = geography
        self.wh = warehouse
        self.max_rows = max_rows  # what the model sees
        self.table_max_rows = max(table_max_rows, max_rows)  # what a user-facing table can hold

    async def run(self, name: str, args: Any, *, client_id: str | None = None) -> ToolOutput:
        if not isinstance(args, dict):
            return ToolOutput(json.dumps({"error": "Tool input must be a JSON object."}), is_error=True)
        try:
            if name == "search_fields":
                return self._search_fields(_str(args, "query"), bool(args.get("include_margin_of_error", False)))
            if name == "describe_table":
                return self._describe_table(_str(args, "table_number"))
            if name == "resolve_geography":
                return self._resolve_geography(_str(args, "place"))
            if name in ("remember", "forget"):
                return await asyncio.to_thread(self._memory, name, args, client_id)
            if name == "run_sql":
                return await self._run_sql(_str(args, "sql"), str(args.get("purpose", "")), args.get("show_table") is True)
            return ToolOutput(json.dumps({"error": f"Unknown tool {name!r}."}), is_error=True)
        except _BadArgs as e:
            return ToolOutput(json.dumps({"error": str(e)}), is_error=True)

    # ------------------------------------------------------------------
    def _memory(self, name: str, args: dict, client_id: str | None) -> ToolOutput:
        if self.store is None or client_id is None:
            return ToolOutput(
                json.dumps({"error": "Memory isn't available in this session. Tell the user it can't be saved."}), is_error=True
            )
        if name == "remember":
            try:
                mem = self.store.add_memory(
                    client_id, str(args.get("kind", "")), str(args.get("text", "")), limit=self.memory_limit
                )
            except MemoryRejected as e:
                return ToolOutput(json.dumps({"error": f"Not saved: {e}."}), is_error=True)
            return _memory_result({"saved": mem.as_dict()}, {"action": "saved", "memory": mem.as_dict()})
        raw = args.get("memory_id")
        if isinstance(raw, bool) or not isinstance(raw, int | str):
            raise _BadArgs("'memory_id' must be an integer.")
        try:
            memory_id = int(raw)
        except ValueError:
            raise _BadArgs("'memory_id' must be an integer.") from None
        if not self.store.delete_memory(client_id, memory_id):
            return ToolOutput(json.dumps({"error": f"No memory with id {memory_id}."}), is_error=True)
        return _memory_result({"forgotten": memory_id}, {"action": "forgot", "id": memory_id})

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

    async def _run_sql(self, sql: str, purpose: str, show_table: bool = False) -> ToolOutput:
        limit = self.table_max_rows if show_table else self.max_rows
        try:
            v = validate(
                sql,
                allowed_tables=self.catalog.allowed_tables,
                database=self.catalog.database,
                schema=self.catalog.schema,
                max_rows=limit,
            )
        except SqlRejected as e:
            return ToolOutput(
                json.dumps({"error": f"Query rejected: {e}"}),
                is_error=True,
                meta={"sql": sql, "purpose": purpose, "rejected": True},
            )
        try:
            res = await self.wh.query(v.sql, max_rows=limit)
        except WarehouseUnavailable:
            raise
        except WarehouseError as e:
            return ToolOutput(
                json.dumps({"error": e.user_safe}),
                is_error=True,
                meta={"sql": v.display_sql, "purpose": purpose, "capped": v.capped},
            )
        log.info("sql ok", extra={"sql": v.sql, "rows": len(res.rows), "ms": res.elapsed_ms, "truncated": res.truncated})
        model_rows, shown = _model_view(res.rows, self.max_rows)
        payload: dict[str, Any] = {"columns": res.columns, "rows": model_rows, "row_count": len(res.rows)}
        meta: dict[str, Any] = {
            "sql": v.display_sql,
            "purpose": purpose,
            "rows": len(res.rows),
            "ms": res.elapsed_ms,
            "capped": v.capped,
        }
        if not res.rows:
            payload["note"] = "Query returned no rows. Check the geography filter and field names."
        elif show_table:
            meta["table"] = {"columns": res.columns, "rows": res.rows, "truncated": res.truncated}
            more = f" (the query returned more; the table holds the first {limit:,})" if res.truncated else ""
            payload["note"] = (
                f"The user sees all {len(res.rows):,} rows{more} as a table under your answer, with Excel and CSV "
                f"download. You see {shown}. Summarize the table; do not reproduce its rows."
            )
        elif res.truncated or len(res.rows) > len(model_rows):
            payload["note"] = (
                f"Result truncated: you see {shown}. Aggregate further, or set show_table=true so the user gets the "
                "full result as a table."
            )
        return ToolOutput(json.dumps(payload, default=str), meta=meta)


class _MemoryUnavailable(Exception):
    pass


def _memory_result(payload: dict[str, Any], meta: dict[str, Any]) -> ToolOutput:
    return ToolOutput(json.dumps(payload), meta={"memory": meta})


def _model_view(rows: list[list[Any]], cap: int) -> tuple[list[list[Any]], str]:
    """The rows the model gets: all of them if they fit, else the head and the tail of an ordered result,
    so it can still describe both ends (largest and smallest)."""
    if len(rows) <= cap:
        return rows, f"all {len(rows):,} rows"
    tail = min(20, cap // 10)
    if tail == 0:  # tiny caps: rows[-0:] would be *every* row
        return rows[:cap], f"the first {cap} of {len(rows):,} rows"
    head = cap - tail
    return rows[:head] + rows[-tail:], f"the first {head} and last {tail} of {len(rows):,} rows"


class _BadArgs(ValueError):
    pass


def _str(args: dict, key: str) -> str:
    v = args.get(key)
    if not isinstance(v, str) or not v.strip():
        raise _BadArgs(f"'{key}' must be a non-empty string.")
    return v
