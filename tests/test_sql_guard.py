import pytest

from app.sql_guard import SqlRejected, validate

ALLOWED = {"2019_CBG_B01", "2019_CBG_B19", "2019_METADATA_CBG_FIPS_CODES"}


def v(sql: str, max_rows: int = 200):
    return validate(sql, allowed_tables=ALLOWED, database="DB", schema="PUBLIC", max_rows=max_rows)


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT SUM("B01003e1") FROM "2019_CBG_B01" WHERE LEFT("CENSUS_BLOCK_GROUP", 2) = \'48\'',
        'SELECT a."CENSUS_BLOCK_GROUP", "B01003e1", "B19013e1" FROM "2019_CBG_B01" a JOIN "2019_CBG_B19" b USING ("CENSUS_BLOCK_GROUP")',
        'WITH s AS (SELECT LEFT("CENSUS_BLOCK_GROUP", 2) st, SUM("B01003e1") p FROM "2019_CBG_B01" GROUP BY 1) SELECT * FROM s ORDER BY p DESC',
        'select * from DB.PUBLIC."2019_CBG_B01"',
        'SELECT 1 FROM "2019_CBG_B01" UNION ALL SELECT 2 FROM "2019_CBG_B19"',
    ],
)
def test_accepts_read_only_queries_on_census_tables(sql):
    out = v(sql)
    assert "LIMIT" in out.sql.upper()
    assert out.tables


@pytest.mark.parametrize(
    "sql, reason",
    [
        ('DROP TABLE "2019_CBG_B01"', "Only SELECT"),
        ('DELETE FROM "2019_CBG_B01"', "Only SELECT"),
        ('INSERT INTO "2019_CBG_B01" SELECT * FROM "2019_CBG_B01"', "Only SELECT"),
        ('CREATE TABLE t AS SELECT * FROM "2019_CBG_B01"', "Only SELECT"),
        ('SELECT * INTO t FROM "2019_CBG_B01"', "read-only"),
        ('SELECT 1 FROM "2019_CBG_B01"; DROP TABLE "2019_CBG_B01"', "Exactly one"),
        ('SELECT * FROM "SECRET_PAYROLL"', "Unknown table"),
        ('SELECT * FROM OTHERDB.PUBLIC."2019_CBG_B01"', "outside"),
        ("SELECT * FROM DB.INFORMATION_SCHEMA.TABLES", "outside"),
        ('SELECT * FROM "2019_CBG_B01", TABLE(RESULT_SCAN(LAST_QUERY_ID()))', "not allowed"),
        ('SELECT SYSTEM$WHITELIST() FROM "2019_CBG_B01"', "not allowed"),
        ('SELECT CURRENT_ROLE() FROM "2019_CBG_B01"', "not allowed"),
        ("SELECT 42", "at least one census table"),
        ("SHOW TABLES", "Only SELECT"),
        ("", "Empty"),
        ("SELEC * FROM", "parse|census table|SELECT"),
    ],
)
def test_rejects_unsafe_or_out_of_scope_sql(sql, reason):
    with pytest.raises(SqlRejected, match=reason):
        v(sql)


def test_cte_names_are_not_mistaken_for_tables():
    assert v('WITH x AS (SELECT * FROM "2019_CBG_B01") SELECT * FROM x').tables == ["2019_CBG_B01"]


def test_cte_cannot_smuggle_unknown_table():
    with pytest.raises(SqlRejected):
        v('WITH x AS (SELECT * FROM "SECRET_PAYROLL") SELECT * FROM x')


def test_limit_added_and_capped_but_small_limits_kept():
    # max_rows + 1 so the caller can detect truncation
    assert v('SELECT * FROM "2019_CBG_B01"', max_rows=50).sql.endswith("LIMIT 51")
    assert v('SELECT * FROM "2019_CBG_B01" LIMIT 100000', max_rows=50).sql.endswith("LIMIT 51")
    assert v('SELECT * FROM "2019_CBG_B01" LIMIT 5', max_rows=50).sql.endswith("LIMIT 5")


def test_union_is_wrapped_for_limit():
    out = v('SELECT 1 FROM "2019_CBG_B01" UNION ALL SELECT 2 FROM "2019_CBG_B19"', max_rows=10)
    assert out.sql.endswith("LIMIT 11")


def test_display_sql_omits_the_safety_cap_but_keeps_user_limits():
    out = v('SELECT SUM("B01003e1") FROM "2019_CBG_B01"', max_rows=200)
    assert "LIMIT" not in out.display_sql.upper() and out.capped
    assert out.sql.endswith("LIMIT 201")
    own = v('SELECT * FROM "2019_CBG_B01" LIMIT 5', max_rows=200)
    assert "LIMIT 5" in own.display_sql and not own.capped
