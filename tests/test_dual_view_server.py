from tools.dual_view_server import choose_legal_action


def make_state():
    return {
        "myTeam": [
            {"slot": 2, "name": "Charizard"},
            {"slot": 1, "name": "Pikachu"},
        ],
        "actions": [
            {"type": "move", "slot": 1, "id": "thunderbolt"},
            {"type": "move", "slot": 1, "id": "thunderbolt", "tera": True},
            {"type": "switch", "slot": 2},
        ],
    }


def test_choose_legal_move_preserves_tera_choice():
    action, selected, fallback, reason = choose_legal_action(make_state(), "thunderbolt-tera")
    assert action == {"type": "move", "slot": 1, "tera": True}
    assert selected == "thunderbolt-tera"
    assert fallback is False
    assert reason is None


def test_choose_legal_switch_uses_public_team_name():
    action, selected, fallback, reason = choose_legal_action(make_state(), "switch charizard")
    assert action == {"type": "switch", "slot": 2}
    assert selected == "switch charizard"
    assert fallback is False
    assert reason is None


def test_choose_legal_action_falls_back_to_highest_probability_legal_action():
    state = make_state()
    action, selected, fallback, reason = choose_legal_action(
        state,
        "recover",
        {"recover": 0.9, "thunderbolt": 0.1},
    )
    assert action == {"type": "move", "slot": 1}
    assert selected == "thunderbolt"
    assert fallback is True
    assert reason == "raw-best-action-not-legal"


def test_choose_legal_action_projects_unavailable_tera_to_same_move():
    state = make_state()
    state["actions"] = [
        {"type": "move", "slot": 1, "id": "thunderbolt"},
    ]

    action, selected, fallback, reason = choose_legal_action(
        state,
        "thunderbolt-tera",
        {"thunderbolt-tera": 0.9, "thunderbolt": 0.1},
    )

    assert action == {"type": "move", "slot": 1}
    assert selected == "thunderbolt"
    assert fallback is True
    assert reason == "tera-unavailable"
