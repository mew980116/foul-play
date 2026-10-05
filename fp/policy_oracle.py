"""Foul Play 离线 policy oracle。

该模块只负责调用当前 pinned poke-engine 的 MCTS，并把根节点结果转换成
可供 belief/shadow 层消费的动作分布。它不连接 PS，也不修改在线决策入口。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from poke_engine import monte_carlo_tree_search

from fp.battle import Battle
from fp.search.poke_engine_helpers import battle_to_poke_engine_state

Perspective = Literal["self", "opponent"]


@dataclass(frozen=True)
class PolicyActionStat:
    """一个根节点动作的 MCTS 统计。"""

    action: str
    visits: int
    total_score: float
    probability: float


@dataclass(frozen=True)
class PolicyOracleResult:
    """一次离线 policy oracle 调用的结果。"""

    perspective: Perspective
    search_time_ms: int
    threads: int
    total_visits: int
    actions: tuple[PolicyActionStat, ...]

    @property
    def best_action(self) -> str | None:
        if not self.actions:
            return None
        return max(
            self.actions, key=lambda action: (action.visits, action.total_score)
        ).action

    def distribution(self) -> dict[str, float]:
        return {action.action: action.probability for action in self.actions}


class FoulPlayPolicyOracle:
    """使用 Foul Play 当前 battle-to-engine 边界执行离线 MCTS。"""

    def evaluate(
        self,
        battle: Battle,
        perspective: Perspective = "self",
        search_time_ms: int = 100,
        threads: int = 1,
    ) -> PolicyOracleResult:
        if perspective not in {"self", "opponent"}:
            raise ValueError("perspective must be 'self' or 'opponent'")
        if search_time_ms <= 0:
            raise ValueError("search_time_ms must be positive")
        if threads <= 0:
            raise ValueError("threads must be positive")

        battle_copy = deepcopy(battle)
        state = battle_to_poke_engine_state(
            battle_copy,
            swap=perspective == "opponent",
        )
        result = monte_carlo_tree_search(state, search_time_ms, threads)
        root_actions = tuple(result.side_one)
        total_visits = int(result.total_visits)

        if total_visits <= 0:
            total_visits = sum(action.visits for action in root_actions)

        actions = tuple(
            PolicyActionStat(
                action=action.move_choice,
                visits=int(action.visits),
                total_score=float(action.total_score),
                probability=(
                    float(action.visits) / total_visits if total_visits > 0 else 0.0
                ),
            )
            for action in root_actions
        )
        return PolicyOracleResult(
            perspective=perspective,
            search_time_ms=search_time_ms,
            threads=threads,
            total_visits=total_visits,
            actions=actions,
        )
