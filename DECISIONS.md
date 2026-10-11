# Decision log

> ✍️ **Draft, generated from the build session. Edit it so every entry matches
> what you actually decided and why.** Delete anything you can't defend in the
> interview.

The significant decisions made while building this, roughly in order. Each
entry says what was chosen, what else was considered, and what triggered it.
Several were changed later after testing against the real data; those changes
are recorded too. All dates are 2026-10-10.

---

### D1. Tool-using agent with schema retrieval, not schema in the prompt
- **Chose:** Claude with four tools (`search_fields`, `describe_table`, `resolve_geography`, `run_sql`). A one-line-per-table overview sits in the system prompt; field-level detail is fetched on demand.
- **Alternatives:** put the full schema in the prompt (~8,000 columns, far too large); hard-code a curated subset of columns (the brief warns against limiting to a subset); a fixed text-to-SQL prompt (no way to explore the schema).
- **Why:** it covers every column, keeps the prompt small and cacheable, and each step is visible and testable.

### D2. Anthropic API rather than Snowflake Cortex
- **Chose:** Claude through the Anthropic API, with Snowflake used only for data.
- **Alternatives:** Cortex Analyst, Cortex Agents, `AI_COMPLETE`, Cortex Search. The full comparison is in the README, "Why not Snowflake Cortex?".
- **Why:** Cortex Analyst's semantic views are meant for curated subsets (Snowflake recommends about 50 columns), which conflicts with covering the whole dataset. A hand-built loop gives control over the deadline, the grounding checks and rolling back failed turns. The newest models and features arrive on the Anthropic API first.
- **Cost, stated openly:** query results leave Snowflake. Fine for public census data; it would be a governance issue for a customer with sensitive data.
- **Follow-up:** Cortex Search for field retrieval is the first planned improvement.

### D3. Hand-written agent loop, not an SDK tool runner or a framework
- **Chose:** a hand-written loop in `agent.py`.
- **Alternatives:** the Anthropic SDK tool runner; LangChain or LangGraph.
- **Why:** the brief grades behaviour on the edges: a hard 50 s deadline, a forced "wrap up" step, progress events, a step cap, and turns that are all-or-nothing (a failed turn never leaves an unanswered tool call in history). These are easier to guarantee and test in about 300 lines I fully control.

### D4. Three layers of safety for model-written SQL
- **Chose:** a static allow-list check with sqlglot (single SELECT, census tables only, no system functions, row cap), a read-only Snowflake role that can only see the share, and a statement timeout plus a credit cap.
- **Why:** the static check catches mistakes cheaply and gives the model an error to fix. The database permissions are the real guarantee if the check ever has a gap.

### D5. Python, FastAPI and streaming responses (SSE)
- **Chose:** Python, FastAPI, Server-Sent Events, and a single static HTML page.
- **Alternatives:** Streamlit (simpler, but less control over streaming and auth; Streamlit-in-Snowflake would need reviewer Snowflake logins); WebSockets.
- **Why:** async suits a workload that mostly waits on the model and database. SSE is the simplest way to stream progress and answers, and there's no front-end build step to break on deploy.

### D6. Staff-level review pass, then fixes
- **Trigger:** I asked for a critical review of the language and tool choices before going further.
- **Changed:**
  - All Snowflake queries ran one at a time behind a single connection; replaced with a connection pool.
  - The rate limiter trusted a client-controlled `X-Forwarded-For` entry; fixed by counting trusted proxy hops.
  - Startup blocked on Snowflake, which could fail health checks; the catalog now loads in the background.
  - Added uv lockfile, ruff, mypy, CI, and JSON logs with request IDs.

### D7. Haiku for the input guardrail, Opus for the agent
- **Chose:** `claude-haiku-5-5` (low effort) for classification; `claude-opus-5-5` (medium effort) for the agent.
- **Why:** the guardrail runs on every message and *is* the fast-fail path, so speed matters most there. Rejections take about 2 s in the evals. Aggregation correctness is a reasoning problem, so the agent gets the strongest model.
- **Related:** if Haiku refuses to classify, the message goes on to the agent rather than being blocked. A small classifier is more likely to wrongly flag legitimate race and income questions, and the agent has its own safety handling.

### D8. Fixed field search after seeing the real metadata
- **Trigger:** the first run against the real share returned *median earnings* for "median household income".
- **Cause:** each field's description levels repeat the table title and universe, which drowned out the words that tell sibling fields apart. Synonym expansion ("income" to "earnings") also counted at full weight.
- **Changed:** stripped the repeated levels from labels, down-weighted synonyms to 0.3, added plural folding, and updated the test fixtures to match the real metadata format.

### D9. Support both ACS releases (2019 and 2020)
- **Trigger:** listing the share showed 2020 ACS tables alongside 2019. The first version loaded only 2019.
- **Chose:** load both. Default to the latest release when no year is named, and always state which release was used.
- **Prompt rules added:** consecutive 5-year releases overlap by four years, so differences aren't year-over-year change; dollars are in each release's own year's dollars; never join block groups across years.
- **Deliberately not included:** the 2020 decennial redistricting tables (different metadata format; next data addition), and two small derived rent tables that duplicate ACS table B25070.

### D10. Rate limits per session, per IP and overall
- **Trigger:** working out how reviewers would reach the demo. Several reviewers behind one office IP would share a per-IP limit of 12 a minute.
- **Chose:** 10 a minute per browser session (fairness between users), 60 per IP (stops session-ID rotation), 120 overall (cost ceiling). A request refused by one limit doesn't count against the others.

### D11. Basic auth rather than a login page
- **Why:** the brief allows credentials, and the browser's built-in prompt can't break. The brief warns specifically about "authentication loops".

### D12. Targeted UI fixes, not a redesign
- **Chose:** a Stop button, Retry with a request ID on failures, a data-coverage note, and readable SQL without the internal row cap.
- **Rejected:** a redesign. UI isn't one of the graded dimensions, and the existing UI already streams answers, shows progress and shows the SQL.

### D13. Narrower follow-up rules
- **Trigger:** reading real answers by hand. After a Florida 2019-vs-2020 question, "Population of Washington?" also got an unrequested year comparison.
- **Changed:** short follow-ups inherit whatever they leave out; a complete question starts fresh. Added the eval case `no_stale_carry_forward`.

### D14. LLM grounding verifier on the output side
- **Trigger:** the brief's "ensure the agent stays within its grounded knowledge base". The only output check was a heuristic for large numbers.
- **Chose:** Haiku checks every data claim against that turn's tool results, allowing arithmetic derivations. One retry, then a visible caveat. If the check itself fails, the answer is shown unverified and logged.
- **Validated:**
  - It caught three planted errors: a wrong percentage, a wrong release label, an outside figure.
  - It passed correct derived and rounded figures.
  - The full evaluation passed 25/25 with it on.
- **Found by the eval:** one false alarm. The verifier only received SQL results, so a true count from the place lookup ("matched 32 places") looked unsupported. Fixed by passing every tool result, plus a regression test.
- **Known limit:** if the agent queries the wrong field, the answer matches the wrong results and passes. The evals with ground-truth SQL are the guard against that.

### D15. Not done, on purpose (for now)
- **Durable conversation storage (Redis or Postgres).** In-memory is fine for a single-instance demo; a restart loses conversations.
- **Significance testing for comparisons between releases.** Needs aggregated margins of error.
- **LLM-graded answer quality in the evals.** The current checks are numbers, keywords and outcomes.
- **Cortex Search** (see D2).
