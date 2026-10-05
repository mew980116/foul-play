"""One-step action-pair rollout over the auditable transition boundary."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from fp.battle import Battle
from fp.belief_transition import BeliefTransitionModel
from fp.belief_sampler import Gen9RandomBattleBeliefSampler
from fp.dual_view import BeliefParticle
from fp.policy_oracle import FoulPlayPolicyOracle, PolicyOracleResult


def _top_actions(
    distribution: Mapping[str, float],
    limit: int,
) -> tuple[tuple[str, float], ...]:
    values = [
        (str(action), float(probability))
        for action, probability in distribution.items()
        if math.isfinite(float(probability)) and float(probability) > 0
    ]
    values.sort(key=lambda item: (-item[1], item[0]))
    return tuple(values[: max(1, limit)])


def _policy_value(result: PolicyOracleResult) -> float:
    if result.total_visits <= 0:
        return 0.0
    value = sum(action.total_score for action in result.actions)
    return max(-1.0, min(1.0, value / result.total_visits))


@dataclass(frozen=True)
class RolloutPairValue:
    """一个我方动作和对手动作组合的 rollout 结果。"""

    our_action: str
    opponent_action: str
    opponent_probability: float
    expected_value: float
    outcome_values: tuple[float, ...] = ()
    transition_count: int = 0
    valid_transition_count: int = 0
    next_oracle_calls: int = 0
    belief_input_count: int = 0
    belief_output_count: int = 0
    fallback_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class RolloutEvaluation:
    """一个根节点动作的可审计 rollout 评估。"""

    action: str
    expected_value: float
    pair_values: tuple[RolloutPairValue, ...]
    used_rollout: bool
    fallback_reason: str | None = None
    transition_count: int = 0
    next_oracle_calls: int = 0
    belief_input_count: int = 0
    belief_output_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "expectedValue": self.expected_value,
            "usedRollout": self.used_rollout,
            "fallbackReason": self.fallback_reason,
            "transitionCount": self.transition_count,
            "nextOracleCalls": self.next_oracle_calls,
            "beliefInputCount": self.belief_input_count,
            "beliefOutputCount": self.belief_output_count,
            "pairValues": [
                {
                    "ourAction": pair.our_action,
                    "opponentAction": pair.opponent_action,
                    "opponentProbability": pair.opponent_probability,
                    "expectedValue": pair.expected_value,
                    "outcomeValues": list(pair.outcome_values),
                    "transitionCount": pair.transition_count,
                    "validTransitionCount": pair.valid_transition_count,
                    "nextOracleCalls": pair.next_oracle_calls,
                    "beliefInputCount": pair.belief_input_count,
                    "beliefOutputCount": pair.belief_output_count,
                    "fallbackReasons": list(pair.fallback_reasons),
                }
                for pair in self.pair_values
            ],
        }


@dataclass(frozen=True)
class OneStepRolloutResult:
    """一组候选动作的 rollout 结果。"""

    evaluations: tuple[RolloutEvaluation, ...]
    opponent_actions: tuple[tuple[str, float], ...]
    transition_count: int
    valid_transition_count: int
    next_oracle_calls: int
    belief_input_count: int = 0
    belief_output_count: int = 0
    fallback_reasons: Mapping[str, int] = field(default_factory=dict)

    @property
    def best_action(self) -> str | None:
        if not self.evaluations:
            return None
        return max(
            self.evaluations,
            key=lambda item: (item.expected_value, item.action),
        ).action

    def to_dict(self) -> dict[str, Any]:
        return {
            "bestAction": self.best_action,
            "opponentActions": [
                {"action": action, "probability": probability}
                for action, probability in self.opponent_actions
            ],
            "transitionCount": self.transition_count,
            "validTransitionCount": self.valid_transition_count,
            "nextOracleCalls": self.next_oracle_calls,
            "beliefInputCount": self.belief_input_count,
            "beliefOutputCount": self.belief_output_count,
            "fallbackReasons": dict(self.fallback_reasons),
            "evaluations": [evaluation.to_dict() for evaluation in self.evaluations],
        }


class OneStepRolloutEvaluator:
    """用下一节点 Foul Play oracle 评估当前根节点候选动作。

    当前版本使用调用方给定的完整 Battle 假设世界，不在 rollout 内重新
    采样或重建 belief。这样可以先验证 transition 和下一节点 oracle 的
    接口，belief 更新由后续版本接入。
    """

    def __init__(
        self,
        *,
        transition_model: BeliefTransitionModel | None = None,
        oracle: FoulPlayPolicyOracle | None = None,
        belief_sampler: Gen9RandomBattleBeliefSampler | None = None,
    ):
        self.transition_model = transition_model or BeliefTransitionModel()
        self.oracle = oracle or FoulPlayPolicyOracle()
        self.belief_sampler = belief_sampler or Gen9RandomBattleBeliefSampler()

    def evaluate(
        self,
        battle: Battle,
        self_policy: Mapping[str, float],
        opponent_policy: Mapping[str, float],
        *,
        self_top_k: int = 3,
        opponent_top_k: int = 3,
        next_search_time_ms: int = 10,
        belief_particles: Sequence[BeliefParticle] | None = None,
        belief_side: str = "p1",
    ) -> OneStepRolloutResult:
        if next_search_time_ms <= 0:
            raise ValueError("next_search_time_ms must be positive")
        our_actions = _top_actions(self_policy, self_top_k)
        opponent_actions = _top_actions(opponent_policy, opponent_top_k)
        if not our_actions:
            raise ValueError("self policy must contain positive actions")
        if not opponent_actions:
            raise ValueError("opponent policy must contain positive actions")

        total_transitions = 0
        valid_transitions = 0
        next_oracle_calls = 0
        belief_input_count = 0
        belief_output_count = 0
        fallback_reasons: dict[str, int] = {}
        evaluations: list[RolloutEvaluation] = []

        for our_action, _self_probability in our_actions:
            pair_values: list[RolloutPairValue] = []
            action_used = False
            action_fallback: str | None = None
            action_transition_count = 0
            action_oracle_calls = 0
            action_belief_input_count = 0
            action_belief_output_count = 0
            for opponent_action, opponent_probability in opponent_actions:
                outcomes = self.transition_model.apply(
                    battle,
                    our_action,
                    opponent_action,
                )
                action_transition_count += len(outcomes)
                total_transitions += len(outcomes)
                values: list[float] = []
                pair_fallbacks: list[str] = []
                pair_oracle_calls = 0
                valid_count = 0
                for outcome in outcomes:
                    if not outcome.valid or outcome.state is None:
                        reason = outcome.fallback_reason or "invalid-transition"
                        pair_fallbacks.append(reason)
                        fallback_reasons[reason] = fallback_reasons.get(reason, 0) + 1
                        continue
                    valid_count += 1
                    valid_transitions += 1
                    if belief_particles is not None:
                        observation = outcome.public_observation
                        if observation is None:
                            reason = "missing-public-observation"
                            pair_fallbacks.append(reason)
                            fallback_reasons[reason] = (
                                fallback_reasons.get(reason, 0) + 1
                            )
                            continue
                        action_belief_input_count += len(belief_particles)
                        belief_input_count += len(belief_particles)
                        filtered = self.belief_sampler.filter_consistent_particles(
                            observation.to_public_battle_state(),
                            tuple(belief_particles),
                            belief_side,
                        )
                        action_belief_output_count += len(filtered)
                        belief_output_count += len(filtered)
                        if not filtered:
                            reason = "belief-collapse"
                            pair_fallbacks.append(reason)
                            fallback_reasons[reason] = (
                                fallback_reasons.get(reason, 0) + 1
                            )
                            continue
                    next_result = self.oracle.evaluate(
                        outcome.state,
                        perspective="self",
                        search_time_ms=next_search_time_ms,
                    )
                    pair_oracle_calls += 1
                    next_oracle_calls += 1
                    action_oracle_calls += 1
                    values.append(_policy_value(next_result))
                if values:
                    action_used = True
                elif pair_fallbacks and action_fallback is None:
                    action_fallback = pair_fallbacks[0]
                pair_value = (
                    sum(values) / len(values)
                    if values
                    else 0.0
                )
                pair_values.append(
                    RolloutPairValue(
                        our_action=our_action,
                        opponent_action=opponent_action,
                        opponent_probability=opponent_probability,
                        expected_value=pair_value,
                        outcome_values=tuple(values),
                        transition_count=len(outcomes),
                        valid_transition_count=valid_count,
                        next_oracle_calls=pair_oracle_calls,
                        belief_input_count=action_belief_input_count,
                        belief_output_count=action_belief_output_count,
                        fallback_reasons=tuple(pair_fallbacks),
                    )
                )
            expected_value = sum(
                pair.opponent_probability * pair.expected_value
                for pair in pair_values
            )
            evaluations.append(
                RolloutEvaluation(
                    action=our_action,
                    expected_value=expected_value,
                    pair_values=tuple(pair_values),
                    used_rollout=action_used,
                    fallback_reason=None if action_used else action_fallback,
                    transition_count=action_transition_count,
                    next_oracle_calls=action_oracle_calls,
                    belief_input_count=action_belief_input_count,
                    belief_output_count=action_belief_output_count,
                )
            )

        return OneStepRolloutResult(
            evaluations=tuple(evaluations),
            opponent_actions=opponent_actions,
            transition_count=total_transitions,
            valid_transition_count=valid_transitions,
            next_oracle_calls=next_oracle_calls,
            belief_input_count=belief_input_count,
            belief_output_count=belief_output_count,
            fallback_reasons=fallback_reasons,
        )
