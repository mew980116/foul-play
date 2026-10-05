from fp.policy_oracle import PolicyActionStat, PolicyOracleResult
from fp.root_reweighting import RootActionReweighter
from fp.shadow_prediction import ShadowPrediction


def make_self_policy() -> PolicyOracleResult:
    return PolicyOracleResult(
        perspective="self",
        search_time_ms=5,
        threads=1,
        total_visits=10,
        actions=(
            PolicyActionStat("move alpha", 6, 0.0, 0.6),
            PolicyActionStat("move beta", 4, 0.0, 0.4),
        ),
    )


def make_shadow_prediction() -> ShadowPrediction:
    return ShadowPrediction(
        action_probabilities={"move attack": 0.75, "switch 2": 0.25},
        particle_results=(),
        particle_weights=(),
    )


def test_root_action_reweighting_uses_opponent_distribution():
    result = RootActionReweighter().evaluate(
        make_self_policy(),
        make_shadow_prediction(),
        lambda our_action, opponent_action: {
            ("move alpha", "move attack"): 0.0,
            ("move alpha", "switch 2"): 1.0,
            ("move beta", "move attack"): 1.0,
            ("move beta", "switch 2"): 0.0,
        }[(our_action, opponent_action)],
    )

    assert result.best_action == "move beta"
    assert result.actions[0].expected_value == 0.25
    assert result.actions[1].expected_value == 0.75
    assert abs(sum(item.reweighted_probability for item in result.actions) - 1.0) < 1e-9


def test_root_action_reweighting_can_disable_value_adjustment():
    result = RootActionReweighter().evaluate(
        make_self_policy(),
        make_shadow_prediction(),
        lambda _our_action, _opponent_action: 100.0,
        strength=0.0,
    )

    assert result.best_action == "move alpha"
    assert [item.reweighted_probability for item in result.actions] == [0.6, 0.4]


def test_root_action_reweighting_validates_inputs():
    reweighter = RootActionReweighter()
    policy = make_self_policy()
    shadow = make_shadow_prediction()

    for kwargs in ({"strength": -1.0}, {"strength": float("nan")}):
        try:
            reweighter.evaluate(
                policy,
                shadow,
                lambda _our_action, _opponent_action: 0.0,
                **kwargs,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("expected strength validation error")

    try:
        reweighter.evaluate(
            policy,
            shadow,
            lambda _our_action, _opponent_action: float("inf"),
        )
    except ValueError:
        pass
    else:
        raise AssertionError("expected action value validation error")
