"""将线上 PS battle-state 转换为 Foul Play reference policy 输入。"""

from __future__ import annotations

import math
import hashlib
import random
import re
from dataclasses import dataclass, field
from typing import Any

from constants import BattleType
import constants
from fp.battle import Battle, LastUsedMove, Pokemon
from fp.battle_modifier import transform as apply_transform
from fp.belief_sampler import Gen9RandomBattleBeliefSampler
from fp.dual_view import PublicBattleState, PublicPokemonState
from fp.policy_oracle import FoulPlayPolicyOracle
from fp.root_reweighting import RootActionReweighter
from fp.shadow_prediction import FoulPlayShadowPredictor, ShadowPrediction
from fp.policy_oracle import PolicyActionStat, PolicyOracleResult
from fp.action_pair_evaluator import ActionPairEvaluator


def _name_id(value: Any) -> str:
    return "".join(char for char in str(value or "").lower() if char.isalnum())


def _format_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    compact = _name_id(text)
    if compact == "gen9randombattle":
        return "gen9randombattle"
    return compact


def _self_side_id(state: dict[str, Any]) -> str:
    """返回 PS 协议中当前决策方的 side id。"""
    request_side = (state.get("request") or {}).get("side") or {}
    side_id = str(request_side.get("id") or "p1").lower()
    return side_id if side_id in {"p1", "p2"} else "p1"


def _opponent_side_id(state: dict[str, Any]) -> str:
    return "p2" if _self_side_id(state) == "p1" else "p1"


def _canonical_side_for_protocol(state: dict[str, Any], side_id: str) -> str:
    """把 PS 的实际 side 映射到 converter 的 canonical 视角：p1=我方，p2=对手。"""
    return "p1" if str(side_id).lower() == _self_side_id(state) else "p2"


def _protocol_side_for_canonical(state: dict[str, Any], side: str) -> str:
    return _self_side_id(state) if side == "p1" else _opponent_side_id(state)


def _normalize_terrain(value: Any) -> str | None:
    """将 PS 的场地文本转换成 Foul Play 使用的内部 terrain id。"""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"", "none"}:
        return None
    text = text.split(":", 1)[-1].strip()
    compact = _name_id(text)
    mapping = {
        "electricterrain": constants.ELECTRIC_TERRAIN,
        "grassyterrain": constants.GRASSY_TERRAIN,
        "mistyterrain": constants.MISTY_TERRAIN,
        "psychicterrain": constants.PSYCHIC_TERRAIN,
    }
    return mapping.get(compact, compact)


def _normalize_weather(value: Any) -> str | None:
    """将 PS 的天气文本转换成 Foul Play 使用的内部 weather id。"""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"", "none"}:
        return None
    compact = _name_id(text)
    mapping = {
        "raindance": constants.RAIN,
        "sunnyday": constants.SUN,
        "sandstorm": constants.SAND,
        "hail": constants.HAIL,
        "snowscape": constants.SNOW,
        "desolateland": constants.DESOLATE_LAND,
        "primordialsea": constants.HEAVY_RAIN,
    }
    return mapping.get(compact, compact)


