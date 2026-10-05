from fp.belief_sampler import Gen9RandomBattleBeliefSampler
import constants
from fp.live_reference import (
    LiveDualViewPolicy,
    LiveReferencePolicy,
    _mix_opponent_policy,
    _public_observed_side,
    _public_state,
    battle_from_live_state,
)
from fp.dual_view import BeliefParticle
from fp.policy_oracle import FoulPlayPolicyOracle
import pytest


def make_state():
    return {
        "battleId": "battle-live-test",
        "turn": 1,
        "format": "gen9randombattle",
        "request": {
            "rqid": 1,
            "side": {
                "id": "p1",
                "pokemon": [
                    {
                        "ident": "p1: Pikachu",
                        "details": "Pikachu, L100",
                        "condition": "100/100",
                        "active": True,
                        "item": "lightball",
                        "ability": "static",
                        "stats": {
                            "atk": 100,
                            "def": 80,
                            "spa": 120,
                            "spd": 90,
                            "spe": 150,
                        },
                        "moves": ["thunderbolt", "protect", "quickattack", "nastyplot"],
                    },
                    {
                        "ident": "p1: Charizard",
                        "details": "Charizard, L100",
                        "condition": "100/100",
                        "active": False,
                        "item": "leftovers",
                        "ability": "blaze",
                        "stats": {
                            "atk": 100,
                            "def": 80,
                            "spa": 150,
                            "spd": 100,
                            "spe": 120,
                        },
                        "moves": ["flamethrower", "roost", "protect", "airslash"],
                    },
                ],
            },
            "active": [
                {
                    "moves": [
                        {"id": "thunderbolt", "move": "Thunderbolt", "disabled": False},
                        {"id": "protect", "move": "Protect", "disabled": False},
                        {
                            "id": "quickattack",
                            "move": "Quick Attack",
                            "disabled": False,
                        },
                        {"id": "nastyplot", "move": "Nasty Plot", "disabled": False},
                    ]
                }
            ],
        },
        "me": {"name": "Pikachu", "hp": 100, "maxHp": 100},
        "opp": {"name": "Carbink", "hp": 100, "maxHp": 100},
        "oppTeam": [
            {
                "slot": 1,
                "name": "Carbink",
                "details": "Carbink, L90",
                "hp": 100,
                "maxHp": 100,
                "moves": [{"name": "moonblast"}],
            }
        ],
        "myTeam": [],
        "weather": None,
        "terrain": None,
    }


def test_engine_boundary_rejects_encore_without_known_last_move():
    from fp.search.poke_engine_helpers import battle_to_poke_engine_state

    state = make_state()
    particle = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )[0]
    for move in ("", "switch pikachu", "surf"):
        battle = battle_from_live_state(state, particle)
        battle.user.active.volatile_statuses.append("encore")
        battle.user.last_used_move = battle.user.last_used_move._replace(move=move)
        with pytest.raises(ValueError, match="Encore requires a known last move"):
            battle_to_poke_engine_state(battle)


def test_engine_boundary_preserves_valid_encore():
    from fp.search.poke_engine_helpers import battle_to_poke_engine_state

    state = make_state()
    particle = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )[0]
    battle = battle_from_live_state(state, particle)
    battle.user.active.volatile_statuses.append("encore")
    battle.user.last_used_move = battle.user.last_used_move._replace(
        move="thunderbolt"
    )
    engine = battle_to_poke_engine_state(battle)
    assert engine.side_one.last_used_move == "move:0"
    assert "encore" in engine.side_one.volatile_statuses


def test_request_recharge_overrides_user_mustrecharge_volatile():
    from fp.search.poke_engine_helpers import battle_to_poke_engine_state

    state = make_state()
    state["request"]["active"][0]["moves"] = [
        {"id": "recharge", "move": "Recharge", "disabled": False, "pp": 39}
    ]
    state["request"]["active"][0]["trapped"] = True
    state["me"]["volatile"] = {"active": ["-mustrecharge"]}
    particle = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )[0]

    battle = battle_from_live_state(state, particle)
    engine = battle_to_poke_engine_state(battle)

    assert "mustrecharge" not in engine.side_one.volatile_statuses
    result = FoulPlayPolicyOracle().evaluate(battle, search_time_ms=5)
    assert result.best_action == "recharge"
    assert result.actions[0].action == "recharge"


def test_live_reference_returns_request_forced_recharge_without_search():
    state = make_state()
    state["actions"] = [{"type": "move", "slot": 1, "id": "recharge"}]
    result = LiveReferencePolicy(
        particle_count=0,
        search_time_ms=100,
        mode="upstream-compatible",
    ).evaluate(state)

    assert result.best_action == "recharge"
    assert result.raw_best_action == "recharge"
    assert result.actions == {"recharge": 1.0}
    assert result.reference_mode == "upstream-compatible-request-forced"
    assert result.search_time_ms == 0


