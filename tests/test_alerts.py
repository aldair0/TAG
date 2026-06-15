"""Outbound alert throttling + suppressed-send behavior (no real SMTP)."""

from __future__ import annotations

from app.alerts import reset_throttle, send_alert


def setup_function():
    reset_throttle()


def test_alert_is_suppressed_in_tests():
    # TAG_DISABLE_ALERTS=1 (conftest) → logged, never sent.
    assert send_alert("Test subject", "body", key="k1") == "logged"


def test_alert_throttles_repeat_within_window():
    assert send_alert("Disk low", "x", key="disk_low", min_interval_sec=3600) == "logged"
    # Second within the window is throttled regardless of content.
    assert send_alert("Disk low", "y", key="disk_low", min_interval_sec=3600) == "throttled"


def test_distinct_keys_are_independent():
    assert send_alert("a", "x", key="alpha") == "logged"
    assert send_alert("b", "x", key="beta") == "logged"


def test_reset_throttle_allows_resend():
    assert send_alert("again", "x", key="again") == "logged"
    assert send_alert("again", "x", key="again") == "throttled"
    reset_throttle()
    assert send_alert("again", "x", key="again") == "logged"
