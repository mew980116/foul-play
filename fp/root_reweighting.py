"""基于对手动作分布重加权我方根节点动作。"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from fp.policy_oracle import PolicyOracleResult
from fp.shadow_prediction import ShadowPrediction

ActionValueFn = Callable[[str, str], float]


@dataclass(frozen=True)
class RootActionScore:
    """一个候选动作在对手动作分布下的根节点评分。"""

    action: str
    prior_probability: float
    expected_value: float
    reweighted_probability: float


@dataclass(frozen=True)
class RootActionReweightingResult:
    """根节点动作重加权结果。"""

    actions: tuple[RootActionScore, ...]
    opponent_action_probabilities: dict[str, float]
    strength: float

    @property
    def best_action(self) -> str | None:
        if not self.actions:
            return None
        return max(
            self.actions,
            key=lambda action: (
                action.reweighted_probability,
                action.expected_value,
            ),
        ).action


class RootActionReweighter:
    """使用 opponent action distribution 调整我方 root policy。"""

    def evaluate(
        self,
        self_policy: PolicyOracleResult,
        shadow_prediction: ShadowPrediction,
        action_value: ActionValueFn,
        strength: float = 1.0,
    ) -> RootActionReweightingResult:
        """按对手动作分布计算候选动作的期望价值并重新归一化。"""
        if not math.isfinite(strength) or strength < 0:
            raise ValueError("strength must be a finite non-negative number")
        if not self_policy.actions:
            raise ValueError("self policy must contain at least one action")

        opponent_distribution = shadow_prediction.action_probabilities
        if not opponent_distribution:
            raise ValueError("shadow prediction must contain opponent actions")

        expected_values: dict[str, float] = {}
        for action in self_policy.actions:
            expected_value = sum(
                probability
                * self._checked_value(
                    action_value,
                    action.action,
                    opponent_action,
                )
                for opponent_action, probability in opponent_distribution.items()
            )
            expected_values[action.action] = expected_value

        log_scores = {
            action.action: (
                math.log(action.probability) + strength * expected_values[action.action]
            )
            for action in self_policy.actions
            if action.probability > 0
        }
        if not log_scores:
            raise ValueError("self policy must contain a positive action probability")

        max_log_score = max(log_scores.values())
        weights = {
            action: math.exp(log_score - max_log_score)
            for action, log_score in log_scores.items()
        }
        total_weight = sum(weights.values())
        scores = tuple(
            RootActionScore(
                action=action.action,
                prior_probability=action.probability,
                expected_value=expected_values[action.action],
                reweighted_probability=(weights.get(action.action, 0.0) / total_weight),
            )
            for action in self_policy.actions
        )
        return RootActionReweightingResult(
            actions=scores,
            opponent_action_probabilities=dict(opponent_distribution),
            strength=strength,
        )

    @staticmethod
    def _checked_value(
        action_value: ActionValueFn,
        our_action: str,
        opponent_action: str,
    ) -> float:
        value = float(action_value(our_action, opponent_action))
        if not math.isfinite(value):
            raise ValueError(
                "action_value must return finite values: "
                f"{our_action!r} vs {opponent_action!r}"
            )
        return value