def test_request_recharge_turn_keeps_only_original_move_usable():
    state = make_state()
    state["request"]["active"][0]["moves"] = [
        {"id": "gigaimpact", "move": "Giga Impact", "disabled": False, "pp": 5},
        {"id": "earthquake", "move": "Earthquake", "disabled": True, "pp": 16},
        {"id": "doubleedge", "move": "Double-Edge", "disabled": True, "pp": 24},
        {"id": "knockoff", "move": "Knock Off", "disabled": True, "pp": 32},
    ]
    state["me"]["volatile"] = {"active": ["-mustrecharge"]}
    state["history"] = [
        "|move|p1a: Pikachu|Giga Impact|p2a: Carbink",
        "|-mustrecharge|p1a: Pikachu",
    ]
    particle = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )[0]

    battle = battle_from_live_state(state, particle)
    result = FoulPlayPolicyOracle().evaluate(battle, search_time_ms=5)

    assert result.best_action in {"gigaimpact", "switch charizard"}
    assert set(action.action for action in result.actions) == {
        "gigaimpact",
        "gigaimpact-tera",
        "switch charizard",
    }


def test_live_reference_battle_uses_request_private_team():
    state = make_state()
    state["format"] = "[Gen 9] Random Battle"
    policy = LiveReferencePolicy(particle_count=1, search_time_ms=5)
    result = policy.evaluate(state)

    assert result.best_action
    assert result.actions
    assert abs(sum(result.actions.values()) - 1.0) < 1e-9


def test_live_reference_upstream_compatible_mode_records_sampling_and_selector():
    state = make_state()
    policy = LiveReferencePolicy(
        particle_count=0,
        search_time_ms=5,
        seed=17,
        mode="upstream-compatible",
    )
    result = policy.evaluate(state)

    assert result.reference_mode == "upstream-compatible-root-oracle"
    assert result.dynamic_sampling is True
    assert result.particle_count == 4
    assert result.search_time_per_world_ms == 2
    assert result.raw_best_action
    assert result.selection_mode == "upstream-75pct-random"
    assert result.selection_candidates
    assert result.world_diagnostics


def test_live_dual_upstream_compatible_strength_zero_is_reference_control():
    state = make_state()
    from fp.live_reference import LiveDualViewPolicy

    dual = LiveDualViewPolicy(
        particle_count=0,
        self_search_time_ms=5,
        opponent_search_time_ms=5,
        strength=0.0,
        opponent_exploration=0.0,
        seed=17,
        mode="upstream-compatible",
    )
    reference = LiveReferencePolicy(
        particle_count=0,
        search_time_ms=5,
        seed=17,
        mode="upstream-compatible",
    ).evaluate(state)
    result = dual.evaluate(state)

    assert result.reference_mode == "dual-control-upstream-compatible"
    assert result.best_action == reference.best_action
    assert set(result.actions) == set(reference.actions)
    assert max(
        abs(result.actions[action] - reference.actions[action])
        for action in result.actions
    ) < 0.15
    assert result.self_policy == result.actions


def test_battle_from_live_state_has_real_request_side():
    state = make_state()
    particles = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )
    battle = battle_from_live_state(state, particles[0])

    assert battle.user.active.name == "pikachu"
    assert battle.user.active.item == "lightball"
    assert battle.opponent.active.name == "carbink"


def test_private_stats_and_hp_reach_engine_without_default_spread():
    from fp.search.poke_engine_helpers import battle_to_poke_engine_state

    state = make_state()
    row = state["request"]["side"]["pokemon"][0]
    row["condition"] = "123/234"
    row["stats"] = {"atk": 111, "def": 112, "spa": 113, "spd": 114, "spe": 115}
    particle = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )[0]
    battle = battle_from_live_state(state, particle)
    engine = battle_to_poke_engine_state(battle)
    actual = engine.side_one.pokemon[0]
    assert (actual.hp, actual.maxhp) == (123, 234)
    assert (
        actual.attack, actual.defense, actual.special_attack,
        actual.special_defense, actual.speed,
    ) == (111, 112, 113, 114, 115)


def test_public_opponent_boosts_reach_engine_keys():
    from fp.search.poke_engine_helpers import battle_to_poke_engine_state

    state = make_state()
    state["history"] = [
        "|switch|p2a: Carbink|Carbink, L90|100/100",
        "|-boost|p2a: Carbink|def|3",
        "|-boost|p2a: Carbink|spa|2",
        "|-unboost|p2a: Carbink|spe|1",
    ]
    particle = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )[0]
    battle = battle_from_live_state(state, particle)
    engine = battle_to_poke_engine_state(battle)
    assert engine.side_two.defense_boost == 3
    assert engine.side_two.special_attack_boost == 2
    assert engine.side_two.speed_boost == -1


