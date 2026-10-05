"""Gen 9 Random Battle 的第一版对手视角 belief sampler。

该模块只接收 PublicBattleState，不接收我方真实 Pokemon、request 或私有 set。
因此它生成的是“对手可能认为我方拥有的队伍配置”，不是我方真实队伍的复制品。
复杂的联合概率和历史一致性过滤留给后续任务。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import random
from typing import Any

import constants
from data.pkmn_sets import PredictedPokemonSet, RandomBattleTeamDatasets
from fp.battle import Pokemon
from fp.dual_view import BeliefParticle, PublicBattleState, PublicPokemonState
from fp.helpers import (
    POKEMON_TYPE_INDICES,
    is_super_effective,
    type_effectiveness_modifier,
)


def _normalize_move(move: str) -> str:
    return "".join(character.lower() for character in str(move) if character.isalnum())


def _normalize_actor(ident: Any) -> str:
    """统一 request 的 p1: 名称和战报的 p1a: 名称。"""
    text = str(ident or "").strip().lower()
    if len(text) >= 3 and text[0:2] in {"p1", "p2"} and text[2].isalpha():
        text = text[:2] + text[3:]
    return text


@dataclass(frozen=True)
class SampledSet:
    """一个可序列化的 Random Battle 完整配置。"""

    species: str
    level: int
    ability: str
    item: str
    nature: str
    evs: tuple[int, ...]
    tera_type: str | None
    moves: tuple[str, ...]
    count: int

    @classmethod
    def from_predicted_set(
        cls, species: str, predicted: PredictedPokemonSet
    ) -> "SampledSet":
        pkmn_set = predicted.pkmn_set
        return cls(
            species=species,
            level=pkmn_set.level or 100,
            ability=pkmn_set.ability,
            item=pkmn_set.item,
            nature=pkmn_set.nature,
            evs=tuple(pkmn_set.evs),
            tera_type=pkmn_set.tera_type,
            moves=tuple(predicted.pkmn_moveset.moves),
            count=pkmn_set.count,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "species": self.species,
            "level": self.level,
            "ability": self.ability,
            "item": self.item,
            "nature": self.nature,
            "evs": list(self.evs),
            "teraType": self.tera_type,
            "moves": list(self.moves),
            "count": self.count,
        }


def _level_from_details(details: str | None) -> int:
    if not details:
        return 100
    for part in details.split(","):
        part = part.strip()
        if part.startswith("L") and part[1:].isdigit():
            return int(part[1:])
    return 100


def _public_to_pokemon(observation: PublicPokemonState) -> Pokemon:
    """将公开观察转换为只带公开约束的 Foul Play Pokemon。"""
    pokemon = Pokemon(observation.species, _level_from_details(observation.details))
    pokemon.ability = observation.revealed_ability
    pokemon.item = observation.revealed_item or constants.UNKNOWN_ITEM
    pokemon.terastallized = observation.revealed_tera_type is not None
    pokemon.tera_type = observation.revealed_tera_type

    for move in observation.revealed_moves:
        pokemon.add_move(move)

    if observation.fainted:
        pokemon.fainted = True
        pokemon.hp = 0
    return pokemon


def _species_key(observation: PublicPokemonState) -> str:
    return observation.species


def _team_constraint_violation(team: list[Pokemon]) -> bool:
    """复用 Gen 9 Random Battle 的基础队伍约束。"""
    species = [pokemon.get_species() for pokemon in team]
    if len(set(species)) != len(species):
        return True

    weak_counts: dict[str, int] = {}
    for pokemon in team:
        for pokemon_type in POKEMON_TYPE_INDICES:
            if is_super_effective(pokemon_type, pokemon.types):
                weak_counts[pokemon_type] = weak_counts.get(pokemon_type, 0) + 1
    if any(count > 3 for count in weak_counts.values()):
        return True

    type_counts: dict[str, int] = {}
    for pokemon in team:
        for pokemon_type in pokemon.types:
            type_counts[pokemon_type] = type_counts.get(pokemon_type, 0) + 1
    if any(count > 2 for count in type_counts.values()):
        return True

    weakness_counts: dict[str, int] = {}
    for pokemon in team:
        for pokemon_type in POKEMON_TYPE_INDICES:
            if type_effectiveness_modifier(pokemon_type, pokemon.types) == 4:
                weakness_counts[pokemon_type] = weakness_counts.get(pokemon_type, 0) + 1
    return any(count > 1 for count in weakness_counts.values())


def _weighted_choice(rng: random.Random, values: list[Any], weights: list[int]) -> Any:
    if not values:
        raise ValueError("cannot sample from an empty candidate list")
    if len(values) != len(weights):
        raise ValueError("candidate and weight lengths differ")
    if not any(weight > 0 for weight in weights):
        return rng.choice(values)
    return rng.choices(values, weights=weights, k=1)[0]


class Gen9RandomBattleBeliefSampler:
    """从公开状态生成对手视角下的 Gen 9 Random Battle 队伍粒子。"""

    def __init__(self, seed: int | None = None):
        self.seed = seed
        self._initialized = False

    def initialize(self) -> None:
        if not self._initialized:
            RandomBattleTeamDatasets.initialize("gen9")
            self._initialized = True

    def candidate_sets(
        self, observation: PublicPokemonState
    ) -> list[PredictedPokemonSet]:
        self.initialize()
        public_pokemon = _public_to_pokemon(observation)
        return RandomBattleTeamDatasets.get_all_remaining_sets(public_pokemon)

    def _sample_revealed(
        self, observation: PublicPokemonState, rng: random.Random
    ) -> SampledSet:
        candidates = self.candidate_sets(observation)
        if not candidates:
            # 公开物种和公开招式不一致时，可能是 Illusion：
            # 对手看到的是伪装物种，但招式属于真实物种。
            # 此时不能因为数据集没有“伪装物种 + 真实招式”的组合而中断
            # 整个决策。先忽略冲突的公开特征采样一个该物种的合法配置，
            # 再把已经公开的招式并入粒子，保持对手已知信息不被丢弃。
            relaxed_observation = PublicPokemonState(
                ident=observation.ident,
                species=observation.species,
                details=observation.details,
                hp=observation.hp,
                revealed_moves=(),
                revealed_item=None,
                revealed_ability=None,
                revealed_tera_type=None,
                status=observation.status,
                boosts=observation.boosts,
                volatile=observation.volatile,
                fainted=observation.fainted,
            )
            candidates = self.candidate_sets(relaxed_observation)
            if not candidates:
                raise ValueError(
                    f"no Gen 9 Random Battle set matches public observation: "
                    f"{observation.species}"
                )
        selected = _weighted_choice(
            rng,
            candidates,
            [candidate.pkmn_set.count for candidate in candidates],
        )
        sampled = SampledSet.from_predicted_set(observation.species, selected)
        if not observation.revealed_moves:
            return sampled

        known_moves = list(sampled.moves)
        known_move_ids = {_normalize_move(move) for move in known_moves}
        for move in observation.revealed_moves:
            if _normalize_move(move) not in known_move_ids:
                known_moves.append(move)
                known_move_ids.add(_normalize_move(move))
        return SampledSet(
            species=sampled.species,
            level=sampled.level,
            ability=sampled.ability,
            item=sampled.item,
            nature=sampled.nature,
            evs=sampled.evs,
            tera_type=sampled.tera_type,
            moves=tuple(known_moves),
            count=sampled.count,
        )

    def _sample_unrevealed(
        self,
        existing: list[Pokemon],
        excluded_species: set[str],
        rng: random.Random,
    ) -> tuple[Pokemon, SampledSet]:
        self.initialize()
        species_names = [
            species
            for species in RandomBattleTeamDatasets.pkmn_sets
            if species not in excluded_species
        ]
        if not species_names:
            raise ValueError("no unseen species remain in Gen 9 Random Battle dataset")

        species_weights = [
            max(
                1,
                sum(
                    candidate.pkmn_set.count
                    for candidate in RandomBattleTeamDatasets.pkmn_sets[species]
                ),
            )
            for species in species_names
        ]

        for _ in range(100):
            species = _weighted_choice(rng, species_names, species_weights)
            candidates = RandomBattleTeamDatasets.pkmn_sets[species]
            if not candidates:
                continue
            selected = _weighted_choice(
                rng,
                candidates,
                [candidate.pkmn_set.count for candidate in candidates],
            )
            pokemon = Pokemon(species, selected.pkmn_set.level or 100)
            sampled = SampledSet.from_predicted_set(species, selected)
            pokemon.types = list(pokemon.types)
            if not _team_constraint_violation(existing + [pokemon]):
                return pokemon, sampled

        # 约束采样失败时退化为唯一物种采样，保证 sampler 不因极端组合卡死。
        species = rng.choice(species_names)
        selected = rng.choice(RandomBattleTeamDatasets.pkmn_sets[species])
        pokemon = Pokemon(species, selected.pkmn_set.level or 100)
        return pokemon, SampledSet.from_predicted_set(species, selected)

    @staticmethod
    def _particle_id(team: list[SampledSet]) -> str:
        encoded = json.dumps(
            [pokemon.to_dict() for pokemon in team],
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    def sample(
        self,
        public: PublicBattleState,
        side: str,
        count: int,
    ) -> tuple[BeliefParticle, ...]:
        """按固定 seed 生成粒子；相同 public 和 seed 得到相同结果。"""
        if public.format_id != "gen9randombattle":
            raise ValueError(
                "Gen9RandomBattleBeliefSampler only supports gen9randombattle"
            )
        if count <= 0:
            return ()

        observations = list(public.sides.get(side, ()))
        if len(observations) > 6:
            raise ValueError("a Random Battle side cannot expose more than six Pokemon")

        particles: list[BeliefParticle] = []
        for particle_index in range(count):
            rng = random.Random(
                None if self.seed is None else self.seed + particle_index
            )
            team: list[SampledSet] = []
            existing_pokemon: list[Pokemon] = []
            known_species = set()

            for observation in observations:
                sampled = self._sample_revealed(observation, rng)
                team.append(sampled)
                pokemon = Pokemon(observation.species, sampled.level)
                existing_pokemon.append(pokemon)
                known_species.add(_species_key(observation))

            while len(team) < 6:
                pokemon, sampled = self._sample_unrevealed(
                    existing_pokemon, known_species, rng
                )
                team.append(sampled)
                existing_pokemon.append(pokemon)
                known_species.add(sampled.species)

            total_count = sum(max(1, pokemon.count) for pokemon in team)
            weight = sum(max(1, pokemon.count) for pokemon in team) / total_count
            payload = {
                "format": public.format_id,
                "side": side,
                "team": [
                    {
                        "slot": index + 1,
                        **pokemon.to_dict(),
                    }
                    for index, pokemon in enumerate(team)
                ],
            }
            particles.append(
                BeliefParticle(
                    particle_id=self._particle_id(team),
                    payload=payload,
                    weight=weight,
                    source="gen9-randombattle-public-sampler",
                )
            )

        normalized_weight = 1.0 / len(particles)
        return tuple(
            BeliefParticle(
                particle_id=particle.particle_id,
                payload=particle.payload,
                weight=normalized_weight,
                source=particle.source,
            )
            for particle in particles
        )

    @staticmethod
    def _particle_team_by_slot(
        particle: BeliefParticle,
    ) -> dict[int, dict[str, Any]]:
        team = particle.payload.get("team", ())
        return {int(pokemon["slot"]): pokemon for pokemon in team if "slot" in pokemon}

    @classmethod
    def _particle_matches_public(
        cls,
        public: PublicBattleState,
        particle: BeliefParticle,
        side: str,
    ) -> bool:
        particle_team = cls._particle_team_by_slot(particle)
        observations = public.sides.get(side, ())
        used_slots: set[int] = set()
        matched_by_actor: dict[str, Any] = {}

        for slot, observation in enumerate(observations, start=1):
            candidate = particle_team.get(slot)
            if (
                slot in used_slots
                or candidate is None
                or _normalize_move(candidate["species"]) != _normalize_move(observation.species)
            ):
                candidate = next(
                    (
                        value
                        for candidate_slot, value in particle_team.items()
                        if candidate_slot not in used_slots
                        and _normalize_move(value["species"]) == _normalize_move(observation.species)
                    ),
                    None,
                )
            if candidate is None:
                return False
            candidate_slot = next(
                (
                    candidate_slot
                    for candidate_slot, value in particle_team.items()
                    if value is candidate
                ),
                slot,
            )
            used_slots.add(candidate_slot)
            matched_by_actor[_normalize_actor(observation.ident)] = candidate

            candidate_moves = {
                _normalize_move(move) for move in candidate.get("moves", ())
            }
            observed_moves = {
                _normalize_move(move) for move in observation.revealed_moves
            }
            if not observed_moves.issubset(candidate_moves):
                return False

            if (
                observation.revealed_item is not None
                and candidate.get("item") != observation.revealed_item
            ):
                return False
            if (
                observation.revealed_ability is not None
                and candidate.get("ability") != observation.revealed_ability
            ):
                return False
            if (
                observation.revealed_tera_type is not None
                and candidate.get("teraType") != observation.revealed_tera_type
            ):
                return False

        for event in public.history:
            if event.get("type") != "move":
                continue
            actor = event.get("actor")
            move = event.get("move")
            if not actor or not move:
                continue
            for slot, observation in enumerate(observations, start=1):
                if _normalize_actor(observation.ident) != _normalize_actor(actor):
                    continue
                candidate = matched_by_actor.get(_normalize_actor(observation.ident))
                if candidate is None:
                    return False
                candidate_moves = {
                    _normalize_move(value) for value in candidate.get("moves", ())
                }
                if _normalize_move(move) not in candidate_moves:
                    return False

        return True

    @classmethod
    def filter_consistent_particles(
        cls,
        public: PublicBattleState,
        particles: tuple[BeliefParticle, ...] | list[BeliefParticle],
        side: str,
    ) -> tuple[BeliefParticle, ...]:
        """删除与公开信息冲突的粒子，并将保留粒子权重重新归一化。"""
        consistent = [
            particle
            for particle in particles
            if cls._particle_matches_public(public, particle, side)
        ]
        if not consistent:
            return ()

        total_weight = sum(particle.weight for particle in consistent)
        if total_weight <= 0:
            normalized_weight = 1.0 / len(consistent)
            return tuple(
                BeliefParticle(
                    particle_id=particle.particle_id,
                    payload=particle.payload,
                    weight=normalized_weight,
                    source=particle.source,
                )
                for particle in consistent
            )

        return tuple(
            BeliefParticle(
                particle_id=particle.particle_id,
                payload=particle.payload,
                weight=particle.weight / total_weight,
                source=particle.source,
            )
            for particle in consistent
        )
