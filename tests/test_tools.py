import json

import pytest

from app.warehouse import WarehouseError, WarehouseUnavailable

from .fakes import POP_DELTA_2020

pytestmark = pytest.mark.asyncio


async def test_search_reports_years_per_field(executor):
    body = json.loads((await executor.run("search_fields", {"query": "internet subscription"})).content)
    hit = next(r for r in body["results"] if r["field"] == "B28002e2")
    assert hit["years"] == ["2020"] and hit["physical_tables"] == {"2020": "2020_CBG_B28"}


async def test_both_years_are_queryable_and_differ(executor):
    sql = 'SELECT SUM("B01003e1") FROM "{y}_CBG_B01" WHERE LEFT("CENSUS_BLOCK_GROUP", 2) = \'48\''
    rows = {}
    for y in ("2019", "2020"):
        out = await executor.run("run_sql", {"sql": sql.format(y=y), "purpose": y})
        rows[y] = json.loads(out.content)["rows"][0][0]
    assert rows == {"2019": 6000, "2020": 6000 + 3 * POP_DELTA_2020}


async def test_redistricting_table_is_not_queryable_yet(executor):
    out = await executor.run("run_sql", {"sql": 'SELECT SUM("P0010001") FROM "2020_REDISTRICTING_CBG_DATA"', "purpose": "x"})
    assert out.is_error and "Unknown table" in out.content


async def test_run_sql_returns_rows(executor):
    out = await executor.run(
        "run_sql",
        {
            "sql": 'SELECT SUM("B01003e1") AS pop FROM "2019_CBG_B01" WHERE LEFT("CENSUS_BLOCK_GROUP", 2) = \'48\'',
            "purpose": "Texas population",
        },
    )
    assert not out.is_error
    body = json.loads(out.content)
    assert body["rows"] == [[6000]]
    assert out.meta["purpose"] == "Texas population"


async def test_run_sql_rejection_is_reported_to_model_not_raised(executor, warehouse):
    out = await executor.run("run_sql", {"sql": 'SELECT * FROM "SECRET_PAYROLL"', "purpose": "x"})
    assert out.is_error and "Unknown table" in json.loads(out.content)["error"]
    assert warehouse.queries == []  # never reached the database


async def test_sql_error_is_fed_back(executor):
    out = await executor.run("run_sql", {"sql": 'SELECT "NOPE" FROM "2019_CBG_B01"', "purpose": "x"})
    assert out.is_error and "SQL error" in json.loads(out.content)["error"]


async def test_truncation_and_empty_results_are_explained(executor):
    out = await executor.run("run_sql", {"sql": 'SELECT * FROM "2019_CBG_B01" WHERE 1 = 0', "purpose": "x"})
    assert "no rows" in json.loads(out.content)["note"]
    executor.max_rows = 2
    out = await executor.run("run_sql", {"sql": 'SELECT * FROM "2019_CBG_B01"', "purpose": "x"})
    assert "truncated" in json.loads(out.content)["note"]


async def test_query_errors_vs_outages(executor, warehouse):
    warehouse.fail_with = WarehouseError("SQL error: boom")
    out = await executor.run("run_sql", {"sql": 'SELECT 1 FROM "2019_CBG_B01"', "purpose": "x"})
    assert out.is_error
    warehouse.fail_with = WarehouseUnavailable("down")
    with pytest.raises(WarehouseUnavailable):  # outages end the turn
        await executor.run("run_sql", {"sql": 'SELECT 1 FROM "2019_CBG_B01"', "purpose": "x"})


@pytest.mark.parametrize(
    "name, args",
    [
        ("run_sql", {"purpose": "missing sql"}),
        ("run_sql", {"sql": "", "purpose": "x"}),
        ("search_fields", {"query": 3}),
        ("describe_table", {}),
        ("no_such_tool", {}),
        ("run_sql", "not a dict"),
    ],
)
async def test_bad_tool_arguments_are_errors_not_crashes(executor, name, args):
    out = await executor.run(name, args)
    assert out.is_error


async def test_describe_table_and_geography(executor):
    body = json.loads((await executor.run("describe_table", {"table_number": "b01001"})).content)
    assert body["physical_tables"] == {"2019": "2019_CBG_B01", "2020": "2020_CBG_B01"} and len(body["fields"]) == 2
    assert (await executor.run("describe_table", {"table_number": "Z99"})).is_error

    geo = json.loads((await executor.run("resolve_geography", {"place": "Washington"})).content)
    assert len(geo["matches"]) >= 3 and "ask the user" in geo["note"]
    none = json.loads((await executor.run("resolve_geography", {"place": "Springfield"})).content)
    assert none["matches"] == [] and "city" in none["note"]
