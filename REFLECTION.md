# Reflection

## How I built it

I used Claude Code for nearly all of the code. My job was deciding what to
build, pushing back on it, and checking that what came out was actually right.
Most of what I'm proud of here came from that checking, not from the first
draft.

The rough order things happened in:

**1. Design and an offline build.** I read the brief and settled on the
architecture before touching the data: a tool-using agent that looks up the
schema as it needs it, rather than a prompt stuffed with column names. I didn't
have Snowflake access yet, so the first version was built and tested entirely
offline. A small DuckDB database with the same table and column names stood in
for Snowflake, and a scripted fake model stood in for Claude. That let me test
the hard parts (SQL validation, the agent loop's error handling, deadlines)
before spending anything.

**2. A staff-level review of my own choices.** Before going further I asked
for a critical review of the stack, as if a staff engineer were reading it. It
found real problems:
- every Snowflake query ran one at a time behind a single lock;
- the rate limiter could be bypassed with a fake `X-Forwarded-For` header;
- there was no lockfile, linting, type checking or CI.

I fixed all of those, and wrote up why I'm not using Snowflake Cortex (in the
README) instead of leaving it as an unexplained gap.

**3. First contact with the real data.** 
Setting up the Snowflake trial took a few attempts:
- the share's database name was slightly different from what I'd assumed;
- my first run of the setup script only executed one statement.

Once the app could connect, three things came up:
- **The data has 2020 too.** The share has both the 2019 and 2020 ACS
  releases, and my code only loaded 2019. That contradicted the brief's own tip
  about not limiting yourself to a subset, so I added both, with rules for
  comparing them honestly.
- **Search returned the wrong field.** It answered "median household income"
  with median *earnings*. The metadata repeats each table's title inside every
  field label, which drowned out the words that tell fields apart.
- **My test data didn't look like the real thing,** which is why the tests
  hadn't caught the search problem. I rebuilt the fixtures to copy the real
  metadata layout, quirks included (one column in the share is literally named
  `FIELD_LEVELl_9`).

**4. Live runs and the eval set.** The very first live question failed. A log
line read an attribute that streamed responses don't have. The error handling
did its job (the user got an apology), but it showed I had no tests
on the real Anthropic wrapper, so I added some. After that the 24-case eval set
passed on its first run.

**5. Reading the answers, not just the scores.** Passing evals weren't the end
of it. When I read the answers myself, I found a problem none of the checks
flagged: after a Florida "2019 vs 2020" question, asking "Population of
Washington?" got an unrequested year comparison tacked on. The prompt said to
carry context forward, and the model did it for every question, not just
follow-ups like "and Ohio?". I narrowed that rule and added an eval case for it.

**6. A real output check.** The brief asks for guardrails that keep the agent
"grounded". The input side was solid, but on the output side I only had a
heuristic for big numbers. So I added a verifier: a small model checks every
figure in an answer against the tool results from that turn. I tested it both
ways:
- it caught a wrong percentage, a wrong release label and an outside figure I
  planted;
- it left correct derived numbers alone.

Even so, the next full eval run turned up a false alarm. It had only been given
SQL results, so a true count from the place lookup ("matched 32 places") looked
made up. I fixed it by giving it every tool result, not by making it less
strict.

**7. Shipping, and a detour.** I'd put the project in my Documents folder,
which syncs to iCloud. My disk was nearly full, so macOS quietly moved most of
the project files to the cloud. Tests that took 7 seconds suddenly took minutes.
Then iCloud got switched off and the local copy came back incomplete. Because
everything was already committed and pushed to GitHub, I cloned it fresh into a
normal folder and only had to recover my local secrets. Lesson learned: commit
and push early, and keep code out of synced folders.

**8. Deployment, late.** I deployed to Render at the end, and I'd do that
differently next time. CI had already built the Docker image and booted it with
no credentials, and writing that check is how I found that startup blocked on
Snowflake (now it loads in the background). But the real deployment still taught
me two things CI couldn't:
- **The proxy chain.** Render routes traffic through Cloudflare *and* its own
  load balancer, so my rate limiter was reading Cloudflare's IP as the
  visitor's. The docs didn't say how many hops there were, so I made `/healthz`
  report the header shape and measured it: two hops.
