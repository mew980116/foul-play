"""Audit saved request stats/HP and active boosts at the engine boundary."""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys

LAB_ROOT = Path(__file__).resolve().parents[1]
if str(LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(LAB_ROOT))

from fp.belief_sampler import Gen9RandomBattleBeliefSampler  # noqa: E402
from fp.live_reference import _public_state, battle_from_live_state  # noqa: E402
from fp.search.poke_engine_helpers import battle_to_poke_engine_state  # noqa: E402


STATS = {
    "atk": "attack",
    "def": "defense",
    "spa": "special_attack",
    "spd": "special_defense",
    "spe": "speed",
}


def normalize(value):
    return re.sub("[^a-z0-9]", "", str(value).lower())


def inspect_state(state):
    particle = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )[0]
    battle = battle_from_live_state(deepcopy(state), particle)
    engine = battle_to_poke_engine_state(deepcopy(battle))
    checks = Counter()
    differences = []

    def check(field, expected, actual):
        checks[field.split(".")[0]] += 1
        if expected != actual:
            differences.append(
                {"field": field, "expected": expected, "actual": actual}
            )

    team = {normalize(p.id): p for p in engine.side_one.pokemon}
    for row in state["request"]["side"]["pokemon"]:
        species = normalize(row["details"].split(",", 1)[0])
        if species not in team:
            differences.append({"field": "identity", "species": species})
            continue
        target = team[species]
        if "terastallized" in row:
            tera_used = bool(row["terastallized"])
            if not tera_used and "fnt" in row.get("condition", ""):
                nickname = normalize(row.get("ident", "").split(":", 1)[-1])
                side_id = state["request"]["side"]["id"]
                for raw in state.get("fullHistory") or state.get("history") or []:
                    parts = raw.split("|")
                    if (len(parts) >= 4
                            and parts[1] in {"-terastallize", "terastallize"}
                            and parts[2].startswith(side_id + "a:")
                            and normalize(parts[2].split(":", 1)[-1]) == nickname):
                        tera_used = True
                        checks["requestClearedTeraAfterFaint"] += 1
                        break
            check(f"privateTera.{species}", tera_used,
                  target.terastallized)
        for short, attr in STATS.items():
            if short in (row.get("stats") or {}):
                check(f"privateStats.{species}.{short}", row["stats"][short],
                      getattr(target, attr))
        condition = row.get("condition", "")
        if "fnt" in condition or condition == "0":
            check(f"privateHP.{species}.hp", 0, target.hp)
        else:
            match = re.match(r"(\d+)/(\d+)", condition)
            if match:
                check(f"privateHP.{species}.hp", int(match[1]), target.hp)
                check(f"privateHP.{species}.maxhp", int(match[2]), target.maxhp)

    for label, key, side in (
        ("self", "me", engine.side_one),
        ("opponent", "opp", engine.side_two),
    ):
        raw = (state.get(key) or {}).get("boosts") or {}
        if isinstance(raw, list):
            boosts = {}
            for item in raw:
                match = re.fullmatch(r"([a-z-]+)([+-]\d+)", str(item).lower())
                if match:
                    boosts[match[1]] = int(match[2])
        else:
            boosts = raw
        for short, attr in STATS.items():
            value = boosts.get(short, boosts.get(attr.replace("_", "-"), 0))
            check(f"activeBoosts.{label}.{short}", int(value),
                  getattr(side, attr + "_boost"))
    opponent = state.get("opp") or {}
    active = engine.side_two.pokemon[0]
    if opponent.get("hp") is not None and opponent.get("maxHp"):
        expected = round(active.maxhp * opponent["hp"] / opponent["maxHp"])
        check("opponentPublicHP.active", expected, active.hp)
    return checks, differences


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; use a new report path")
    counts = Counter()
    records = []
    input_hash = hashlib.sha256()
    with args.input.open("rb") as handle:
        for line_no, line in enumerate(handle, 1):
            input_hash.update(line)
            row = json.loads(line)
            state = row.get("state")
            if not state:
                counts["skipped"] += 1
                continue
            counts["states"] += 1
            try:
                checks, differences = inspect_state(state)
                counts.update(checks)
            except Exception as error:
                counts["errors"] += 1
                records.append({"line": line_no, "error": str(error),
                                "battleId": state.get("battleId"),
                                "turn": state.get("turn")})
                continue
            if differences:
                counts["statesWithDifferences"] += 1
                records.append({"line": line_no, "battleId": state.get("battleId"),
                                "turn": state.get("turn"),
                                "differences": differences})
    paths = [
        Path(__file__),
        LAB_ROOT / "fp/live_reference.py",
        LAB_ROOT / "fp/search/poke_engine_helpers.py",
        LAB_ROOT / "fp/belief_sampler.py",
    ]
    report = {
        "schemaVersion": "engine-input-audit/v1",
        "scope": ["request-private-stats", "request-private-HP",
                  "request-private-tera-used", "adapter-active-five-stat-boosts",
                  "adapter-opponent-active-public-HP"],
        "notVerified": ["protocol-to-adapter", "full-engine-state-parity",
                        "hidden-opponent-configuration", "policy-equivalence"],
        "classifications": {
            "requestClearedTeraAfterFaint":
                "Fainted request clears terastallized; public history retains "
                "team-wide Tera usage to prevent a second Tera."
        },
        "input": str(args.input.resolve()),
        "inputSha256": input_hash.hexdigest(),
        "sourceSha256": {str(p.relative_to(LAB_ROOT)): hashlib.sha256(
            p.read_bytes()).hexdigest() for p in paths},
        "counts": dict(counts),
        "records": records,
    }
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=True)
    print(json.dumps(report["counts"], sort_keys=True))
    print(f"Report: {args.output}")


if __name__ == "__main__":
    main()
