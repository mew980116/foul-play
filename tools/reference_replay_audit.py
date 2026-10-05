"""Replay saved decision inputs through the candidate policy without PS access."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import time

LAB_ROOT = Path(__file__).resolve().parents[1]
if str(LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(LAB_ROOT))

from fp.live_reference import LiveReferencePolicy  # noqa: E402
from tools.live_reference_server import (  # noqa: E402
    TRANSLATION_VERSION,
    choose_legal_action,
    request_forces_switch,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--search-time-ms", type=int, default=100)
    args = parser.parse_args()
    if args.output.exists() or args.limit <= 0:
        parser.error("use a new output path and a positive limit")
    with args.input.open(encoding="utf-8") as source:
        rows = [json.loads(line) for line in source if line.strip()]
    available = [(i + 1, r["state"]) for i, r in enumerate(rows) if r.get("state")]
    if len(available) > args.limit:
        available = [available[i * len(available) // args.limit]
                     for i in range(args.limit)]
    policy = LiveReferencePolicy(particle_count=0, search_time_ms=args.search_time_ms,
                                 mode="upstream-compatible", seed=17)
    records = []
    counts = Counter()
    for line_no, state in available:
        record = {"line": line_no, "battleId": state.get("battleId"),
                  "turn": state.get("turn"), "rqid": state.get("rqid"),
                  "forcedSwitch": request_forces_switch(state)}
        start = time.perf_counter()
        before = json.dumps(state, sort_keys=True)
        try:
            result = policy.evaluate(state)
            action, selected, fallback, reason = choose_legal_action(
                state, result.best_action, result.actions
            )
            record.update(rawBestAction=result.raw_best_action,
                          bestAction=result.best_action, action=action,
                          selectedAction=selected, selectionFallback=fallback,
                          selectionFallbackReason=reason)
            counts["successful"] += 1
            if fallback:
                counts["forcedSwitchProjection" if record["forcedSwitch"]
                       else "ordinaryProjection"] += 1
        except Exception as error:
            record["error"] = str(error)
            counts["errors"] += 1
        record["latencyMs"] = round((time.perf_counter() - start) * 1000, 3)
        if before != json.dumps(state, sort_keys=True):
            counts["inputMutations"] += 1
        counts["attempted"] += 1
        records.append(record)
    paths = [Path(__file__), LAB_ROOT / "fp/live_reference.py",
             LAB_ROOT / "fp/search/poke_engine_helpers.py",
             LAB_ROOT / "fp/belief_sampler.py", LAB_ROOT / "fp/policy_oracle.py",
             LAB_ROOT / "tools/live_reference_server.py"]
    report = {
        "schemaVersion": "offline-reference-policy-audit/v1",
        "translationVersion": TRANSLATION_VERSION,
        "mode": "upstream-compatible", "seed": 17,
        "searchTimeMs": args.search_time_ms, "searchParallelism": 1,
        "sampling": "evenly-spaced saved states, no live execution",
        "input": str(args.input.resolve()),
        "inputSha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
        "sourceSha256": {str(p.relative_to(LAB_ROOT)):
                         hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
        "counts": dict(counts), "records": records,
        "performanceEquivalenceVerified": False,
    }
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=True)
    print(json.dumps(dict(counts), sort_keys=True))
    print(f"Report: {args.output}")


if __name__ == "__main__":
    main()