def test_sampled_spread_is_applied_before_public_hp():
    from fp.battle import Pokemon

    state = make_state()
    candidate = {
        "slot": 1, "species": "Carbink", "level": 90, "nature": "timid",
        "evs": [0, 0, 0, 252, 4, 252], "item": "leftovers",
        "ability": "sturdy", "moves": ["moonblast"],
    }
    particle = BeliefParticle("spread-regression", {"team": [candidate]})
    battle = battle_from_live_state(state, particle)
    expected = Pokemon("carbink", 90, nature="timid", evs=tuple(candidate["evs"]))
    assert battle.opponent.active.stats == expected.stats
    assert battle.opponent.active.max_hp == expected.max_hp


def test_form_nickname_last_move_survives_encore_conversion():
    from fp.live_reference import _apply_live_last_moves
    from fp.battle import Battle, Pokemon
    from fp.search.poke_engine_helpers import battle_to_poke_engine_state

    battle = Battle("encore-form")
    battle.user.active = Pokemon("tornadustherian", 80)
    battle.user.active.add_move("hurricane")
    battle.user.active.volatile_statuses.append("encore")
    battle.opponent.active = Pokemon("pikachu", 80)
    battle.opponent.active.add_move("thunderbolt")
    state = {
        "turn": 5, "request": {"side": {"id": "p1"}},
        "history": [
            "|switch|p1a: Tornadus|Tornadus-Therian, L80|100/100",
            "|move|p1a: Tornadus|Hurricane|p2a: Pikachu",
            "|-start|p1a: Tornadus|Encore",
        ],
    }
    _apply_live_last_moves(battle, state)
    assert battle.user.last_used_move.move == "hurricane"
    assert battle_to_poke_engine_state(battle).side_one.last_used_move == "move:0"


def test_live_reference_handles_p2_request_side_without_swapping_canonical_sides():
    state = make_state()
    state["request"]["side"]["id"] = "p2"
    state["request"]["side"]["pokemon"] = [
        {
            "ident": "p2: Carbink",
            "details": "Carbink, L90",
            "condition": "100/100",
            "active": True,
            "item": "leftovers",
            "ability": "sturdy",
            "moves": ["moonblast"],
        },
        {
            "ident": "p2: Garchomp",
            "details": "Garchomp, L90",
            "condition": "100/100",
            "active": False,
            "item": "rockyhelmet",
            "ability": "roughskin",
            "moves": ["earthquake"],
        },
    ]
    state["request"]["active"] = [{"moves": [
        {"id": "moonblast", "move": "Moonblast", "disabled": False, "pp": 16},
    ]}]
    state["myTeam"] = [
        {
            "slot": 1,
            "name": "Carbink",
            "details": "Carbink, L90",
            "condition": "100/100",
            "moves": ["moonblast"],
        },
        {
            "slot": 2,
            "name": "Garchomp",
            "details": "Garchomp, L90",
            "condition": "100/100",
            "moves": ["earthquake"],
        },
    ]
    state["me"] = {"name": "Carbink", "hp": 100, "maxHp": 100}
    state["opp"] = {"name": "Pikachu", "hp": 100, "maxHp": 100}
    state["oppTeam"] = [
        {
            "slot": 1,
            "name": "Pikachu",
            "details": "Pikachu, L100",
            "hp": 100,
            "maxHp": 100,
            "moves": [{"name": "thunderbolt"}],
        }
    ]
    state["history"] = [
        "|switch|p1a: Pikachu|Pikachu, L100|100/100",
        "|switch|p2a: Carbink|Carbink, L90|100/100",
        "|move|p2a: Carbink|Moonblast|p1a: Pikachu",
    ]
    public = _public_state(state)

    assert public.sides["p1"][0].species == "Carbink"
    assert public.sides["p1"][0].ident == "p2: Carbink"
    assert public.sides["p2"][0].species == "Pikachu"
    assert public.sides["p2"][0].ident == "p1: Pikachu"

    particles = Gen9RandomBattleBeliefSampler(seed=17).sample(public, "p2", 1)
    battle = battle_from_live_state(state, particles[0])

    assert battle.user.active.name == "carbink"
    assert battle.opponent.active.name == "pikachu"
    assert battle.user.last_used_move.move == "moonblast"
    assert battle.opponent.last_used_move.move == "switch Pikachu"


