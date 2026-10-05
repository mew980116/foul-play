"""Inspect a Gen 9 Random Battle replay decision point offline."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

LAB_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = LAB_ROOT.parent
if str(LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(LAB_ROOT))

from constants import BattleType  # noqa: E402
from fp.battle import Battle, Pokemon  # noqa: E402
from fp.belief_sampler import Gen9RandomBattleBeliefSampler  # noqa: E402
from fp.dual_view import PublicBattleState, PublicPokemonState  # noqa: E402
from fp.policy_oracle import FoulPlayPolicyOracle  # noqa: E402
from fp.shadow_prediction import (  # noqa: E402
    FoulPlayShadowPredictor,
    _particle_pokemon,
)


DEFAULT_MATCHES = (
    WORKSPACE_ROOT
    / "pokemon-battle-ai"
    / "platform"
    / "ps"
    / "benchmark"
    / "results"
    / "matches.jsonl"
)


@dataclass
class TrackedPokemon:
    side: str
    ident: str
    species: str
    details: str
    hp: str | None = None
    status: str | None = None
    moves: list[str] | None = None
    item: str | None = None
    ability: str | None = None
    tera_type: str | None = None
    boosts: dict[str, int] | None = None
    fainted: bool = False

    def __post_init__(self) -> None:
        if self.moves is None:
            self.moves = []
        if self.boosts is None:
            self.boosts = {}


class PublicReplayTracker:
    """Track only information that is present in the public protocol."""

    def __init__(self) -> None:
        self.turn = 0
        self.weather = None
        self.terrain = None
        self.active: dict[str, str | None] = {"p1": None, "p2": None}
        self.pokemon: dict[str, list[TrackedPokemon]] = {"p1": [], "p2": []}
        self.last_move: dict[str, str] = {"p1": "", "p2": ""}

    def clone(self) -> "PublicReplayTracker":
        return copy.deepcopy(self)

    def apply(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        args = event.get("args") or []
        if event_type == "turn" and args:
            self.turn = int(args[0])
            return
        if event_type in {"switch", "drag", "replace"} and len(args) >= 3:
            side, ident = parse_ident(args[0])
            pokemon = self.ensure(side, ident, args[1])
            pokemon.details = args[1]
            pokemon.hp = args[2]
            pokemon.status = parse_status(args[2])
            pokemon.fainted = is_fainted(args[2])
            self.active[side] = ident
            return
        if event_type == "move" and len(args) >= 2:
            side, ident = parse_ident(args[0])
            pokemon = self.ensure(side, ident, ident.split(": ", 1)[-1])
            move = normalize_token(args[1])
            if move not in pokemon.moves:
                pokemon.moves.append(move)
            self.last_move[side] = move
            return
        if event_type in {"damage", "heal", "sethp"} and len(args) >= 2:
            side, ident = parse_ident(args[0])
            pokemon = self.ensure(side, ident, ident.split(": ", 1)[-1])
            pokemon.hp = args[1]
            pokemon.status = parse_status(args[1])
            pokemon.fainted = is_fainted(args[1])
            return
        if event_type == "faint" and args:
            side, ident = parse_ident(args[0])
            pokemon = self.ensure(side, ident, ident.split(": ", 1)[-1])
            pokemon.hp = "0 fnt"
            pokemon.fainted = True
            return
        if event_type == "status" and len(args) >= 2:
            side, ident = parse_ident(args[0])
            self.ensure(side, ident, ident.split(": ", 1)[-1]).status = args[1]
            return
        if event_type in {"ability", "item"} and len(args) >= 2:
            side, ident = parse_ident(args[0])
            pokemon = self.ensure(side, ident, ident.split(": ", 1)[-1])
            if event_type == "ability":
                pokemon.ability = normalize_token(args[1])
            else:
                pokemon.item = normalize_token(args[1])
            return
        if event_type == "enditem" and args:
            side, ident = parse_ident(args[0])
            self.ensure(side, ident, ident.split(": ", 1)[-1]).item = None
            return
        if event_type == "terastallize" and len(args) >= 2:
            side, ident = parse_ident(args[0])
            self.ensure(
                side, ident, ident.split(": ", 1)[-1]
            ).tera_type = normalize_token(args[1])
            return
        if event_type in {"boost", "unboost", "setboost"} and len(args) >= 3:
            side, ident = parse_ident(args[0])
            pokemon = self.ensure(side, ident, ident.split(": ", 1)[-1])
            amount = int(args[2])
            if event_type == "unboost":
                amount = -amount
            if event_type == "setboost":
                pokemon.boosts[args[1]] = amount
            else:
                pokemon.boosts[args[1]] = pokemon.boosts.get(args[1], 0) + amount

    def ensure(self, side: str, ident: str, details: str) -> TrackedPokemon:
        for pokemon in self.pokemon[side]:
            if pokemon.ident == ident:
                return pokemon
        species = normalize_token(details.split(",", 1)[0])
        pokemon = TrackedPokemon(side, ident, species, details)
        self.pokemon[side].append(pokemon)
        return pokemon

    def public_state(self, format_id: str) -> PublicBattleState:
        sides: dict[str, tuple[PublicPokemonState, ...]] = {}
        for side, team in self.pokemon.items():
            sides[side] = tuple(
                PublicPokemonState(
                    ident=pokemon.ident,
                    species=pokemon.species,
                    details=pokemon.details,
                    hp=pokemon.hp,
                    status=pokemon.status,
                    revealed_moves=tuple(pokemon.moves or ()),
                    revealed_item=pokemon.item,
                    revealed_ability=pokemon.ability,
                    revealed_tera_type=pokemon.tera_type,
                    boosts=pokemon.boosts or {},
                    fainted=pokemon.fainted,
                )
                for pokemon in team
            )
        return PublicBattleState(
            turn=self.turn,
            format_id=format_id,
            sides=sides,
            weather=self.weather,
            terrain=self.terrain,
        )

    def summary(self) -> dict[str, Any]:
        result: dict[str, Any] = {"turn": self.turn, "sides": {}}
        for side, team in self.pokemon.items():
            result["sides"][side] = {
                "active": self.active[side],
                "team": [asdict(pokemon) for pokemon in team],
            }
        return result


def normalize_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def parse_ident(value: str) -> tuple[str, str]:
    match = re.match(r"^(p[12])[a-z]?:\s*(.*)$", str(value))
    if not match:
        raise ValueError(f"invalid Pokemon ident: {value!r}")
    return match.group(1), f"{match.group(1)}: {match.group(2)}"


def parse_status(condition: str) -> str | None:
    parts = str(condition).split()
    return parts[1] if len(parts) > 1 and parts[1] != "fnt" else None


def is_fainted(condition: str) -> bool:
    return "fnt" in str(condition)


def load_rows(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def find_row(rows: list[dict[str, Any]], match_id: str) -> dict[str, Any]:
    for row in rows:
        if row.get("matchId") == match_id:
            return row
    raise ValueError(f"match not found: {match_id}")


def build_snapshots(
    row: dict[str, Any],
) -> list[tuple[int, int, PublicReplayTracker]]:
    tracker = PublicReplayTracker()
    snapshots: list[tuple[int, int, PublicReplayTracker]] = []
    request_ordinal = 0
    for event_index, event in enumerate(row.get("protocol", [])):
        if event.get("type") == "request":
            request_ordinal += 1
            snapshots.append((request_ordinal, event_index, tracker.clone()))
        else:
            tracker.apply(event)
    return snapshots


def select_snapshot(
    row: dict[str, Any],
    snapshots: list[tuple[int, int, PublicReplayTracker]],
    decision_index: int | None,
    turn: int | None,
    request_ordinal: int | None,
) -> tuple[tuple[int, int, PublicReplayTracker], dict[str, Any] | None]:
    decisions = row.get("decisions", [])
    if request_ordinal is not None:
        for snapshot in snapshots:
            if snapshot[0] == request_ordinal:
                return snapshot, None
        raise ValueError(f"request ordinal not found: {request_ordinal}")

    if decision_index is not None:
        if decision_index < 0 or decision_index >= len(decisions):
            raise ValueError(f"decision index out of range: {decision_index}")
        decision = decisions[decision_index]
        wanted_turn = int(decision.get("turn") or 0)
        same_turn_decisions = [
            item for item in decisions if int(item.get("turn") or 0) == wanted_turn
        ]
        rank = same_turn_decisions.index(decision)
        candidates = [item for item in snapshots if item[2].turn == wanted_turn]
        if not candidates:
            raise ValueError(f"no request snapshot for turn {wanted_turn}")
        return candidates[min(rank, len(candidates) - 1)], decision

    if turn is not None:
        candidates = [item for item in snapshots if item[2].turn == turn]
        if not candidates:
            raise ValueError(f"no request snapshot for turn {turn}")
        return candidates[0], None

    if not snapshots:
        raise ValueError("replay has no request snapshots")
    return snapshots[0], decisions[0] if decisions else None


def particle_team(particle: Any) -> list[Pokemon]:
    return [
        _particle_pokemon(candidate) for candidate in particle.payload.get("team", ())
    ]


def apply_public_condition(pokemon: Pokemon, condition: str | None) -> None:
    if not condition:
        return
    match = re.match(r"^(\d+)(?:/(\d+))?", condition)
    if not match:
        return
    current = int(match.group(1))
    maximum = int(match.group(2) or match.group(1))
    if maximum <= 0 or pokemon.max_hp <= 0:
        return
    pokemon.hp = round(pokemon.max_hp * current / maximum)
    pokemon.status = parse_status(condition)


def make_battle(
    tracker: PublicReplayTracker,
    public: PublicBattleState,
    own_particle: Any,
    opponent_particle: Any,
) -> Battle:
    battle = Battle("replay-observer")
    battle.battle_type = BattleType.RANDOM_BATTLE
    battle.generation = "gen9"
    battle.pokemon_format = "gen9randombattle"
    battle.turn = public.turn
    battle.user.name = "p1"
    battle.opponent.name = "p2"

    generated = {
        "p1": particle_team(own_particle),
        "p2": particle_team(opponent_particle),
    }
    sides = {"p1": battle.user, "p2": battle.opponent}
    for side, battler in sides.items():
        public_team = tracker.pokemon[side]
        team = generated[side]
        for index, observed in enumerate(public_team):
            if index >= len(team):
                break
            team[index].nickname = observed.ident
            apply_public_condition(team[index], observed.hp)
            team[index].status = observed.status
            team[index].boosts.update(observed.boosts or {})
        active_ident = tracker.active[side]
        active_index = next(
            (
                index
                for index, item in enumerate(public_team)
                if item.ident == active_ident
            ),
            0,
        )
        battler.active = team[active_index]
        battler.reserve = [
            pokemon for index, pokemon in enumerate(team) if index != active_index
        ]
        battler.last_used_move = battler.last_used_move._replace(
            pokemon_name=battler.active.name,
            move=tracker.last_move[side],
            turn=public.turn,
        )
    return battle


def compact_policy(result: Any) -> list[dict[str, Any]]:
    return [
        {
            "action": item.action,
            "visits": item.visits,
            "probability": item.probability,
        }
        for item in result.actions
    ]


def build_report(
    row: dict[str, Any],
    snapshot: tuple[int, int, PublicReplayTracker],
    decision: dict[str, Any] | None,
    particle_count: int,
    search_time_ms: int,
) -> dict[str, Any]:
    request_ordinal, event_index, tracker = snapshot
    public = tracker.public_state(row["format"])
    sampler = Gen9RandomBattleBeliefSampler(seed=17)
    own_particles = sampler.sample(public, "p1", particle_count)
    opponent_particles = sampler.sample(public, "p2", 1)
    battle = make_battle(tracker, public, own_particles[0], opponent_particles[0])
    oracle = FoulPlayPolicyOracle()
    self_policy = oracle.evaluate(
        battle, perspective="self", search_time_ms=search_time_ms
    )
    shadow = FoulPlayShadowPredictor(oracle).predict(
        battle, own_particles, search_time_ms=search_time_ms
    )
    return {
        "schemaVersion": "replay-observer/v1",
        "reconstructionMode": "public-plus-sampled-configurations",
        "warning": (
            "Benchmark protocol records do not contain the original private request JSON; "
            "Foul Play Battle is reconstructed from public replay data and sampled sets."
        ),
        "match": {
            "matchId": row.get("matchId"),
            "room": row.get("room"),
            "format": row.get("format"),
            "result": row.get("result"),
            "winner": row.get("winner"),
        },
        "decisionPoint": {
            "requestOrdinal": request_ordinal,
            "protocolEventIndex": event_index,
            "turn": tracker.turn,
            "recordedDecision": decision,
        },
        "publicState": tracker.summary(),
        "belief": {
            "particleCount": len(own_particles),
            "weights": [particle.weight for particle in own_particles],
            "sampleIds": [particle.particle_id for particle in own_particles],
        },
        "selfPolicy": {
            "bestAction": self_policy.best_action,
            "actions": compact_policy(self_policy),
        },
        "opponentShadowPrediction": {
            "bestAction": shadow.best_action,
            "actions": sorted(
                [
                    {"action": action, "probability": probability}
                    for action, probability in shadow.action_probabilities.items()
                ],
                key=lambda item: item["probability"],
                reverse=True,
            ),
        },
        "rootReweighting": {
            "status": "not-evaluated",
            "reason": "No replay-validated action-pair value evaluator is available yet.",
        },
    }


def print_report(report: dict[str, Any]) -> None:
    point = report["decisionPoint"]
    print("=== replay observer ===")
    print(f"match: {report['match']['matchId']}")
    print(f"turn: {point['turn']}")
    print(f"request ordinal: {point['requestOrdinal']}")
    if point["recordedDecision"]:
        print(
            f"recorded decision: {json.dumps(point['recordedDecision'], ensure_ascii=True)}"
        )
    print("reconstruction: public replay plus sampled configurations")
    print("")
    print("public teams:")
    for side, data in report["publicState"]["sides"].items():
        print(f"  {side} active={data['active']}")
        print("   " + ", ".join(item["species"] for item in data["team"]))
    print("")
    print("self policy:")
    for item in report["selfPolicy"]["actions"][:8]:
        print(
            f"  {item['action']}: p={item['probability']:.4f} visits={item['visits']}"
        )
    print("")
    print("opponent shadow prediction:")
    for item in report["opponentShadowPrediction"]["actions"][:8]:
        print(f"  {item['action']}: p={item['probability']:.4f}")
    print("")
    print("root reweighting: not evaluated")
    print(report["warning"])


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=DEFAULT_MATCHES)
    parser.add_argument("--match-id", required=True)
    parser.add_argument("--decision-index", type=int)
    parser.add_argument("--turn", type=int)
    parser.add_argument("--request-ordinal", type=int)
    parser.add_argument("--particles", type=int, default=3)
    parser.add_argument("--search-time-ms", type=int, default=5)
    parser.add_argument("--json-out", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    rows = load_rows(args.file)
    row = find_row(rows, args.match_id)
    snapshots = build_snapshots(row)
    snapshot, decision = select_snapshot(
        row,
        snapshots,
        args.decision_index,
        args.turn,
        args.request_ordinal,
    )
    report = build_report(
        row,
        snapshot,
        decision,
        args.particles,
        args.search_time_ms,
    )
    print_report(report)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(report, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"json report: {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
