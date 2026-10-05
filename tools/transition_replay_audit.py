"""Audit one-step transitions and public observations on saved live states."""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any

LAB_ROOT = Path(__file__).resolve().parents[1]
if str(LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(LAB_ROOT))

import constants  # noqa: E402
from data import all_move_json  # noqa: E402
from fp.belief_sampler import Gen9RandomBattleBeliefSampler  # noqa: E402
from fp.belief_transition import (  # noqa: E402
    BeliefTransitionModel,
    _action_parts,
)
from fp.live_reference import _public_state, battle_from_live_state  # noqa: E402
from fp.policy_oracle import FoulPlayPolicyOracle  # noqa: E402
from fp.public_observation import build_public_observation  # noqa: E402


SCHEMA_VERSION = "belief-transition-replay-audit/v1"


def _is_basic_action(action: str) -> bool:
    kind, value, _tera = _action_parts(action)
    if kind == "switch":
        return True
    if kind != "move" or value == "no move":
        return False
    category = all_move_json.get(value, {}).get(constants.CATEGORY)
    return category in {constants.PHYSICAL, constants.SPECIAL}


def _hidden_fields_stable(battle: Any, observation: Any) -> bool:
    changed = copy.deepcopy(battle)
    if changed.user.active is not None:
        changed.user.active.item = "hidden-test-item"
        changed.user.active.ability = "hidden-test-ability"
        changed.user.active.add_move("protect")
    payload = observation.to_dict()
    reveals: dict[str, dict[str, dict[str, Any]]] = {}
    for side, rows in (payload.get("sides") or {}).items():
        for row in rows or ():
            key = str(row.get("name") or "").lower()
            if not key:
                continue
            item: dict[str, Any] = {}
            if row.get("movesKnown"):
                item["moves"] = list(row.get("moves") or ())
            if row.get("itemKnown"):
                item["item"] = row.get("item")
            if row.get("abilityKnown"):
                item["ability"] = row.get("ability")
            if item:
                reveals.setdefault(side, {})[key] = item
    changed_observation = build_public_observation(changed, reveals=reveals)
    return observation.observation_hash == changed_observation.observation_hash


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(fraction * (len(ordered) - 1)))
    return round(ordered[index], 3)


def audit(
    input_path: Path,
    output_path: Path,
    limit: int,
    search_time_ms: int,
    seed: int,
) -> dict[str, Any]:
    oracle = FoulPlayPolicyOracle()
    sampler = Gen9RandomBattleBeliefSampler(seed=seed)
    transition_model = BeliefTransitionModel(allow_approximate_preview=True)
    fallback_reasons: Counter[str] = Counter()
    basic_fallback_reasons: Counter[str] = Counter()
    transition_latencies: list[float] = []
    exceptions: list[dict[str, Any]] = []
    hidden_leaks = 0
    decision_points = 0
    outcome_count = 0
    valid_outcomes = 0
    invalid_outcomes = 0
    basic_pairs = 0
    basic_pair_fallbacks = 0

    with input_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if decision_points >= limit:
                break
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                state = record["state"]
                public = _public_state(state)
                particle = sampler.sample(public, "p2", 1)[0]
                battle = battle_from_live_state(state, particle)
                our_action = oracle.evaluate(
                    battle,
                    perspective="self",
                    search_time_ms=search_time_ms,
                ).best_action
                opponent_action = oracle.evaluate(
                    battle,
                    perspective="opponent",
                    search_time_ms=search_time_ms,
                ).best_action
                if not our_action or not opponent_action:
                    raise ValueError("oracle returned an empty action")
                is_basic = _is_basic_action(our_action) and _is_basic_action(
                    opponent_action
                )
                if is_basic:
                    basic_pairs += 1
                started = time.perf_counter()
                outcomes = transition_model.apply(
                    battle,
                    our_action,
                    opponent_action,
                )
                transition_latencies.append(
                    (time.perf_counter() - started) * 1000.0
                )
                decision_points += 1
                for outcome in outcomes:
                    outcome_count += 1
                    if outcome.valid:
                        valid_outcomes += 1
                        if outcome.public_observation is not None and not _hidden_fields_stable(
                            outcome.state,
                            outcome.public_observation,
                        ):
                            hidden_leaks += 1
                    else:
                        invalid_outcomes += 1
                        reason = outcome.fallback_reason or "unknown"
                        fallback_reasons[reason] += 1
                        if is_basic:
                            basic_pair_fallbacks += 1
                            basic_fallback_reasons[reason] += 1
            except Exception as error:
                exceptions.append(
                    {
                        "line": line_number,
                        "type": type(error).__name__,
                        "message": str(error),
                    }
                )
                decision_points += 1

    report = {
        "schemaVersion": SCHEMA_VERSION,
        "backend": "approximate-preview-only",
        "rulesFidelityVerified": False,
        "input": str(input_path),
        "limit": limit,
        "searchTimeMs": search_time_ms,
        "seed": seed,
        "decisionPoints": decision_points,
        "outcomeCount": outcome_count,
        "validOutcomes": valid_outcomes,
        "invalidOutcomes": invalid_outcomes,
        "fallbackRate": (
            invalid_outcomes / outcome_count if outcome_count else None
        ),
        "fallbackReasons": dict(fallback_reasons),
        "basicActionPairs": basic_pairs,
        "basicActionPairFallbacks": basic_pair_fallbacks,
        "basicFallbackReasons": dict(basic_fallback_reasons),
        "exceptionCount": len(exceptions),
        "exceptions": exceptions[:20],
        "hiddenLeakCount": hidden_leaks,
        "transitionLatencyMs": {
            "count": len(transition_latencies),
            "p50": median(transition_latencies) if transition_latencies else None,
            "p95": _percentile(transition_latencies, 0.95),
            "max": max(transition_latencies) if transition_latencies else None,
        },
        "basicGatePass": (
            not exceptions
            and hidden_leaks == 0
            and basic_pair_fallbacks == 0
        ),
        "rolloutReady": False,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, ensure_ascii=True, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--search-time-ms", type=int, default=10)
    parser.add_argument("--seed", type=int, default=17)
    args = parser.parse_args()
    report = audit(
        args.input,
        args.output,
        args.limit,
        args.search_time_ms,
        args.seed,
    )
    print(json.dumps(report, ensure_ascii=True, indent=2))
    if not report["rolloutReady"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
