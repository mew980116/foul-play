"""一层 belief rollout 使用的最小 Battle 转移模型。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from itertools import product
import math
from typing import Any

import constants
from data import all_move_json
from fp.action_pair_evaluator import _canonical_action, _move_name, _priority
from fp.battle import Battle, LastUsedMove, Pokemon
from fp.public_observation import PublicObservation, build_public_observation
from fp.search.poke_engine_helpers import (
    battle_to_poke_engine_state,
    poke_engine_get_damage_rolls,
)
from poke_engine import generate_instructions


SCHEMA_VERSION = "belief-transition/v1"


@dataclass(frozen=True)
class EngineTransitionOutcome:
    """Native engine branch; not a PS public observation or Python Battle."""

    engine_state: Any
    probability: float
    instructions: tuple[str, ...]
    reversible: bool


def _engine_invariants(state: Any) -> None:
    for side in (state.side_one, state.side_two):
        if not 0 <= int(side.active_index) < len(side.pokemon):
            raise ValueError("engine active index out of bounds")
        for pokemon in side.pokemon:
            if pokemon.maxhp <= 0 or not 0 <= pokemon.hp <= pokemon.maxhp:
                raise ValueError("engine HP out of bounds")
        for name in (
            "attack_boost", "defense_boost", "special_attack_boost",
            "special_defense_boost", "speed_boost", "accuracy_boost", "evasion_boost",
        ):
            if not -6 <= getattr(side, name) <= 6:
                raise ValueError("engine boost out of bounds")
        if "encore" in {str(v).lower() for v in side.volatile_statuses}:
            if side.last_used_move == "move:none" or not side.last_used_move.startswith("move:"):
                raise ValueError("encore requires a known last move")


def _engine_action(action: str, side: Any, replacement_phase: bool) -> str:
    value = str(action or "").strip().lower()
    active = side.pokemon[int(side.active_index)]
    if value in {"no move", "none"}:
        if not replacement_phase:
            raise ValueError("no move outside replacement phase")
        return "none"
    if value.startswith("switch "):
        target = value[7:].replace("-", "").replace(" ", "")
        if active.hp > 0 and side.force_trapped and not side.force_switch:
            raise ValueError("cannot switch while trapped")
        for index, pokemon in enumerate(side.pokemon):
            if (
                index != int(side.active_index)
                and str(pokemon.id).lower() == target
                and pokemon.hp > 0
            ):
                return target
        raise ValueError("switch target not alive or not in reserve")
    if value.startswith("move "):
        value = value[5:]
    tera = value.endswith("-tera")
    move_id = value[:-5] if tera else value
    if active.hp <= 0 or side.force_switch:
        raise ValueError("cannot attack during forced replacement")
    if not any(
        str(move.id).lower() == move_id and not move.disabled and move.pp > 0
        for move in active.moves
    ):
        raise ValueError("move missing, disabled or out of PP")
    if tera and (
        any(pokemon.terastallized for pokemon in side.pokemon)
        or str(active.tera_type).lower() in {"none", "typeless"}
    ):
        raise ValueError("tera unavailable")
    return value


@dataclass(frozen=True)
class TransitionOutcome:
    """一个可审计的假想动作结果。"""

    state: Battle | None
    public_observation: PublicObservation | None
    valid: bool
    transition_kind: str
    outcome_probability: float
    forced_switch_sides: tuple[str, ...] = ()
    tera_used_sides: tuple[str, ...] = ()
    fallback_reason: str | None = None
    invariant_violations: tuple[str, ...] = ()

    def to_diagnostic(self) -> dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "valid": self.valid,
            "transitionKind": self.transition_kind,
            "outcomeProbability": self.outcome_probability,
            "forcedSwitchSides": list(self.forced_switch_sides),
            "teraUsedSides": list(self.tera_used_sides),
            "fallbackReason": self.fallback_reason,
            "invariantViolations": list(self.invariant_violations),
            "observationHash": (
                self.public_observation.observation_hash
                if self.public_observation is not None
                else None
            ),
        }


def _action_parts(action: str) -> tuple[str, str, bool]:
    value = str(action or "").strip().lower()
    value = " ".join(value.split())
    tera = value.endswith("-tera")
    if tera:
        value = value[:-5]
    if value.startswith("move "):
        value = value[5:]
    if value.startswith("switch "):
        return "switch", value[7:].strip(), False
    if value.startswith("team "):
        return "team", value[5:].strip(), False
    return "move", value, tera


def _side_and_active(battle: Battle, side: str):
    if side == "p1":
        return battle.user, battle.user.active
    if side == "p2":
        return battle.opponent, battle.opponent.active
    raise ValueError(f"unknown side: {side}")


def _find_reserve(battle: Battle, side: str, target: str) -> Pokemon | None:
    battler, _ = _side_and_active(battle, side)
    wanted = _canonical_action(target).replace("-", "")
    for pokemon in battler.reserve:
        names = {
            _canonical_action(pokemon.name).replace("-", ""),
            _canonical_action(pokemon.base_name).replace("-", ""),
            _canonical_action(pokemon.nickname).replace("-", ""),
        }
        if wanted in names:
            return pokemon
    return None


def _switch_in(battle: Battle, side: str, target: str) -> bool:
    battler, active = _side_and_active(battle, side)
    incoming = _find_reserve(battle, side, target)
    if incoming is None or incoming.hp <= 0:
        return False
    if active is not None and active.hp > 0:
        battler.reserve.append(active)
    battler.reserve.remove(incoming)
    battler.active = incoming
    battler.last_used_move = LastUsedMove(
        pokemon_name=incoming.name,
        move=f"switch {incoming.name}",
        turn=int(battle.turn or 0),
    )
    if side == "p1":
        battle.force_switch = False
    return True


def _apply_tera(battle: Battle, side: str) -> bool:
    battler, active = _side_and_active(battle, side)
    del battler
    # 传入的 Tera 动作已经通过当前 PS request 的合法动作集合；
    # live state 转换不一定保留 can_terastallize 标志，因此以未使用且有
    # Tera 类型作为此处的最小合法条件。
    if active is None or active.terastallized:
        return False
    if not active.tera_type:
        return False
    active.terastallized = True
    active.can_terastallize = False
    active.types = (active.tera_type,)
    return True


def _damage_choices(rolls: tuple[int, ...] | list[int]) -> tuple[int, ...]:
    values = sorted({max(0, int(value)) for value in rolls})
    if not values:
        return (0,)
    middle = values[len(values) // 2]
    return tuple(dict.fromkeys((values[0], middle, values[-1])))


def _set_damage(pokemon: Pokemon, damage: int) -> None:
    pokemon.hp = max(0, min(pokemon.max_hp, pokemon.hp - max(0, damage)))
    if pokemon.hp == 0:
        pokemon.fainted = True


def _normalize_faint_flags(battle: Battle) -> None:
    """将输入 state 中的 0 HP reserve 统一标记为 faint。"""
    for battler in (battle.user, battle.opponent):
        all_pokemon = ([battler.active] if battler.active is not None else []) + list(
            battler.reserve
        )
        for pokemon in all_pokemon:
            if pokemon.hp <= 0:
                pokemon.hp = 0
                pokemon.fainted = True


def _invariant_violations(battle: Battle) -> tuple[str, ...]:
    violations: list[str] = []
    for side_name, battler in (("p1", battle.user), ("p2", battle.opponent)):
        all_pokemon = ([battler.active] if battler.active is not None else []) + list(
            battler.reserve
        )
        identities = [id(pokemon) for pokemon in all_pokemon]
        if len(identities) != len(set(identities)):
            violations.append(f"{side_name}:duplicate-active-reserve")
        for pokemon in all_pokemon:
            if not 0 <= pokemon.hp <= pokemon.max_hp:
                violations.append(f"{side_name}:{pokemon.name}:hp-range")
            if pokemon.hp == 0 and not pokemon.fainted:
                violations.append(f"{side_name}:{pokemon.name}:faint-flag")
            if any(value < -6 or value > 6 for value in pokemon.boosts.values()):
                violations.append(f"{side_name}:{pokemon.name}:boost-range")
            if pokemon.terastallized and not pokemon.tera_type:
                violations.append(f"{side_name}:{pokemon.name}:tera-type")
    return tuple(violations)


class BeliefTransitionModel:
    """实现安全的普通攻击、换人、击杀和 Tera 一步转移。

    复杂状态招式不做部分模拟，而是显式返回 fallback，避免把错误的
    after-state 继续送入下一次 oracle。
    """

    def __init__(
        self,
        *,
        visible_reserves: dict[str, set[str]] | None = None,
        revealed: dict[str, dict[str, dict[str, Any]]] | None = None,
        allow_approximate_preview: bool = False,
    ):
        self.visible_reserves = visible_reserves or {}
        self.revealed = deepcopy(revealed or {})
        self.allow_approximate_preview = allow_approximate_preview

    def apply_engine(
        self, battle: Battle, our_action: str, opponent_action: str
    ) -> tuple[EngineTransitionOutcome, ...]:
        """Use the pinned engine kernel, preserving branch weights and state.

        No inverse Battle conversion or public-event inference is attempted.
        Python damage previews are explicitly separate from this backend.
        """
        if battle.gravity:
            raise ValueError("gravity is not represented by this engine binding")
        for battler in (battle.user, battle.opponent):
            if battler.active is None:
                raise ValueError("missing active Pokemon")
            for pokemon in [battler.active, *battler.reserve]:
                if len(pokemon.moves) > 4:
                    raise ValueError("more than four moves cannot be represented")
        state = battle_to_poke_engine_state(deepcopy(battle))
        _engine_invariants(state)
        replacement = any(
            side.force_switch or side.pokemon[int(side.active_index)].hp == 0
            for side in (state.side_one, state.side_two)
        )
        action_one = _engine_action(our_action, state.side_one, replacement)
        action_two = _engine_action(opponent_action, state.side_two, replacement)
        before = state.to_string()
        branches = generate_instructions(state, action_one, action_two)
        probabilities = [float(branch.percentage) / 100 for branch in branches]
        if (
            not probabilities
            or any(not math.isfinite(p) or p < 0 for p in probabilities)
            or not math.isclose(sum(probabilities), 1.0, abs_tol=1e-5)
        ):
            raise ValueError("invalid engine branch probability mass")
        outcomes = []
        for branch, probability in zip(branches, probabilities):
            after = state.apply_instructions(branch)
            _engine_invariants(after)
            reversible = after.reverse_instructions(branch).to_string() == before
            if not reversible or state.to_string() != before:
                raise ValueError("engine transition mutated input or was not reversible")
            outcomes.append(EngineTransitionOutcome(
                engine_state=after,
                probability=probability,
                instructions=tuple(repr(item) for item in branch.instruction_list),
                reversible=reversible,
            ))
        return tuple(outcomes)

    def apply(
        self,
        battle: Battle,
        our_action: str,
        opponent_action: str,
    ) -> tuple[TransitionOutcome, ...]:
        if not self.allow_approximate_preview:
            return (self._fallback("approximate-transition-not-rollout-safe"),)
        base = deepcopy(battle)
        _normalize_faint_flags(base)
        our_kind, our_value, our_tera = _action_parts(our_action)
        opp_kind, opp_value, opp_tera = _action_parts(opponent_action)
        if our_kind == "team" or opp_kind == "team":
            return (self._fallback("team-preview-unsupported"),)
        if our_kind == "move" and our_value == "no move":
            return (self._fallback("no-our-action"),)
        if opp_kind == "move" and opp_value == "no move":
            return (self._fallback("no-opponent-action"),)

        if our_kind == "move" and not _move_name(our_value):
            return (self._fallback("empty-our-action"),)
        if opp_kind == "move" and not _move_name(opp_value):
            return (self._fallback("empty-opponent-action"),)

        for side, kind, tera in (
            ("p1", our_kind, our_tera),
            ("p2", opp_kind, opp_tera),
        ):
            if kind == "switch":
                if not _switch_in(base, side, _canonical_action(our_value if side == "p1" else opp_value)):
                    return (self._fallback(f"invalid-{side}-switch"),)
            elif tera and not _apply_tera(base, side):
                return (self._fallback(f"invalid-{side}-tera"),)

        if our_kind == "switch" and opp_kind == "switch":
            return self._finalize(base, "switch-vs-switch", 1.0)

        if (
            our_kind == "move"
            and all_move_json.get(_move_name(our_value), {}).get(constants.CATEGORY)
            == constants.STATUS
        ) or (
            opp_kind == "move"
            and all_move_json.get(_move_name(opp_value), {}).get(constants.CATEGORY)
            == constants.STATUS
        ):
            return (self._fallback("status-or-utility-move-unsupported"),)

        our_move = _move_name(our_value) if our_kind == "move" else "switch"
        opp_move = _move_name(opp_value) if opp_kind == "move" else "switch"
        if base.user.active is None or base.opponent.active is None:
            return (self._fallback("missing-active-pokemon"),)

        side_one_first = self._side_one_goes_first(base, our_value, opp_value)
        try:
            our_rolls, opponent_rolls = poke_engine_get_damage_rolls(
                base,
                our_move,
                opp_move,
                side_one_first,
            )
        except (KeyError, TypeError, ValueError, RuntimeError):
            return (self._fallback("damage-roll-failed"),)

        our_choices = _damage_choices(our_rolls) if our_kind == "move" else (0,)
        opp_choices = (
            _damage_choices(opponent_rolls) if opp_kind == "move" else (0,)
        )
        outcomes: list[TransitionOutcome] = []
        pairs = list(product(our_choices, opp_choices))
        if not pairs:
            pairs = [(0, 0)]
        reveals = deepcopy(self.revealed)
        if our_kind == "move":
            self._record_move_reveal(reveals, "p1", base.user.active, our_move)
        if opp_kind == "move":
            self._record_move_reveal(
                reveals,
                "p2",
                base.opponent.active,
                opp_move,
            )
        for our_damage, opp_damage in pairs:
            state = deepcopy(base)
            if side_one_first:
                _set_damage(state.opponent.active, our_damage)
                if state.opponent.active.hp > 0:
                    _set_damage(state.user.active, opp_damage)
            else:
                _set_damage(state.user.active, opp_damage)
                if state.user.active.hp > 0:
                    _set_damage(state.opponent.active, our_damage)
            state.user.last_used_move = LastUsedMove(
                pokemon_name=state.user.active.name,
                move=our_move if our_kind == "move" else f"switch {state.user.active.name}",
                turn=int(state.turn or 0),
            )
            state.opponent.last_used_move = LastUsedMove(
                pokemon_name=state.opponent.active.name,
                move=opp_move
                if opp_kind == "move"
                else f"switch {state.opponent.active.name}",
                turn=int(state.turn or 0),
            )
            outcomes.append(
                self._finalize(
                    state,
                    f"{our_kind}-vs-{opp_kind}",
                    1.0 / len(pairs),
                    reveals=reveals,
                )[0]
            )
        return tuple(outcomes)

    @staticmethod
    def _side_one_goes_first(
        battle: Battle,
        our_action: str,
        opponent_action: str,
    ) -> bool:
        our_kind, our_value, _ = _action_parts(our_action)
        opp_kind, opp_value, _ = _action_parts(opponent_action)
        if our_kind == "switch":
            return False
        if opp_kind == "switch":
            return True
        our_priority = _priority(_move_name(our_value) or our_value)
        opp_priority = _priority(_move_name(opp_value) or opp_value)
        if our_priority != opp_priority:
            return our_priority > opp_priority
        return battle.get_effective_speed(battle.user) >= battle.get_effective_speed(
            battle.opponent
        )

    def _finalize(
        self,
        state: Battle,
        transition_kind: str,
        probability: float,
        *,
        reveals: dict[str, dict[str, dict[str, Any]]] | None = None,
    ) -> tuple[TransitionOutcome, ...]:
        violations = _invariant_violations(state)
        if violations:
            return (
                TransitionOutcome(
                    state=None,
                    public_observation=None,
                    valid=False,
                    transition_kind=transition_kind,
                    outcome_probability=probability,
                    fallback_reason="battle-invariant-violation",
                    invariant_violations=violations,
                ),
            )
        forced = tuple(
            side
            for side, battler in (("p1", state.user), ("p2", state.opponent))
            if battler.active is not None and battler.active.fainted
        )
        tera_used = tuple(
            side
            for side, battler in (("p1", state.user), ("p2", state.opponent))
            if battler.active is not None and battler.active.terastallized
        )
        if "p1" in forced:
            state.force_switch = True
        observation = build_public_observation(
            state,
            visible_reserves=self.visible_reserves,
            reveals=reveals,
        )
        return (
            TransitionOutcome(
                state=state,
                public_observation=observation,
                valid=True,
                transition_kind=transition_kind,
                outcome_probability=probability,
                forced_switch_sides=forced,
                tera_used_sides=tera_used,
            ),
        )

    @staticmethod
    def _fallback(reason: str) -> TransitionOutcome:
        return TransitionOutcome(
            state=None,
            public_observation=None,
            valid=False,
            transition_kind="unsupported",
            outcome_probability=1.0,
            fallback_reason=reason,
        )

    @staticmethod
    def _record_move_reveal(
        reveals: dict[str, dict[str, dict[str, Any]]],
        side: str,
        pokemon: Pokemon | None,
        move: str,
    ) -> None:
        if pokemon is None:
            return
        key = str(pokemon.nickname or pokemon.name).strip().lower()
        side_reveals = reveals.setdefault(side, {})
        pokemon_reveals = side_reveals.setdefault(key, {})
        moves = list(pokemon_reveals.get("moves") or ())
        if move not in moves:
            moves.append(move)
        pokemon_reveals["moves"] = moves
