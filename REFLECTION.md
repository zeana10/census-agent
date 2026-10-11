# Reflection

> **DRAFT. Rewrite in your own words before submitting.** Everything marked
> ✍️ needs your real experience: what you actually did, what surprised you,
> real eval numbers. Reviewers will ask about this document in the follow-up.

## Development process

✍️ *Describe your actual timeline. Suggested skeleton:*

1. **Read the brief and the data first (~1 h).** Before writing the agent I
   looked at the share's metadata tables. The deciding fact was ~7,500 opaque
   columns keyed at block-group level, which ruled out "put the schema in the
   prompt" and made aggregation correctness (medians, MOEs) the main risk.
2. **Built the deterministic parts first:** catalog discovery, field search,
   geography resolution, SQL validator. These are where silent wrong answers
   come from, and they can be fully unit-tested.
3. **Agent loop and guardrails**, tested against a scripted model and DuckDB
   before spending any API tokens.
4. **Deployed early** ✍️ *(when?)* to catch environment issues (key-pair auth
   on a PaaS, SSE buffering behind the proxy).
5. **Ran the live eval set, then iterated on prompts** ✍️ *(what changed, and
   what the before/after pass rate was)*.

**How I used AI tools.** ✍️ *Be specific: which parts Claude Code wrote, what
you reviewed or rewrote, and where it got something wrong. Examples from this
build:*
- *The first SQL validator capped results at exactly `max_rows`, so truncation could never be detected. A test caught it.*
- *A staff-level review pass found that all Snowflake queries were serialized behind one lock (fixed with a connection pool), that the rate limiter trusted a client-controlled `X-Forwarded-For` entry (fixed by counting trusted proxy hops), and that startup blocked on Snowflake (moved to a background load).*
- *Turning on mypy found several places where optional values were used as if always present.*
- *The first live run failed on every turn: streamed messages have no `_request_id`, which a log line read. The error handling turned it into a polite apology rather than a crash. Fixed, and a wrapper test now covers it.*
- *Reading real answers (not just pass/fail) found the agent repeating an earlier year comparison in an unrelated question. Fixed in the prompt, plus a new eval case.*
- *The new output verifier's first eval run produced one false alarm. It had been given only SQL results, so a true count from the place lookup looked unsupported. Fixed, plus a regression test.*

## Key architectural decisions

See the README "Key decisions" section for the full list. The ones I'd defend
most strongly:

- **Schema access through retrieval tools, not prompt stuffing.** A compact
  table-level overview stays in the cached system prompt; field-level detail is
  fetched on demand. This scales to the full dataset with no hard-coded columns.
- **Deterministic code for anything that has a right answer.** FIPS codes, SQL
  safety and row limits are enforced by code. The model is only trusted for
  language and analysis.
- **Three SQL safety layers.** The static allow-list catches mistakes early and
  cheaply. The read-only role and timeouts are what actually guarantee safety
  if the validator has a gap.
- **Fail clearly, and keep state valid.** A turn either commits completely or
  is replaced with an explanation. A database outage or timeout never corrupts
  the conversation.

## What I'd improve with more time

1. **The share's 2020 decennial redistricting tables.** These are exact 2020
   Census counts (race, Hispanic origin, voting age, housing occupancy) rather
   than survey estimates. They need a second catalog loader for their different
   metadata format, and prompt rules for when to prefer a count over an estimate.

2. **Semantic field retrieval with Cortex Search.** BM25 plus a synonym list misses paraphrases ("folks without health insurance" vs "No health insurance coverage"). I'd also evaluate Cortex Analyst for the most common measures. The README section "Why not Snowflake Cortex?" covers the trade-offs and the migration path.
3. **Durable, shared session state** (Redis or Postgres). Today a restart loses
   conversations and the app can't scale past one instance.
4. **Pre-aggregated county and state tables** (a materialized view or dbt).
   Most questions are state- or county-level; summing 220k block groups each
   time costs latency and credits, and makes medians impossible. ACS publishes
   county and state tables directly, including true medians.
5. **Margins of error.** Report a proper aggregated MOE (root-sum-of-squares
   approximation) instead of only a caveat.
