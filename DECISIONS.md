# Decision log

These are the choices that shaped the project, roughly in the order I made
them, with what I considered instead and what pushed me one way. A few of them
were reversals: something I believed until the real data or a real deployment
proved otherwise. Everything here happened on 2026-10-10.

---

### 1. Let the model look up the schema instead of pasting it into the prompt

The share has about 8,000 columns with names like `B19013e1`. There's no way
to fit that in a prompt. Hand-picking the 50 "important" ones would go against
the brief's tip not to limit yourself to a subset.

So the prompt gets a one-line summary per census table. The model has four
tools: search the field descriptions, list a table's fields, turn a place name
into its code, and run SQL. It finds the exact column when it needs it.

*Considered:* the whole schema in the prompt (too big); a curated subset (goes
against the brief); a fixed text-to-SQL prompt (no way to explore).

### 2. Claude through the Anthropic API, not Snowflake Cortex

This is the decision I expect to be asked about most, so the README has a full
comparison. The short version:
- **Cortex Analyst** works from curated semantic views, and Snowflake
  recommends keeping those to about 50 columns. That's the opposite of covering
  the whole dataset.
- **Cortex Agents** would have saved me writing the loop, but I wanted direct
  control over the deadline, the grounding checks and how failed turns are
  thrown away.

The honest cost: query results leave Snowflake to go to Anthropic. That's fine
for public census data, but it would be a real governance conversation for a
customer with private data. Cortex Search is the first thing I'd bring in, for
field search.

### 3. Write the agent loop myself

*Considered:* the Anthropic SDK's tool runner, LangChain, LangGraph.

What the brief cares about is behaviour at the edges:
- a hard time limit per question;
- a forced "answer with what you have" when time runs low;
- progress updates while it works;
- never leaving the conversation broken when something fails halfway.

All of that is easier to guarantee, and to test, in about 300 lines I fully
understand than inside a framework.

### 4. Three layers of protection on model-written SQL

1. **A parser-based check** before anything runs: a single SELECT, census
   tables only, no system functions, and a row cap.
2. **A Snowflake role** that can only read the census share.
3. **A statement timeout and a credit cap** on the warehouse.

The first layer is about catching mistakes early and handing the model an error
it can fix. The second and third are the real guarantee if the first one ever
has a gap.

### 5. Python, FastAPI and a plain HTML page with streaming

Most of the time is spent waiting on the model and the database, which suits
async Python. Server-sent events are the simplest way to stream progress lines
and the answer. A single static HTML page means no front-end build that could
break on deploy.

*Considered:* Streamlit. It's quicker to start, but gives less control over
streaming and login, and Streamlit-in-Snowflake would have needed every
reviewer to have a Snowflake login.

### 6. Asking for a staff-level review before going further

Once the first version worked offline, I asked for a critical review of the
stack, as if from a staff engineer. Things it caught and I fixed:
- **Concurrency:** all Snowflake queries went through one locked connection,
  so two users would wait on each other. Now it's a connection pool.
- **Rate limiting:** the limiter read the client IP from a header the client
  controls. Now it only trusts entries added by known proxies.
- **Tooling:** no lockfile, linting, type checking, CI or structured logs. All
  added.

Writing the CI check that boots the container without credentials then exposed
another one: startup waited on Snowflake, so a slow or failed connection could
stop the server answering health checks at all. The catalog now loads in the
background.

### 7. A small fast model for gatekeeping, the strongest one for the agent

The input classifier first ran on the same big model as the agent. I moved it
(and later the output verifier) to Haiku. It runs on every single message and
*is* the fast-fail path, so speed is what matters there; rejections now take
about 2 seconds. The agent stays on Opus because getting aggregations right is
genuinely a reasoning problem.

If Haiku refuses to classify something, the message goes to the agent rather
than being blocked. A small classifier is more likely to wrongly flag a
legitimate question about race and income, and the agent has its own safety
handling.

### 8. Fixing search once I saw the real metadata

The first real query for "median household income" came back with median
*earnings*. Each field's description repeats its table title and population
group, so every field in a table looked the same to keyword search. And my
synonym list counted "income" → "earnings" as strongly as the real words.

The fix:
- strip the repeated parts from labels;
- give synonyms much less weight;
- treat plurals as their singular, so "veterans" finds "veteran".

The bigger lesson was that my test fixtures didn't look like the real
metadata, which is why no test caught this. I rebuilt them to match, quirks
included.

### 9. Supporting both 2019 and 2020

Listing the real tables showed the share has two ACS releases, and I'd only
loaded 2019. Ignoring 2020 would have made the agent refuse "how did Texas
change?" when the data was sitting right there.

Now both are loaded:
- When no year is named, it uses 2020 and says so.
- Comparisons always come with two caveats: the two releases share four of
  their five survey years, so the difference isn't a clean one-year change, and
  each release reports dollars in its own year's dollars.
- It never matches block groups across years, because boundaries can change.

*Left out on purpose:* the 2020 Census redistricting tables, which use a
different metadata format and need their own loader, and two small derived rent
tables that duplicate an existing ACS table.

