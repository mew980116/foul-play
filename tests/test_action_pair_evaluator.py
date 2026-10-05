from constants import BattleType
from fp.action_pair_evaluator import ActionPairEvaluator
from fp.battle import Battle, Pokemon


def make_battle() -> Battle:
    battle = Battle("action-pair-test")
    battle.battle_type = BattleType.RANDOM_BATTLE
    battle.generation = "gen9"
    battle.pokemon_format = "gen9randombattle"
    battle.user.active = Pokemon("pikachu", 100)
    battle.opponent.active = Pokemon("squirtle", 100)
    battle.user.active.add_move("thunderbolt")
    battle.user.active.add_move("swordsdance")
    battle.opponent.active.add_move("waterpulse")
    battle.opponent.active.add_move("protect")
    return battle


def test_action_pair_uses_damage_rolls():
    value = ActionPairEvaluator().evaluate(
        make_battle(),
        "thunderbolt",
        "waterpulse",
    )

    assert value > 0


def test_action_pair_marks_setup_risky_against_attack():
    value = ActionPairEvaluator().evaluate(
        make_battle(),
        "swordsdance",
        "waterpulse",
    )

    assert value < 0


def test_action_pair_handles_switches_without_engine_failure():
    evaluator = ActionPairEvaluator()
    battle = make_battle()

    assert evaluator.evaluate(battle, "switch charizard", "waterpulse") < 0
    assert evaluator.evaluate(battle, "swordsdance", "switch venusaur") > 0