def test_battle_from_live_state_preserves_disabled_active_request_moves_and_pp():
    state = make_state()
    state["request"]["active"][0]["moves"][3]["disabled"] = True
    state["request"]["active"][0]["moves"][3]["pp"] = 0
    particles = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )

    battle = battle_from_live_state(state, particles[0])

    assert [move.name for move in battle.user.active.moves] == [
        "thunderbolt",
        "protect",
        "quickattack",
        "nastyplot",
    ]
    assert battle.user.active.get_move("nastyplot").disabled is True
    assert battle.user.active.get_move("nastyplot").current_pp == 0


def test_battle_from_live_state_syncs_force_switch_and_last_selected_move():
    state = make_state()
    state["request"]["forceSwitch"] = [True]
    state["lastSelectedMove"] = {
        "move": "switch Charizard",
        "turn": 1,
        "type": "switch",
    }
    particles = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )

    battle = battle_from_live_state(state, particles[0])

    assert battle.force_switch is True
    assert battle.user.last_selected_move.move == "switch Charizard"


def test_battle_from_live_state_syncs_side_effect_details():
    state = make_state()
    state["sideEffectDetails"] = {
        "me": {
            "wish": {
                "turnsRemaining": 2,
                "amount": 50,
                "source": "Pikachu",
            },
            "futureSight": {
                "turnsRemaining": 3,
                "source": "Pikachu",
            },
            "healingWish": 1,
            "batonPassing": True,
            "shedTailing": False,
        },
        "opp": {
            "wish": {
                "turnsRemaining": 1,
                "amount": 40,
                "source": "Carbink",
            },
            "futureSight": {
                "turnsRemaining": 2,
                "source": "Carbink",
            },
            "healingWish": 0,
            "batonPassing": False,
            "shedTailing": True,
        },
    }
    particles = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )

    battle = battle_from_live_state(state, particles[0])

    assert battle.user.wish == (2, 50)
    assert battle.user.future_sight == (3, "Pikachu")
    assert battle.user.side_conditions[constants.HEALING_WISH] == 1
    assert battle.user.baton_passing is True
    assert battle.user.shed_tailing is False
    assert battle.opponent.wish == (1, 40)
    assert battle.opponent.future_sight == (2, "Carbink")
    assert battle.opponent.baton_passing is False
    assert battle.opponent.shed_tailing is True


def test_battle_from_live_state_syncs_last_public_switch_and_move():
    state = make_state()
    state["history"] = [
        "|switch|p1a: Pikachu|Pikachu, L100|100/100",
        "|move|p1a: Pikachu|Thunderbolt|p2a: Carbink",
    ]
    particles = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )

    battle = battle_from_live_state(state, particles[0])

    assert battle.user.last_used_move.move == "thunderbolt"


def test_battle_from_live_state_syncs_trapped_constraint():
    state = make_state()
    state["request"]["active"][0]["trapped"] = True
    particles = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )

    battle = battle_from_live_state(state, particles[0])
    result = FoulPlayPolicyOracle().evaluate(battle, search_time_ms=5)

    assert battle.user.trapped is True
    assert all(not action.action.startswith("switch ") for action in result.actions)


def test_battle_from_live_state_syncs_existing_side_hazards():
    state = make_state()
    state["oppHazards"] = ["move: Stealth Rock"]
    particles = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )

    battle = battle_from_live_state(state, particles[0])
    result = FoulPlayPolicyOracle().evaluate(battle, search_time_ms=5)

    assert battle.opponent.side_conditions[constants.STEALTH_ROCK] == 1
    assert all(action.action != "stealthrock" for action in result.actions)


def test_battle_from_live_state_syncs_hazard_layers():
    state = make_state()
    state["oppHazards"] = ["Spikes", "Spikes", "move: Toxic Spikes", "move: Toxic Spikes"]
    particles = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )

    battle = battle_from_live_state(state, particles[0])
    result = FoulPlayPolicyOracle().evaluate(battle, search_time_ms=5)

    assert battle.opponent.side_conditions[constants.SPIKES] == 2
    assert battle.opponent.side_conditions[constants.TOXIC_SPIKES] == 2
    assert all(action.action not in {"toxicspikes"} for action in result.actions)


def test_battle_from_live_state_syncs_toxic_count():
    state = make_state()
    state["sideConditionDetails"] = {
        "me": {
            "toxic_count": {
                "id": "toxic_count",
                "count": 2,
            }
        },
        "opp": {},
    }
    particles = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )

    battle = battle_from_live_state(state, particles[0])

    assert battle.user.side_conditions[constants.TOXIC_COUNT] == 2


def test_battle_from_live_state_syncs_protect_count():
    state = make_state()
    state["screens"] = {"me": [], "opp": []}
    state["sideConditionDetails"] = {
        "me": {
            "protect": {
                "id": "protect",
                "count": 1,
            }
        },
        "opp": {},
    }
    particles = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )

    battle = battle_from_live_state(state, particles[0])

    assert battle.user.side_conditions[constants.PROTECT] == 1


