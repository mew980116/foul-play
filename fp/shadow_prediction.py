"""基于 Foul Play policy oracle 的离线对手动作预测。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from fp.battle import Battle, Pokemon
from fp.dual_view import BeliefParticle
from fp.policy_oracle import FoulPlayPolicyOracle, PolicyOracleResult


def _particle_pokemon(candidate: dict[str, Any]) -> Pokemon:
    pokemon = Pokemon(candidate["species"], int(candidate.get("level", 100)))
    pokemon.ability = candidate.get("ability")
    pokemon.item = candidate.get("item", pokemon.item)
    pokemon.tera_type = candidate.get("teraType")
    for move in candidate.get("moves", ()):
        pokemon.add_move(move)
    pokemon.set_spread(
        candidate.get("nature", "serious"),
        ",".join(str(value) for value in candidate.get("evs", (85,) * 6)),
    )
    return pokemon


def _replace_user_with_particle(
    battle: Battle,
    particle: BeliefParticle,
) -> Battle:
    """把我方真实队伍替换为对手视角下的一个假设队伍。"""
    result = deepcopy(battle)
    candidate_team = sorted(
        particle.payload.get("team", ()),
        key=lambda candidate: int(candidate.get("slot", 0)),
    )
    if not candidate_team:
        raise ValueError("belief particle has no team")

    observed_team = [result.user.active] + list(result.user.reserve)
    generated_team = [_particle_pokemon(candidate) for candidate in candidate_team]

    for index, generated in enumerate(generated_team):
        if index < len(observed_team):
            observed = observed_team[index]
            if observed is not None and observed.max_hp > 0:
                generated.hp = round(generated.max_hp * observed.hp / observed.max_hp)
                generated.status = observed.status
                generated.boosts.update(observed.boosts)
                generated.volatile_statuses = list(observed.volatile_statuses)
            generated.nickname = observed.nickname if observed is not None else None

    result.user.active = generated_team[0]
    result.user.reserve = generated_team[1:]
    return result


@dataclass(frozen=True)
class ShadowPrediction:
    """多个 opponent-perspective oracle 结果聚合后的动作分布。"""

    action_probabilities: dict[str, float]
    particle_results: tuple[PolicyOracleResult, ...]
    particle_weights: tuple[float, ...]

    @property
    def best_action(self) -> str | None:
        if not self.action_probabilities:
            return None
        return max(self.action_probabilities.items(), key=lambda item: item[1])[0]


class FoulPlayShadowPredictor:
    """用对手视角的 Foul Play oracle 预测其下一步动作。"""

    def __init__(self, oracle: FoulPlayPolicyOracle | None = None):
        self.oracle = oracle or FoulPlayPolicyOracle()

    def predict(
        self,
        battle: Battle,
        particles: tuple[BeliefParticle, ...] | list[BeliefParticle],
        search_time_ms: int = 100,
        threads: int = 1,
    ) -> ShadowPrediction:
        if not particles:
            raise ValueError("at least one belief particle is required")

        total_weight = sum(particle.weight for particle in particles)
        if total_weight <= 0:
            normalized_weights = [1.0 / len(particles)] * len(particles)
        else:
            normalized_weights = [
                particle.weight / total_weight for particle in particles
            ]

        particle_results: list[PolicyOracleResult] = []
        action_probabilities: dict[str, float] = {}
        for particle, particle_weight in zip(particles, normalized_weights):
            assumed_battle = _replace_user_with_particle(battle, particle)
            result = self.oracle.evaluate(
                assumed_battle,
                perspective="opponent",
                search_time_ms=search_time_ms,
                threads=threads,
            )
            particle_results.append(result)
            for action, probability in result.distribution().items():
                action_probabilities[action] = action_probabilities.get(action, 0.0) + (
                    particle_weight * probability
                )

        total_probability = sum(action_probabilities.values())
        if total_probability > 0:
            action_probabilities = {
                action: probability / total_probability
                for action, probability in action_probabilities.items()
            }

        return ShadowPrediction(
            action_probabilities=action_probabilities,
            particle_results=tuple(particle_results),
            particle_weights=tuple(normalized_weights),
        )
