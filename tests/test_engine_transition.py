from copy import deepcopy

import pytest

from fp.battle import Pokemon
from fp.belief_transition import BeliefTransitionModel
from tests.test_belief_transition import make_battle


def test_native_branches_preserve_mass_and_input():
    battle = make_battle()
    before = deepcopy(battle)
    outcomes = BeliefTransitionModel().apply_engine(battle, "thunderbolt", "waterpulse")
    assert sum(item.probability for item in outcomes) == pytest.approx(1, abs=1e-5)
    assert all(item.reversible for item in outcomes)
    assert battle.user.active.hp == before.user.active.hp
    assert battle.opponent.active.hp == before.opponent.active.hp


def test_native_status_move_and_hazard():
    battle = make_battle()
    battle.user.active.add_move("stealthrock")
    battle.opponent.active.add_move("swordsdance")
    outcomes = BeliefTransitionModel().apply_engine(battle, "stealthrock", "swordsdance")
    for outcome in outcomes:
        assert outcome.engine_state.side_two.side_conditions.stealth_rock == 1
        assert outcome.engine_state.side_two.attack_boost == 2


def test_native_tera_is_preserved_and_cannot_repeat():
    battle = make_battle()
    battle.user.active.tera_type = "electric"
    outcomes = BeliefTransitionModel().apply_engine(
        battle, "thunderbolt-tera", "waterpulse"
    )
    assert all(outcome.engine_state.side_one.pokemon[0].terastallized for outcome in outcomes)
    battle.user.reserve[0].terastallized = True
    battle.user.reserve[0].tera_type = "fire"
    with pytest.raises(ValueError, match="tera unavailable"):
        BeliefTransitionModel().apply_engine(battle, "thunderbolt-tera", "waterpulse")


def test_native_switch_keeps_dead_teammate_and_removes_old_boosts():
    battle = make_battle()
    battle.user.active.boosts["attack"] = 4
    dead = Pokemon("carbink", 80)
    dead.hp = 0
    battle.user.reserve.append(dead)
    outcomes = BeliefTransitionModel().apply_engine(battle, "switch charizard", "waterpulse")
    for outcome in outcomes:
        side = outcome.engine_state.side_one
        assert str(side.pokemon[int(side.active_index)].id).lower() == "charizard"
        assert side.attack_boost == 0
        assert str(side.pokemon[2].id).lower() == "carbink"
        assert side.pokemon[2].hp == 0


def test_native_hit_and_miss_not_equal_min_median_max():
    battle = make_battle()
    battle.user.active.add_move("zapcannon")
    battle.opponent.active.add_move("splash")
    outcomes = BeliefTransitionModel().apply_engine(battle, "zapcannon", "splash")
    full_hp = battle.opponent.active.hp
    miss_mass = sum(
        o.probability for o in outcomes if o.engine_state.side_two.pokemon[0].hp == full_hp
    )
    assert miss_mass == pytest.approx(0.5, abs=1e-5)


def test_native_ko_does_not_allow_knocked_out_pokemon_to_hit():
    battle = make_battle()
    battle.opponent.active.hp = 1
    battle.user.active.moves[0].current_pp = 10
    outcomes = BeliefTransitionModel().apply_engine(battle, "thunderbolt", "waterpulse")
    assert all(o.engine_state.side_two.pokemon[0].hp == 0 for o in outcomes)
    assert all(o.engine_state.side_one.pokemon[0].hp == battle.user.active.hp for o in outcomes)


def test_native_rejects_unknown_or_zero_pp_moves():
    battle = make_battle()
    battle.user.active.moves[0].current_pp = 0
    with pytest.raises(ValueError, match="PP"):
        BeliefTransitionModel().apply_engine(battle, "thunderbolt", "waterpulse")
    with pytest.raises(ValueError, match="missing"):
        BeliefTransitionModel().apply_engine(battle, "notamove", "waterpulse")


def test_native_rejects_encore_without_known_last_move():
    battle = make_battle()
    battle.user.active.volatile_statuses.append("encore")
    with pytest.raises(ValueError, match="(?i)encore"):
        BeliefTransitionModel().apply_engine(battle, "thunderbolt", "waterpulse")