def test_battle_from_live_state_uses_screen_remaining_turns():
    state = make_state()
    state["screens"] = {"me": ["move: Aurora Veil"], "opp": []}
    state["sideConditionDetails"] = {
        "me": {
            "move: Aurora Veil": {
                "id": "auroraveil",
                "count": 1,
                "turnsRemaining": 7,
            }
        },
        "opp": {},
    }
    particles = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )

    battle = battle_from_live_state(state, particles[0])

    assert battle.user.side_conditions[constants.AURORA_VEIL] == 7


def test_battle_from_live_state_disables_history_limited_moves():
    state = make_state()
    state["request"]["active"][0]["moves"] = [
        {"id": "fakeout", "move": "Fake Out", "disabled": False},
        {"id": "noretreat", "move": "No Retreat", "disabled": False},
        {"id": "sleeptalk", "move": "Sleep Talk", "disabled": False},
    ]
    state["request"]["side"]["pokemon"][0]["condition"] = "100/100"
    state["history"] = [
        "|switch|p1a: Pikachu|Pikachu, L100|100/100",
        "|move|p1a: Pikachu|Fake Out|p2a: Carbink",
        "|move|p1a: Pikachu|No Retreat|p1a: Pikachu",
    ]
    particles = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )

    battle = battle_from_live_state(state, particles[0])
    assert battle.user.active.get_move("fakeout").disabled is True
    assert battle.user.active.get_move("noretreat").disabled is True
    assert battle.user.active.get_move("sleeptalk").disabled is True


def test_battle_from_live_state_uses_current_opponent_active_not_team_order():
    state = make_state()
    state["turn"] = 2
    state["opp"] = {"name": "Mienshao", "hp": 100, "maxHp": 100}
    state["oppTeam"] = [
        {
            "name": "Garganacl",
            "details": "Garganacl, L80",
            "hp": 100,
            "maxHp": 100,
        },
        {
            "name": "Mienshao",
            "details": "Mienshao, L83",
            "hp": 100,
            "maxHp": 100,
        },
    ]
    state["history"] = [
        "|switch|p2a: Garganacl|Garganacl, L80, F|100/100",
        "|switch|p2a: Mienshao|Mienshao, L83, M|100/100",
    ]
    particle = BeliefParticle(
        "active-switch",
        {
            "team": [
                {
                    "slot": 1,
                    "species": "Garganacl",
                    "level": 80,
                    "ability": "purifyingsalt",
                    "item": "leftovers",
                    "moves": ["saltcure", "recover", "bodypress", "protect"],
                    "evs": [85] * 6,
                    "nature": "serious",
                },
                {
                    "slot": 2,
                    "species": "Mienshao",
                    "level": 83,
                    "ability": "regenerator",
                    "item": "choicescarf",
                    "moves": ["highjumpkick", "uturn", "knockoff", "stoneedge"],
                    "evs": [85] * 6,
                    "nature": "serious",
                },
            ]
        },
    )

    battle = battle_from_live_state(state, particle)

    assert battle.opponent.active.name == "mienshao"
    assert "highjumpkick" in {move.name for move in battle.opponent.active.moves}
    assert battle.opponent.reserve[0].name == "garganacl"


def test_battle_from_live_state_preserves_used_tera_for_our_team():
    state = make_state()
    state["request"]["side"]["pokemon"][0]["teraType"] = "Electric"
    state["request"]["active"][0]["canTerastallize"] = "Electric"
    state["history"] = ["|terastallize|p1a: Pikachu|Electric"]
    particles = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )

    battle = battle_from_live_state(state, particles[0])
    result = FoulPlayPolicyOracle().evaluate(battle, search_time_ms=5)

    assert battle.user.active.terastallized is True
    assert battle.user.active.tera_type == "electric"
    assert all(not action.action.endswith("-tera") for action in result.actions)


def test_live_reference_preserves_available_tera_action_identity():
    state = make_state()
    state["request"]["active"][0]["canTerastallize"] = "Electric"

    result = LiveReferencePolicy(particle_count=1, search_time_ms=5).evaluate(state)

    assert any(action.endswith("-tera") for action in result.actions)
    assert abs(sum(result.actions.values()) - 1.0) < 1e-9


def test_public_observed_side_replays_opponent_boosts():
    state = make_state()
    state["opp"] = {"name": "Virizion", "hp": 100, "maxHp": 100}
    state["oppTeam"] = [
        {
            "slot": 1,
            "name": "Virizion",
            "details": "Virizion, L82",
            "condition": "100/100",
            "boosts": [],
        }
    ]
    state["history"] = [
        "|switch|p2a: Virizion|Virizion, L82|100/100",
        "|-boost|p2a: Virizion|atk|2",
        "|-boost|p2a: Virizion|atk|2",
        "|-unboost|p2a: Virizion|atk|1",
    ]

    observed = _public_observed_side(state, "p2")

    assert observed[0].species == "Virizion"
    assert observed[0].boosts == {"atk": 3}


