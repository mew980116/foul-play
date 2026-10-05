from fp.battle import Battle, Pokemon
from fp.public_observation import SCHEMA_VERSION, build_public_observation


def make_battle():
    battle = Battle("public-observation-test")
    battle.pokemon_format = "gen9randombattle"
    battle.user.active = Pokemon("pikachu", 100)
    battle.opponent.active = Pokemon("carbink", 100)
    battle.user.active.item = "lightball"
    battle.user.active.ability = "static"
    battle.user.active.add_move("thunderbolt")
    battle.opponent.active.item = "leftovers"
    battle.opponent.active.ability = "sturdy"
    battle.opponent.active.add_move("moonblast")
    return battle


def test_public_observation_does_not_infer_private_configuration():
    battle = make_battle()
    observation = build_public_observation(battle).to_dict()

    user = observation["sides"]["p1"][0]
    assert user["moves"] == []
    assert user["movesKnown"] is False
    assert user["item"] is None
    assert user["itemKnown"] is False
    assert user["ability"] is None
    assert user["abilityKnown"] is False


def test_public_observation_includes_only_explicit_reveals():
    battle = make_battle()
    observation = build_public_observation(
        battle,
        reveals={
            "p2": {
                "carbink": {
                    "moves": ["moonblast"],
                    "item": "leftovers",
                    "ability": "sturdy",
                }
            }
        },
    ).to_dict()
    opponent = observation["sides"]["p2"][0]
    assert opponent["moves"] == ["moonblast"]
    assert opponent["movesKnown"] is True
    assert opponent["item"] == "leftovers"
    assert opponent["ability"] == "sturdy"


def test_public_observation_hash_is_invariant_to_hidden_fields():
    first = make_battle()
    second = make_battle()
    second.user.active.item = "choiceband"
    second.user.active.ability = "hugepower"
    second.user.active.add_move("protect")

    assert (
        build_public_observation(first).observation_hash
        == build_public_observation(second).observation_hash
    )
    assert SCHEMA_VERSION == "public-observation/v1"


def test_exact_hp_and_hidden_duration_do_not_leak():
    first = make_battle()
    second = make_battle()
    first.user.active.hp, first.user.active.max_hp = 151, 300
    second.user.active.hp, second.user.active.max_hp = 202, 400
    first.weather, second.weather = "sunnyday", "sunnyday"
    first.weather_turns_remaining, second.weather_turns_remaining = 5, 8
    a, b = build_public_observation(first), build_public_observation(second)
    assert a.observation_hash == b.observation_hash
    assert a.to_dict()["sides"]["p1"][0]["hp"] == 51
    assert a.to_dict()["sides"]["p1"][0]["maxHp"] == 100


def test_near_full_hp_does_not_reveal_exact_maximum():
    battle = make_battle()
    battle.user.active.hp, battle.user.active.max_hp = 999, 1000
    assert build_public_observation(battle).to_dict()["sides"]["p1"][0]["hp"] == 99
