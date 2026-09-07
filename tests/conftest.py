"""Explicit safe defaults for isolated test processes."""

import pytest


@pytest.fixture(autouse=True)
def configured_public_origin(monkeypatch: pytest.MonkeyPatch):
    """Give each test an explicit origin without masking startup-failure tests."""
    monkeypatch.setenv("HORIZON_PUBLIC_ORIGIN", "https://games.example.com")
