from constants import BattleType
from fp.battle import Battle, Pokemon
from fp.policy_oracle import FoulPlayPolicyOracle


def make_battle() -> Battle:
    battle = Battle("offline-oracle-test")
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


def test_oracle_returns_root_visit_distribution():
    result = FoulPlayPolicyOracle().evaluate(
        make_battle(),
        perspective="self",
        search_time_ms=5,
    )

    assert result.perspective == "self"
    assert result.total_visits > 0
    assert result.actions
    assert result.best_action in result.distribution()
    assert abs(sum(result.distribution().values()) - 1.0) < 1e-9
    assert all(action.visits >= 0 for action in result.actions)


def test_oracle_supports_opponent_perspective_without_mutating_battle():
    battle = make_battle()
    before = battle.user.active.name, battle.opponent.active.name

    result = FoulPlayPolicyOracle().evaluate(
        battle,
        perspective="opponent",
        search_time_ms=5,
    )

    assert result.perspective == "opponent"
    assert result.actions
    assert (battle.user.active.name, battle.opponent.active.name) == before


def test_oracle_validates_arguments():
    oracle = FoulPlayPolicyOracle()
    battle = make_battle()

    for kwargs in (
        {"perspective": "invalid"},
        {"search_time_ms": 0},
        {"threads": 0},
    ):
        try:
            oracle.evaluate(battle, **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError("expected oracle argument validation error")
