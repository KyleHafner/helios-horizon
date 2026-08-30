from game_control.sessions import SessionTracker


T0 = "2026-07-14T01:00:00Z"
T1 = "2026-07-14T01:05:00Z"
T2 = "2026-07-14T01:10:00Z"


def test_join_opens_session_once():
    tracker = SessionTracker()
    events = tracker.observe("minecraft", {"Swag"}, now=T0)
    assert [(e.kind, e.player) for e in events] == [("open", "Swag")]
    assert tracker.observe("minecraft", {"Swag"}, now=T1) == []


def test_leave_closes_session_with_matching_id():
    tracker = SessionTracker()
    opened = tracker.observe("minecraft", {"Swag"}, now=T0)[0]
    closed = tracker.observe("minecraft", set(), now=T1)[0]
    assert closed.kind == "close"
    assert closed.session_id == opened.session_id
    assert closed.at == T1


def test_none_names_is_noop():
    tracker = SessionTracker()
    tracker.observe("minecraft", {"Swag"}, now=T0)
    assert tracker.observe("minecraft", None, now=T1) == []
    assert tracker.observe("minecraft", set(), now=T2)[0].kind == "close"


def test_profile_stopped_closes_everyone():
    tracker = SessionTracker()
    tracker.observe("terraria-vanilla", {"A", "B"}, now=T0)
    closed = tracker.profile_stopped("terraria-vanilla", now=T1)
    assert sorted(e.player for e in closed) == ["A", "B"]
    assert all(e.kind == "close" for e in closed)
    assert tracker.observe("terraria-vanilla", set(), now=T2) == []
