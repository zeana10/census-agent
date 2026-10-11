"""Full result tables: model sees a capped view, the user gets every row plus Excel/CSV downloads."""

import io
import json

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app import export
from app.main import Runtime, create_app
from app.sessions import ResultTable
from app.tools import _model_view

from .fakes import ScriptedLLM, text_turn, tool_turn

LIST_SQL = 'SELECT "CENSUS_BLOCK_GROUP" AS "Block group", "B01003e1" AS "Population" FROM "2019_CBG_B01" ORDER BY 2 DESC'


@pytest.mark.asyncio
async def test_show_table_gives_user_all_rows_and_model_a_capped_view(executor):
    executor.max_rows = 4  # the model's cap
    out = await executor.run("run_sql", {"sql": LIST_SQL, "purpose": "Block groups", "show_table": True})
    body = json.loads(out.content)
    assert len(out.meta["table"]["rows"]) == 6 and not out.meta["table"]["truncated"]  # user: every row
    assert len(body["rows"]) == 4 and body["row_count"] == 6  # model: capped, but knows the total
    assert "do not reproduce its rows" in body["note"]


@pytest.mark.asyncio
async def test_show_table_has_its_own_higher_limit(executor):
    executor.max_rows, executor.table_max_rows = 2, 3
    out = await executor.run("run_sql", {"sql": LIST_SQL, "purpose": "x", "show_table": True})
    assert len(out.meta["table"]["rows"]) == 3 and out.meta["table"]["truncated"]
    assert "the table holds the first 3" in json.loads(out.content)["note"]


@pytest.mark.asyncio
async def test_plain_queries_keep_the_model_cap_and_suggest_show_table(executor):
    executor.max_rows = 2
    out = await executor.run("run_sql", {"sql": LIST_SQL, "purpose": "x"})
    assert "table" not in out.meta
    assert "show_table=true" in json.loads(out.content)["note"]


def test_model_view_keeps_head_and_tail_of_large_results():
    rows = [[i] for i in range(1000)]
    view, desc = _model_view(rows, 200)
    assert len(view) == 200 and view[0] == [0] and view[-1] == [999] and view[179] == [179]
    assert desc == "the first 180 and last 20 of 1,000 rows"
    assert _model_view(rows[:5], 200) == (rows[:5], "all 5 rows")
    assert _model_view(rows, 4) == (rows[:4], "the first 4 of 1,000 rows")  # regression: rows[-0:] is all rows


def _table():
    return ResultTable(
        purpose="Population by county, 2020",
        sql='SELECT "COUNTY" FROM x',
        columns=["County", "Population"],
        rows=[["Doña Ana County, NM", 218195], ["=HYPERLINK(evil)", 1], ["Cook County, IL", 5169517]],
        truncated=False,
    )


def test_csv_export_opens_cleanly_in_excel():
    raw = export.to_csv(_table())
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM so Excel reads UTF-8 place names
    text = raw.decode("utf-8-sig")
    assert '"Doña Ana County, NM",218195' in text  # commas inside values are quoted
    assert "'=HYPERLINK(evil)" in text  # formula injection neutralised


def test_xlsx_export_has_data_and_about_sheets():
    wb = load_workbook(io.BytesIO(export.to_xlsx(_table())))
    assert wb.sheetnames == ["Data", "About"]
    data = list(wb["Data"].values)
    assert data[0] == ("County", "Population") and data[3] == ("Cook County, IL", 5169517)
    assert data[2][0] == "'=HYPERLINK(evil)"
    about = dict(wb["About"].values)
    assert about["What"] == "Population by county, 2020" and "SELECT" in about["SQL"] and "Snowflake" in about["Source"]
    assert export.filename(_table(), "xlsx") == "population-by-county-2020.xlsx"


@pytest.fixture
def client(settings, warehouse):
    def _make(llm, **overrides):
        for k, v in overrides.items():
            setattr(settings, k, v)
        return TestClient(create_app(settings, runtime=Runtime(settings, warehouse=warehouse, llm=llm)))

    return _make


def _events(text):
    out = []
    for chunk in text.strip().split("\n\n"):
        if chunk.startswith(":"):
            continue
        lines = dict(line.split(": ", 1) for line in chunk.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_end_to_end_table_event_and_downloads(client):
    llm = ScriptedLLM(
        steps=[
            tool_turn(("run_sql", {"sql": LIST_SQL, "purpose": "Block groups by population", "show_table": True})),
            text_turn("Six block groups; the largest has 3,000 people."),
        ]
    )
    with client(llm, basic_auth_user="reviewer", basic_auth_password="pw") as c:
        r = c.post("/api/chat", json={"message": "List block groups"}, auth=("reviewer", "pw"))
        ev = _events(r.text)
        table = next(d for e, d in ev if e == "table")
        assert len(table["rows"]) == 6 and table["columns"] == ["Block group", "Population"]
        base = f"/api/tables/{table['session_id']}/{table['id']}"

        assert c.get(base + ".csv").status_code == 401  # downloads need the login too
        csv_r = c.get(base + ".csv", auth=("reviewer", "pw"))
        assert csv_r.status_code == 200 and csv_r.headers["content-type"].startswith("text/csv")
        assert 'filename="block-groups-by-population.csv"' in csv_r.headers["content-disposition"]
        assert csv_r.content.decode("utf-8-sig").count("\n") == 7  # header + 6 rows

        x = c.get(base + ".xlsx", auth=("reviewer", "pw"))
        assert x.status_code == 200 and len(list(load_workbook(io.BytesIO(x.content))["Data"].values)) == 7

        assert c.get(f"/api/tables/{table['session_id']}/nope.csv", auth=("reviewer", "pw")).status_code == 404
        assert c.get(base + ".pdf", auth=("reviewer", "pw")).status_code == 404

        hist = c.get("/api/history", params={"session_id": table["session_id"]}, auth=("reviewer", "pw")).json()
        assert "Table shown: Block groups by population: 6 rows" in hist["messages"][-1]["text"]


def test_tables_per_session_are_bounded(executor):
    from app.sessions import Session

    s = Session(id="s")
    ids = [s.add_table(_table(), keep=3) for _ in range(5)]
    assert list(s.tables) == ids[-3:]