### 10. Three rate limits instead of one

When I thought through how reviewers would actually use the demo, I realised
several of them might sit behind one office IP, sharing a single "12 per
minute" allowance. Now there are three limits:
- **10 per browser session:** fairness between people.
- **60 per IP:** stops someone dodging the first limit by faking new sessions.
- **120 overall:** a cost ceiling.

A request turned away by one limit doesn't count against the others.

### 11. The browser's built-in login instead of a login page

The brief allows a shared credential and warns about "authentication loops". The
browser's own username and password prompt looks plain, but it can't loop or
break.

### 12. A few UI fixes, not a redesign

UI isn't one of the things being graded, and the page already streamed answers
and showed its SQL. I only added what a reviewer would actually hit:
- a Stop button;
- a Retry button with a reference ID when something fails;
- a note on what data is covered;
- readable SQL in the "How I got this" panel.

### 13. Tightening how follow-up questions work

Every eval case passed, but reading the answers by hand turned up a problem.
After a Florida "2019 vs 2020" question, "Population of Washington?" got an
unrequested year comparison. My prompt said to carry context forward, and the
model did it for *every* question.

The new rule: short follow-ups like "and Ohio?" or "in 2019?" inherit what they
leave out, and a complete question starts fresh. I added an eval case so it
can't quietly come back.

### 14. An output check for grounding

The input side was well covered, but on the output side I only had a heuristic
for large numbers stated with no query behind them. I added a verifier: Haiku
reads the answer and checks every figure against that turn's tool results,
allowing for arithmetic such as percentages and sums. If something isn't
supported, the agent gets one retry, then the answer goes out with a visible
caveat.

How it was tested:
- **On planted errors** (a wrong percentage, a wrong release label, an outside
  figure): it caught all three.
- **On correct derived numbers** that it shouldn't flag: it left them alone.
- **In the next full eval run:** one false alarm. I'd only given it the SQL
  results, so a true fact from the place lookup ("the name matched 32 places")
  looked invented. I fixed it by giving it all the evidence, not by making it
  more lenient.

What it can't do: if the agent queries the *wrong field*, the answer matches
the wrong results and passes. The evals' independent ground-truth SQL is the
guard there.

### 15. Moving the project out of iCloud

My project lived in Documents, which syncs to iCloud, on a nearly full disk.
macOS had moved most of the project files to the cloud, so tests went from 7
seconds to several minutes. Then iCloud was switched off and the local copy came
back incomplete.

Everything was already on GitHub, so I cloned it into a normal folder
(`~/code`) and only had to restore my local secrets. It reinforced pushing
early and keeping code out of synced folders.

### 16. Deploying on Render, and measuring rather than assuming

I chose Render because the whole thing ships as one Docker container with a
blueprint file, and the paid Starter instance doesn't go to sleep while
reviewers are looking.

Two decisions came out of the deployment:
- **Trusted proxy hops = 2.** My rate limiter assumed one proxy in front of
  the app. Render's docs didn't say, so I added header counts (no IP addresses)
  to `/healthz` and measured it. There are two hops, Cloudflare and then
  Render's load balancer. Before the fix, the per-IP limit was counting
  Cloudflare's data centres, not visitors.
- **Check config on the running service.** Updating `render.yaml` and pushing
  redeployed the code but didn't apply the new variable. `/healthz` reports
  the live value, which is how I caught it.

I deployed late in the day. Next time I'd deploy a bare skeleton first, since
both of these were cheap to fix and only showed up on the real platform.

### 17. Degrade at the deadline instead of apologising, and fix how medians are estimated

The first outside tester asked for "population of all states and median income"
and got a timeout, even though the query had finished in 2 seconds. Three
decisions came out of it:
- **What happens at the deadline.** If time runs out, keep what's been written
  (with a note that it was cut short and not double-checked), or show the query
  results as a plain table. Only apologise when there's nothing at all. The old
  behaviour, throwing away finished work, was the opposite of "degrade
  gracefully".
- **Where per-row work happens.** For questions covering many places, the
  agent now does the arithmetic in SQL and joins the FIPS table for state names,
  instead of calculating while writing or looking each state up separately.
- **How medians are estimated.** I'd suggested averaging block-group medians.
  Checked against published figures, that was about 15% too high for California
  and Texas. Interpolating within the income brackets is within about 1%, so
  that's now required. I also stopped the prompt from quoting that accuracy
  figure, because the agent started repeating it to users as if it were data,
  and the verifier rightly flagged it.

The question is now an eval case, and its check would fail if the biased median
came back.

### 18. Things I chose not to do (yet)

- **Durable conversation storage.** Memory is fine for a one-instance demo;
  Redis would be the next step.
- **Significance tests for year comparisons.** These need properly combined
  margins of error first.
- **LLM-graded answer quality in the evals.** That's the gap the follow-up bug
  slipped through.
- **Running the live evals in CI.** Each run costs about 75¢ and needs real
  credentials, so for now I run them by hand, and the results of the latest run
  are committed (`evals/latest_run.json`).