def test_public_observed_side_uses_latest_switch_condition():
    state = make_state()
    state["opp"] = {"name": "Virizion", "hp": 40, "maxHp": 100}
    state["oppTeam"] = [
        {
            "slot": 1,
            "name": "Virizion",
            "details": "Virizion, L82",
            "condition": "40/100",
            "boosts": [],
        }
    ]
    state["history"] = [
        "|switch|p2a: Virizion|Virizion, L82|100/100",
        "|switch|p2a: Virizion|Virizion, L82|40/100",
    ]

    observed = _public_observed_side(state, "p2")

    assert observed[0].hp == "40/100"


@pytest.mark.parametrize("clear_event", [
    "|-clearboost|p2a: Carbink",
    "|-clearallboost",
    "|-clearpositiveboost|p2a: Carbink",
    "|faint|p2a: Carbink",
    "|switch|p2a: Carbink|Carbink, L90|100/100",
])
def test_public_boost_replay_clears_stages(clear_event):
    state = make_state()
    state["history"] = [
        "|switch|p2a: Carbink|Carbink, L90|100/100",
        "|-boost|p2a: Carbink|def|2",
        clear_event,
    ]
    assert _public_observed_side(state, "p2")[0].boosts == {}


def test_public_boost_replay_separates_same_named_opponents():
    state = make_state()
    state["history"] = [
        "|switch|p2a: Carbink|Carbink, L90|100/100",
        "|-boost|p1a: Carbink|def|2",
    ]
    assert _public_observed_side(state, "p2")[0].boosts == {}


def test_public_form_change_preserves_current_hp_and_boosts():
    state = make_state()
    state["opp"] = {
        "name": "Minior-Meteor", "hp": 63, "maxHp": 100, "boosts": ["atk+2"]
    }
    state["oppTeam"] = [state["opp"]]
    state["history"] = [
        "|switch|p2a: Minior|Minior-Indigo, L79|100/100",
        "|-formechange|p2a: Minior|Minior-Meteor||[from] ability: Shields Down",
        "|-boost|p2a: Minior|atk|2",
        "|-unboost|p2a: Minior|def|1",
        "|-clearnegativeboost|p2a: Minior|[silent]",
    ]
    observed = _public_observed_side(state, "p2")[0]
    assert observed.species == "Minior-Meteor"
    assert "L79" in observed.details
    assert observed.hp == "63/100"
    assert observed.boosts == {"atk": 2}


def test_real_protocol_tera_marks_entire_team_usage():
    from fp.search.poke_engine_helpers import battle_to_poke_engine_state

    state = make_state()
    state["request"]["side"]["pokemon"][0]["terastallized"] = "Electric"
    state["history"] = [
        "|switch|p1a: Pikachu|Pikachu, L100|100/100",
        "|-terastallize|p1a: Pikachu|Electric",
        "|switch|p2a: Carbink|Carbink, L90|100/100",
        "|-terastallize|p2a: Carbink|Fairy",
    ]
    particle = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )[0]
    battle = battle_from_live_state(state, particle)
    engine = battle_to_poke_engine_state(battle)
    assert engine.side_one.pokemon[0].terastallized
    assert engine.side_two.pokemon[0].terastallized
    assert engine.side_two.pokemon[0].tera_type.lower() == "fairy"


def test_opponent_view_does_not_expose_exact_private_hp():
    state = make_state()
    state["history"] = ["|switch|p1a: Pikachu|Pikachu, L100|180/301"]
    state["myTeam"] = [{"name": "Pikachu", "hp": 180, "maxHp": 301}]
    observed = _public_observed_side(state, "p1")[0]
    assert observed.hp == "60/100"


def test_live_transform_reconstructs_copied_configuration():
    state = make_state()
    state["history"] = [
        "|switch|p2a: Carbink|Carbink, L90|50/100",
        "|-transform|p2a: Carbink|p1a: Pikachu|[from] ability: Imposter",
    ]
    state["oppTeam"][0]["condition"] = "50/100"
    state["oppTeam"][0]["hp"] = 50
    particle = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )[0]
    battle = battle_from_live_state(state, particle)

    assert battle.opponent.active.name == "carbink"
    assert battle.opponent.active.moves == battle.user.active.moves
    assert battle.opponent.active.stats == battle.user.active.stats
    assert battle.opponent.active.types == battle.user.active.types
    assert battle.opponent.active.boosts == battle.user.active.boosts
    assert battle.opponent.active.ability == battle.user.active.ability
    assert "transform" in battle.opponent.active.volatile_statuses
    assert battle.opponent.active.original_ability == "imposter"
    assert battle.opponent.active.hp < battle.opponent.active.max_hp


