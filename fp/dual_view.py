"""双向信息集的最小数据模型。

这个模块只负责信息边界和视角转换，不负责 Pokemon 规则模拟、采样或搜索。
所有输入都会在构造时复制，所有输出都会重新复制，避免调用方通过可变字典
间接修改另一种视角。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


def _freeze(value: Any) -> Any:
    """将 JSON 兼容数据转换为不可变结构。"""
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    """将内部不可变结构复制为普通 JSON 兼容结构。"""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, frozenset)):
        return [_thaw(item) for item in value]
    return deepcopy(value)


def _as_frozen_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    return _freeze(dict(value))


@dataclass(frozen=True)
class PublicPokemonState:
    """双方都能从公开战报重建的单只 Pokemon 状态。"""

    ident: str
    species: str
    details: str | None = None
    hp: str | None = None
    status: str | None = None
    revealed_moves: tuple[str, ...] = ()
    revealed_item: str | None = None
    revealed_ability: str | None = None
    revealed_tera_type: str | None = None
    boosts: Mapping[str, int] = field(default_factory=dict)
    volatile: Mapping[str, Any] = field(default_factory=dict)
    fainted: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "revealed_moves", tuple(self.revealed_moves))
        object.__setattr__(self, "boosts", _as_frozen_mapping(self.boosts))
        object.__setattr__(self, "volatile", _as_frozen_mapping(self.volatile))

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "ident": self.ident,
            "species": self.species,
            "details": self.details,
            "hp": self.hp,
            "status": self.status,
            "revealedMoves": list(self.revealed_moves),
            "revealedItem": self.revealed_item,
            "revealedAbility": self.revealed_ability,
            "revealedTeraType": self.revealed_tera_type,
            "boosts": _thaw(self.boosts),
            "volatile": _thaw(self.volatile),
            "fainted": self.fainted,
        }


@dataclass(frozen=True)
class PublicBattleState:
    """不包含任何一方尚未公开的私有配置。"""

    turn: int
    format_id: str
    sides: Mapping[str, tuple[PublicPokemonState, ...]]
    weather: str | None = None
    terrain: str | None = None
    field: Mapping[str, Any] = field(default_factory=dict)
    history: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        frozen_sides = {
            str(side): tuple(pokemon for pokemon in pokemon_list)
            for side, pokemon_list in self.sides.items()
        }
        object.__setattr__(self, "sides", MappingProxyType(frozen_sides))
        object.__setattr__(self, "field", _as_frozen_mapping(self.field))
        object.__setattr__(
            self,
            "history",
            tuple(_as_frozen_mapping(event) for event in self.history),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "format": self.format_id,
            "weather": self.weather,
            "terrain": self.terrain,
            "field": _thaw(self.field),
            "sides": {
                side: [pokemon.to_public_dict() for pokemon in pokemon_list]
                for side, pokemon_list in self.sides.items()
            },
            "history": [_thaw(event) for event in self.history],
        }


@dataclass(frozen=True)
class PrivatePokemonState:
    """我方真实知道、但不一定已公开的单只 Pokemon 配置。"""

    public: PublicPokemonState
    private_fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "private_fields", _as_frozen_mapping(self.private_fields)
        )

    def to_private_dict(self) -> dict[str, Any]:
        result = self.public.to_public_dict()
        result["privateFields"] = _thaw(self.private_fields)
        return result


@dataclass(frozen=True)
class BeliefParticle:
    """某个信息集下对隐藏配置的一个假设。"""

    particle_id: str
    payload: Mapping[str, Any]
    weight: float = 1.0
    source: str = "sampler"

    def __post_init__(self) -> None:
        if self.weight < 0:
            raise ValueError("belief particle weight cannot be negative")
        object.__setattr__(self, "payload", _as_frozen_mapping(self.payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "particleId": self.particle_id,
            "payload": _thaw(self.payload),
            "weight": self.weight,
            "source": self.source,
        }


@dataclass(frozen=True)
class DualViewState:
    """同时保存公开状态、我方私有状态和双方 belief 的边界对象。

    `public` 是唯一可以直接交给对手视角的真实状态。
    `self_private` 只允许进入 `self_decision_input()`。
    `belief_about_us` 是对手可能持有的我方配置假设，不是真实私有状态。
    """

    public: PublicBattleState
    self_private: Mapping[str, PrivatePokemonState]
    belief_about_opponent: tuple[BeliefParticle, ...] = ()
    belief_about_us: tuple[BeliefParticle, ...] = ()

    def __post_init__(self) -> None:
        private = {str(ident): pokemon for ident, pokemon in self.self_private.items()}
        object.__setattr__(self, "self_private", MappingProxyType(private))
        object.__setattr__(
            self, "belief_about_opponent", tuple(self.belief_about_opponent)
        )
        object.__setattr__(self, "belief_about_us", tuple(self.belief_about_us))

    def public_view(self) -> dict[str, Any]:
        """返回只包含公开信息的副本。"""
        return self.public.to_dict()

    def self_decision_input(self) -> dict[str, Any]:
        """构造我方决策输入，允许使用我方真实私有配置。"""
        return {
            "perspective": "self",
            "public": self.public_view(),
            "privateSelf": {
                ident: pokemon.to_private_dict()
                for ident, pokemon in self.self_private.items()
            },
            "beliefAboutOpponent": [
                particle.to_dict() for particle in self.belief_about_opponent
            ],
        }

    def opponent_visible_input(self) -> dict[str, Any]:
        """构造对手实际能观察到的输入，不包含任何 belief 或私有字段。"""
        return {
            "perspective": "opponent",
            "public": self.public_view(),
        }

    def opponent_policy_input(
        self,
        opponent_private: Mapping[str, Any],
        assumed_self_particle: BeliefParticle,
    ) -> dict[str, Any]:
        """构造对手 policy oracle 的输入。

        `opponent_private` 必须由调用方提供，因为我方通常只能通过采样得到
        对手真实拥有的私有配置。`assumed_self_particle` 是对手眼中的我方配置，
        不能替换成 `self_private`。
        """
        return {
            "perspective": "opponent",
            "public": self.public_view(),
            "privateObserver": _thaw(opponent_private),
            "assumedOpponent": assumed_self_particle.to_dict(),
        }

    def belief_about_us_dict(self) -> list[dict[str, Any]]:
        return [particle.to_dict() for particle in self.belief_about_us]
