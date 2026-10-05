"""生成不泄漏隐藏配置的公开战场观察。"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping

from fp.battle import Battle, Battler, Pokemon


SCHEMA_VERSION = "public-observation/v1"


def _name(value: Any) -> str:
    return str(value or "").strip().lower()


def _pokemon_key(pokemon: Pokemon) -> str:
    return _name(pokemon.nickname or pokemon.name)


def _revealed_for(
    reveals: Mapping[str, Mapping[str, Mapping[str, Any]]],
    side: str,
    pokemon: Pokemon,
) -> Mapping[str, Any]:
    side_reveals = reveals.get(side, {})
    return side_reveals.get(_pokemon_key(pokemon), side_reveals.get(_name(pokemon.name), {}))


def _public_pokemon(
    pokemon: Pokemon,
    reveals: Mapping[str, Any],
) -> dict[str, Any]:
    revealed_moves = reveals.get("moves")
    if pokemon.max_hp <= 0 or not 0 <= pokemon.hp <= pokemon.max_hp:
        raise ValueError("invalid HP for public observation")
    hp_percent = math.ceil(100 * pokemon.hp / pokemon.max_hp)
    if hp_percent == 100 and pokemon.hp < pokemon.max_hp:
        hp_percent = 99
    result = {
        "name": pokemon.name,
        "details": f"{pokemon.name}, L{pokemon.level}",
        "hp": hp_percent,
        "maxHp": 100,
        "status": pokemon.status,
        "boosts": {
            str(key): int(value)
            for key, value in pokemon.boosts.items()
            if int(value) != 0
        },
        "volatile": sorted(str(value) for value in pokemon.volatile_statuses),
        "fainted": bool(pokemon.fainted or pokemon.hp <= 0),
        "terastallized": bool(pokemon.terastallized),
        "teraType": pokemon.tera_type if pokemon.terastallized else None,
        "moves": sorted(_name(move) for move in (revealed_moves or ())),
        "movesKnown": revealed_moves is not None,
        "moveSetComplete": bool(reveals.get("moveSetComplete", False)),
        "item": reveals.get("item"),
        "itemKnown": "item" in reveals,
        "ability": reveals.get("ability"),
        "abilityKnown": "ability" in reveals,
    }
    return result


def _public_side(
    battler: Battler,
    side: str,
    visible_reserves: set[str],
    reveals: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    pokemon: list[Pokemon] = []
    if battler.active is not None:
        pokemon.append(battler.active)
    pokemon.extend(
        reserve
        for reserve in battler.reserve
        if _pokemon_key(reserve) in visible_reserves
        or _name(reserve.name) in visible_reserves
    )
    result = []
    for item in pokemon:
        row = _public_pokemon(item, _revealed_for(reveals, side, item))
        row["active"] = item is battler.active
        result.append(row)
    return result


@dataclass(frozen=True)
class PublicObservation:
    """公开观察及其稳定哈希。"""

    payload: Mapping[str, Any]
    observation_hash: str

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.payload, ensure_ascii=True))

    def to_public_battle_state(self):
        """转换为 belief sampler 使用的公开状态对象。"""
        from fp.dual_view import PublicBattleState, PublicPokemonState

        sides: dict[str, tuple[PublicPokemonState, ...]] = {}
        for side, rows in (self.payload.get("sides") or {}).items():
            converted = []
            for row in rows or ():
                hp = None
                if row.get("hp") is not None and row.get("maxHp"):
                    hp = f"{row['hp']}/{row['maxHp']}"
                converted.append(
                    PublicPokemonState(
                        ident=str(row.get("name") or ""),
                        species=str(row.get("name") or ""),
                        details=row.get("details"),
                        hp=hp,
                        status=row.get("status"),
                        revealed_moves=tuple(row.get("moves") or ())
                        if row.get("movesKnown")
                        else (),
                        revealed_item=(
                            row.get("item") if row.get("itemKnown") else None
                        ),
                        revealed_ability=(
                            row.get("ability") if row.get("abilityKnown") else None
                        ),
                        revealed_tera_type=row.get("teraType"),
                        boosts=row.get("boosts") or {},
                        volatile={"active": row.get("volatile") or ()},
                        fainted=bool(row.get("fainted")),
                    )
                )
            sides[str(side)] = tuple(converted)
        return PublicBattleState(
            turn=int(self.payload.get("turn") or 0),
            format_id=str(self.payload.get("format") or ""),
            sides=sides,
            weather=self.payload.get("weather"),
            terrain=self.payload.get("terrain"),
            field={
                "weatherTurnsRemaining": self.payload.get("weatherTurnsRemaining"),
                "terrainTurnsRemaining": self.payload.get(
                    "terrainTurnsRemaining"
                ),
                "trickRoom": self.payload.get("trickRoom"),
                "trickRoomTurnsRemaining": self.payload.get(
                    "trickRoomTurnsRemaining"
                ),
            },
        )


class PublicObservationBuilder:
    """只从显式公开集合生成观察，不从 Pokemon 私有字段推断公开信息。"""

    def build(
        self,
        battle: Battle,
        *,
        visible_reserves: Mapping[str, set[str] | list[str] | tuple[str, ...]] | None = None,
        reveals: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    ) -> PublicObservation:
        visible_reserves = visible_reserves or {}
        reveals = reveals or {}
        sides = {
            "p1": _public_side(
                battle.user,
                "p1",
                {_name(value) for value in visible_reserves.get("p1", ())},
                reveals,
            ),
            "p2": _public_side(
                battle.opponent,
                "p2",
                {_name(value) for value in visible_reserves.get("p2", ())},
                reveals,
            ),
        }
        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "turn": int(battle.turn or 0),
            "format": battle.pokemon_format,
            "sides": sides,
            "weather": battle.weather,
            "weatherTurnsRemaining": None,
            "terrain": battle.field,
            "terrainTurnsRemaining": None,
            "trickRoom": bool(battle.trick_room),
            "trickRoomTurnsRemaining": int(battle.trick_room_turns_remaining or 0),
            "sideConditions": {
                side: {
                    str(key): (
                        None if key in {"reflect", "lightscreen", "auroraveil", "tailwind"}
                        else int(value)
                    )
                    for key, value in battler.side_conditions.items()
                    if int(value) != 0
                }
                for side, battler in (("p1", battle.user), ("p2", battle.opponent))
            },
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return PublicObservation(
            payload=payload,
            observation_hash=hashlib.sha256(encoded).hexdigest(),
        )


def build_public_observation(
    battle: Battle,
    *,
    visible_reserves: Mapping[str, set[str] | list[str] | tuple[str, ...]] | None = None,
    reveals: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
) -> PublicObservation:
    """便捷入口。默认只公开双方 active，且不公开任何配置字段。"""
    return PublicObservationBuilder().build(
        battle,
        visible_reserves=visible_reserves,
        reveals=reveals,
    )
