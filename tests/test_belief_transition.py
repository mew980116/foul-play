from fp.battle import Battle, Pokemon
from fp.belief_transition import BeliefTransitionModel
import pytest


def make_battle():
    battle = Battle("belief-transition-test")
    battle.battle_type = "random_battle"
    battle.generation = "gen9"
    battle.pokemon_format = "gen9randombattle"
    battle.turn = 3
    battle.user.active = Pokemon("pikachu", 100)
    battle.opponent.active = Pokemon("squirtle", 100)
    battle.user.active.add_move("thunderbolt")
    battle.opponent.active.add_move("waterpulse")
    reserve = Pokemon("charizard", 100)
    reserve.add_move("flamethrower")
    battle.user.reserve.append(reserve)
    return battle


def test_attack_transition_has_valid_after_states():
    result = BeliefTransitionModel(allow_approximate_preview=True).apply(
        make_battle(),
        "move thunderbolt",
        "move waterpulse",
    )
    assert result
    assert all(outcome.valid for outcome in result)
    assert all(
        outcome.state.user.active.hp <= outcome.state.user.active.max_hp
        for outcome in result
    )
    assert all(outcome.public_observation is not None for outcome in result)
    assert all(
        outcome.public_observation.to_dict()["sides"]["p1"][0]["moves"]
        == ["thunderbolt"]
        for outcome in result
    )


def test_switch_transition_does_not_leak_reserve_configuration():
    result = BeliefTransitionModel(allow_approximate_preview=True).apply(
        make_battle(),
        "switch charizard",
        "move waterpulse",
    )
    assert result
    assert all(outcome.valid for outcome in result)
    observation = result[0].public_observation.to_dict()
    assert observation["sides"]["p1"][0]["name"] == "charizard"
    assert observation["sides"]["p1"][0]["moves"] == []


def test_unsupported_status_move_has_explicit_fallback():
    result = BeliefTransitionModel(allow_approximate_preview=True).apply(
        make_battle(),
        "move thunderwave",
        "move waterpulse",
    )
    assert len(result) == 1
    assert result[0].valid is False
    assert result[0].fallback_reason == "status-or-utility-move-unsupported"


def test_damage_roll_outcomes_use_cartesian_product(monkeypatch):
    monkeypatch.setattr(
        "fp.belief_transition.poke_engine_get_damage_rolls",
        lambda *_args: ((10, 20, 30), (1, 2, 3)),
    )
    result = BeliefTransitionModel(allow_approximate_preview=True).apply(
        make_battle(),
        "move thunderbolt",
        "move waterpulse",
    )
    assert len(result) == 9
    assert sum(outcome.outcome_probability for outcome in result) == pytest.approx(1.0)


def test_lethal_damage_marks_the_opponent_for_forced_switch(monkeypatch):
    battle = make_battle()
    monkeypatch.setattr(
        "fp.belief_transition.poke_engine_get_damage_rolls",
        lambda *_args: ((9999,), (1,)),
    )
    result = BeliefTransitionModel(allow_approximate_preview=True).apply(
        battle,
        "move thunderbolt",
        "move waterpulse",
    )
    assert result[0].valid
    assert result[0].forced_switch_sides == ("p2",)


def test_tera_transition_records_public_tera_state(monkeypatch):
    battle = make_battle()
    battle.user.active.can_terastallize = True
    battle.user.active.tera_type = "electric"
    monkeypatch.setattr(
        "fp.belief_transition.poke_engine_get_damage_rolls",
        lambda *_args: ((10,), (1,)),
    )
    result = BeliefTransitionModel(allow_approximate_preview=True).apply(
        battle,
        "move thunderbolt-tera",
        "move waterpulse",
    )
    assert result[0].valid
    assert result[0].tera_used_sides == ("p1",)
    assert result[0].public_observation.to_dict()["sides"]["p1"][0]["teraType"] == "electric"


def test_zero_hp_reserve_is_normalized_before_invariant_check():
    battle = make_battle()
    battle.user.reserve[0].hp = 0
    battle.user.reserve[0].fainted = False
    result = BeliefTransitionModel(allow_approximate_preview=True).apply(
        battle,
        "move thunderbolt",
        "move waterpulse",
    )
    assert all(outcome.valid for outcome in result)


def test_no_move_is_an_explicit_fallback():
    result = BeliefTransitionModel(allow_approximate_preview=True).apply(
        make_battle(),
        "switch charizard",
        "No Move",
    )
    assert result[0].valid is False
    assert result[0].fallback_reason == "no-opponent-action"


def test_approximate_preview_is_not_a_production_transition():
    outcome = BeliefTransitionModel().apply(
        make_battle(), "thunderbolt", "waterpulse"
    )[0]
    assert not outcome.valid
    assert outcome.state is None
    assert outcome.fallback_reason == "approximate-transition-not-rollout-safe"
