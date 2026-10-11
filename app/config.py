"""Runtime configuration, loaded from environment variables (or a local .env file)."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- LLM -------------------------------------------------------------
    anthropic_api_key: str = ""
    agent_model: str = "claude-opus-5-5"
    agent_effort: Effort = "medium"
    # The guardrail is a short classification call on every message (including the
    # fast-fail rejections), so it uses the small, fast model. Verify with the guardrail
    # cases in evals/cases.yaml before changing.
    guardrail_model: str = "claude-haiku-5-5"
    guardrail_effort: Effort = "low"
    # The output verifier re-checks arithmetic (percentages, sums), so it gets a bit more thinking.
    verifier_effort: Effort = "medium"

    # --- Snowflake -------------------------------------------------------
    snowflake_account: str = ""
    snowflake_user: str = ""
    snowflake_password: str = ""
    snowflake_private_key_path: str = ""  # preferred over password when set
    snowflake_private_key_pem: str = ""  # PEM contents; handy on PaaS hosts without a disk
    snowflake_role: str = "CENSUS_READER"
    snowflake_warehouse: str = "CENSUS_WH"
    snowflake_database: str = "US_OPEN_CENSUS_DATA_NEIGHBORHOOD_INSIGHTS_FREE_DATASET"
    snowflake_schema: str = "PUBLIC"
    snowflake_pool_size: int = 4  # max concurrent queries; keep <= warehouse MAX_CONCURRENCY_LEVEL
    # ACS 5-year releases to load (table-name prefixes in the share, e.g. 2019_CBG_B01).
    # Comma-separated; the latest is the default for questions that don't name a year.
    census_years: str = "2019,2020"

    @property
    def years(self) -> list[str]:
        return sorted({y.strip() for y in self.census_years.split(",") if y.strip()})

    # --- Limits ----------------------------------------------------------
    request_deadline_s: float = 50.0  # hard budget per user turn (spec: < 60 s)
    sql_timeout_s: int = 25
    max_result_rows: int = 200
    max_agent_steps: int = 10
    max_question_chars: int = 2000
    max_history_turns: int = 20
    session_ttl_s: int = 60 * 60 * 6
    # Messages per rolling minute. See TieredLimiter in main.py for why there are three.
    rate_limit_per_session_per_minute: int = 10
    rate_limit_per_ip_per_minute: int = 60
    rate_limit_global_per_minute: int = 120
    # Number of reverse proxies in front of the app that append to X-Forwarded-For
    # (1 on Render/Fly/Cloud Run). 0 = ignore the header and use the socket peer.
    trusted_proxy_hops: int = 1

    # --- Web -------------------------------------------------------------
    basic_auth_user: str = ""  # leave empty to disable auth (local dev only)
    basic_auth_password: str = ""
    catalog_cache_path: str = "catalog_cache.json"
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"  # "text" is easier to read locally


@lru_cache
def get_settings() -> Settings:
    return Settings()