- **Config doesn't follow a push.** Changing `render.yaml` and pushing didn't
  update the running service's environment variables. I only noticed because
  `/healthz` reports the live value.

**9. Someone else tried it.** The first person outside the project to use the
live demo asked for "population of all states and median income", and got
"Sorry, that question took too long". All my tests had been about one or two
places. When I reproduced it, three separate things turned up:
- **It threw away good data.** The query had finished in 2 seconds; it was
  writing a 52-row answer that ran past the deadline. Then the app replaced
  everything with an apology. Now, when the deadline hits, it keeps whatever
  answer it has written so far, or shows the query's results as a table. It
  only apologises when there's genuinely nothing to show.
- **It spent its time badly.** It worked out each state's median while writing
  the answer, and in one run it looked up all 53 state names one tool call at a
  time. The prompt now says to do per-row maths in SQL and to join the state
  names table in the same query.
- **One of its estimates was wrong.** Averaging block-group medians, which my
  own prompt had suggested, overstated California and Texas by about 15%.
  Interpolating within the income brackets lands within about 1% of the
  published figures. The prompt now requires that method.

Same question, after the fixes: about 35–45 seconds, accurate medians, and it's
now eval case 26. The lesson is the uncomfortable one: my eval set reflected the
questions *I* thought to ask, and the first other person found a gap in a few
minutes.

### Mistakes I'd call out

Most of these were bugs in code Claude wrote that I didn't catch on the first
read. Tests, live runs or reading output found them:
- The SQL check capped results at exactly the row limit, so the app could never
  tell when a result had been cut off. A test caught it.
- The "Rank" column header wrapped as "Ran / k". Small, but a reviewer would
  notice.
- Turning on mypy found several places where a value could be missing but the
  code assumed it wasn't.
- The streaming log bug, the stale carry-forward and the verifier false alarm
  above.

The pattern I took away: generated code was usually right on the main path, and
the problems were at the edges, in places that only show up with real data, a
real deployment, or someone actually reading the output.

## Key decisions

`DECISIONS.md` has the full list with alternatives. The ones I'd defend
hardest:

- **The model looks the schema up; I don't paste it in.** There are about
  8,000 columns with codes like `B19013e1`. The prompt gets a one-line summary
  per table, and the model searches for specific fields when it needs them.
  This covers the whole dataset without hard-coding anything.
- **Code handles anything with a right answer.** Turning "Cook County" into
  its FIPS code, checking SQL is safe, capping rows: these are deterministic
  code, not left to the model.
- **Three layers of SQL safety.** A parser-based allow-list catches mistakes
  cheaply and gives the model an error it can fix. The real guarantee is that
  the Snowflake role can only read the census share, plus a statement timeout
  and a credit cap.
- **A turn either completes or never happened.** If the database goes down or
  the deadline hits mid-turn, that turn is thrown away and replaced with a plain
  explanation. The stored conversation never ends up in a broken state.
- **Haiku for the gatekeeping, Opus for the thinking.** The input classifier
  and the output verifier run on every message, so they use the fast model.
  Getting aggregations right is a reasoning problem, so the agent gets the
  strongest one.
- **Census correctness rules live in the prompt and the evals.** You can't add
  up block-group medians into a county median. Consecutive 5-year releases
  overlap by four years. Dollar figures are in each release's own year's
  dollars. These are the mistakes that would look completely plausible in an
  answer, so they're spelled out and tested.

## What I'd do with more time

1. **Deploy a skeleton in the first hour.** Both of the issues deployment
   found were cheap to fix, but I found them at the end of the day.
2. **Better field search.** Keyword search plus a synonym list still misses
   paraphrases ("folks without health insurance"). Snowflake's Cortex Search is
   the obvious next step and would slot in behind the existing search function.