6. **Evaluation:** a larger, regularly refreshed eval set, an LLM-as-judge pass
   for answer quality (clarity, correct caveats), and a CI job that runs the
   live evals on prompt changes with a cost cap.
7. **Observability:** structured traces per turn (OpenTelemetry), with a
   dashboard for latency percentiles, step counts, guardrail decisions,
   grounding retries and token cost.
8. **Auth:** real user accounts (SSO) and per-user quotas instead of a shared
   Basic-auth credential and per-IP rate limiting.

## Edge cases and failure modes not fully addressed

- **Comparisons between releases.** 2019 and 2020 5-year estimates share
  2016–2019, so their difference mostly isn't real change, and it's often
  within the margin of error. The agent is told to say so every time, but it
  doesn't run a significance test, which would need the aggregated MOEs (see
  "Margins of error" above). The 2020 release was also affected by the
  pandemic's disruption to data collection.
- **Boundary changes between releases.** Counties are unioned by GEOID; one
  Alaska county exists only in 2020. Block groups can be redrawn, so the agent
  aggregates each year separately and never joins block groups across years.

- **Medians and other non-additive statistics** above block-group level. The
  agent explains the limitation and labels approximations, but it can't give
  the true county median.
- **Cities, ZIP codes, metro areas.** There's no place-level geography; the
  agent offers the containing county, which is only approximate for cities that
  span several counties.
- **What the grounding verifier can't catch.** It checks that the answer
  faithfully reports the query results, including arithmetic and release labels.
  In spot checks it caught a wrong percentage, a mislabelled release and an
  outside "national rate", and passed correct derived figures. But if the agent
  queries the *wrong field* (for example the wrong universe for a denominator),
  the answer will be consistent with wrong results and pass. That's now the main
  remaining class of error. The live evals with ground-truth SQL are the guard
  against it, and a field-selection review step would be the next layer.
- **The verifier fails open and adds latency.** If it errors or times out, the
  answer is shown unverified (logged as `verifier=error`). It adds about 2–4 s
  to answers containing figures, and up to ~7 s on long ones.
- **The guardrail classifier fails open.** If it errors or times out, the agent
  still answers. Its system prompt and read-only tools keep it in scope, but a
  cleverly phrased off-topic request is more likely to get through then.
- **Prompt injection through data.** Field labels come from the share's
  metadata, so they are trusted. Free-text values in query results could, in
  principle, carry instructions; this dataset has essentially none.
- **Long conversations** hit a turn limit (20) instead of being compacted.
  History is append-only so the model's thinking blocks stay valid; server-side
  compaction would be the next step.
- **Rate limiting and sessions are per process,** so they reset on deploy.
- **Cost controls** rely on the Snowflake resource monitor and Anthropic
  workspace limits. There is no per-user token budget.

## Testing approach

**Strategy:** test deterministic components exhaustively offline, test the
agent loop's *control flow* offline with a scripted model, and test *answer
quality* live with a small set of evals whose numeric answers are checked
against independent ground-truth SQL.

- **118 offline tests (~7 s):**
  - The SQL validator against write, DDL, multi-statement, cross-database, table-function and system-function attempts.
  - Catalog discovery and search, and geography resolution, including ambiguity.
  - Tool error mapping.
  - The agent loop: retry after SQL errors, an outage mid-turn leaving valid history, the deadline, the step limit, the grounding retry, refusals.
  - The HTTP layer: auth, SSE framing, degraded mode, rate limiting, request-id propagation into the logs.
  - The connection pool's concurrency, and spoof-resistant client-IP parsing.
- **Trade-off:** the offline suite runs on DuckDB, not Snowflake. Dialect gaps
  are papered over with sqlglot transpilation, so Snowflake-specific SQL
  failures only surface in the live evals.
- **Live evals:** ✍️ *report actual results, e.g. "18/20 on the first run.
  Failures were X and Y; I fixed X by …"*. Also report p50/p95 latency and cost
  per question.

**What I'd add:**
- Property-based tests for the SQL validator (generated hostile SQL).
- A contract test that runs a sample of model-written queries against real Snowflake in CI.
- Load tests for concurrent SSE streams.
- Regression evals built from logged production failures.
- Tracking eval scores over time, per prompt or model change.
