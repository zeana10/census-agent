"""Smoke-test the Snowflake connection and print what the catalog sees.

    python -m scripts.inspect_schema

Run this first after setting up .env. It builds (and caches) the catalog, then
prints table counts, a sample of field labels, and a test query.
"""

from app.catalog import load_or_build
from app.config import get_settings
from app.geography import Geography
from app.warehouse import SnowflakeWarehouse


def main() -> None:
    s = get_settings()
    wh = SnowflakeWarehouse(s)
    cat = load_or_build(
        wh, cache_path=s.catalog_cache_path, database=s.snowflake_database, schema=s.snowflake_schema, years=s.years
    )
    print(f"physical tables: {len(cat.physical_tables)}  ACS tables: {len(cat.tables)}  fields: {len(cat.fields)}")
    print(f"states: {len(cat.states)}  counties: {len(cat.counties)}  geo table: {cat.geo_table}")
    print("\nfirst 10 ACS tables:\n" + "\n".join(cat.table_overview().splitlines()[:10]))
    print("\nsearch 'median household income':")
    for f in cat.search_fields("median household income", limit=5):
        print(f"  {f.id:<12} {f.label}")
    tx = Geography(cat).resolve("Texas")[0]
    for year in cat.years:
        sql = f'SELECT SUM("B01003e1") AS pop FROM "{year}_CBG_B01" WHERE {tx.sql_filter()}'
        print(f"\n{sql}\n  -> {wh.query_sync(sql, max_rows=1).rows}")
    prompt_chars = len(cat.table_overview())
    print(f"\ntable overview in system prompt: {prompt_chars:,} chars (~{prompt_chars // 4:,} tokens)")


if __name__ == "__main__":
    main()