def test_battle_from_live_state_matches_base_name_tera_event_to_form():
    state = make_state()
    state["request"]["side"]["pokemon"][0]["details"] = "Sinistcha-Masterpiece, L100"
    state["request"]["side"]["pokemon"][0]["ident"] = "p1: Sinistcha-Masterpiece"
    state["request"]["active"][0]["canTerastallize"] = "Steel"
    state["me"] = {"name": "Sinistcha-Masterpiece", "hp": 100, "maxHp": 100}
    state["history"] = [
        "|switch|p1a: Sinistcha|Sinistcha-Masterpiece, L100|100/100",
        "|terastallize|p1a: Sinistcha|Steel",
    ]
    particles = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )

    battle = battle_from_live_state(state, particles[0])
    result = FoulPlayPolicyOracle().evaluate(battle, search_time_ms=5)

    assert battle.user.active.name == "sinistchamasterpiece"
    assert battle.user.active.terastallized is True
    assert all(not action.action.endswith("-tera") for action in result.actions)


def test_battle_from_live_state_preserves_public_opponent_tera_usage():
    state = make_state()
    state["history"] = [
        "|switch|p2a: Carbink|Carbink, L90|100/100",
        "|terastallize|p2a: Carbink|Fairy",
    ]
    particle = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )[0]

    battle = battle_from_live_state(state, particle)
    result = FoulPlayPolicyOracle().evaluate(
        battle,
        perspective="opponent",
        search_time_ms=5,
    )

    assert battle.opponent.active.terastallized is True
    assert battle.opponent.active.tera_type == "fairy"
    assert all(not action.action.endswith("-tera") for action in result.actions)


def test_battle_from_live_state_applies_current_private_boosts():
    state = make_state()
    state["myTeam"] = [
        {
            "name": "Charizard",
            "hp": 100,
            "maxHp": 100,
            "boosts": [],
        },
        {
            "name": "Pikachu",
            "hp": 100,
            "maxHp": 100,
            "boosts": ["spa+6"],
        },
    ]
    particles = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )

    battle = battle_from_live_state(state, particles[0])

    assert battle.user.active.boosts[constants.SPECIAL_ATTACK] == 6


def test_battle_from_live_state_falls_back_to_private_my_team():
    state = make_state()
    state.pop("request")
    state["myTeam"] = [
        {
            "slot": 1,
            "name": "Pikachu",
            "details": "Pikachu, L100",
            "condition": "100/100",
            "item": "lightball",
            "ability": "static",
            "stats": {
                "atk": 100,
                "def": 80,
                "spa": 120,
                "spd": 90,
                "spe": 150,
            },
            "moves": ["thunderbolt", "protect", "quickattack", "nastyplot"],
        }
    ]
    particles = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )
    battle = battle_from_live_state(state, particles[0])

    assert battle.user.active.name == "pikachu"
    assert battle.user.active.item == "lightball"


def test_live_state_normalizes_showdown_terrain_text():
    state = make_state()
    state["terrain"] = "move: Electric Terrain"
    particles = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )

    battle = battle_from_live_state(state, particles[0])

    assert battle.field == constants.ELECTRIC_TERRAIN


def test_live_state_normalizes_unknown_weather_turns_to_engine_sentinel():
    state = make_state()
    state["weather"] = "RainDance"
    state["weatherState"] = {
        "turnsRemaining": None,
        "source": None,
    }
    particles = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )

    battle = battle_from_live_state(state, particles[0])

    assert battle.weather == constants.RAIN
    assert battle.weather_turns_remaining == -1


@pytest.mark.parametrize(
    ("raw_weather", "expected_weather"),
    [
        ("RainDance", constants.RAIN),
        ("Sandstorm", constants.SAND),
        ("Snowscape", constants.SNOW),
    ],
)
def test_live_state_normalizes_showdown_weather_text(
    raw_weather, expected_weather
):
    state = make_state()
    state["weather"] = raw_weather
    particles = Gen9RandomBattleBeliefSampler(seed=17).sample(
        _public_state(state), "p2", 1
    )

    battle = battle_from_live_state(state, particles[0])

    assert battle.weather == expected_weather


