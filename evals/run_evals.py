"""Run the live eval set against the real agent (Claude + Snowflake) in-process.

    python -m evals.run_evals                 # all cases
    python -m evals.run_evals -k ambiguous    # cases whose id contains "ambiguous"

Costs real API tokens and warehouse credits (~20 cases, a few cents each).
Writes evals/results-<timestamp>.json and prints a pass/fail table with latency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

import yaml

from app.config import get_settings
from app.main import Runtime
from app.sessions import Session

HERE = Path(__file__).parent


def _numbers(text: str) -> list[float]:
    return [
        float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*\.?\d*", text) if n.replace(",", "").replace(".", "").isdigit()
    ]


def check(case: dict, answer: str, stats, target: float | None = None, tables: list[dict] | None = None) -> list[str]:
    fails = []
    if case.get("expect_table_rows") is not None:
        rows = max((len(t["rows"]) for t in tables or []), default=0)
        if rows < case["expect_table_rows"]:
            fails.append(f"expected a table with >= {case['expect_table_rows']} rows, got {rows}")
    low = answer.lower()
    if case.get("expect_outcome") and stats.outcome not in case["expect_outcome"]:
        fails.append(f"outcome {stats.outcome} not in {case['expect_outcome']}")
    if case.get("expect_any") and not any(s.lower() in low for s in case["expect_any"]):
        fails.append(f"none of {case['expect_any']} in answer")
    for s in case.get("expect_none") or []:
        if s.lower() in low:
            fails.append(f"forbidden text {s!r} in answer")
    if case.get("min_queries") and stats.queries_ok < case["min_queries"]:
        fails.append(f"only {stats.queries_ok} successful queries")
    if target is not None:
        tol = case.get("rel_tol", 0.01) * target
        if not any(abs(n - target) <= tol for n in _numbers(answer)):
            fails.append(f"expected ~{target:,} in answer")
    if stats.outcome in {"internal_error", "timeout", "llm_error", "warehouse_unavailable"}:
        fails.append(f"failure outcome {stats.outcome}")
    return fails


async def main(filter_: str | None) -> int:
    cases = yaml.safe_load((HERE / "cases.yaml").read_text())
    if filter_:
        cases = [c for c in cases if filter_ in c["id"]]
    rt = Runtime(get_settings())
    agent = await rt.ensure_ready()
    if agent is None:
        print(f"agent not ready: {rt.last_error}")
        return 2

    results, passed = [], 0
    for case in cases:
        target = None
        if case.get("ground_truth_sql"):
            target = float((await rt.wh.query(case["ground_truth_sql"], max_rows=1)).rows[0][0])
        session = Session(id=case["id"])
        answer, stats, slowest = "", None, 0.0
        for q in case["turns"]:
            t0 = time.monotonic()
            chunks: list[str] = []
            tables: list[dict] = []

            async def emit(ev, data, chunks=chunks, tables=tables):
                if ev == "table":
                    tables.append(data)
                if ev == "delta":
                    chunks.append(data["text"])
                elif ev in ("reset", "replace"):
                    chunks.clear()
                    if ev == "replace":
                        chunks.append(data["text"])

            stats = await agent.answer(session, q, emit)
            answer = "".join(chunks)
            slowest = max(slowest, time.monotonic() - t0)
        elapsed = slowest  # the 60 s requirement is per turn
        fails = check(case, answer, stats, target, tables)
        if elapsed > 60:
            fails.append(f"too slow: {elapsed:.1f}s")
        passed += not fails
        print(
            f"{'PASS' if not fails else 'FAIL'}  {case['id']:<26} {elapsed:5.1f}s  {stats.outcome:<24} "
            f"verifier={stats.verifier:<8} {'; '.join(fails)}",
            flush=True,
        )
        results.append(
            {
                "id": case["id"],
                "category": case["category"],
                "pass": not fails,
                "fails": fails,
                "elapsed_s": round(elapsed, 2),
                "outcome": stats.outcome,
                "steps": stats.steps,
                "verifier": stats.verifier,
                "grounding_retry": stats.grounding_retry,
                "usage": stats.usage,
                "answer": answer,
            }
        )

    out = HERE / f"results-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"\n{passed}/{len(cases)} passed  ->  {out}")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", dest="filter", default=None)
    sys.exit(asyncio.run(main(ap.parse_args().filter)))
