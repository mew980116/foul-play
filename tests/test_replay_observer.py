from tools.replay_observer import (
    PublicReplayTracker,
    build_snapshots,
    select_snapshot,
)


def test_public_replay_tracker_keeps_only_public_observations():
    tracker = PublicReplayTracker()
    tracker.apply(
        {
            "type": "switch",
            "args": ["p1a: Pikachu", "Pikachu, L100", "100/100"],
        }
    )
    tracker.apply(
        {
            "type": "move",
            "args": ["p1a: Pikachu", "Thunderbolt", "p2a: Squirtle"],
        }
    )
    tracker.apply(
        {
            "type": "ability",
            "args": ["p2a: Squirtle", "Torrent"],
        }
    )

    state = tracker.public_state("gen9randombattle").to_dict()

    assert state["sides"]["p1"][0]["species"] == "pikachu"
    assert state["sides"]["p1"][0]["revealedMoves"] == ["thunderbolt"]
    assert state["sides"]["p2"][0]["revealedAbility"] == "torrent"
    assert "privateFields" not in state["sides"]["p1"][0]


def test_snapshots_and_decision_index_align_with_replay_requests():
    row = {
        "decisions": [
            {"turn": 1, "action": {"type": "move", "slot": 1}},
            {"turn": 2, "action": {"type": "switch", "slot": 2}},
        ],
        "protocol": [
            {
                "type": "switch",
                "args": ["p1a: Pikachu", "Pikachu, L100", "100/100"],
            },
            {"type": "turn", "args": ["1"]},
            {"type": "request", "args": []},
            {
                "type": "move",
                "args": ["p1a: Pikachu", "Thunderbolt", "p2a: Squirtle"],
            },
            {"type": "turn", "args": ["2"]},
            {"type": "request", "args": []},
        ],
    }

    snapshots = build_snapshots(row)
    snapshot, decision = select_snapshot(row, snapshots, 1, None, None)

    assert snapshot[0] == 2
    assert snapshot[2].turn == 2
    assert decision == row["decisions"][1]