def _normalize_side_condition(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = text.split(":", 1)[-1].strip()
    return _name_id(text)


def _parse_boosts(value: Any) -> dict[str, int]:
    """把 PS 的 boosts 字段统一转换为带正负号的字典。"""
    result: dict[str, int] = {}
    if isinstance(value, dict):
        items = value.items()
    else:
        parsed: list[tuple[str, Any]] = []
        for raw in value or ():
            match = re.match(
                r"^([a-z]+)([+-]\d+)$",
                str(raw).strip().lower(),
            )
            if match:
                parsed.append((match.group(1), match.group(2)))
        items = parsed
    for stat, raw_amount in items:
        try:
            result[str(stat).lower()] = max(-6, min(6, int(raw_amount)))
        except (TypeError, ValueError):
            continue
    return {stat: amount for stat, amount in result.items() if amount}


def _apply_public_boost_event(
    boosts_by_actor: dict[str, dict[str, int]],
    event_type: str,
    parts: list[str],
) -> None:
    """按公开战报增量更新一只宝可梦的能力等级。"""
    if event_type == "clearallboost":
        for boosts in boosts_by_actor.values():
            boosts.clear()
        return
    if not parts:
        return
    actor_id = _name_id(parts[0].split(":", 1)[-1])
    boosts = boosts_by_actor.setdefault(actor_id, {})
    if event_type in {"boost", "unboost", "setboost"}:
        if len(parts) < 3:
            return
        stat = _name_id(parts[1])
        try:
            amount = int(parts[2])
        except (TypeError, ValueError):
            return
        if event_type == "unboost":
            amount = -amount
        if event_type == "setboost":
            boosts[stat] = max(-6, min(6, amount))
        else:
            boosts[stat] = max(-6, min(6, boosts.get(stat, 0) + amount))
        if boosts.get(stat) == 0:
            boosts.pop(stat, None)
        return
    if event_type == "clearboost":
        boosts.clear()
    elif event_type == "clearpositiveboost":
        for stat in list(boosts):
            if boosts[stat] > 0:
                boosts.pop(stat)
    elif event_type == "clearnegativeboost":
        for stat in list(boosts):
            if boosts[stat] < 0:
                boosts.pop(stat)


def _apply_live_side_conditions(
    battle: Battle,
    state: dict[str, Any],
) -> None:
    """将 PS 当前双方场地状态同步到 Foul Play。"""
    condition_map = {
        "stealthrock": constants.STEALTH_ROCK,
        "spikes": constants.SPIKES,
        "toxicspikes": constants.TOXIC_SPIKES,
        "stickyweb": constants.STICKY_WEB,
        "reflect": constants.REFLECT,
        "lightscreen": constants.LIGHT_SCREEN,
        "auroraveil": constants.AURORA_VEIL,
        "safeguard": constants.SAFEGUARD,
        "mist": constants.MIST,
        "tailwind": constants.TAILWIND,
    }
    side_values = {
        battle.user: (
            list(state.get("myHazards") or ())
            + list((state.get("screens") or {}).get("me") or ())
        ),
        battle.opponent: (
            list(state.get("oppHazards") or ())
            + list((state.get("screens") or {}).get("opp") or ())
        ),
    }
    side_details = {
        battle.user: (state.get("sideConditionDetails") or {}).get("me") or {},
        battle.opponent: (state.get("sideConditionDetails") or {}).get("opp") or {},
    }
    duration_defaults = {
        "reflect": 5,
        "lightscreen": 5,
        "auroraveil": 5,
        "safeguard": 5,
        "mist": 5,
        "tailwind": 4,
    }
    for battler, values in side_values.items():
        counts: dict[str, int] = {}
        for raw_value in values:
            normalized = _normalize_side_condition(raw_value)
            counts[normalized] = counts.get(normalized, 0) + 1
        for normalized, count in counts.items():
            condition = condition_map.get(normalized)
            if condition is None:
                continue
            detail = side_details[battler].get(normalized)
            if detail is None:
                for raw_key, raw_detail in side_details[battler].items():
                    if _normalize_side_condition(raw_key) == normalized:
                        detail = raw_detail
                        break
            if normalized in duration_defaults and count == 1:
                remaining = None
                if isinstance(detail, dict):
                    remaining = detail.get("turnsRemaining")
                if remaining is None:
                    active = battler.active
                    light_clay = bool(active and _name_id(active.item) == "lightclay")
                    remaining = duration_defaults[normalized] + (3 if light_clay else 0)
                try:
                    count = max(0, int(remaining))
                except (TypeError, ValueError):
                    count = duration_defaults[normalized]
            battler.side_conditions[condition] = max(
                count,
                int(battler.side_conditions[condition] or 0),
            )
        toxic_detail = side_details[battler].get("toxic_count")
        if isinstance(toxic_detail, dict):
            try:
                battler.side_conditions[constants.TOXIC_COUNT] = max(
                    0,
                    int(toxic_detail.get("count") or 0),
                )
            except (TypeError, ValueError):
                battler.side_conditions[constants.TOXIC_COUNT] = 0
        protect_detail = side_details[battler].get("protect")
        if isinstance(protect_detail, dict):
            try:
                battler.side_conditions[constants.PROTECT] = max(
                    0,
                    int(protect_detail.get("count") or 0),
                )
            except (TypeError, ValueError):
                battler.side_conditions[constants.PROTECT] = 0


def _history_last_moves(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """从公开协议恢复双方最后一次公开动作，供 Foul Play 锁招逻辑使用。"""
    result: dict[str, dict[str, Any]] = {}
    for raw in _history_lines(state):
        if not (raw.startswith("|move|") or raw.startswith("|switch|")):
            continue
        parts = raw.split("|")
        if len(parts) < 4:
            continue
        actor = parts[2]
        side_id = _canonical_side_for_protocol(state, actor.split(":", 1)[0][:2])
        move = (
            f"switch {_species_from_details(parts[3])}"
            if raw.startswith("|switch|")
            else _name_id(parts[3])
        )
        result[side_id] = {
            "pokemon": actor.split(":", 1)[-1].strip(),
            "move": move,
            "turn": int(state.get("turn") or 0),
        }
    return result


def _apply_live_field_state(battle: Battle, state: dict[str, Any]) -> None:
    """同步天气、场地和持续回合；缺少剩余回合时保持未知而不是伪造。"""
    weather_state = state.get("weatherState") or {}
    field_state = state.get("fieldState") or {}
    weather_turns_remaining = weather_state.get("turnsRemaining")
    if weather_turns_remaining is None:
        battle.weather_turns_remaining = -1
    else:
        try:
            battle.weather_turns_remaining = int(weather_turns_remaining)
        except (TypeError, ValueError):
            battle.weather_turns_remaining = -1
    battle.weather_source = weather_state.get("source") or ""
    battle.field_turns_remaining = field_state.get("terrainTurnsRemaining", 0) or 0
    battle.trick_room = bool(field_state.get("trickRoom"))
    battle.trick_room_turns_remaining = (
        field_state.get("trickRoomTurnsRemaining", 0) or 0
    )
    battle.gravity = bool(field_state.get("gravity"))


def _apply_live_last_moves(battle: Battle, state: dict[str, Any]) -> None:
    """把协议中最后一次 move 映射到当前 Foul Play active，避免错误锁招。"""
    last_moves = _history_last_moves(state)
    for battler, side_id in ((battle.user, "p1"), (battle.opponent, "p2")):
        item = last_moves.get(side_id)
        if not item:
            continue
        pokemon_name = _name_id(item["pokemon"])
        active_name = _name_id(battler.active.name if battler.active else "")
        move_name = str(item["move"])
        if (
            pokemon_name
            and active_name
            and pokemon_name != active_name
            and not move_name.startswith("switch ")
        ):
            protocol_side = _protocol_side_for_canonical(state, side_id)
            aliases = _history_species_aliases(
                state, f"{protocol_side}a: {item['pokemon']}", item["pokemon"]
            )
            if active_name not in aliases:
                continue
        battler.last_used_move = battler.last_used_move._replace(
            pokemon_name=battler.active.name if battler.active else item["pokemon"],
            move=move_name if move_name.startswith("switch ") else _name_id(move_name),
            turn=int(item["turn"]),
        )
    selected = state.get("lastSelectedMove") or {}
    if selected and battle.user.active is not None:
        battle.user.last_selected_move = LastUsedMove(
            pokemon_name=battle.user.active.name,
            move=str(selected.get("move") or ""),
            turn=int(selected.get("turn") or state.get("turn") or 0),
        )


def _apply_live_volatile_state(battle: Battle, state: dict[str, Any]) -> None:
    """将 adapter 提供的 active volatile 状态映射到 Foul Play 对象。"""
    for battler, row in (
        (battle.user, state.get("me") or {}),
        (battle.opponent, state.get("opp") or {}),
    ):
        if battler.active is None:
            continue
        volatile = row.get("volatile") or {}
        names = volatile.get("active") if isinstance(volatile, dict) else volatile
        if bool(row.get("substituteHit")) or (
            isinstance(volatile, dict) and volatile.get("substituteHit")
        ):
            battler.active.substitute_hit = True
        for raw_name in names or ():
            normalized = _name_id(raw_name)
            if normalized and normalized not in battler.active.volatile_statuses:
                battler.active.volatile_statuses.append(normalized)
        durations = volatile.get("durations") if isinstance(volatile, dict) else {}
        for key, value in (durations or {}).items():
            try:
                battler.active.volatile_status_durations[_name_id(key)] = int(value)
            except (TypeError, ValueError):
                continue


def _apply_request_recharge_semantics(
    battle: Battle,
    request: dict[str, Any],
) -> None:
    """按 PS request 覆盖我方的 recharge 状态。

    upstream Foul Play 不会把我方的 ``mustrecharge`` volatile 交给
    poke-engine；PS 会在 request 中直接提供唯一的 Recharge 动作，或把
    原招式保留为唯一可用动作。若同时把 ``mustrecharge`` 传入 engine，
    engine 会错误地产生 ``No Move``，随后只能由 adapter 做动作投影。
    """
    active_rows = request.get("active") or []
    if not active_rows or battle.user.active is None:
        return
    active_request = active_rows[0] or {}
    move_rows = active_request.get("moves") or []
    if not move_rows:
        return

    # ``mustrecharge`` 是上一回合动作的历史事件。当前 request 已经给出
    # 本回合可提交的动作，因此不能再把这个 volatile 传入 poke-engine。
    battle.user.active.volatile_statuses = [
        status
        for status in battle.user.active.volatile_statuses
        if _name_id(status) != "mustrecharge"
    ]

    # ``recharge`` 是 PS 暴露给当前请求的强制动作；它不是普通可搜索招式。
    # 对于下一回合重新暴露原招式的请求，PS 已经把其他招式标为 disabled，
    # 让 poke-engine 直接看到唯一合法动作即可。
    if any(
        _name_id(row.get("id") or row.get("move")) == "recharge"
        for row in move_rows
    ):
        battle.user.active.moves = [
            move for move in battle.user.active.moves if _name_id(move.name) == "recharge"
        ]


def _apply_live_side_effects(battle: Battle, state: dict[str, Any]) -> None:
    """同步 Wish、Future Sight、Healing Wish 和换人保留标记。"""
    details = state.get("sideEffectDetails") or {}
    for battler, side_name in (
        (battle.user, "me"),
        (battle.opponent, "opp"),
    ):
        current = details.get(side_name) or {}
        wish = current.get("wish") or {}
        future_sight = current.get("futureSight") or {}
        try:
            wish_turns = max(0, int(wish.get("turnsRemaining") or 0))
        except (TypeError, ValueError):
            wish_turns = 0
        try:
            wish_amount = max(0, int(wish.get("amount") or 0))
        except (TypeError, ValueError):
            wish_amount = 0
        if wish_turns > 0 and wish_amount <= 0:
            wish_source = _name_id(wish.get("source"))
            if wish_source:
                candidates = list(battler.reserve)
                if battler.active is not None:
                    candidates.insert(0, battler.active)
                for candidate in candidates:
                    if _name_id(candidate.name) == wish_source:
                        wish_amount = max(0, int(candidate.max_hp // 2))
                        break
        battler.wish = (wish_turns, wish_amount)

        try:
            future_turns = max(0, int(future_sight.get("turnsRemaining") or 0))
        except (TypeError, ValueError):
            future_turns = 0
        future_source = str(future_sight.get("source") or "")
        battler.future_sight = (future_turns, future_source)

        try:
            healing_wish = max(0, int(current.get("healingWish") or 0))
        except (TypeError, ValueError):
            healing_wish = 0
        battler.side_conditions[constants.HEALING_WISH] = healing_wish
        battler.baton_passing = bool(current.get("batonPassing"))
        battler.shed_tailing = bool(current.get("shedTailing"))


def _apply_active_item_move_constraints(battle: Battle) -> None:
    """补齐原版 lock_moves 中可由当前状态确定的锁招。"""
    from data import all_move_json

    for battler in (battle.user, battle.opponent):
        active = battler.active
        if active is None:
            continue

        if _name_id(active.item) == "assaultvest":
            for move in active.moves:
                try:
                    if all_move_json[move.name][constants.CATEGORY] == constants.STATUS:
                        move.disabled = True
                except KeyError:
                    continue

        if _name_id(active.item) in {"choicescarf", "choiceband", "choicespecs"}:
            last = battler.last_used_move
            if (
                last.pokemon_name == active.name
                and last.move
                and not last.move.startswith("switch ")
            ):
                for move in active.moves:
                    if _name_id(move.name) != _name_id(last.move):
                        move.disabled = True

        if constants.TAUNT in active.volatile_statuses:
            for move in active.moves:
                try:
                    if all_move_json[move.name][constants.CATEGORY] == constants.STATUS:
                        move.disabled = True
                except KeyError:
                    continue

        if constants.LOCKED_MOVE in active.volatile_statuses and battler.last_used_move.move:
            for move in active.moves:
                if _name_id(move.name) != _name_id(battler.last_used_move.move):
                    move.disabled = True


def _disable_redundant_setup_moves(battle: Battle) -> None:
    """禁止已经达到上限、不能继续布置的场地招式。"""
    setup_limits = {
        constants.STEALTH_ROCK: ("stealthrock", 1),
        constants.STICKY_WEB: ("stickyweb", 1),
        constants.SPIKES: ("spikes", 3),
        constants.TOXIC_SPIKES: ("toxicspikes", 2),
    }
    for active_side, target_side in (
        (battle.user, battle.opponent),
        (battle.opponent, battle.user),
    ):
        if active_side.active is None:
            continue
        for condition, (move_name, limit) in setup_limits.items():
            if int(target_side.side_conditions[condition] or 0) < limit:
                continue
            move = active_side.active.get_move(move_name)
            if move is not None:
                move.disabled = True


def _disable_history_invalid_moves(
    battle: Battle,
    state: dict[str, Any],
) -> None:
    """根据当前回合公开协议，补充引擎无法从快照推导的动作限制。"""
    active = battle.user.active
    if active is None:
        return

    history = _history_lines(state)
    side_id = str((state.get("request") or {}).get("side", {}).get("id") or "p1")
    active_id = f"{side_id}a:"
    active_name = _name_id(active.name)
    latest_switch = -1
    for index, raw in enumerate(history):
        if not raw.startswith("|switch|") or not raw.startswith(f"|switch|{active_id}"):
            continue
        parts = raw.split("|")
        if len(parts) >= 4 and active_name in {
            _name_id(parts[2].split(":", 1)[-1]),
            _name_id(_species_from_details(parts[3])),
        }:
            latest_switch = index

    moves_since_switch: list[str] = []
    for raw in history[latest_switch + 1 :]:
        if not raw.startswith("|move|") or not raw.startswith(f"|move|{active_id}"):
            continue
        parts = raw.split("|")
        if len(parts) >= 4:
            moves_since_switch.append(_name_id(parts[3]))

    def disable(move_name: str) -> None:
        move = active.get_move(move_name)
        if move is not None:
            move.disabled = True

    # Fake Out、First Impression 只在本次上场后的第一回合可用。
    if moves_since_switch:
        disable("fakeout")
        disable("firstimpression")
    # No Retreat 成功或失败后都不应再次作为候选动作。
    if "noretreat" in moves_since_switch:
        disable("noretreat")
    # Sleep Talk 没有睡眠状态时必然失败。
    if active.status != constants.SLEEP:
        disable("sleeptalk")
    # Yawn 对已有非无状态目标没有意义，并且 PS 会直接拒绝。
    if battle.opponent.active is not None and battle.opponent.active.status:
        disable("yawn")


def _species_from_details(details: Any) -> str:
    return str(details or "").split(",", 1)[0].strip()


def _history_details(
    state: dict[str, Any],
    ident: Any,
    name: Any,
) -> str | None:
    """从公开的 switch 事件恢复 PS 隐藏在昵称后的形态名称。"""
    target_name = _name_id(str(name or "").split(":", 1)[-1])
    target_ident = _name_id(str(ident or "").split(":", 1)[-1])
    history = state.get("fullHistory") or state.get("history") or ()
    for raw in reversed(history):
        if not isinstance(raw, str) or not raw.startswith("|switch|"):
            continue
        parts = raw.split("|")
        if len(parts) < 4:
            continue
        switch_ident = parts[2]
        details = parts[3]
        switch_name = switch_ident.split(":", 1)[-1]
        if _name_id(switch_name) == target_ident or _name_id(switch_name) == target_name:
            return details
    return None


def _history_species_aliases(
    state: dict[str, Any],
    ident: str,
    actor_name: str,
) -> set[str]:
    """为 PS 的战斗参与者建立基础名和形态名别名。"""
    aliases = {_name_id(actor_name)}
    target_ident = str(ident or "").strip()
    for raw in _history_lines(state):
        if not raw.startswith("|switch|"):
            continue
        parts = raw.split("|")
        if len(parts) < 4 or parts[2] != target_ident:
            continue
        details_species = str(parts[3]).split(",", 1)[0].strip()
        if details_species:
            aliases.add(_name_id(details_species))
        break
    return {alias for alias in aliases if alias}


def _level_from_details(details: Any) -> int:
    for part in str(details or "").split(","):
        part = part.strip()
        if part.startswith("L") and part[1:].isdigit():
            return int(part[1:])
    return 100


def _condition(condition: Any) -> tuple[int | None, int | None, str | None]:
    text = str(condition or "").strip()
    if "fnt" in text or text == "0":
        return 0, 0, None
    parts = text.split()
    hp_part = parts[0] if parts else ""
    if "/" not in hp_part:
        return None, None, parts[1] if len(parts) > 1 else None
    current_text, max_text = hp_part.split("/", 1)
    try:
        current = int(float(current_text))
        maximum = int(float(max_text))
    except ValueError:
        return None, None, parts[1] if len(parts) > 1 else None
    status = parts[1] if len(parts) > 1 else None
    return current, maximum, status


def _set_public_condition(pokemon: Pokemon, condition: Any) -> None:
    current, maximum, status = _condition(condition)
    if current is not None and maximum and pokemon.max_hp > 0:
        pokemon.hp = round(pokemon.max_hp * current / maximum)
    elif current == 0:
        pokemon.hp = 0
    pokemon.fainted = current == 0 or "fnt" in str(condition or "")
    if status:
        pokemon.status = status


def _set_boosts(pokemon: Pokemon, boosts: Any) -> None:
    """将 PS 的 stat-stage 表示同步到 Foul Play Pokemon。"""
    stat_names = {
        "atk": constants.ATTACK,
        "attack": constants.ATTACK,
        "def": constants.DEFENSE,
        "defense": constants.DEFENSE,
        "spa": constants.SPECIAL_ATTACK,
        "special-attack": constants.SPECIAL_ATTACK,
        "special_attack": constants.SPECIAL_ATTACK,
        "spd": constants.SPECIAL_DEFENSE,
        "special-defense": constants.SPECIAL_DEFENSE,
        "special_defense": constants.SPECIAL_DEFENSE,
        "spe": constants.SPEED,
        "speed": constants.SPEED,
        "accuracy": constants.ACCURACY,
        "evasion": constants.EVASION,
    }

    def set_boost(stat: Any, value: Any) -> None:
        normalized_stat = stat_names.get(str(stat).strip().lower())
        if normalized_stat is None:
            return
        try:
            pokemon.boosts[normalized_stat] = max(-6, min(6, int(value)))
        except (TypeError, ValueError):
            return

    if isinstance(boosts, dict):
        for stat, value in boosts.items():
            set_boost(stat, value)
        return

    for raw_boost in boosts or ():
        text = str(raw_boost).strip().lower()
        for stat in (
            "special-attack",
            "special-defense",
            "accuracy",
            "evasion",
            "attack",
            "defense",
            "speed",
            "atk",
            "def",
            "spa",
            "spd",
            "spe",
        ):
            if not text.startswith(stat):
                continue
            value_text = text[len(stat) :]
            if value_text.startswith(("+", "-")):
                set_boost(stat, value_text)
            break


def _set_request_pokemon(pokemon: Pokemon, row: dict[str, Any]) -> None:
    if row.get("evs") is not None:
        pokemon.set_spread(row.get("nature") or "serious", row["evs"])
    pokemon.item = row.get("item") or None
    pokemon.ability = row.get("ability") or None
    pokemon.index = int(row.get("slot", 0) or 0)
    pokemon.tera_type = row.get("teraType") or row.get("tera_type")
    pokemon.terastallized = bool(row.get("terastallized"))
    pokemon.reviving = bool(row.get("reviving"))
    _set_public_condition(pokemon, row.get("condition"))
    current_hp, max_hp, _status = _condition(row.get("condition"))
    if max_hp and current_hp is not None:
        pokemon.max_hp = max_hp
        pokemon.hp = current_hp
    _set_boosts(pokemon, row.get("boosts"))

    stats = row.get("stats") or {}
    stat_names = {
        "atk": constants.ATTACK,
        "def": constants.DEFENSE,
        "spa": constants.SPECIAL_ATTACK,
        "spd": constants.SPECIAL_DEFENSE,
        "spe": constants.SPEED,
    }
    for source, target in stat_names.items():
        if source in stats:
            pokemon.stats[target] = int(stats[source])

    for move in row.get("moves") or ():
        if isinstance(move, dict):
            move = move.get("id") or move.get("name")
        if move:
            pokemon.add_move(str(move))


def _apply_active_request_moves(
    pokemon: Pokemon,
    request: dict[str, Any],
) -> None:
    """按原版 update_from_request_json 保留招式、disabled 和当前 PP。"""
    active_rows = request.get("active") or []
    if not active_rows:
        return
    move_rows = active_rows[0].get("moves") or []
    if not move_rows:
        return
    existing = {
        str(move.name).lower().replace("-", ""): move
        for move in pokemon.moves
    }
    pokemon.moves = []
    for row in move_rows:
        move_id = str(row.get("id") or row.get("move") or "")
        if not move_id:
            continue
        move = existing.get(move_id.lower().replace("-", ""))
        if move is None:
            move = pokemon.add_move(
                str(row.get("move") or move_id)
                if move_id == "hiddenpower"
                else move_id
            )
        else:
            pokemon.moves.append(move)
        if move is None:
            continue
        move.disabled = bool(row.get("disabled"))
        if row.get("pp") is not None:
            try:
                move.current_pp = int(row["pp"])
            except (TypeError, ValueError):
                pass


def _apply_active_request_constraints(
    battler: Any,
    request: dict[str, Any],
) -> None:
    """同步 PS request 对当前活动方的强制换人限制。"""
    active_rows = request.get("active") or []
    if not active_rows:
        battler.trapped = False
        return
    active_request = active_rows[0] or {}
    battler.trapped = bool(
        active_request.get("trapped")
        or active_request.get("maybeTrapped")
        or active_request.get("maybe_trapped")
    )


def _request_team(request: dict[str, Any]) -> tuple[list[Pokemon], int]:
    side = request.get("side") or {}
    rows = side.get("pokemon") or []
    team: list[Pokemon] = []
    active_index = 0
    for index, row in enumerate(rows):
        species = _species_from_details(row.get("details"))
        if not species:
            continue
        pokemon = Pokemon(species, _level_from_details(row.get("details")))
        _set_request_pokemon(pokemon, {**row, "slot": index + 1})
        team.append(pokemon)
        if row.get("active"):
            active_index = len(team) - 1
    return team, active_index


def _state_team(state: dict[str, Any]) -> tuple[list[Pokemon], int]:
    """Rebuild the private team from Node battle-state myTeam as a fallback."""
    rows = state.get("myTeam") or []
    team: list[Pokemon] = []
    active_name = _name_id((state.get("me") or {}).get("name"))
    active_slot = int((state.get("me") or {}).get("slot", 0) or 0)
    active_index = 0
    for index, row in enumerate(rows):
        species = str(row.get("name") or _species_from_details(row.get("details")))
        if not species:
            continue
        current_hp = row.get("hp")
        maximum_hp = row.get("maxHp")
        condition = row.get("condition")
        if condition is None and current_hp is not None and maximum_hp is not None:
            condition = f"{current_hp}/{maximum_hp}"
        pokemon = Pokemon(species, _level_from_details(row.get("details")))
        _set_request_pokemon(pokemon, {**row, "condition": condition, "slot": index + 1})
        team.append(pokemon)
        if (
            (active_slot and int(row.get("slot", index + 1) or 0) == active_slot)
            or _name_id(species) == active_name
        ):
            active_index = len(team) - 1
    return team, active_index


def _public_pokemon(row: dict[str, Any], fallback_ident: str) -> PublicPokemonState:
    details = row.get("details")
    name = _species_from_details(details) or row.get("name")
    moves = row.get("moves") or ()
    revealed_moves = tuple(
        str(move.get("name") or move.get("id")) if isinstance(move, dict) else str(move)
        for move in moves
        if move
    )
    return PublicPokemonState(
        ident=str(row.get("ident") or fallback_ident),
        species=str(name),
        details=details,
        hp=(
            f"{row.get('hp')}/{row.get('maxHp')}"
            if row.get("hp") is not None and row.get("maxHp") is not None
            else None
        ),
        status=row.get("status"),
        revealed_moves=revealed_moves,
        revealed_item=row.get("itemInferred") or row.get("item"),
        revealed_ability=row.get("abilityInferred") or row.get("ability"),
        revealed_tera_type=row.get("teraType"),
        boosts=_parse_boosts(row.get("boosts")),
        fainted=bool(row.get("fainted") or row.get("ko")),
    )


def _public_state(state: dict[str, Any]) -> PublicBattleState:
    opponent_rows = list(state.get("oppTeam") or [])
    active = state.get("opp") or {}
    active_name = _name_id(active.get("name"))
    if active_name:
        active_rows = [
            row
            for row in opponent_rows
            if active_name
            in {
                _name_id(row.get("name")),
                _name_id(_species_from_details(row.get("details"))),
            }
        ]
        if active_rows:
            active_row = active_rows[0]
            opponent_rows = [
                active_row,
                *[
                    row
                    for row in opponent_rows
                    if row is not active_row
                ],
            ]
    if active and not any(
        _name_id(row.get("name")) == _name_id(active.get("name"))
        for row in opponent_rows
    ):
        opponent_rows.insert(0, active)

    own_rows = list(state.get("myTeam") or [])
    if not own_rows and state.get("me"):
        own_rows = [state["me"]]
    own_protocol_side = _protocol_side_for_canonical(state, "p1")
    opponent_protocol_side = _protocol_side_for_canonical(state, "p2")

    # PS adapter 有时把 Necrozma-Dusk-Mane 等形态压缩成基础名称，
    # 但公开 switch 事件仍然包含完整 details。优先使用公开事件恢复形态，
    # 这样 belief sampler 才能匹配正确的 Random Battle 数据集。
    opponent_rows = [
        {
            **row,
            "ident": row.get("ident") or (
                f"{opponent_protocol_side}: {row.get('name', 'unknown')}"
            ),
            "details": row.get("details")
            or _history_details(
                state,
                row.get("ident") or f"p2: {row.get('name', 'unknown')}",
                row.get("name"),
            ),
        }
        for row in opponent_rows
    ]
    own_rows = [
        {
            **row,
            "ident": row.get("ident") or (
                f"{own_protocol_side}: {row.get('name', 'unknown')}"
            ),
            "details": row.get("details")
            or _history_details(
                state,
                row.get("ident") or f"p1: {row.get('name', 'unknown')}",
                row.get("name"),
            ),
        }
        for row in own_rows
    ]

    return PublicBattleState(
        turn=int(state.get("turn") or 0),
        format_id=_format_id(state.get("format") or "gen9randombattle"),
        sides={
            "p1": tuple(
                _public_pokemon(
                    row,
                    f"{own_protocol_side}a: {row.get('name', 'unknown')}",
                )
                for row in own_rows
            ),
            "p2": tuple(
                _public_pokemon(
                    row,
                    f"{opponent_protocol_side}a: {row.get('name', 'unknown')}",
                )
                for row in opponent_rows
            ),
        },
        weather=_normalize_weather(state.get("weather")),
        terrain=_normalize_terrain(state.get("terrain")),
        history=_public_history(state),
    )


def _history_lines(state: dict[str, Any]) -> list[str]:
    history = state.get("fullHistory") or state.get("history") or ()
    return [line for line in history if isinstance(line, str)]


def _active_transform_events(state: dict[str, Any]) -> list[list[str]]:
    """返回当前仍处于变身状态的协议事件，按公开历史顺序排列。"""
    active_events: dict[str, list[str]] = {}
    for raw in _history_lines(state):
        parts = raw.split("|")
        if len(parts) < 3:
            continue
        actor = parts[2].split(":", 1)[0][:2]
        if parts[1] in {"switch", "drag", "replace", "faint"}:
            active_events.pop(actor, None)
        elif parts[1] == "-transform" and len(parts) >= 4:
            active_events[actor] = parts
    return list(active_events.values())


def _apply_live_transforms(battle: Battle, state: dict[str, Any]) -> None:
    """按 Foul Play 原生语义重建当前活动中的 Transform。"""
    for event in _active_transform_events(state):
        actor = event[2].split(":", 1)[0][:2]
        side = battle.user if actor == "p1" else battle.opponent
        if side.active is None:
            raise ValueError(
                f"invalid-live-transform: {event[2]} has no active Pokemon"
            )
        try:
            apply_transform(battle, event)
        except (AttributeError, IndexError) as error:
            raise ValueError(
                f"invalid-live-transform: unable to apply {event[2]}"
            ) from error


def _public_history(state: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """把 PS 的公开战报转换成 belief sampler 可消费的公开事件。

    这里明确只读取 history/fullHistory，不读取 request、myTeam 的私有配置，
    也不把隐藏的 set、PP 或能力写入公开历史。
    """
    events: list[dict[str, Any]] = []
    for raw in _history_lines(state):
        parts = raw.split("|")
        if len(parts) < 2:
            continue
        event_type = parts[1]
        if event_type == "move" and len(parts) >= 4:
            events.append(
                {
                    "type": "move",
                    "actor": parts[2],
                    "move": parts[3],
                }
            )
        elif event_type in {"switch", "drag", "replace"} and len(parts) >= 4:
            events.append(
                {
                    "type": "switch",
                    "actor": parts[2],
                    "details": parts[3],
                    "condition": parts[4] if len(parts) >= 5 else None,
                }
            )
        elif event_type in {"terastallize", "detailschange", "formechange"}:
            if len(parts) >= 4:
                events.append(
                    {
                        "type": event_type,
                        "actor": parts[2],
                        "details": parts[3],
                    }
                )
        elif event_type in {
            "-ability",
            "-item",
            "-enditem",
            "-status",
            "-curestatus",
            "-boost",
            "-unboost",
            "-damage",
            "-heal",
            "-sethp",
            "-sidestart",
            "-sideend",
            "-fieldstart",
            "-fieldend",
        }:
            # 这些事件仍然是公开信息；保留参数以供后续一致性过滤，
            # 但当前 sampler 只使用 type=move。
            events.append(
                {
                    "type": event_type[1:],
                    "args": parts[2:],
                }
            )
    return tuple(events)


def _apply_terastallization_history(
    team: list[Pokemon],
    state: dict[str, Any],
    side: str,
) -> None:
    """将公开的 Tera 使用记录同步到整支队伍，避免重复生成 Tera 动作。"""
    for raw in _history_lines(state):
        if not raw.startswith(("|-terastallize|", "|terastallize|")):
            continue
        parts = raw.split("|")
        if len(parts) < 4:
            continue
        ident = parts[2]
        protocol_side = _protocol_side_for_canonical(state, side)
        if not ident.startswith(f"{protocol_side}a:"):
            continue
        actor_name = ident.split(":", 1)[-1].strip()
        actor_aliases = _history_species_aliases(state, ident, actor_name)
        tera_type = _name_id(parts[3])
        for pokemon in team:
            nickname = _name_id(
                str(pokemon.nickname or "").split(":", 1)[-1].strip()
            )
            pokemon_aliases = {
                nickname,
                _name_id(pokemon.name),
                _name_id(getattr(pokemon, "base_name", "")),
            }
            if not actor_aliases.intersection(pokemon_aliases):
                continue
            pokemon.terastallized = True
            if tera_type:
                pokemon.tera_type = tera_type
            break


def _public_observed_side(state: dict[str, Any], side: str) -> tuple[PublicPokemonState, ...]:
    """只从协议公开事件重建一方，禁止读取该方 request 的隐藏配置。"""
    protocol_side = _protocol_side_for_canonical(state, side)
    prefix = f"{protocol_side}a: "
    switches: list[dict[str, Any]] = []
    for raw in _history_lines(state):
        parts = raw.split("|")
        if raw.startswith(("|detailschange|", "|-formechange|")) and len(parts) >= 4:
            for observed in reversed(switches):
                if observed["ident"] == parts[2]:
                    suffix = observed["details"].partition(",")[2]
                    observed["details"] = (
                        parts[3] if "," in parts[3] or not suffix
                        else f"{parts[3]},{suffix}"
                    )
                    break
        if not raw.startswith(("|switch|", "|drag|")):
            continue
        if len(parts) < 5 or not parts[2].startswith(prefix):
            continue
        switches.append(
            {
                "ident": parts[2],
                "details": parts[3],
                "condition": parts[4],
            }
        )

    if not switches:
        current = state.get("me") if side == "p1" else state.get("opp")
        if current and current.get("name"):
            switches = [
                {
                    "ident": f"{side}a: {current['name']}",
                    "details": current.get("details") or current.get("name"),
                    "condition": (
                        f"{current.get('hp')}/{current.get('maxHp')}"
                        if current.get("hp") is not None
                        and current.get("maxHp") is not None
                        else "",
                    ),
                }
            ]

    rows_by_name: dict[str, dict[str, Any]] = {}
    source_rows = list(state.get("myTeam") or []) if side == "p1" else list(
        state.get("oppTeam") or []
    )
    for row in source_rows:
        row_name = row.get("name") or _species_from_details(row.get("details"))
        if row_name:
            rows_by_name[_name_id(row_name)] = row

    revealed_moves: dict[str, list[str]] = {}
    revealed_abilities: dict[str, str] = {}
    revealed_items: dict[str, str] = {}
    revealed_tera: dict[str, str] = {}
    row_boosts: dict[str, dict[str, int]] = {}
    for row in source_rows:
        row_name = row.get("name") or _species_from_details(row.get("details"))
        if row_name:
            row_boosts[_name_id(row_name)] = _parse_boosts(row.get("boosts"))

    for raw in _history_lines(state):
        parts = raw.split("|")
        if len(parts) < 4 or not parts[2].startswith(prefix):
            continue
        if raw.startswith("|move|") and len(parts) >= 4:
            actor = _name_id(parts[2].split(":", 1)[-1])
            revealed_moves.setdefault(actor, []).append(parts[3])
        elif raw.startswith("|-ability|") and len(parts) >= 4:
            revealed_abilities[_name_id(parts[2].split(":", 1)[-1])] = parts[3]
        elif raw.startswith("|-item|") and len(parts) >= 4:
            revealed_items[_name_id(parts[2].split(":", 1)[-1])] = parts[3]
        elif raw.startswith(("|-terastallize|", "|terastallize|")) and len(parts) >= 4:
            revealed_tera[_name_id(parts[2].split(":", 1)[-1])] = parts[3]

    # session 里的 oppTeam 可能已经有当前值，但完整 history 才能覆盖
    # clear、换人前后的增减变化，优先按公开事件从零重放。
    history_boosts: dict[str, dict[str, int]] = {}
    history_boost_actors: set[str] = set()
    previous_actor = None
    for raw in _history_lines(state):
        parts = raw.split("|")
        if raw == "|-clearallboost":
            _apply_public_boost_event(history_boosts, "clearallboost", [])
            continue
        if len(parts) < 3:
            continue
        event_type = parts[1].lstrip("-").lower()
        if event_type == "clearallboost":
            _apply_public_boost_event(history_boosts, event_type, parts[2:])
            continue
        if not parts[2].startswith(prefix):
            continue
        actor_id = _name_id(parts[2].split(":", 1)[-1])
        if event_type == "faint":
            history_boosts[actor_id] = {}
            history_boost_actors.add(actor_id)
            continue
        if event_type in {"switch", "drag"}:
            if previous_actor is not None:
                history_boosts[previous_actor] = {}
                history_boost_actors.add(previous_actor)
            history_boosts[actor_id] = {}
            history_boost_actors.add(actor_id)
            previous_actor = actor_id
            continue
        if event_type not in {
            "boost",
            "unboost",
            "setboost",
            "clearboost",
            "clearpositiveboost",
            "clearnegativeboost",
            "clearallboost",
        }:
            continue
        history_boost_actors.add(actor_id)
        _apply_public_boost_event(history_boosts, event_type, parts[2:])

    # 保留首次公开顺序，但每只宝可梦使用最后一次 switch 的 details/HP，
    # 避免把初次满血状态错误地带入当前搜索。
    active_name = _name_id(
        (state.get("me") if side == "p1" else state.get("opp") or {}).get("name")
    )
    ordered = sorted(
        enumerate(switches),
        key=lambda item: (0 if _name_id(item[1]["ident"].split(":", 1)[-1]) == active_name else 1, item[0]),
    )
    latest_by_actor: dict[str, dict[str, Any]] = {}
    actor_order: list[str] = []
    for _, observed in ordered:
        actor_name = observed["ident"].split(":", 1)[-1]
        actor_id = _name_id(actor_name)
        if actor_id not in latest_by_actor:
            actor_order.append(actor_id)
        latest_by_actor[actor_id] = observed

    result: list[PublicPokemonState] = []
    for actor_id in actor_order:
        observed = latest_by_actor[actor_id]
        details = observed["details"]
        species = _species_from_details(details)
        row = rows_by_name.get(actor_id) or rows_by_name.get(_name_id(species), {})
        moves = tuple(dict.fromkeys(revealed_moves.get(actor_id, ())))
        condition = row.get("condition")
        if not condition and row.get("hp") is not None and row.get("maxHp") is not None:
            condition = f"{row['hp']}/{row['maxHp']}"
        if not condition:
            condition = observed["condition"]
        current_hp, max_hp, condition_status = _condition(condition)
        status = row.get("status") or condition_status
        if side == "p1" and max_hp and current_hp is not None:
            percent = math.ceil(100 * current_hp / max_hp)
            if percent == 100 and current_hp < max_hp:
                percent = 99
            condition = f"{percent}/100"
        boosts = (
            history_boosts.get(actor_id, {})
            if actor_id in history_boost_actors
            else row_boosts.get(actor_id, {})
        )
        result.append(
            PublicPokemonState(
                ident=observed["ident"],
                species=species,
                details=details,
                hp=condition,
                status=status,
                revealed_moves=moves,
                revealed_item=revealed_items.get(actor_id),
                revealed_ability=revealed_abilities.get(actor_id),
                revealed_tera_type=revealed_tera.get(actor_id),
                boosts=boosts,
                fainted=current_hp == 0 or "fnt" in str(condition or ""),
            )
        )
    return tuple(result)


def _public_state_for_opponent(state: dict[str, Any]) -> PublicBattleState:
    """构造对手视角，p1 只保留已经公开的我方信息。"""
    current = _public_state(state)
    return PublicBattleState(
        turn=current.turn,
        format_id=current.format_id,
        sides={
            "p1": _public_observed_side(state, "p1"),
            "p2": current.sides.get("p2", ()),
        },
        weather=current.weather,
        terrain=current.terrain,
        field=current.field,
        history=current.history,
    )


def _apply_sampled_public_state(
    battle: Battle,
    state: dict[str, Any],
    opponent_particle: Any,
) -> None:
    public_opponent = list(_public_observed_side(state, "p2"))
    sampled_team = list(opponent_particle.payload.get("team") or ())
    if not sampled_team:
        raise ValueError("opponent particle has no team")

    # sampled_team 的 slot 顺序不等于当前 active 顺序。对手发生换人后，
    # 不能再用 enumerate(public_opponent) 与 enumerate(sampled_team) 对齐，
    # 否则会把 Mienshao 的 HP/招式信息写到 Garganacl 上。
    sampled_by_species = {
        _name_id(candidate.get("species")): candidate for candidate in sampled_team
    }
    pokemon_by_species = {
        _name_id(pokemon.name): pokemon
        for pokemon in [battle.opponent.active, *battle.opponent.reserve]
        if pokemon is not None
    }
    for observed in public_opponent:
        species_id = _name_id(observed.species)
        target = pokemon_by_species.get(species_id)
        if target is None:
            continue
        if species_id not in sampled_by_species:
            continue
        _set_public_condition(target, observed.hp)
        target.status = observed.status
        _set_boosts(target, dict(observed.boosts))
        if observed.revealed_item:
            target.item = observed.revealed_item
        if observed.revealed_ability:
            target.ability = observed.revealed_ability
        if observed.revealed_tera_type:
            target.tera_type = _name_id(observed.revealed_tera_type)
            target.terastallized = True


def battle_from_live_state(
    state: dict[str, Any],
    opponent_particle: Any,
) -> Battle:
    request = state.get("request") or {}
    team, active_index = _request_team(request)
    if not team:
        team, active_index = _state_team(state)
    if not team:
        raise ValueError("live state contains no private team")
    state_team_rows = list(state.get("myTeam") or [])
    rows_by_name = {
        _name_id(row.get("name") or _species_from_details(row.get("details"))): row
        for row in state_team_rows
        if row.get("name") or row.get("details")
    }
    for pokemon in team:
        row = rows_by_name.get(_name_id(pokemon.name))
        if row is None:
            continue
        condition = row.get("condition")
        if condition is None and row.get("hp") is not None and row.get("maxHp") is not None:
            condition = f"{row['hp']}/{row['maxHp']}"
        _set_public_condition(pokemon, condition)
        _set_boosts(pokemon, row.get("boosts"))
    _apply_terastallization_history(team, state, "p1")

    battle = Battle(str(state.get("battleId") or "live-reference"))
    battle.battle_type = BattleType.RANDOM_BATTLE
    battle.generation = "gen9"
    battle.pokemon_format = "gen9randombattle"
    battle.turn = int(state.get("turn") or 0)
    battle.rqid = request.get("rqid")
    battle.wait = bool(request.get("wait"))
    battle.user.name = str((request.get("side") or {}).get("id") or "p1")
    battle.opponent.name = "opponent"
    battle.user.active = team[active_index]
    _apply_active_request_moves(battle.user.active, request)
    _apply_active_request_constraints(battle.user, request)
    battle.force_switch = any(
        bool(item) for item in (request.get("forceSwitch") or ())
    )
    battle.user.reserve = [
        pokemon for index, pokemon in enumerate(team) if index != active_index
    ]

    sampled_team = [
        Pokemon(
            str(candidate["species"]),
            int(candidate.get("level", 100) or 100),
        )
        for candidate in opponent_particle.payload.get("team", ())
    ]
    for pokemon, candidate in zip(
        sampled_team, opponent_particle.payload.get("team", ())
    ):
        _set_request_pokemon(pokemon, candidate)
    if not sampled_team:
        raise ValueError("sampled opponent team is empty")

    public_opponent = list((_public_state(state).sides.get("p2") or ()))
    active_name = _name_id(public_opponent[0].species if public_opponent else (state.get("opp") or {}).get("name"))
    active_index = next(
        (
            index
            for index, pokemon in enumerate(sampled_team)
            if _name_id(pokemon.name) == active_name
        ),
        0,
    )
    battle.opponent.active = sampled_team[active_index]
    battle.opponent.reserve = [
        pokemon for index, pokemon in enumerate(sampled_team) if index != active_index
    ]
    _apply_sampled_public_state(battle, state, opponent_particle)
    _apply_live_side_conditions(battle, state)
    _apply_live_side_effects(battle, state)
    _apply_live_field_state(battle, state)
    _apply_live_last_moves(battle, state)
    _apply_live_volatile_state(battle, state)
    _apply_request_recharge_semantics(battle, request)
    _apply_live_transforms(battle, state)
    _apply_active_item_move_constraints(battle)
    _disable_redundant_setup_moves(battle)
    _disable_history_invalid_moves(battle, state)

    battle.weather = _normalize_weather(state.get("weather"))
    battle.field = _normalize_terrain(
        (state.get("fieldState") or {}).get("terrain") or state.get("terrain")
    )
    battle.team_preview = bool(request.get("teamPreview"))
    return battle


def _normalize_reference_action(action: str) -> str:
    # Tera 与普通招式是两个不同的根节点动作，不能在 reference 层合并。
    # 如果这里丢掉 -tera，服务端就无法知道 Foul Play 是否选择了钛晶化。
    return str(action or "").strip().lower()


def _action_distribution(
    oracle_result: Any,
) -> dict[str, float]:
    return {
        _normalize_reference_action(item.action): float(item.probability)
        for item in oracle_result.actions
    }


def _request_forced_recharge(state: dict[str, Any]) -> bool:
    """判断当前 PS request 是否只允许提交 Recharge。"""
    actions = list(state.get("actions") or ())
    if len(actions) != 1:
        return False
    action = actions[0] or {}
    return (
        action.get("type") == "move"
        and _name_id(action.get("id") or action.get("name")) == "recharge"
    )


@dataclass(frozen=True)
class LiveReferenceResult:
    """线上 reference policy 的可序列化结果。"""

    best_action: str
    actions: dict[str, float]
    search_time_ms: int
    particle_count: int
    reference_mode: str = "sampled-opponent-root-oracle"
    raw_best_action: str | None = None
    selection_mode: str = "argmax"
    selection_candidates: tuple[dict[str, Any], ...] = ()
    world_diagnostics: tuple[dict[str, Any], ...] = ()
    dynamic_sampling: bool = False
    search_time_per_world_ms: int = 0


@dataclass(frozen=True)
class LiveDualViewResult:
    """线上 DualView 根节点重加权结果。"""

    best_action: str
    actions: dict[str, float]
    self_policy: dict[str, float]
    opponent_prediction: dict[str, float]
    root_action_scores: dict[str, dict[str, float]]
    search_time_ms: int
    self_search_time_ms: int
    opponent_search_time_ms: int
    particle_count: int
    strength: float
    particle_diagnostics: tuple[dict[str, Any], ...] = ()
    action_pair_values: dict[str, dict[str, float]] = field(default_factory=dict)
    opponent_model_prediction: dict[str, float] = field(default_factory=dict)
    opponent_exploration: float = 0.0
    reference_mode: str = "dual-view-root-reweighted"


class LiveReferencePolicy:
    """用私有 request + 公开对手信息计算 Foul Play reference policy。"""

    def __init__(
        self,
        particle_count: int = 1,
        search_time_ms: int = 50,
        seed: int = 17,
        mode: str = "diagnostic",
        search_parallelism: int = 1,
    ):
        if particle_count < 0:
            raise ValueError("particle_count must be non-negative")
        if search_time_ms <= 0:
            raise ValueError("search_time_ms must be positive")
        if mode not in {"diagnostic", "upstream-compatible"}:
            raise ValueError("mode must be diagnostic or upstream-compatible")
        if search_parallelism <= 0:
            raise ValueError("search_parallelism must be positive")
        self.particle_count = particle_count
        self.search_time_ms = search_time_ms
        self.seed = seed
        self.mode = mode
        self.search_parallelism = search_parallelism
        self.oracle = FoulPlayPolicyOracle()

    @staticmethod
    def _public_active_move_count(state: dict[str, Any]) -> int:
        active = state.get("opp") or {}
        details = active.get("moveDetails")
        if isinstance(details, dict) and details:
            return len(details)
        moves = active.get("moves")
        return len(moves) if isinstance(moves, list) else 0

    def _sampling_plan(
        self,
        state: dict[str, Any],
        public: PublicBattleState,
    ) -> tuple[int, int, bool]:
        if self.mode == "diagnostic":
            count = self.particle_count or 1
            return count, self.search_time_ms, False

        # 复刻 upstream Random Battle 的信息量分段：早期且对手尚未公开招式时，
        # 多采样、每个世界使用较浅的搜索；其他阶段使用较少但更深的世界。
        if self.particle_count:
            return self.particle_count, self.search_time_ms, False
        revealed = len(public.sides.get("p2", ()))
        active = state.get("opp") or {}
        active_hp = active.get("hpPct")
        try:
            active_alive = active_hp is None or float(active_hp) > 0
        except (TypeError, ValueError):
            active_alive = True
        no_revealed_moves = self._public_active_move_count(state) == 0
        if revealed <= 3 and active_alive and no_revealed_moves:
            return (
                self.search_parallelism * 4,
                max(1, self.search_time_ms // 2),
                True,
            )
        return self.search_parallelism * 2, self.search_time_ms, True

    def _select_action(
        self,
        state: dict[str, Any],
        aggregate: dict[str, float],
    ) -> tuple[str, str, tuple[dict[str, Any], ...], str]:
        raw_best = max(aggregate.items(), key=lambda item: item[1])[0]
        if self.mode != "upstream-compatible":
            return raw_best, raw_best, (), "argmax"

        threshold = aggregate[raw_best] * 0.75
        candidates = [
            {"action": action, "probability": probability}
            for action, probability in aggregate.items()
            if probability >= threshold
        ]
        candidates.sort(key=lambda item: (-item["probability"], item["action"]))
        key = "{}:{}:{}".format(
            state.get("battleId") or "",
            state.get("rqid") or "",
            state.get("turn") or 0,
        )
        digest = hashlib.sha256(
            "{}:{}".format(self.seed, key).encode("utf-8")
        ).hexdigest()
        rng = random.Random(int(digest[:16], 16))
        selected = rng.choices(
            [item["action"] for item in candidates],
            weights=[item["probability"] for item in candidates],
            k=1,
        )[0]
        return selected, raw_best, tuple(candidates), "upstream-75pct-random"

    def evaluate(self, state: dict[str, Any]) -> LiveReferenceResult:
        format_id = _format_id(state.get("format"))
        if format_id != "gen9randombattle":
            raise ValueError("live reference only supports gen9randombattle")

        # PS 在 recharge 回合已经把合法动作确定为 Recharge。此时不能
        # 让 engine 把协议层强制动作解释成普通搜索节点。
        if _request_forced_recharge(state):
            return LiveReferenceResult(
                best_action="recharge",
                actions={"recharge": 1.0},
                search_time_ms=0,
                particle_count=0,
                reference_mode="upstream-compatible-request-forced",
                raw_best_action="recharge",
                selection_mode="request-forced",
                selection_candidates=(
                    {"action": "recharge", "probability": 1.0},
                ),
                dynamic_sampling=False,
                search_time_per_world_ms=0,
            )

        public = _public_state(state)
        particle_count, search_time_per_world, dynamic_sampling = self._sampling_plan(
            state,
            public,
        )
        sampler = Gen9RandomBattleBeliefSampler(seed=self.seed)
        particles = sampler.sample(public, "p2", particle_count)
        aggregate: dict[str, float] = {}
        world_diagnostics: list[dict[str, Any]] = []
        for particle in particles:
            battle = battle_from_live_state(state, particle)
            result = self.oracle.evaluate(
                battle,
                perspective="self",
                search_time_ms=search_time_per_world,
            )
            world_diagnostics.append(
                {
                    "particleId": particle.particle_id,
                    "weight": particle.weight,
                    "bestAction": result.best_action,
                    "totalVisits": result.total_visits,
                    "searchTimeMs": result.search_time_ms,
                    "actions": result.distribution(),
                }
            )
            for action, probability in _action_distribution(result).items():
                aggregate[action] = aggregate.get(action, 0.0) + (
                    particle.weight * probability
                )

        total = sum(aggregate.values())
        if total <= 0 or not math.isfinite(total):
            raise ValueError("reference oracle returned no positive action probability")
        aggregate = {
            action: probability / total for action, probability in aggregate.items()
        }
        best_action, raw_best_action, selection_candidates, selection_mode = (
            self._select_action(state, aggregate)
        )
        return LiveReferenceResult(
            best_action=best_action,
            actions=aggregate,
            search_time_ms=self.search_time_ms,
            particle_count=len(particles),
            raw_best_action=raw_best_action,
            selection_mode=selection_mode,
            selection_candidates=selection_candidates,
            world_diagnostics=tuple(world_diagnostics),
            dynamic_sampling=dynamic_sampling,
            search_time_per_world_ms=search_time_per_world,
            reference_mode=(
                "upstream-compatible-root-oracle"
                if self.mode == "upstream-compatible"
                else "sampled-opponent-root-oracle"
            ),
        )


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


def _canonical_action(value: Any) -> str:
    return str(value or "").strip().lower().replace("-tera", "")


def _root_action_value(our_action: str, opponent_action: str) -> float:
    """对候选动作施加很小的对手策略风险修正。

    这里不伪装成第二阶 damage search：只惩罚在对手可能攻击时使用
    setup，奖励在对手可能换人时保留主动权。真正的逐动作模拟留给后续阶段。
    """
    our = _canonical_action(our_action)
    opponent = _canonical_action(opponent_action)
    our_switch = our.startswith("switch ")
    opponent_switch = opponent.startswith("switch ")
    our_move = our.split(" ", 1)[-1] if our.startswith("move ") else our

    if opponent_switch and not our_switch:
        return 0.12
    if our_switch and not opponent_switch:
        return 0.04
    if our_move in _SETUP_MOVES and not opponent_switch:
        return -0.18
    return 0.0


def _aggregate_policy_results(
    results: list[tuple[float, PolicyOracleResult]],
    perspective: str,
) -> PolicyOracleResult:
    probabilities: dict[str, float] = {}
    for particle_weight, result in results:
        for action in result.actions:
            probabilities[action.action] = probabilities.get(action.action, 0.0) + (
                particle_weight * action.probability
            )
    total = sum(probabilities.values())
    if total <= 0:
        raise ValueError("policy oracle returned no positive action probability")
    actions = tuple(
        PolicyActionStat(
            action=action,
            visits=max(1, round(probability / total * 10000)),
            total_score=0.0,
            probability=probability / total,
        )
        for action, probability in probabilities.items()
    )
    return PolicyOracleResult(
        perspective=perspective,
        search_time_ms=max(result.search_time_ms for _, result in results),
        threads=max(result.threads for _, result in results),
        total_visits=sum(result.total_visits for _, result in results),
        actions=actions,
    )


def _mix_opponent_policy(
    model_distribution: dict[str, float],
    exploration: float,
    action_universe: set[str] | None = None,
) -> dict[str, float]:
    """为对手策略加入少量均匀探索，避免把预测当成游戏规则概率。"""
    if not math.isfinite(exploration) or not 0.0 <= exploration <= 1.0:
        raise ValueError("opponent_exploration must be between 0 and 1")
    if not model_distribution:
        raise ValueError("opponent model distribution must not be empty")
    total = sum(
        probability
        for probability in model_distribution.values()
        if math.isfinite(probability) and probability >= 0
    )
    if total <= 0:
        raise ValueError("opponent model distribution must contain positive mass")
    normalized = {
        action: probability / total
        for action, probability in model_distribution.items()
        if math.isfinite(probability) and probability >= 0
    }
    support = set(action_universe or ())
    support.update(normalized)
    if not support:
        raise ValueError("opponent action universe must not be empty")
    uniform_probability = 1.0 / len(support)
    return {
        action: (1.0 - exploration) * normalized.get(action, 0.0)
        + exploration * uniform_probability
        for action in support
    }


class LiveDualViewPolicy:
    """使用 belief、双向 Foul Play oracle 和根节点风险修正生成实际动作。"""

    def __init__(
        self,
        particle_count: int = 1,
        search_time_ms: int | None = None,
        seed: int = 17,
        strength: float = 1.0,
        self_search_time_ms: int | None = None,
        opponent_search_time_ms: int | None = None,
        opponent_exploration: float = 0.1,
        mode: str = "diagnostic",
        search_parallelism: int = 1,
    ):
        if particle_count < 0:
            raise ValueError("particle_count must be non-negative")
        if search_time_ms is not None:
            if self_search_time_ms is None:
                self_search_time_ms = search_time_ms
            if opponent_search_time_ms is None:
                opponent_search_time_ms = search_time_ms
        if self_search_time_ms is None:
            self_search_time_ms = 50
        if opponent_search_time_ms is None:
            opponent_search_time_ms = self_search_time_ms
        if self_search_time_ms <= 0:
            raise ValueError("self_search_time_ms must be positive")
        if opponent_search_time_ms <= 0:
            raise ValueError("opponent_search_time_ms must be positive")
        if strength < 0 or not math.isfinite(strength):
            raise ValueError("strength must be a finite non-negative number")
        if not math.isfinite(opponent_exploration) or not 0.0 <= opponent_exploration <= 1.0:
            raise ValueError("opponent_exploration must be between 0 and 1")
        if mode not in {"diagnostic", "upstream-compatible"}:
            raise ValueError("mode must be diagnostic or upstream-compatible")
        if search_parallelism <= 0:
            raise ValueError("search_parallelism must be positive")
        self.particle_count = particle_count
        # 保留旧字段，避免外部调用方读取 search_time_ms 时失效。
        self.search_time_ms = self_search_time_ms
        self.self_search_time_ms = self_search_time_ms
        self.opponent_search_time_ms = opponent_search_time_ms
        self.seed = seed
        self.strength = strength
        self.opponent_exploration = opponent_exploration
        self.mode = mode
        self.search_parallelism = search_parallelism
        self.oracle = FoulPlayPolicyOracle()
        self.shadow_predictor = FoulPlayShadowPredictor(self.oracle)
        self.reweighter = RootActionReweighter()
        self.action_pair_evaluator = ActionPairEvaluator()

    def evaluate(self, state: dict[str, Any]) -> LiveDualViewResult:
        format_id = _format_id(state.get("format"))
        if format_id != "gen9randombattle":
            raise ValueError("live DualView only supports gen9randombattle")

        public = _public_state(state)
        opponent_view = _public_state_for_opponent(state)
        reference_plan = LiveReferencePolicy(
            particle_count=self.particle_count,
            search_time_ms=self.self_search_time_ms,
            seed=self.seed,
            mode=self.mode,
            search_parallelism=self.search_parallelism,
        )
        opponent_particle_count, self_search_time_ms, _dynamic_sampling = (
            reference_plan._sampling_plan(state, public)
        )
        # strength=0 的 control 必须与 8093 使用完全相同的 reference policy，
        # 不能因为仍然计算 shadow/action-pair 而引入额外的随机和采样差异。
        if self.strength == 0 and self.mode == "upstream-compatible":
            reference = reference_plan.evaluate(state)
            return LiveDualViewResult(
                best_action=reference.best_action,
                actions=reference.actions,
                self_policy=reference.actions,
                opponent_prediction={},
                root_action_scores={
                    action: {
                        "priorProbability": probability,
                        "expectedValue": 0.0,
                        "reweightedProbability": probability,
                    }
                    for action, probability in reference.actions.items()
                },
                search_time_ms=reference.search_time_ms,
                self_search_time_ms=reference.search_time_per_world_ms,
                opponent_search_time_ms=self.opponent_search_time_ms,
                particle_count=reference.particle_count,
                strength=0.0,
                reference_mode="dual-control-upstream-compatible",
            )
        sampler = Gen9RandomBattleBeliefSampler(seed=self.seed)
        opponent_particles = sampler.sample(public, "p2", opponent_particle_count)
        our_particles = sampler.sample(opponent_view, "p1", self.particle_count or 1)
        self_results: list[tuple[float, PolicyOracleResult]] = []
        shadow_results: list[tuple[float, ShadowPrediction]] = []
        particle_diagnostics: list[dict[str, Any]] = []
        action_pair_values: dict[str, dict[str, float]] = {}
        action_pair_weights: dict[str, dict[str, float]] = {}
        opponent_action_universe: set[str] = set()
        for opponent_particle in opponent_particles:
            battle = battle_from_live_state(state, opponent_particle)
            if battle.opponent.active is not None:
                opponent_action_universe.update(
                    move.name for move in battle.opponent.active.moves
                )
            opponent_action_universe.update(
                f"switch {pokemon.name}" for pokemon in battle.opponent.reserve
            )
            self_result = self.oracle.evaluate(
                battle,
                perspective="self",
                search_time_ms=self_search_time_ms,
            )
            self_results.append((opponent_particle.weight, self_result))
            for our_particle in our_particles:
                shadow = self.shadow_predictor.predict(
                    battle,
                    [our_particle],
                    search_time_ms=self.opponent_search_time_ms,
                )
                shadow_results.append(
                    (
                        opponent_particle.weight * our_particle.weight,
                        shadow,
                    )
                )
                opponent_result = shadow.particle_results[0]
                opponent_action_universe.update(opponent_result.distribution())
                particle_diagnostics.append(
                    {
                        "opponentParticleId": opponent_particle.particle_id,
                        "ourBeliefParticleId": our_particle.particle_id,
                        "opponentParticleWeight": opponent_particle.weight,
                        "ourBeliefParticleWeight": our_particle.weight,
                        "jointWeight": (
                            opponent_particle.weight * our_particle.weight
                        ),
                        "selfBestAction": self_result.best_action,
                        "selfTotalVisits": self_result.total_visits,
                        "selfSearchTimeMs": self_result.search_time_ms,
                        "selfActions": self_result.distribution(),
                        "opponentBestAction": opponent_result.best_action,
                        "opponentTotalVisits": opponent_result.total_visits,
                        "opponentSearchTimeMs": opponent_result.search_time_ms,
                        "opponentActions": opponent_result.distribution(),
                    }
                )
                for our_action in self_result.distribution():
                    values = action_pair_values.setdefault(our_action, {})
                    for opponent_action in opponent_result.distribution():
                        value = self.action_pair_evaluator.evaluate(
                            battle,
                            our_action,
                            opponent_action,
                        )
                        joint_weight = (
                            opponent_particle.weight * our_particle.weight
                        )
                        values[opponent_action] = values.get(opponent_action, 0.0) + (
                            joint_weight * value
                        )
                        weights = action_pair_weights.setdefault(our_action, {})
                        weights[opponent_action] = (
                            weights.get(opponent_action, 0.0) + joint_weight
                        )

        self_policy = _aggregate_policy_results(self_results, "self")
        opponent_model_probabilities: dict[str, float] = {}
        for particle_weight, shadow in shadow_results:
            for action, probability in shadow.action_probabilities.items():
                opponent_model_probabilities[action] = (
                    opponent_model_probabilities.get(action, 0.0)
                    + particle_weight * probability
                )
        total = sum(opponent_model_probabilities.values())
        if total <= 0:
            raise ValueError("opponent prediction returned no positive probabilities")
        opponent_model_probabilities = {
            action: probability / total
            for action, probability in opponent_model_probabilities.items()
        }
        opponent_probabilities = _mix_opponent_policy(
            opponent_model_probabilities,
            self.opponent_exploration,
            action_universe=opponent_action_universe,
        )
        shadow_prediction = ShadowPrediction(
            action_probabilities=opponent_probabilities,
            particle_results=tuple(
                result for _, shadow in shadow_results for result in shadow.particle_results
            ),
            particle_weights=tuple(weight for weight, _ in shadow_results),
        )
        def action_value(our_action: str, opponent_action: str) -> float:
            value = action_pair_values.get(our_action, {}).get(opponent_action)
            weight = action_pair_weights.get(our_action, {}).get(opponent_action, 0.0)
            if value is None or weight <= 0:
                return 0.0
            return value / weight

        reweighted = self.reweighter.evaluate(
            self_policy,
            shadow_prediction,
            action_value,
            strength=self.strength,
        )
        return LiveDualViewResult(
            best_action=reweighted.best_action or self_policy.best_action or "",
            actions={
                item.action: item.reweighted_probability
                for item in reweighted.actions
            },
            self_policy=self_policy.distribution(),
            opponent_prediction=opponent_probabilities,
            root_action_scores={
                item.action: {
                    "priorProbability": item.prior_probability,
                    "expectedValue": item.expected_value,
                    "reweightedProbability": item.reweighted_probability,
                }
                for item in reweighted.actions
            },
            search_time_ms=self_search_time_ms,
            self_search_time_ms=self_search_time_ms,
            opponent_search_time_ms=self.opponent_search_time_ms,
            particle_count=len(opponent_particles) * len(our_particles),
            strength=self.strength,
            particle_diagnostics=tuple(particle_diagnostics),
            action_pair_values=action_pair_values,
            opponent_model_prediction=opponent_model_probabilities,
            opponent_exploration=self.opponent_exploration,
        )
