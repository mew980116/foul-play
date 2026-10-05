from constants import BattleType
from fp.battle import Battle, Pokemon
from fp.belief_sampler import Gen9RandomBattleBeliefSampler
from fp.dual_view import PublicBattleState, PublicPokemonState
from fp.shadow_prediction import FoulPlayShadowPredictor


def make_battle() -> Battle:
    battle = Battle("shadow-test")
    battle.battle_type = BattleType.RANDOM_BATTLE
    battle.generation = "gen9"
    battle.pokemon_format = "gen9randombattle"
    battle.user.active = Pokemon("pikachu", 100)
    battle.opponent.active = Pokemon("squirtle", 100)
    battle.user.reserve = [Pokemon("charizard", 100)]
    battle.opponent.reserve = [Pokemon("venusaur", 100)]
    for move in ("thunderbolt", "quickattack", "protect", "nastyplot"):
        battle.user.active.add_move(move)
    for move in ("waterpulse", "icebeam", "protect", "tackle"):
        battle.opponent.active.add_move(move)
    return battle


def make_particles():
    public = PublicBattleState(
        turn=1,
        format_id="gen9randombattle",
        sides={
            "p1": (
                PublicPokemonState(
                    ident="p1: Pikachu",
                    species="pikachu",
                    details="Pikachu, L100",
                ),
            ),
            "p2": (),
        },
    )
    return Gen9RandomBattleBeliefSampler(seed=3).sample(public, "p1", 2)


def test_shadow_prediction_aggregates_opponent_policy():
    prediction = FoulPlayShadowPredictor().predict(
        make_battle(),
        make_particles(),
        search_time_ms=5,
    )

    assert len(prediction.particle_results) == 2
    assert prediction.particle_weights == (0.5, 0.5)
    assert prediction.action_probabilities
    assert abs(sum(prediction.action_probabilities.values()) - 1.0) < 1e-9
    assert prediction.best_action in prediction.action_probabilities


def test_shadow_prediction_requires_particles():
    try:
        FoulPlayShadowPredictor().predict(make_battle(), [], search_time_ms=5)
    except ValueError as error:
        assert "particle" in str(error)
    else:
        raise AssertionError("expected empty particle validation error")
