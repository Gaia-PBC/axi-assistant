"""Unit-test env defaults.

axi.config validates BOT_NAMESPACE (and resolves DISCORD_TOKEN) at import
time, so every unit test that imports an axi module needs these set before
import. pytest imports this conftest before any test module; the per-file
`setdefault` lines already present in some test files are harmless duplicates.
"""

import os

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")
os.environ.setdefault("BOT_NAMESPACE", "off")