def test_live_reference_recovers_public_form_from_switch_history():
    state = make_state()
    state["opp"] = {
        "name": "Necrozma",
        "hp": 94,
        "maxHp": 100,
        "moves": [
            {"name": "Dragon Dance"},
            {"name": "Earthquake"},
            {"name": "Sunsteel Strike"},
        ],
    }
    state["oppTeam"] = [state["opp"]]
    state["history"] = [
        "|switch|p2a: Necrozma|Necrozma-Dusk-Mane, L69|100/100",
    ]

    public = _public_state(state)

    assert public.sides["p2"][0].species == "Necrozma-Dusk-Mane"
    assert (
        Gen9RandomBattleBeliefSampler(seed=17).candidate_sets(public.sides["p2"][0])
    )


def test_battle_from_live_state_preserves_public_form_for_active_pokemon():
    state = make_state()
    state["opp"] = {
        "name": "Goodra",
        "hp": 94,
        "maxHp": 100,
    }
    state["oppTeam"] = [state["opp"]]
    state["history"] = [
        "|switch|p2a: Goodra|Goodra-Hisui, L82, F|100/100",
    ]

    public = _public_state(state)
    particle = Gen9RandomBattleBeliefSampler(seed=17).sample(
        public, "p2", 1
    )[0]
    battle = battle_from_live_state(state, particle)

    assert public.sides["p2"][0].species == "Goodra-Hisui"
    assert battle.opponent.active.name == "goodrahisui"


def test_policy_oracle_handles_public_active_history_for_a_truncated_move_set():
    state = make_state()
    state["turn"] = 2
    state["opp"] = {"name": "Frosmoth", "hp": 100, "maxHp": 100}
    state["oppTeam"] = [state["opp"]]
    state["history"] = [
        "|switch|p2a: Frosmoth|Frosmoth, L80|100/100",
        "|move|p2a: Frosmoth|Hurricane|p1a: Pikachu",
    ]
    particle = BeliefParticle(
        "six-move-frosmoth",
        {
            "team": [
                {
                    "slot": 1,
                    "species": "Frosmoth",
                    "level": 80,
                    "ability": "ice scales",
                    "item": "heavy-duty-boots",
                    "moves": [
                        "bugbuzz",
                        "gigadrain",
                        "icebeam",
                        "quiverdance",
                        "terablast",
                        "hurricane",
                    ],
                    "evs": [85] * 6,
                    "nature": "modest",
                }
            ]
        },
    )

    battle = battle_from_live_state(state, particle)
    result = FoulPlayPolicyOracle().evaluate(
        battle,
        perspective="opponent",
        search_time_ms=5,
    )

    assert result.actions


def test_live_dual_view_returns_reweighted_policy():
    state = make_state()
    state["actions"] = [
        {"type": "move", "slot": 1, "id": "thunderbolt"},
        {"type": "switch", "slot": 2},
    ]

    result = LiveDualViewPolicy(search_time_ms=5, seed=17).evaluate(state)

    assert result.best_action
    assert result.actions
    assert result.self_policy
    assert result.opponent_prediction
    assert result.opponent_model_prediction
    assert result.opponent_exploration == 0.1
    assert result.action_pair_values
    assert abs(sum(result.actions.values()) - 1.0) < 1e-9


def test_live_dual_view_supports_independent_search_budgets_and_diagnostics():
    state = make_state()
    state["actions"] = [
        {"type": "move", "slot": 1, "id": "thunderbolt"},
        {"type": "switch", "slot": 2},
    ]

    result = LiveDualViewPolicy(
        self_search_time_ms=5,
        opponent_search_time_ms=7,
        seed=17,
    ).evaluate(state)

    assert result.search_time_ms == 5
    assert result.self_search_time_ms == 5
    assert result.opponent_search_time_ms == 7
    assert len(result.particle_diagnostics) == 1
    diagnostic = result.particle_diagnostics[0]
    assert diagnostic["selfSearchTimeMs"] == 5
    assert diagnostic["opponentSearchTimeMs"] == 7
    assert diagnostic["selfActions"]
    assert diagnostic["opponentActions"]
    assert diagnostic["selfTotalVisits"] > 0
    assert diagnostic["opponentTotalVisits"] > 0


def test_live_dual_view_keeps_legacy_search_time_parameter():
    policy = LiveDualViewPolicy(search_time_ms=6)

    assert policy.self_search_time_ms == 6
    assert policy.opponent_search_time_ms == 6


def test_opponent_policy_mixes_model_and_uniform_exploration():
    mixed = _mix_opponent_policy(
        {"attack": 0.8, "switch": 0.2},
        0.25,
        action_universe={"attack", "switch", "rare"},
    )

    assert mixed["attack"] == pytest.approx(0.8 * 0.75 + 1 / 3 * 0.25)
    assert mixed["switch"] == pytest.approx(0.2 * 0.75 + 1 / 3 * 0.25)
    assert mixed["rare"] == pytest.approx(1 / 3 * 0.25)
    assert abs(sum(mixed.values()) - 1.0) < 1e-9
