from tools.live_reference_server import (
    REFERENCE_CONTRACT_VERSION,
    SCHEMA_VERSION,
    TRANSLATION_VERSION,
    choose_legal_action,
)


def make_state():
    return {
        "myTeam": [
            {"slot": 2, "name": "Charizard"},
            {"slot": 1, "name": "Pikachu"},
        ],
        "actions": [
            {"type": "switch", "slot": 2},
        ],
    }


def test_reference_action_is_projected_to_legal_switch():
    action, selected, fallback, reason = choose_legal_action(
        make_state(),
        "dracometeor",
        {"dracometeor": 1.0, "switch charizard": 0.0},
    )
    assert action == {"type": "switch", "slot": 2}
    assert selected == "switch charizard"
    assert fallback is True
    assert reason == "raw-best-action-not-legal"


def test_reference_action_keeps_protocol_recharge():
    state = make_state()
    state["actions"] = [{"type": "move", "slot": 1, "id": "recharge"}]
    action, selected, fallback, reason = choose_legal_action(
        state,
        "no move",
        {"no move": 1.0, "recharge": 0.0},
    )

    assert action == {"type": "move", "slot": 1}
    assert selected == "recharge"
    assert fallback is False
    assert reason is None


def test_reference_contract_versions_are_stable():
    assert SCHEMA_VERSION == "foul-play-reference/v1"
    assert REFERENCE_CONTRACT_VERSION == "reference-contract/v0.1"
    assert TRANSLATION_VERSION == "ps-adapter-state-sync/v0.4.1"
