"""Unit-test env defaults + isolation from the E2E fixture chain.

axi.config validates BOT_NAMESPACE (and resolves DISCORD_TOKEN) at import
time, so every unit test that imports an axi module needs these set before
import. pytest imports this conftest before any test module; the per-file
`setdefault` lines already present in some test files are harmless duplicates.

The autouse ``_recover_after_failure`` fixture overrides the session-level
one in tests/conftest.py (which chains to the live-Discord ``warmup``/
``discord``/``test_config`` fixtures) so unit tests run without Discord
credentials or a bot instance.
"""

import os

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")
os.environ.setdefault("BOT_NAMESPACE", "off")


@pytest.fixture(autouse=True)
def _recover_after_failure():
    """Override the E2E autouse fixture so unit tests skip Discord warmup."""
