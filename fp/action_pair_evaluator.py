"""Static action-pair evaluation built on the pinned poke-engine damage API."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

import constants
from data import all_move_json
from fp.battle import Battle
from fp.search.poke_engine_helpers import poke_engine_get_damage_rolls


_SETUP_MOVES = {
    "bellydrum",
    "calmmind",
    "coil",
    "dragondance",
    "nastyplot",
    "quiverdance",
    "shellsmash",
    "swordsdance",
}

_RECOVERY_MOVES = {
    "aquaring",
    "morningsun",
    "recover",
    "rest",
    "roost",
    "slackoff",
    "softboiled",
    "synthesis",
}


def _canonical_action(action: Any) -> str:
    return str(action or "").strip().lower().replace("-tera", "")


def _move_name(action: Any) -> str | None:
    value = _canonical_action(action)
    if not value or value.startswith(("switch ", "team ")):
        return None
    if value.startswith("move "):
        value = value.split(" ", 1)[1]
    return value or None


def _move_data(action: Any) -> dict[str, Any]:
    move = _move_name(action)
    if not move:
        return {}
    return all_move_json.get(move, {})


def _is_switch(action: Any) -> bool:
    return _canonical_action(action).startswith("switch ")


def _priority(action: Any) -> int:
    return int(_move_data(action).get(constants.PRIORITY, 0) or 0)


def _is_damage_move(action: Any) -> bool:
    return _move_data(action).get(constants.CATEGORY) in {
        constants.PHYSICAL,
        constants.SPECIAL,
    }


def _mean_roll(rolls: list[int] | tuple[int, ...]) -> float:
    if not rolls:
        return 0.0
    return sum(float(value) for value in rolls) / len(rolls)


class ActionPairEvaluator:
    """Evaluate one our-action/opponent-action pair without simulating a full turn.

    Damage moves use poke-engine damage rolls. Switch targets, status effects,
    recovery and boosts are represented with conservative heuristics because
    this module does not mutate a complete battle simulator state.
    """

    def evaluate(
        self,
        battle: Battle,
        our_action: str,
        opponent_action: str,
    ) -> float:
        our = _canonical_action(our_action)
        opponent = _canonical_action(opponent_action)
        if not our or not opponent:
            return 0.0
        if our.startswith("team ") or opponent.startswith("team "):
            return 0.0

        our_switch = _is_switch(our)
        opponent_switch = _is_switch(opponent)
        if our_switch and opponent_switch:
            return 0.0
        if opponent_switch:
            return self._value_against_opponent_switch(our)
        if our_switch:
            return self._value_from_our_switch(battle, opponent)

        our_data = _move_data(our)
        opponent_data = _move_data(opponent)
        if not our_data or not opponent_data:
            return self._status_heuristic(our, opponent)

        if not _is_damage_move(our) and not _is_damage_move(opponent):
            return self._status_heuristic(our, opponent)

        try:
            side_one_first = self._side_one_goes_first(battle, our, opponent)
            state = deepcopy(battle)
            our_rolls, opponent_rolls = poke_engine_get_damage_rolls(
                state,
                _move_name(our) or "switch",
                _move_name(opponent) or "switch",
                side_one_first,
            )
        except (KeyError, TypeError, ValueError, RuntimeError):
            return self._status_heuristic(our, opponent)

        user = battle.user.active
        target = battle.opponent.active
        if user is None or target is None:
            return self._status_heuristic(our, opponent)

        our_damage = _mean_roll(our_rolls) / max(1, target.max_hp)
        opponent_damage = _mean_roll(opponent_rolls) / max(1, user.max_hp)
        if side_one_first and max(our_rolls or (0,)) >= target.hp:
            opponent_damage = 0.0
        elif not side_one_first and max(opponent_rolls or (0,)) >= user.hp:
            our_damage = 0.0

        value = 0.7 * our_damage - 0.7 * opponent_damage
        if max(our_rolls or (0,)) >= target.hp:
            value += 0.25
        if max(opponent_rolls or (0,)) >= user.hp:
            value -= 0.25
        if not _is_damage_move(our):
            value += self._non_damage_bonus(our, opponent_damage)
        if not _is_damage_move(opponent):
            value -= self._non_damage_bonus(opponent, our_damage)
        return max(-1.0, min(1.0, value))

    @staticmethod
    def _side_one_goes_first(
        battle: Battle,
        our_action: str,
        opponent_action: str,
    ) -> bool:
        our_priority = _priority(our_action)
        opponent_priority = _priority(opponent_action)
        if our_priority != opponent_priority:
            return our_priority > opponent_priority
        return battle.get_effective_speed(battle.user) >= battle.get_effective_speed(
            battle.opponent
        )

    @staticmethod
    def _value_against_opponent_switch(our_action: str) -> float:
        move = _move_name(our_action)
        if move in _SETUP_MOVES:
            return 0.12
        if _is_damage_move(our_action):
            return 0.02
        return 0.04

    @staticmethod
    def _value_from_our_switch(battle: Battle, opponent_action: str) -> float:
        del battle
        if _is_damage_move(opponent_action):
            return -0.08
        return 0.02

    @staticmethod
    def _non_damage_bonus(action: str, opposing_damage: float) -> float:
        move = _move_name(action)
        if move in _SETUP_MOVES:
            return 0.10 if opposing_damage <= 0.05 else -0.10
        if move in _RECOVERY_MOVES:
            return 0.06 if opposing_damage <= 0.05 else -0.06
        if move == "protect":
            return 0.03 if opposing_damage > 0.05 else -0.02
        return 0.0

    def _status_heuristic(self, our_action: str, opponent_action: str) -> float:
        opponent_damage = 0.12 if _is_damage_move(opponent_action) else 0.0
        value = self._non_damage_bonus(our_action, opponent_damage)
        value -= self._non_damage_bonus(opponent_action, 0.12 if _is_damage_move(our_action) else 0.0)
        if not math.isfinite(value):
            return 0.0
        return max(-1.0, min(1.0, value))
