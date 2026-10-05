from fp.dual_view import (
    BeliefParticle,
    DualViewState,
    PrivatePokemonState,
    PublicBattleState,
    PublicPokemonState,
)


def make_state(hidden_ability: str) -> DualViewState:
    public_pokemon = PublicPokemonState(
        ident="p1: Gardevoir",
        species="gardevoir",
        details="Gardevoir, L80",
        hp="100/100",
        revealed_moves=("Psychic",),
    )
    private_pokemon = PrivatePokemonState(
        public=public_pokemon,
        private_fields={
            "ability": hidden_ability,
            "item": "choicespecs",
            "moves": ["Psychic", "Mystical Fire", "Moonblast", "Trick"],
        },
    )
    public = PublicBattleState(
        turn=4,
        format_id="gen9randombattle",
        sides={
            "p1": (public_pokemon,),
            "p2": (
                PublicPokemonState(
                    ident="p2: Corviknight",
                    species="corviknight",
                    details="Corviknight, L80",
                    hp="80/100",
                ),
            ),
        },
        history=({"type": "move", "actor": "p1: Gardevoir", "move": "Psychic"},),
    )
    particle = BeliefParticle(
        particle_id="gardevoir-particle-1",
        payload={
            "species": "gardevoir",
            "ability": "trace",
            "item": "choicespecs",
        },
        weight=1.0,
        source="gen9-randombattle",
    )
    return DualViewState(
        public=public,
        self_private={"p1: Gardevoir": private_pokemon},
        belief_about_us=(particle,),
    )


def test_opponent_view_does_not_depend_on_true_private_configuration():
    state_a = make_state("pixilate")
    state_b = make_state("synchronize")

    assert state_a.public_view() == state_b.public_view()
    assert state_a.opponent_visible_input() == state_b.opponent_visible_input()
    assert state_a.self_decision_input() != state_b.self_decision_input()


def test_opponent_view_contains_no_private_field_or_belief():
    state = make_state("pixilate")
    opponent_view = state.opponent_visible_input()
    serialized = str(opponent_view)

    assert "privateSelf" not in opponent_view
    assert "beliefAboutOpponent" not in opponent_view
    assert "privateFields" not in serialized
    assert "pixilate" not in serialized
    assert "synchronize" not in serialized
    assert state.belief_about_us_dict()[0]["payload"]["ability"] == "trace"


def test_opponent_policy_input_uses_hypothesis_not_true_private_state():
    state = make_state("pixilate")
    assumed = BeliefParticle(
        particle_id="hypothesis-2",
        payload={"ability": "synchronize", "item": "leftovers"},
        weight=0.25,
        source="visibility-filter",
    )
    policy_input = state.opponent_policy_input(
        opponent_private={"species": "corviknight", "ability": "pressure"},
        assumed_self_particle=assumed,
    )

    assert policy_input["privateObserver"]["ability"] == "pressure"
    assert policy_input["assumedOpponent"]["payload"]["ability"] == "synchronize"
    assert (
        policy_input["assumedOpponent"]["payload"]["ability"]
        != state.self_private["p1: Gardevoir"].private_fields["ability"]
    )


def test_state_inputs_are_defensive_copies():
    state = make_state("pixilate")
    public_input = state.public_view()
    public_input["sides"]["p1"][0]["revealedMoves"].append("Moonblast")
    public_input["history"][0]["move"] = "Explosion"

    next_public_input = state.public_view()
    assert next_public_input["sides"]["p1"][0]["revealedMoves"] == ["Psychic"]
    assert next_public_input["history"][0]["move"] == "Psychic"
