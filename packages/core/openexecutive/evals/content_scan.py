"""Run the outside-content check over its test set (``content_scan_cases.yaml``).

    uv run python -m openexecutive.evals.content_scan [--model M] [--runs N]

Each case goes through ``content_scan.scan`` (or ``scan_list``) exactly as a
read does, our own words taken out first, with a real model call, so it
needs a model key. A ``clean`` case must not be flagged, a ``flag`` case
must be; ``--runs`` checks each case that many times (the check is a model,
so one pass can hide a flaky answer). Prints one line per case and exits 1
on any miss.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

import yaml

CASES = Path(__file__).with_name("content_scan_cases.yaml")


def load(path: Path = CASES) -> list[tuple[str, dict[str, Any]]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [(want, case) for want in ("clean", "flag") for case in data.get(want, [])]


async def check(case: dict[str, Any], model: str | None) -> Any:
    from openexecutive.orchestrator import content_scan

    content_scan.reset()
    if case.get("list"):
        return await content_scan.scan_list(case["entries"], model=model)
    return await content_scan.scan(case["content"], model=model)


async def run(model: str | None, runs: int) -> int:
    misses = 0
    for want, case in load():
        for n in range(runs):
            verdict = await check(case, model)
            got = "flag" if verdict.flagged else "unchecked" if verdict.unchecked else "clean"
            ok = got == want
            misses += not ok
            evidence = f" ({verdict.evidence})" if verdict.evidence else ""
            print(f"{'ok  ' if ok else 'MISS'} {want:5} {case['id']}#{n + 1}: {got}{evidence}")
    print(f"{misses} missed")
    return 1 if misses else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="content_scan eval")
    parser.add_argument("--model", default=None, help="the check's model (default: content_scan.scan_model())")
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args(argv)
    return asyncio.run(run(args.model, max(1, args.runs)))


if __name__ == "__main__":
    sys.exit(main())
