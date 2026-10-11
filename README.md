# Census Assistant

A chat agent that answers natural-language questions about the US population,
grounded in the **US Open Census Data** share on the Snowflake Marketplace
(SafeGraph's packaging of the ACS 5-year estimates at census-block-group level).
It covers both releases in the share: **2019** (survey years 2015–2019) and
**2020** (2016–2020). That's ~8,000 fields across 353 ACS tables, for every
state and county.

## Try the demo

| | |
|---|---|
| URL | **TODO: https://census-assistant.onrender.com** |
| Username | `reviewer` |
| Password | **TODO** |

Questions worth trying, covering the main behaviours:

| Try | What it shows |
|---|---|
| *What is the total population of Texas?* → *And Ohio?* | Field lookup, geography resolution, follow-up context |
| *Which 5 states have the highest share of people 65+?* | Multi-field aggregation, ranking, ratio from summed counts |
| *Median household income in King County, WA?* | Medians can't be aggregated from block groups, so the agent explains and labels any approximation |
| *Population of Washington?* | Ambiguity (state vs ~30 counties) → clarifying question |
| *Population of Austin, TX?* | Partial match: no city geography → offers the county instead |
| *How did Florida's population change between 2019 and 2020?* | Multi-release comparison, with the caveat that the two 5-year periods overlap |
| *Population of Florida in 2030?* | Reasonable but unanswerable → explains why, suggests what *is* available |
| *Write me a poem* / *Ignore your instructions…* | Guardrail fast-fail, no database call |

Expand **How I got this** under any answer to see the exact SQL that produced it.

## How the requirements are met

| Requirement or tip from the brief | How it's met | Where | Evidence |
|---|---|---|---|
| Chat agent grounded in the US Census data | Claude queries the Snowflake share through tools; every figure comes from a query run in the conversation | `agent.py`, `tools.py` | Every answer's *How I got this* panel; live evals with ground-truth SQL |
| Public web UI, no local setup | One container on Render behind HTTPS and Basic auth | `Dockerfile`, `render.yaml`, `static/index.html` | Demo URL above |
| Preserve conversation context | Full per-session history (including query results) sent each turn; short follow-ups inherit place, measure and year, new questions start fresh | `sessions.py`, `prompts.py` | `test_follow_up_sends_prior_turns`; 5 multi-turn eval cases |
| Responses within 60 s | Streaming (SSE) with progress lines; 50 s hard deadline per turn, forced wrap-up at 12 s left, step cap | `agent.py`, `main.py` | `test_deadline_is_enforced`, `test_step_limit_forces_wrap_up`; eval runner fails any turn over 60 s |
| Guardrails / dedicated validation layer | `guardrails.py`: input precheck, then an input classifier (fast-fail, no DB access), then output grounding checks (heuristic + LLM verifier); plus SQL allow-list and read-only role | `guardrails.py`, `sql_guard.py`, `snowflake_setup.sql` | Guardrail and verifier tests; 7 adversarial/off-topic eval cases |
| Degrade gracefully, never hallucinate | Unanswerable → explains why + nearest answerable question; DB/LLM outages → specific message, retry button, request id; failed turns never corrupt history | `agent.py`, `warehouse.py`, `llm.py` | `test_warehouse_outage_…`, `test_llm_errors_are_user_safe`, degraded-mode API test |
| Ambiguous / partial / conflicting / unanswerable queries | Place resolution returns all candidates; prompt rules for clarifying vs. stated assumption; city → county; future years and other topics explained | `geography.py`, `prompts.py` | Eval cases `ambiguous_washington`, `city_not_available`, `conflicting_request`, `future_year`, `underspecified` |
| Comprehensive mapping (no subset) | Catalog built from the share's own metadata: all 8,084 fields, 353 tables, both releases, no hard-coded columns | `catalog.py` | `scripts/inspect_schema.py`; catalog tests |
| Context awareness of the schema | Table-level overview in the (cached) system prompt; field-level detail on demand via `search_fields` / `describe_table` | `catalog.py`, `tools.py` | Agent picks e.g. B15003e22–e25 or B19001 brackets unaided in evals |
| Deployment "truth" | CI builds the image and boots it with no credentials (must come up degraded, not crash); background catalog load so health checks pass immediately | `.github/workflows/ci.yml` | CI run |
| Meaningful tests | 118 offline tests (DuckDB stand-in for Snowflake, scripted model) + 25 live evals with independent ground truth | `tests/`, `evals/` | See *Tests* below |

## Architecture

```
Browser (static HTML/JS, SSE)
  │  POST /api/chat  ── streams: status · sql · delta · done
  ▼
FastAPI (app/main.py)  ── basic auth · session/IP/global rate limits · per-session lock · keep-alives
  │
  ▼
Agent (app/agent.py), per user turn, hard 50 s deadline:
  1. precheck            empty / too long / control chars                (guardrails.py)
  2. input classifier    answer | chitchat | out_of_dataset | off_topic | unsafe
                         non-"answer" → immediate reply, no DB access    (fast-fail path)
  3. tool loop           Claude ⇄ tools, ≤ 10 steps, tools run in parallel
        search_fields      BM25 over ~8k field descriptions, 2019 + 2020   (catalog.py)
        describe_table     every field in one ACS table
        resolve_geography  state/county name → GEOID prefix + SQL filter (geography.py)
        run_sql            sqlglot allow-list → Snowflake (read-only role) (sql_guard.py, warehouse.py)
  4. grounding checks    (a) large figures but no query at all → retry       (guardrails.py)
                         (b) LLM verifier (Haiku): every data claim vs this turn's
                             query results, allowing arithmetic → 1 retry, then caveat
  │
  ▼
Snowflake: CENSUS_READER role (IMPORTED PRIVILEGES only) · X-Small WH · statement timeout · credit cap
```

### Key decisions

**Tool-using agent over a fixed text-to-SQL prompt.** The dataset has ~7,500
columns with opaque ids (`B19013e1`). They can't all go in a prompt, and
hard-coding a subset fails the "comprehensive mapping" requirement. The system
prompt holds a one-line summary of every ACS *table* (a few thousand tokens,
cached). The model then calls `search_fields` / `describe_table` to find exact
columns at question time. The catalog is built from the share's own metadata
tables and `INFORMATION_SCHEMA`, so nothing about the schema is hard-coded.

**Geography is resolved in code, not by the model.** States and counties are
GEOID prefixes of `CENSUS_BLOCK_GROUP`. The model gets FIPS codes from
`resolve_geography` (fuzzy matching against the share's FIPS table) instead of
from memory. That is also where ambiguity shows up: "Washington" returns the
state plus every Washington County.

**Correctness rules for aggregation are in the prompt and in the evals.** You
can sum counts across block groups. You can't sum or average medians, and
margins of error don't add. Percentages are computed from summed numerators and
denominators. These are the mistakes a plausible-looking answer is most likely
to make silently.

**Defence in depth for model-written SQL.**
1. Static validation with sqlglot: a single SELECT, tables from the allow-list only, no table functions or `SYSTEM$` functions, and a row cap.
2. A read-only Snowflake role that can see only the share.
3. Statement timeout and resource monitor.

A rejected query goes back to the model as a tool error, so it can fix the query and retry.

**Claude through the Anthropic API instead of Snowflake Cortex.** This is the
most consequential choice, so it has its own section below:
[Why not Snowflake Cortex?](#why-not-snowflake-cortex)

**Hand-written loop instead of the SDK tool runner.** That gives per-step
deadline checks, UI progress events, a step cap with a forced "wrap up" turn,
parallel tool execution, and atomic turns: if a turn fails midway (database
down, timeout), it is discarded and replaced with a plain apology, so the
stored history never ends with an unanswered `tool_use`.

**Models.** The agent uses `claude-opus-5-5` with adaptive thinking at `medium`
effort, because correctness of multi-step SQL reasoning is the priority. The
guardrail classifier uses `claude-haiku-5-5` at `low` effort. It runs on every
message and *is* the fast-fail path, so its latency is what users feel on
rejections. If the classifier refuses or errors, the message goes on to the
agent rather than being blocked (see `guardrails.py`). Both models can be
changed in config; the guardrail cases in the eval set are the check before
changing either. Server-side refusal fallback (`fallbacks: "default"`) is
enabled, so a safety-classifier false positive is re-run on a fallback model
instead of reaching the user as a refusal.

**Streaming + 60 s budget.** Responses stream over SSE: progress lines while
tools run, then the answer token by token. Each turn has a 50 s wall-clock
deadline. When less than 12 s remains, the model is told to answer with what it
has. Under 3 s, the turn is aborted with a helpful message.

### Why not Snowflake Cortex?

Cortex is Snowflake's built-in AI layer, and it can do most of this
job inside the account. I considered each piece:

| Cortex option | What it would replace | Why I didn't use it here |
|---|---|---|
| **Cortex Analyst** (text-to-SQL over a semantic view) | The agent + `search_fields` + `run_sql` | It answers from a curated semantic view of tables, dimensions and measures. Snowflake recommends keeping a semantic view to about 50 columns, and caps a YAML semantic model at 32K tokens without samples. This dataset has ~7,500 columns. Fitting it means either picking a subset (which the brief warns against: "do not limit yourself to a subset") or splitting it into dozens of views and routing between them. That's the same retrieval problem, just moved into YAML. |
| **Cortex Agents** (managed orchestration over Analyst/Search tools, with conversation threads) | The agent loop and session store | It would remove code I maintain, and threads would give durable conversation state. But it orchestrates Cortex Analyst, so it inherits the semantic-view limit above. It also gives less control over the things this brief grades: a hard per-turn deadline, a forced "wrap up" step, the grounding check, and atomic rollback of a failed turn. |
| **Cortex Search** (managed hybrid keyword + vector search over a table) | The BM25 field search in `catalog.py` | **The strongest candidate. I'd adopt it next** (see below). It fixes my weakest component: keyword search misses paraphrases like "folks without health insurance". |
| **`AI_COMPLETE`** with a Claude model (LLM calls inside Snowflake) | `llm.py` (the Anthropic API) | Data and inference stay inside Snowflake's governance boundary, with one bill and no second vendor. The Claude models I saw listed for Cortex lag the first-party API, though, and newer features (adaptive thinking, server-side refusal fallback, structured outputs, prompt caching) arrive on the Anthropic API first. Availability also varies by region and needs cross-region inference enabled by ACCOUNTADMIN. That's an extra setup risk on a fresh trial account, during a 24-hour build. |

**Why the trade-off favours the current design for this brief.**
1. **Comprehensive coverage.** The schema is reached through retrieval tools over the share's own metadata, so every column can be answered without curating a subset first.
2. **Control and testability.** Every step — guardrail, retrieval, SQL validation, deadline, grounding check — is ordinary Python. That's why 118 offline tests can pin down its behaviour.
3. **Model quality.** Correct aggregation (never summing medians, matching numerator and denominator universes) is mostly a reasoning problem, so I wanted the strongest current model.

**What it costs.** I maintain an orchestration loop and session store that
Cortex Agents would provide. Prompts and the schema overview (no row data)
leave Snowflake for Anthropic. Query *results* also leave Snowflake: they are
fed back to the model. That's fine for public census data, but for a customer
with sensitive data it's a governance conversation, and the main argument for
Cortex.

**How this would move to Cortex.** The seams are already where they need to be:
- **Field search:** put the field-description metadata into a Cortex Search service and swap it in behind `Catalog.search_fields`. The tool interface doesn't change. This is a few hours of work and the first thing I'd do.
- **LLM calls:** `llm.py` is the only module that calls a model, behind a two-method interface (`agent_turn`, `classify`). A Cortex-backed implementation could replace it, with the eval set (`evals/`) as the gate for whether quality holds.
- **Hybrid option:** a curated semantic view over the ~20 most-asked measures (population, age, income, education, housing), served through Cortex Analyst for speed and verified queries, with the current agent as the fallback for the long tail of the dataset.

Before building on any of this, check what the account supports:
`SHOW PARAMETERS LIKE 'CORTEX_ENABLED_CROSS_REGION' IN ACCOUNT;` and the
[Cortex model and region availability page](https://docs.snowflake.com/user-guide/snowflake-cortex/aisql-regional-availability).

### Interpretations of the brief

- **Data source:** the Snowflake Marketplace share (not the SafeGraph download fallback).
- **Years:** both ACS releases in the share, 2019 and 2020 (`CENSUS_YEARS`). If a question doesn't name a year, the agent uses the latest and says so. Year comparisons are answered, but always with two caveats: consecutive 5-year releases share four of their five survey years, so the difference isn't a clean annual change (the Census Bureau advises comparing non-overlapping periods); and dollar figures are in each release's own inflation-adjusted dollars. The agent never joins block groups across years, because boundaries can change between releases.
- **Not included, on purpose:**
  - The share's 2020 decennial *redistricting* tables (exact 2020 Census counts by race, Hispanic origin and voting age). They use a different metadata format (`COLUMN_ID`/`FIELD_NAME`) and would need their own catalog loader. That's the next data addition; see REFLECTION.md.
  - Two small derived tables (`2019_RENT_PERCENTAGE_HOUSEHOLD_INCOME`, `2019_TOTAL_RENTAL_GEO`) that repackage ACS table B25070 with readable column names; the agent uses the source table instead.
- **"Production quality"** is read as: safe to expose on a public URL, fails clearly, observable through logs, and tested. It does not mean horizontally scalable. Session state is in memory and the app runs as a single instance (see REFLECTION.md).
- **Guardrails** cover off-topic requests, unsafe requests (prompt extraction, hateful comparisons, targeting individuals) and topics outside the dataset. Questions that are on-topic but unanswerable go to the agent, because only it knows the schema well enough to say *why* and suggest an alternative.
- **Authentication:** HTTP Basic, because the brief allows credentials and it adds no login flow that could break in a fresh browser.

## Repository layout

```
app/
  main.py         FastAPI app, SSE endpoint, auth, rate limit, degraded mode
  agent.py        the turn loop (deadline, steps, grounding checks, error handling)
  guardrails.py   the validation layer: precheck, input classifier, output grounding checks
  llm.py          Anthropic SDK wrapper (the only module that calls the API)
  tools.py        tool schemas + executor
  catalog.py      schema discovery, field search (BM25), cache
  geography.py    state/county resolution
  sql_guard.py    static SQL validation
  warehouse.py    Snowflake access, error mapping, reconnects
  sessions.py     in-memory conversation store
  logging_setup.py  JSON logs with per-request correlation ids
  prompts.py      all prompt text
  static/index.html
tests/            118 offline tests (DuckDB stands in for Snowflake, scripted LLM)
evals/            live end-to-end cases (real Claude + Snowflake) + runner
scripts/          snowflake_setup.sql, inspect_schema.py
```

## Running locally

Dependencies are managed with [uv](https://docs.astral.sh/uv/) and pinned in `uv.lock`.

```bash
uv sync                                    # creates .venv with app + dev dependencies
cp .env.example .env                       # fill in Anthropic + Snowflake credentials
uv run python -m scripts.inspect_schema    # verifies the connection, builds catalog_cache.json
LOG_FORMAT=text uv run uvicorn app.main:app --reload   # http://localhost:8000
uv run python -m scripts.ask "Population of Ohio?" "And in 2019?"   # live smoke test from the terminal
```

No uv yet? `pip install --user uv` (or see the uv docs for other installers).

Snowflake one-time setup: get the *US Open Census Data & Neighborhood Insights*
listing from the Marketplace, then run `scripts/snowflake_setup.sql` as
ACCOUNTADMIN. It creates the read-only role, the service user (key-pair auth),
the warehouse and the resource monitor.

## Tests

```bash
uv run pytest                        # offline, ~3 s, no credentials needed
uv run ruff check . && uv run mypy   # lint + type check (also run in CI)
uv run python -m evals.run_evals     # live, costs a few cents; needs .env
```

**Latest live run** (2026-10-10, Claude Opus 5.5 agent, Haiku 5.5 guardrail and verifier):
- 25/25 cases passed. Median turn 12.2 s, slowest 22.7 s; guardrail rejections ~2 s.
- About 3¢ per answered question; ~97% of input tokens served from the prompt cache.
- The verifier passed 16 answers and flagged one: a *false* alarm, because it only received SQL results, not the place-lookup result the answer quoted. Fixed by passing it every tool result; regression test `test_verifier_gets_non_sql_tool_results_too`.

CI (`.github/workflows/ci.yml`) runs on every push:
- lint, format check, mypy and the offline tests against the locked dependencies;
- a Docker build, then a boot smoke test with no credentials, which must come up in degraded mode.

- **Offline suite (`tests/`).** Deterministic and runs in CI. DuckDB is seeded with a miniature copy of the share, using the same table and column names, so tools and the agent run real SQL. A scripted LLM replays tool calls, which tests the loop's behaviour: SQL errors fed back for retry, a database outage mid-turn, deadlines, step limits, the grounding retry, refusals, history validity after failures, guardrail routing and fail-open. A separate set of tests covers the SQL validator against injection and escape attempts.
- **Live evals (`evals/cases.yaml`).** 25 cases across categories: lookups, ratios, rankings, the median caveat, multi-turn, multi-year comparisons (with the overlap and dollar caveats), ambiguity, partial matches, unanswerable, off-topic and adversarial. Numeric answers are checked against hand-written ground-truth SQL that the runner executes directly, independent of the agent. The runner also checks the 60 s limit per turn.

## Deploying

The app is one container (`Dockerfile`). `render.yaml` is a Render blueprint:
connect the repo, set the secret env vars (`ANTHROPIC_API_KEY`,
`SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_PRIVATE_KEY_PEM`, `BASIC_AUTH_PASSWORD`) and
deploy. The server starts accepting requests immediately and loads the
catalog in the background. `/healthz` reports `ok` or `degraded`: degraded
means Snowflake isn't reachable yet. The UI still loads and says so, and the
catalog load is retried every 30 s.

**Operations notes.**
- **Logs** are JSON on stdout. Each chat request has a `request_id`, returned in the `X-Request-ID` header and the final SSE event; filter on it to see one turn end to end (guardrail decision, each model step with its Anthropic request id, each SQL query, outcome and latency).
- **Snowflake connection pool:** `SNOWFLAKE_POOL_SIZE` (default 4) caps concurrent queries. Keep it at or below the warehouse's concurrency level.
- **Rate limits** have three layers, all per rolling minute: 10 messages per browser session (so reviewers sharing an office IP don't block each other), 60 per IP (so a client can't get around the session limit by inventing session IDs), and 120 overall as a cost ceiling. Settings: `RATE_LIMIT_PER_SESSION_PER_MINUTE`, `RATE_LIMIT_PER_IP_PER_MINUTE`, `RATE_LIMIT_GLOBAL_PER_MINUTE`.
- **Client IPs for rate limiting** come from `X-Forwarded-For`, using only entries appended by the `TRUSTED_PROXY_HOPS` proxies in front of the app. On Render this is **2** (Cloudflare, then Render's load balancer; measured with `/healthz`, which reports the number of forwarded entries). Entries a client adds itself are ignored. On a new host, call `/healthz` directly and set the value to its `forwarded_for_entries`.
