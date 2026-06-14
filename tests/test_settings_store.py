from app.settings_store import get_setting, set_setting


def test_get_default_when_missing(session):
    assert get_setting(session, "absent.key", default="x") == "x"


def test_returns_none_default_when_unspecified(session):
    assert get_setting(session, "absent.key") is None


def test_set_then_get(session):
    set_setting(session, "k", "v")
    assert get_setting(session, "k", default="x") == "v"


def test_set_updates_existing(session):
    set_setting(session, "k", "first")
    set_setting(session, "k", "second")
    assert get_setting(session, "k") == "second"
