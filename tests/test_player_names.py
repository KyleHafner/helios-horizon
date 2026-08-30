from game_control.adapters.crafty import _extract_player_names
from game_control.players import PlayerTracker


def test_extract_from_json_list_string():
    assert _extract_player_names({"players": "['Swag', 'Guest1']"}) == ("Swag", "Guest1")


def test_extract_from_real_list():
    assert _extract_player_names({"online_players": ["Swag"]}) == ("Swag",)


def test_extract_missing_returns_none():
    assert _extract_player_names({"players_online": 2}) is None


def test_extract_empty_list_is_known_empty():
    assert _extract_player_names({"players": "[]"}) == ()


def test_player_tracker_names_returns_copy_for_tracked_profile():
    tracker = PlayerTracker()
    tracker._players["terraria-tmod"] = {"Swag"}
    names = tracker.names("terraria-tmod")
    assert names == {"Swag"}
    names.add("mutated")
    assert tracker.names("terraria-tmod") == {"Swag"}


def test_player_tracker_names_returns_none_for_untracked_profile():
    assert PlayerTracker().names("minecraft") is None
