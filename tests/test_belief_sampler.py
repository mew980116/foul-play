from fp.belief_sampler import Gen9RandomBattleBeliefSampler
from fp.dual_view import BeliefParticle, PublicBattleState, PublicPokemonState


def make_public_state() -> PublicBattleState:
    return PublicBattleState(
        turn=3,
        format_id="gen9randombattle",
        sides={
            "p1": (
                PublicPokemonState(
                    ident="p1: Gardevoir",
                    species="gardevoir",
                    details="Gardevoir, L83",
                    hp="100/100",
                    revealed_moves=("Mystical Fire",),
                ),
            ),
            "p2": (),
        },
    )


def test_gen9_sampler_is_seeded_and_returns_full_particles():
    public = make_public_state()
    first = Gen9RandomBattleBeliefSampler(seed=17).sample(public, "p1", 3)
    second = Gen9RandomBattleBeliefSampler(seed=17).sample(public, "p1", 3)

    assert first == second
    assert len(first) == 3
    for particle in first:
        team = particle.payload["team"]
        assert len(team) == 6
        assert len({pokemon["species"] for pokemon in team}) == 6
        gardevoir = team[0]
        assert gardevoir["species"] == "gardevoir"
        assert "mysticalfire" in gardevoir["moves"]
        assert particle.weight == 1 / 3
        assert particle.source == "gen9-randombattle-public-sampler"


def test_sampler_does_not_use_true_private_state():
    public = make_public_state()
    first = Gen9RandomBattleBeliefSampler(seed=23).sample(public, "p1", 2)
    changed_public = make_public_state()
    second = Gen9RandomBattleBeliefSampler(seed=23).sample(changed_public, "p1", 2)

    assert first == second
    serialized = str(first)
    assert "private" not in serialized.lower()
    assert "pixilate" not in serialized.lower()


def test_sampler_rejects_non_gen9_random_battle():
    public = PublicBattleState(
        turn=1,
        format_id="gen8battlefactory",
        sides={"p1": (), "p2": ()},
    )

    try:
        Gen9RandomBattleBeliefSampler(seed=1).sample(public, "p1", 1)
    except ValueError as error:
        assert "gen9randombattle" in str(error)
    else:
        raise AssertionError("expected format validation error")


def test_sampler_relaxes_illusion_like_public_move_conflict():
    public = PublicBattleState(
        turn=12,
        format_id="gen9randombattle",
        sides={
            "p1": (
                PublicPokemonState(
                    ident="p1: Tropius",
                    species="tropius",
                    details="Tropius, L91",
                    hp="100/100",
                    revealed_moves=("Hyper Voice",),
                ),
            ),
            "p2": (),
        },
    )

    particles = Gen9RandomBattleBeliefSampler(seed=17).sample(public, "p1", 1)

    assert len(particles) == 1
    assert "hyper voice" in {
        move.lower() for move in particles[0].payload["team"][0]["moves"]
    }


def test_consistency_filter_uses_only_publicly_revealed_information():
    public = make_public_state()
    sampler = Gen9RandomBattleBeliefSampler(seed=7)
    particles = sampler.sample(public, "p1", 2)
    valid = particles[0]
    invalid_payload = {
        "format": "gen9randombattle",
        "side": "p1",
        "team": [
            {
                **pokemon,
                "moves": [],
            }
            if pokemon["slot"] == 1
            else pokemon
            for pokemon in valid.payload["team"]
        ],
    }
    invalid = BeliefParticle(
        particle_id="invalid-public-move",
        payload=invalid_payload,
        weight=3.0,
        source="test",
    )
    history_public = PublicBattleState(
        turn=3,
        format_id="gen9randombattle",
        sides=public.sides,
        history=(
            {
                "type": "move",
                "actor": "p1a: Gardevoir",
                "move": "Mystical Fire",
            },
        ),
    )

    filtered = sampler.filter_consistent_particles(
        history_public,
        [valid, invalid],
        "p1",
    )
    assert [particle.particle_id for particle in filtered] == [valid.particle_id]
    assert filtered[0].weight == 1.0


def test_consistency_filter_renormalizes_weights():
    public = make_public_state()
    particles = Gen9RandomBattleBeliefSampler(seed=11).sample(public, "p1", 2)
    filtered = Gen9RandomBattleBeliefSampler.filter_consistent_particles(
        public,
        [
            BeliefParticle(
                particle_id=particles[0].particle_id,
                payload=particles[0].payload,
                weight=1.0,
                source="test",
            ),
            BeliefParticle(
                particle_id=particles[1].particle_id,
                payload=particles[1].payload,
                weight=3.0,
                source="test",
            ),
        ],
        "p1",
    )
    assert sum(particle.weight for particle in filtered) == 1.0
    assert [particle.weight for particle in filtered] == [0.25, 0.75]


def test_reordered_public_team_history_matches_species_not_slot():
    particle = BeliefParticle("ordered", {"team": [
        {"slot": 1, "species": "Pikachu", "moves": ["thunderbolt"]},
        {"slot": 2, "species": "Charizard", "moves": ["flamethrower"]},
    ]})
    public = PublicBattleState(
        turn=3, format_id="gen9randombattle",
        sides={"p1": (
            PublicPokemonState(ident="p1: Charizard", species="charizard"),
            PublicPokemonState(ident="p1: Pikachu", species="pikachu"),
        )},
        history=({"type": "move", "actor": "p1a: Charizard", "move": "flamethrower"},),
    )
    assert Gen9RandomBattleBeliefSampler.filter_consistent_particles(public, [particle], "p1")