3. **The 2020 Census redistricting tables.** These are exact counts rather
   than survey estimates. They use a different metadata format, so they need
   their own loader and rules for when a count beats an estimate.
4. **Pre-aggregated state and county tables.** Most questions are about
   states or counties, and summing 220,000 block groups every time is slow and
   rules out true medians. The Census Bureau publishes county-level tables
   directly.
5. **Margins of error.** Report a proper combined margin of error instead of
   just a caveat. That would also let the agent say whether a 2019-vs-2020
   difference is meaningful.
6. **Evals that judge quality, not just keywords.** My checks are numbers,
   keywords and outcomes. They passed while the stale carry-forward was
   happening. An LLM-graded pass would have caught it.
7. **Durable conversations.** They live in memory, so a restart loses them and
   I can only run one instance. Redis would fix both.
8. **Proper accounts** instead of one shared demo login, with per-user limits.

## Edge cases I know about but didn't fully solve

- **The verifier can't catch the wrong field.** It checks that the answer
  matches the query results. If the agent queries the wrong field, for example
  the wrong population as a denominator, the answer will faithfully report wrong
  numbers and pass. That's the main remaining risk. The evals with independent
  ground-truth SQL are what guard against it.
- **Medians above block-group level.** The agent explains why it can't give a
  true county median and offers a clearly labelled estimate. For King County it
  came within 0.2% of the published figure, but it's still an estimate.
- **Cities and ZIP codes** aren't in the data. The agent offers the county
  instead, which is only approximate for cities that span several counties.
- **Comparing 2019 and 2020.** The agent always gives the overlap caveat, but
  it doesn't test whether a difference is statistically significant. Boundaries
  also change between releases, so it never matches block groups across years.
- **Both guardrails fail open.** If the classifier or the verifier errors or
  times out, the answer still goes out (unverified, and logged). I chose
  availability over strictness for a demo; a customer deployment might choose
  differently.
- **Long conversations** stop at 20 turns rather than being summarised.
- **Rate limits and sessions reset on every deploy,** because they're in
  memory.
- **Costs** are capped by Snowflake's resource monitor and an Anthropic spend
  limit. There's no per-user budget.

## Testing

I split testing by what each kind of test is good at:
- **Deterministic parts, tested thoroughly offline.**
- **The agent loop's control flow, tested offline** with a scripted model.
- **Answer quality, tested live** against real data.

**Offline (122 tests, about 10 seconds, no credentials).** These cover:
- the SQL validator against injection and escape attempts;
- schema discovery, field search and place lookup, including ambiguous names;
- the agent loop under failure: SQL errors, the database dropping mid-turn,
  deadlines, step limits, the verifier's retry, refusals;
- the web layer: login, streaming, degraded mode, the three rate limits;
- the connection pool.

CI runs all of it on every push, then builds the Docker image and boots it with
no credentials.

The trade-off is that the offline tests run on DuckDB, not Snowflake.
Snowflake-specific SQL problems only show up live.

**Live (26 cases, about 8 minutes, about 75¢).** These are lookups, ratios,
rankings, follow-ups, year comparisons, ambiguous and unanswerable questions,
and adversarial ones. For the cases with a specific number, the runner works
out the right answer with its own hand-written SQL, independent of the agent,
and checks the answer matches.
- **Results:** 24/24 on the first run; 26/26 on the latest, with the verifier
  switched on.
- **Speed:** median turn about 13 seconds, slowest about 38 (every state at once). Guardrail rejections about
  2 seconds.
- **Cost:** about 4 cents per answered question, with most of the prompt
  served from cache.

The evals passing isn't the whole story, though. They passed while the stale
carry-forward problem was happening, and I only found it by reading answers.
That's the clearest gap in my testing.

**What I'd add:**
- an LLM-graded quality check on the eval answers;
- generated hostile SQL to fuzz the validator;
- a CI job that runs a sample of model-written SQL against real Snowflake;
- load tests for many streaming users at once;
- tracking eval scores over time so prompt changes can't silently regress.
