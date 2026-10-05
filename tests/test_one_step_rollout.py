from fp.battle import Battle, Pokemon
from fp.dual_view import BeliefParticle
from fp.one_step_rollout import OneStepRolloutEvaluator
from fp.policy_oracle import PolicyActionStat, PolicyOracleResult
from fp.public_observation import build_public_observation


def make_battle():
    battle = Battle("one-step-rollout-test")
    battle.generation = "gen9"
    battle.pokemon_format = "gen9randombattle"
    battle.user.active = Pokemon("pikachu", 100)
    battle.opponent.active = Pokemon("squirtle", 100)
    battle.user.active.add_move("thunderbolt")
    battle.opponent.active.add_move("waterpulse")
    return battle


class FakeOracle:
    def evaluate(self, battle, perspective="self", search_time_ms=10):
        del battle, perspective, search_time_ms
        return PolicyOracleResult(
            perspective="self",
            search_time_ms=10,
            threads=1,
            total_visits=10,
            actions=(
                PolicyActionStat("thunderbolt", 6, 4.0, 0.6),
                PolicyActionStat("waterpulse", 4, -2.0, 0.4),
            ),
        )


class FakeTransition:
    def apply(self, battle, our_action, opponent_action):
        del battle, opponent_action
        return (
            type(
                "Outcome",
                (),
                {
                    "valid": our_action == "thunderbolt",
                    "state": make_battle() if our_action == "thunderbolt" else None,
                    "public_observation": (
                        build_public_observation(make_battle())
                        if our_action == "thunderbolt"
                        else None
                    ),
                    "fallback_reason": (
                        None
                        if our_action == "thunderbolt"
                        else "unsupported-test"
                    ),
                },
            )(),
        )


def test_one_step_rollout_uses_next_oracle_and_records_diagnostics():
    result = OneStepRolloutEvaluator(
        transition_model=FakeTransition(),
        oracle=FakeOracle(),
    ).evaluate(
        make_battle(),
        {"thunderbolt": 0.7, "protect": 0.3},
        {"waterpulse": 0.8, "switch squirtle": 0.2},
        self_top_k=2,
        opponent_top_k=2,
        next_search_time_ms=5,
    )

    assert result.best_action == "thunderbolt"
    assert result.transition_count == 4
    assert result.valid_transition_count == 2
    assert result.next_oracle_calls == 2
    assert result.fallback_reasons == {"unsupported-test": 2}
    assert result.evaluations[0].used_rollout is True
    assert result.evaluations[1].used_rollout is False


def test_one_step_rollout_can_filter_belief_after_public_observation():
    result = OneStepRolloutEvaluator(
        transition_model=FakeTransition(),
        oracle=FakeOracle(),
    ).evaluate(
        make_battle(),
        {"thunderbolt": 1.0},
        {"waterpulse": 1.0},
        belief_particles=[
            BeliefParticle(
                "particle",
                {
                    "team": [
                        {
                            "slot": 1,
                            "species": "pikachu",
                            "moves": ["thunderbolt"],
                        }
                    ]
                },
            )
        ],
        belief_side="p1",
    )

    assert result.belief_input_count == 1
    assert result.belief_output_count == 1
    assert result.next_oracle_calls == 1
