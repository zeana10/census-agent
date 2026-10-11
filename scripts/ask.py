"""Ask the agent questions from the terminal, against the real stack (Claude + Snowflake).

    uv run python -m scripts.ask "What is the population of Texas?"
    uv run python -m scripts.ask "Population of Ohio?" "And in 2019?"   # a multi-turn conversation

Prints progress, the SQL that ran, the answer, and per-turn timing, verifier result
and token usage. A quick live smoke test that doesn't need the web server.
Needs a filled-in .env.
"""

import asyncio
import sys
import time

from app.config import get_settings
from app.main import Runtime
from app.sessions import Session


async def main(questions: list[str]) -> int:
    rt = Runtime(get_settings())
    agent = await rt.ensure_ready()
    if agent is None:
        print(f"Agent not ready: {rt.last_error}", file=sys.stderr)
        return 2
    session = Session(id="0" * 32)  # one conversation across all the questions
    ok = True
    for q in questions:
        t0 = time.monotonic()
        answer: list[str] = []

        async def emit(event: str, data: dict, t0: float = t0, answer: list[str] = answer) -> None:
            elapsed = f"[{time.monotonic() - t0:5.1f}s]"
            if event == "status":
                print(f"  {elapsed} {data['text']}")
            elif event == "sql":
                print(f"  {elapsed} SQL {'ok' if data['ok'] else 'FAILED'}: {' '.join(data['sql'].split())[:150]}")
            elif event == "delta":
                answer.append(data["text"])
            elif event in ("reset", "replace"):
                answer.clear()
                if event == "replace":
                    answer.append(data["text"])

        print(f"\nQ: {q}")
        stats = await agent.answer(session, q, emit)
        print("\n" + "".join(answer))
        print(
            f"\n--- {stats.outcome} in {time.monotonic() - t0:.1f}s | steps={stats.steps} "
            f"verifier={stats.verifier} retry={stats.grounding_retry} tokens={stats.usage}"
        )
        ok = ok and (stats.outcome == "ok" or stats.outcome.startswith("guardrail_"))
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1:])))
