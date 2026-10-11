"""Test doubles.

DuckWarehouse: an in-process DuckDB database seeded with a miniature copy of the
census share (same table/column naming), so tool and agent tests run real SQL
without Snowflake. Snowflake SQL is transpiled with sqlglot.

ScriptedLLM: replays a fixed sequence of model turns and guardrail decisions, and
records the requests it received so tests can assert on history handling.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace

import duckdb
import sqlglot

from app.llm import GroundingVerdict, GuardrailDecision, LLMError, ToolCall, Turn
from app.warehouse import QueryResult, WarehouseError, WarehouseUnavailable

DB = "CENSUSDB"
SCHEMA = "PUBLIC"
YEAR = "2019"  # the older release
YEARS = ["2019", "2020"]
LATEST = "2020"
# 2020 values are the 2019 values plus this many people per block group, so tests can tell the years apart.
POP_DELTA_2020 = 10

# Two states, three counties, a handful of block groups each.
# geoid -> (total pop, pop 65+ male, pop 65+ female, households, median hh income, bachelor+ count, pop 25+)
BLOCK_GROUPS = {
    "480010001001": (1000, 60, 70, 400, 50000, 150, 650),
    "480010001002": (2000, 100, 140, 700, 62000, 300, 1300),
    "482011001001": (3000, 150, 210, 1100, 71000, 900, 2000),  # Harris County, TX
    "530330001001": (1500, 90, 110, 600, 98000, 700, 1000),  # King County, WA
    "530330001002": (500, 40, 60, 250, 120000, 260, 350),
    "530710001001": (800, 70, 90, 300, 54000, 120, 560),  # Walla Walla County, WA
}

# Same FIELD_LEVEL layout as the real share:
# kind, TABLE TITLE IN CAPS, universe, then the breakdown.
FIELDS = [
    # id, table_number, title, universe, levels...
    ("B01003e1", "B01003", "Total Population", "Total population", "Estimate", "TOTAL POPULATION", "Total population", "Total"),
    (
        "B01003m1",
        "B01003",
        "Total Population",
        "Total population",
        "MarginOfError",
        "TOTAL POPULATION",
        "Total population",
        "Total",
    ),
    (
        "B01001e20",
        "B01001",
        "Sex By Age",
        "Total population",
        "Estimate",
        "SEX BY AGE",
        "Total population",
        "Total",
        "Male",
        "65 and 66 years",
    ),
    (
        "B01001e44",
        "B01001",
        "Sex By Age",
        "Total population",
        "Estimate",
        "SEX BY AGE",
        "Total population",
        "Total",
        "Female",
        "65 and 66 years",
    ),
    (
        "B11001e1",
        "B11001",
        "Household Type (Including Living Alone)",
        "Households",
        "Estimate",
        "HOUSEHOLD TYPE (INCLUDING LIVING ALONE)",
        "Households",
        "Total",
    ),
    (
        "B19013e1",
        "B19013",
        "Median Household Income In The Past 12 Months (In 2019 Inflation-Adjusted Dollars)",
        "Households",
        "Estimate",
        "MEDIAN HOUSEHOLD INCOME IN THE PAST 12 MONTHS (IN 2019 INFLATION-ADJUSTED DOLLARS)",
        "Households",
        "Median household income in the past 12 months (in 2019 inflation-adjusted dollars)",
    ),
    (
        "B15003e22",
        "B15003",
        "Educational Attainment For The Population 25 Years And Over",
        "Population 25 years and over",
        "Estimate",
        "EDUCATIONAL ATTAINMENT FOR THE POPULATION 25 YEARS AND OVER",
        "Population 25 years and over",
        "Total",
        "Bachelor's degree",
    ),
    (
        "B15003e1",
        "B15003",
        "Educational Attainment For The Population 25 Years And Over",
        "Population 25 years and over",
        "Estimate",
        "EDUCATIONAL ATTAINMENT FOR THE POPULATION 25 YEARS AND OVER",
        "Population 25 years and over",
        "Total",
    ),
    (
        "B99999e1",
        "B99999",
        "Described But Missing",
        "Nobody",
        "Estimate",
        "DESCRIBED BUT MISSING",
        "Nobody",
        "Total",
    ),  # not present in data tables
]

FIELDS_2020_ONLY = [
    (
        "B28002e2",
        "B28002",
        "Presence And Types Of Internet Subscriptions In Household",
        "Households",
        "Estimate",
        "PRESENCE AND TYPES OF INTERNET SUBSCRIPTIONS IN HOUSEHOLD",
        "Households",
        "Total",
        "With an Internet subscription",
    ),
]

# A county that exists in the 2020 release only (Alaska's Chugach Census Area was created in 2019).
FIPS_2020_ONLY = [("AK", "02", "063", "Chugach Census Area", "H6")]

FIPS = [
    ("TX", "48", "001", "Anderson County", "H1"),
    ("TX", "48", "201", "Harris County", "H1"),
    ("WA", "53", "033", "King County", "H1"),
    ("WA", "53", "071", "Walla Walla County", "H1"),
    ("WA", "53", "075", "Whitman County", "H1"),
    ("PA", "42", "125", "Washington County", "H1"),
    ("OH", "39", "167", "Washington County", "H1"),
    ("IL", "17", "031", "Cook County", "H1"),
]


def seed(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"ATTACH ':memory:' AS {DB}")
    con.execute(f"CREATE SCHEMA {DB}.{SCHEMA}")
    con.execute(f"USE {DB}.{SCHEMA}")
    for year in YEARS:
        _seed_year(con, year)
    # Decoys that must never be loaded into the catalog or be queryable.
    con.execute('CREATE TABLE "SECRET_PAYROLL" ("NAME" VARCHAR, "SALARY" BIGINT)')
    con.execute('CREATE TABLE "2020_REDISTRICTING_CBG_DATA" ("CENSUS_BLOCK_GROUP" VARCHAR, "P0010001" BIGINT)')
    con.execute(
        'CREATE TABLE "2020_REDISTRICTING_METADATA_CBG_FIELD_DESCRIPTIONS" '
        '("FIELD_NAME" VARCHAR, "COLUMN_ID" VARCHAR, "TABLE_TITLE" VARCHAR, "TABLE_UNIVERSE" VARCHAR)'
    )


def _seed_year(con: duckdb.DuckDBPyConnection, year: str) -> None:
    delta = POP_DELTA_2020 if year == "2020" else 0
    con.execute(
        f'CREATE TABLE "{year}_CBG_B01" ("CENSUS_BLOCK_GROUP" VARCHAR, "B01003e1" BIGINT, "B01003m1" BIGINT, "B01001e20" BIGINT, "B01001e44" BIGINT)'
    )
    con.execute(f'CREATE TABLE "{year}_CBG_B11" ("CENSUS_BLOCK_GROUP" VARCHAR, "B11001e1" BIGINT)')
    con.execute(f'CREATE TABLE "{year}_CBG_B19" ("CENSUS_BLOCK_GROUP" VARCHAR, "B19013e1" BIGINT)')
    con.execute(f'CREATE TABLE "{year}_CBG_B15" ("CENSUS_BLOCK_GROUP" VARCHAR, "B15003e1" BIGINT, "B15003e22" BIGINT)')
    if year == "2020":  # a table that exists in one release only
        con.execute(f'CREATE TABLE "{year}_CBG_B28" ("CENSUS_BLOCK_GROUP" VARCHAR, "B28002e2" BIGINT)')
    for g, (pop, m65, f65, hh, inc, ba, p25) in BLOCK_GROUPS.items():
        con.execute(f'INSERT INTO "{year}_CBG_B01" VALUES (?, ?, ?, ?, ?)', [g, pop + delta, pop // 10, m65, f65])
        con.execute(f'INSERT INTO "{year}_CBG_B11" VALUES (?, ?)', [g, hh])
        con.execute(f'INSERT INTO "{year}_CBG_B19" VALUES (?, ?)', [g, inc])
        con.execute(f'INSERT INTO "{year}_CBG_B15" VALUES (?, ?, ?)', [g, p25, ba])
        if year == "2020":
            con.execute(f'INSERT INTO "{year}_CBG_B28" VALUES (?, ?)', [g, hh // 2])
    # The real share names the 9th column "FIELD_LEVELl_9" (sic); the fixture keeps that quirk.
    levels = ", ".join(f'"FIELD_LEVEL{"l" if i == 9 else ""}_{i}" VARCHAR' for i in range(1, 11))
    con.execute(
        f'CREATE TABLE "{year}_METADATA_CBG_FIELD_DESCRIPTIONS" ("TABLE_ID" VARCHAR, "TABLE_NUMBER" VARCHAR, "TABLE_TITLE" VARCHAR, "TABLE_TOPICS" VARCHAR, "TABLE_UNIVERSE" VARCHAR, {levels})'
    )
    rows = FIELDS + (FIELDS_2020_ONLY if year == "2020" else [])
    for fid, num, title, universe, *lv in rows:
        lv = (lv + [None] * 10)[:10]
        # Titles mention the release's dollar year, as in the real metadata.
        title, lv = title.replace("2019", year), [x.replace("2019", year) if isinstance(x, str) else x for x in lv]
        con.execute(
            f'INSERT INTO "{year}_METADATA_CBG_FIELD_DESCRIPTIONS" VALUES ({", ".join(["?"] * 15)})',
            [fid, num, title, "", universe, *lv],
        )
    con.execute(
        f'CREATE TABLE "{year}_METADATA_CBG_FIPS_CODES" ("STATE" VARCHAR, "STATE_FIPS" VARCHAR, "COUNTY_FIPS" VARCHAR, "COUNTY" VARCHAR, "CLASS_CODE" VARCHAR)'
    )
    for row in FIPS + (FIPS_2020_ONLY if year == "2020" else []):
        con.execute(f'INSERT INTO "{year}_METADATA_CBG_FIPS_CODES" VALUES (?, ?, ?, ?, ?)', list(row))


class DuckWarehouse:
    def __init__(self):
        self.con = duckdb.connect()
        seed(self.con)
        self.queries: list[str] = []
        self.fail_with: Exception | None = None
        self.delay_s = 0.0

    def query_sync(self, sql: str, *, max_rows: int) -> QueryResult:
        if self.fail_with:
            raise self.fail_with
        self.queries.append(sql)
        # DuckDB has no database-qualified information_schema.
        sql = re.sub(r'"[^"]+"\.information_schema', "information_schema", sql)
        try:
            duck_sql = sqlglot.transpile(sql, read="snowflake", write="duckdb")[0]
        except sqlglot.errors.ParseError:
            duck_sql = sql
        start = time.monotonic()
        if self.delay_s:
            time.sleep(self.delay_s)
        try:
            cur = self.con.execute(duck_sql)
        except duckdb.Error as e:
            raise WarehouseError(f"SQL error: {e}") from e
        rows = cur.fetchmany(max_rows + 1)
        cols = [d[0] for d in cur.description]
        return QueryResult(cols, [list(r) for r in rows[:max_rows]], len(rows) > max_rows, int((time.monotonic() - start) * 1000))

    async def query(self, sql: str, *, max_rows: int) -> QueryResult:
        return await asyncio.to_thread(self.query_sync, sql, max_rows=max_rows)


def block(type_: str, **kw) -> SimpleNamespace:
    return SimpleNamespace(type=type_, **kw)


def text_turn(text: str, stop: str = "end_turn") -> Turn:
    return Turn(content=[block("text", text=text)], stop_reason=stop, text=text, usage={"input": 10, "output": 5})


def tool_turn(*calls: tuple[str, dict], text: str = "") -> Turn:
    tcs = [ToolCall(id=f"toolu_{i}_{name}", name=name, input=args) for i, (name, args) in enumerate(calls)]
    content = ([block("text", text=text)] if text else []) + [block("tool_use", id=c.id, name=c.name, input=c.input) for c in tcs]
    return Turn(content=content, stop_reason="tool_use", text=text, tool_calls=tcs, usage={"input": 10, "output": 5})


Step = Turn | Exception | Callable[[list[dict]], Turn]


@dataclass
class ScriptedLLM:
    steps: list[Step] = field(default_factory=list)
    decision: GuardrailDecision | Exception = field(
        default_factory=lambda: GuardrailDecision(decision="answer", reason="test", reply="")
    )
    turn_delay_s: float = 0.0
    requests: list[list[dict]] = field(default_factory=list)
    classify_inputs: list[str] = field(default_factory=list)
    # Verdicts returned by verify(), in order; once used up, every answer passes.
    verdicts: list[GroundingVerdict | Exception] = field(default_factory=list)
    verify_inputs: list[str] = field(default_factory=list)
    memories_seen: list[str] = field(default_factory=list)  # the memory block passed on each model call

    async def agent_turn(self, *, system: str, messages: list[dict], tools: list[dict], on_text, memory: str = "") -> Turn:
        self.requests.append([dict(m) for m in messages])
        self.memories_seen.append(memory)
        if self.turn_delay_s:
            await asyncio.sleep(self.turn_delay_s)
        if not self.steps:
            raise AssertionError("ScriptedLLM ran out of steps")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        turn = step(messages) if callable(step) else step
        if turn.text:
            await on_text(turn.text)
        return turn

    async def verify(self, *, system: str, payload: str) -> GroundingVerdict:
        self.verify_inputs.append(payload)
        v = self.verdicts.pop(0) if self.verdicts else GroundingVerdict(supported=True, reason="test")
        if isinstance(v, Exception):
            raise v
        return v

    async def classify(self, *, system: str, transcript: str) -> GuardrailDecision:
        self.classify_inputs.append(transcript)
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision


__all__ = [
    "DB",
    "SCHEMA",
    "YEAR",
    "DuckWarehouse",
    "GuardrailDecision",
    "LLMError",
    "ScriptedLLM",
    "WarehouseUnavailable",
    "text_turn",
    "tool_turn",
]
