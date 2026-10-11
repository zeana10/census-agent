import pytest

from app.catalog import build_catalog
from app.config import Settings
from app.geography import Geography
from app.tools import ToolExecutor

from .fakes import DB, SCHEMA, YEARS, DuckWarehouse


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        anthropic_api_key="test",
        snowflake_database=DB,
        snowflake_schema=SCHEMA,
        census_years=",".join(YEARS),
        catalog_cache_path=str(tmp_path / "catalog.json"),
        request_deadline_s=50,
        max_agent_steps=6,
        rate_limit_per_session_per_minute=100,
        rate_limit_per_ip_per_minute=100,
        rate_limit_global_per_minute=100,
    )


@pytest.fixture
def warehouse() -> DuckWarehouse:
    return DuckWarehouse()


@pytest.fixture
def catalog(warehouse):
    cat = build_catalog(warehouse, database=DB, schema=SCHEMA, years=YEARS)
    warehouse.queries.clear()  # tests assert on queries issued after startup
    return cat


@pytest.fixture
def geography(catalog):
    return Geography(catalog)


@pytest.fixture
def executor(catalog, geography, warehouse):
    return ToolExecutor(catalog, geography, warehouse, max_rows=50)
