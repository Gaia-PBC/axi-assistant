"""Regression tests for ensure_guild_infrastructure combined-mode discovery.

Covers BUG 1: when AXI_COMBINE_LIVE_CATEGORIES=1, the category discovery must
include pre-existing categories matching AXI_CATEGORY_NAME / ACTIVE_CATEGORY_NAME
so channels from a previous non-combined config are still visible to
ensure_agent_channel and reconstruct_agents_from_channels. Otherwise the bot
creates a duplicate #axi-master inside the new combined category while the
pre-existing #axi-master remains in the legacy Axi category.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

from axi import channels


class FakeRole:
    """Hashable stand-in for discord.Role / discord.Member (dict-key compatible)."""
    def __init__(self, rid: int) -> None:
        self.id = rid


class FakeChannel:
    """Stand-in for discord.TextChannel with a working move() that mutates source+dest."""
    def __init__(self, cid: int, name: str, category: FakeCategory | None = None) -> None:
        self.id = cid
        self.name = name
        self.category = category
        self.category_id = category.id if category else None
        self.permissions_synced = True
        self.position = 0
        self.move = AsyncMock(side_effect=self._move)
        self.edit = AsyncMock()
        if category is not None:
            category.text_channels.append(self)

    async def _move(self, *, category: FakeCategory, sync_permissions: bool = False, **kwargs) -> None:
        if self.category is not None and self in self.category.text_channels:
            self.category.text_channels.remove(self)
        self.category = category
        self.category_id = category.id
        category.text_channels.append(self)


BOT_MEMBER_ID = 2


class FakeCategory:
    def __init__(
        self,
        cid: int,
        name: str,
        text_channels: list | None = None,
        *,
        owner_id: int | None = BOT_MEMBER_ID,
    ) -> None:
        self.id = cid
        self.name = name
        self.text_channels = text_channels or []
        self.channels = self.text_channels  # channel limit check uses .channels
        # ensure_guild_infrastructure treats a category as its own only when this
        # bot's member appears in the permission overwrites (0f85e12) — every
        # category the bot creates gets guild.me via _build_category_overwrites.
        # owner_id=None models a category created by a DIFFERENT bot instance.
        self.overwrites: dict = {} if owner_id is None else {FakeRole(owner_id): object()}
        self.edit = AsyncMock()
        self.delete = AsyncMock()


class FakeGuild:
    def __init__(self, cats: list[FakeCategory]) -> None:
        self.id = 999
        self.categories = cats
        self.default_role = FakeRole(1)
        self.me = FakeRole(2)
        self.create_category = AsyncMock(side_effect=self._create_category)
        self._created: list[FakeCategory] = []

    async def _create_category(self, name: str, overwrites: dict | None = None) -> FakeCategory:
        cat = FakeCategory(cid=100000 + len(self._created), name=name)
        cat.overwrites = overwrites or {}
        self._created.append(cat)
        self.categories.append(cat)
        return cat


class FakeBot:
    def __init__(self, guild: FakeGuild) -> None:
        self._guild = guild

    def get_guild(self, _gid: int) -> FakeGuild:
        return self._guild


def _reset_channel_state() -> None:
    channels.target_guild = None
    channels.axi_categories = []
    channels.combined_categories = []
    channels.active_categories = []
    channels.killed_categories = []


@pytest.mark.asyncio
async def test_default_mode_unchanged() -> None:
    """No combine env vars — classic Axi/Active/Killed discovery still works."""
    _reset_channel_state()
    axi = FakeCategory(1, "Axi")
    active = FakeCategory(2, "Active")
    killed = FakeCategory(3, "Killed")
    guild = FakeGuild([axi, active, killed])

    with patch.object(channels, "_bot", FakeBot(guild)), \
         patch.object(channels.config, "DISCORD_GUILD_ID", 999), \
         patch.object(channels.config, "COMBINE_LIVE_CATEGORIES", False), \
         patch.object(channels.config, "AXI_CATEGORY_NAME", "Axi"), \
         patch.object(channels.config, "ACTIVE_CATEGORY_NAME", "Active"), \
         patch.object(channels.config, "KILLED_CATEGORY_NAME", "Killed"), \
         patch.object(channels.config, "COMBINED_CATEGORY_NAME", "Axi"), \
         patch.object(channels.config, "ALLOWED_USER_IDS", []):
        await channels.ensure_guild_infrastructure()

    assert [c.name for c in channels.axi_categories] == ["Axi"]
    assert [c.name for c in channels.active_categories] == ["Active"]
    assert [c.name for c in channels.killed_categories] == ["Killed"]
    guild.create_category.assert_not_called()


@pytest.mark.asyncio
async def test_combine_mode_empty_legacy_categories_are_deleted() -> None:
    """Combined mode with empty pre-existing legacy 'Axi'+'Active' — migration deletes them."""
    _reset_channel_state()
    legacy_axi = FakeCategory(1, "Axi")
    legacy_active = FakeCategory(2, "Active")
    killed = FakeCategory(3, "Killed")
    guild = FakeGuild([legacy_axi, legacy_active, killed])

    with patch.object(channels, "_bot", FakeBot(guild)), \
         patch.object(channels.config, "DISCORD_GUILD_ID", 999), \
         patch.object(channels.config, "COMBINE_LIVE_CATEGORIES", True), \
         patch.object(channels.config, "AXI_CATEGORY_NAME", "Axi"), \
         patch.object(channels.config, "ACTIVE_CATEGORY_NAME", "Active"), \
         patch.object(channels.config, "KILLED_CATEGORY_NAME", "Killed"), \
         patch.object(channels.config, "COMBINED_CATEGORY_NAME", "Combined"), \
         patch.object(channels.config, "ALLOWED_USER_IDS", []):
        await channels.ensure_guild_infrastructure()

    # Combined is created and is the only axi category — empty legacy cats are deleted.
    names = [c.name for c in channels.axi_categories]
    assert names == ["Combined"], f"Expected only 'Combined' after migration, got {names}"

    # combined_categories holds the strict move target.
    assert [c.name for c in channels.combined_categories] == ["Combined"]

    # Empty legacy cats were deleted.
    legacy_axi.delete.assert_awaited_once()
    legacy_active.delete.assert_awaited_once()

    # Active should NOT have been created independently.
    assert channels.active_categories == []

    # Only Combined was created; legacy Axi/Active were discovered + deleted, not recreated.
    created_names = [call.args[0] for call in guild.create_category.call_args_list]
    assert "Combined" in created_names
    assert "Axi" not in created_names
    assert "Active" not in created_names


@pytest.mark.asyncio
async def test_combine_mode_alias_no_double_discovery() -> None:
    """When COMBINED_CATEGORY_NAME == AXI_CATEGORY_NAME, don't double-count."""
    _reset_channel_state()
    axi = FakeCategory(1, "Axi")
    killed = FakeCategory(2, "Killed")
    guild = FakeGuild([axi, killed])

    with patch.object(channels, "_bot", FakeBot(guild)), \
         patch.object(channels.config, "DISCORD_GUILD_ID", 999), \
         patch.object(channels.config, "COMBINE_LIVE_CATEGORIES", True), \
         patch.object(channels.config, "AXI_CATEGORY_NAME", "Axi"), \
         patch.object(channels.config, "ACTIVE_CATEGORY_NAME", "Active"), \
         patch.object(channels.config, "KILLED_CATEGORY_NAME", "Killed"), \
         patch.object(channels.config, "COMBINED_CATEGORY_NAME", "Axi"), \
         patch.object(channels.config, "ALLOWED_USER_IDS", []):
        await channels.ensure_guild_infrastructure()

    axi_ids = [c.id for c in channels.axi_categories]
    assert axi_ids.count(axi.id) == 1, f"Axi category listed more than once: {axi_ids}"
    assert len(channels.axi_categories) == 1, "Expected only one axi category when aliased"


@pytest.mark.asyncio
async def test_combine_mode_fresh_install_only_creates_combined() -> None:
    """Combined mode with no pre-existing categories — only Combined + Killed created."""
    _reset_channel_state()
    guild = FakeGuild([])

    with patch.object(channels, "_bot", FakeBot(guild)), \
         patch.object(channels.config, "DISCORD_GUILD_ID", 999), \
         patch.object(channels.config, "COMBINE_LIVE_CATEGORIES", True), \
         patch.object(channels.config, "AXI_CATEGORY_NAME", "Axi"), \
         patch.object(channels.config, "ACTIVE_CATEGORY_NAME", "Active"), \
         patch.object(channels.config, "KILLED_CATEGORY_NAME", "Killed"), \
         patch.object(channels.config, "COMBINED_CATEGORY_NAME", "Combined"), \
         patch.object(channels.config, "ALLOWED_USER_IDS", []):
        await channels.ensure_guild_infrastructure()

    created_names = [call.args[0] for call in guild.create_category.call_args_list]
    assert created_names == ["Combined", "Killed"]
    assert [c.name for c in channels.axi_categories] == ["Combined"]
    assert channels.active_categories == []


@pytest.mark.asyncio
async def test_combine_mode_legacy_active_only() -> None:
    """Combined mode with only legacy 'Active' (empty) — migration deletes it."""
    _reset_channel_state()
    legacy_active = FakeCategory(1, "Active")
    killed = FakeCategory(2, "Killed")
    guild = FakeGuild([legacy_active, killed])

    with patch.object(channels, "_bot", FakeBot(guild)), \
         patch.object(channels.config, "DISCORD_GUILD_ID", 999), \
         patch.object(channels.config, "COMBINE_LIVE_CATEGORIES", True), \
         patch.object(channels.config, "AXI_CATEGORY_NAME", "Axi"), \
         patch.object(channels.config, "ACTIVE_CATEGORY_NAME", "Active"), \
         patch.object(channels.config, "KILLED_CATEGORY_NAME", "Killed"), \
         patch.object(channels.config, "COMBINED_CATEGORY_NAME", "Combined"), \
         patch.object(channels.config, "ALLOWED_USER_IDS", []):
        await channels.ensure_guild_infrastructure()

    names = [c.name for c in channels.axi_categories]
    assert names == ["Combined"], f"Empty legacy 'Active' should have been deleted; got {names}"
    legacy_active.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_migration_moves_legacy_channels_to_combined() -> None:
    """Pre-existing channels in legacy Axi+Active categories move to Combined on startup."""
    _reset_channel_state()
    legacy_axi = FakeCategory(1, "Axi")
    legacy_active = FakeCategory(2, "Active")
    killed = FakeCategory(3, "Killed")
    # Populate channels in each legacy category.
    master_ch = FakeChannel(10, "axi-master", legacy_axi)
    worker_a = FakeChannel(11, "agent-foo", legacy_axi)
    worker_b = FakeChannel(12, "agent-bar", legacy_active)
    worker_c = FakeChannel(13, "agent-baz", legacy_active)
    guild = FakeGuild([legacy_axi, legacy_active, killed])

    with patch.object(channels, "_bot", FakeBot(guild)), \
         patch.object(channels.config, "DISCORD_GUILD_ID", 999), \
         patch.object(channels.config, "COMBINE_LIVE_CATEGORIES", True), \
         patch.object(channels.config, "AXI_CATEGORY_NAME", "Axi"), \
         patch.object(channels.config, "ACTIVE_CATEGORY_NAME", "Active"), \
         patch.object(channels.config, "KILLED_CATEGORY_NAME", "Killed"), \
         patch.object(channels.config, "COMBINED_CATEGORY_NAME", "Combined"), \
         patch.object(channels.config, "ALLOWED_USER_IDS", []):
        await channels.ensure_guild_infrastructure()

    # Combined was created.
    combined = next(c for c in guild.categories if c.name == "Combined")

    # Every channel moved to Combined.
    for ch in (master_ch, worker_a, worker_b, worker_c):
        ch.move.assert_awaited()
        assert ch.category_id == combined.id, f"#{ch.name} should be in Combined, got {ch.category_id}"
        # sync_permissions=True on every move (inherits combined perms).
        last_call = ch.move.await_args_list[-1]
        assert last_call.kwargs.get("sync_permissions") is True, (
            f"#{ch.name} move missing sync_permissions=True: {last_call}"
        )

    # Combined now contains all 4 channels.
    assert {c.id for c in combined.text_channels} == {10, 11, 12, 13}

    # Legacy cats are empty and got deleted.
    assert legacy_axi.text_channels == []
    assert legacy_active.text_channels == []
    legacy_axi.delete.assert_awaited_once()
    legacy_active.delete.assert_awaited_once()

    # axi_categories ends up [Combined] only; combined_categories is strict target.
    assert [c.name for c in channels.axi_categories] == ["Combined"]
    assert [c.name for c in channels.combined_categories] == ["Combined"]


@pytest.mark.asyncio
async def test_startup_migration_deletes_empty_legacy_categories() -> None:
    """After channels are moved out, empty legacy categories are deleted."""
    _reset_channel_state()
    legacy_axi = FakeCategory(1, "Axi")
    legacy_active = FakeCategory(2, "Active")
    killed = FakeCategory(3, "Killed")
    # One channel in each legacy cat — both cats end up empty after migration.
    FakeChannel(100, "agent-one", legacy_axi)
    FakeChannel(101, "agent-two", legacy_active)
    guild = FakeGuild([legacy_axi, legacy_active, killed])

    with patch.object(channels, "_bot", FakeBot(guild)), \
         patch.object(channels.config, "DISCORD_GUILD_ID", 999), \
         patch.object(channels.config, "COMBINE_LIVE_CATEGORIES", True), \
         patch.object(channels.config, "AXI_CATEGORY_NAME", "Axi"), \
         patch.object(channels.config, "ACTIVE_CATEGORY_NAME", "Active"), \
         patch.object(channels.config, "KILLED_CATEGORY_NAME", "Killed"), \
         patch.object(channels.config, "COMBINED_CATEGORY_NAME", "Combined"), \
         patch.object(channels.config, "ALLOWED_USER_IDS", []):
        await channels.ensure_guild_infrastructure()

    legacy_axi.delete.assert_awaited_once()
    legacy_active.delete.assert_awaited_once()
    # Neither legacy cat is in axi_categories post-migration.
    names = [c.name for c in channels.axi_categories]
    assert "Axi" not in names
    assert "Active" not in names


@pytest.mark.asyncio
async def test_startup_migration_preserves_combined_category() -> None:
    """Combined category itself is never deleted during migration, even if pre-existing."""
    _reset_channel_state()
    # Combined already exists with a channel in it (e.g. previous combined run).
    combined = FakeCategory(1, "Combined")
    FakeChannel(10, "axi-master", combined)
    legacy_axi = FakeCategory(2, "Axi")
    FakeChannel(11, "agent-foo", legacy_axi)
    killed = FakeCategory(3, "Killed")
    guild = FakeGuild([combined, legacy_axi, killed])

    with patch.object(channels, "_bot", FakeBot(guild)), \
         patch.object(channels.config, "DISCORD_GUILD_ID", 999), \
         patch.object(channels.config, "COMBINE_LIVE_CATEGORIES", True), \
         patch.object(channels.config, "AXI_CATEGORY_NAME", "Axi"), \
         patch.object(channels.config, "ACTIVE_CATEGORY_NAME", "Active"), \
         patch.object(channels.config, "KILLED_CATEGORY_NAME", "Killed"), \
         patch.object(channels.config, "COMBINED_CATEGORY_NAME", "Combined"), \
         patch.object(channels.config, "ALLOWED_USER_IDS", []):
        await channels.ensure_guild_infrastructure()

    # Combined was NOT deleted.
    combined.delete.assert_not_called()
    # Legacy was deleted after its channel moved.
    legacy_axi.delete.assert_awaited_once()
    # Combined still contains master (pre-existing) + migrated channel.
    assert {c.id for c in combined.text_channels} == {10, 11}
    assert [c.name for c in channels.combined_categories] == ["Combined"]


@pytest.mark.asyncio
async def test_combined_mode_new_channel_goes_to_combined() -> None:
    """After migration, combined_categories is the strict target — not legacy."""
    _reset_channel_state()
    # Fresh install — no legacy.
    guild = FakeGuild([])

    with patch.object(channels, "_bot", FakeBot(guild)), \
         patch.object(channels.config, "DISCORD_GUILD_ID", 999), \
         patch.object(channels.config, "COMBINE_LIVE_CATEGORIES", True), \
         patch.object(channels.config, "AXI_CATEGORY_NAME", "Axi"), \
         patch.object(channels.config, "ACTIVE_CATEGORY_NAME", "Active"), \
         patch.object(channels.config, "KILLED_CATEGORY_NAME", "Killed"), \
         patch.object(channels.config, "COMBINED_CATEGORY_NAME", "Combined"), \
         patch.object(channels.config, "ALLOWED_USER_IDS", []):
        await channels.ensure_guild_infrastructure()

    # combined_categories == axi_categories == [Combined] in fresh install.
    assert [c.name for c in channels.combined_categories] == ["Combined"]
    assert [c.name for c in channels.axi_categories] == ["Combined"]
    # combined_categories is a strict subset that ensure_agent_channel uses as target.
    # ensure_agent_channel uses target_group = combined_categories in combined mode,
    # so new channels will only ever be created in Combined (or its overflow).


@pytest.mark.asyncio
async def test_combined_mode_overflow_to_second_combined_when_full() -> None:
    """When migration source has > 50 channels, overflow 'Combined 2' is created."""
    _reset_channel_state()
    legacy_axi = FakeCategory(1, "Axi")
    # Put 60 channels in legacy to force overflow when migrating into Combined.
    for i in range(60):
        FakeChannel(1000 + i, f"agent-{i:02d}", legacy_axi)
    killed = FakeCategory(2, "Killed")
    guild = FakeGuild([legacy_axi, killed])

    with patch.object(channels, "_bot", FakeBot(guild)), \
         patch.object(channels.config, "DISCORD_GUILD_ID", 999), \
         patch.object(channels.config, "COMBINE_LIVE_CATEGORIES", True), \
         patch.object(channels.config, "AXI_CATEGORY_NAME", "Axi"), \
         patch.object(channels.config, "ACTIVE_CATEGORY_NAME", "Active"), \
         patch.object(channels.config, "KILLED_CATEGORY_NAME", "Killed"), \
         patch.object(channels.config, "COMBINED_CATEGORY_NAME", "Combined"), \
         patch.object(channels.config, "ALLOWED_USER_IDS", []):
        await channels.ensure_guild_infrastructure()

    # Combined AND Combined 2 exist — primary hit 50, overflow took the rest.
    names = [c.name for c in channels.combined_categories]
    assert names == ["Combined", "Combined 2"], f"Expected overflow; got {names}"

    combined = channels.combined_categories[0]
    combined_2 = channels.combined_categories[1]
    assert len(combined.text_channels) == 50, (
        f"Primary should be full at 50; got {len(combined.text_channels)}"
    )
    assert len(combined_2.text_channels) == 10, (
        f"Overflow should hold remaining 10; got {len(combined_2.text_channels)}"
    )

    # Legacy was migrated empty and deleted.
    assert legacy_axi.text_channels == []
    legacy_axi.delete.assert_awaited_once()

    # axi_categories includes overflow too (for lookup).
    assert [c.name for c in channels.axi_categories] == ["Combined", "Combined 2"]


# ---------------------------------------------------------------------------
# Category ownership (0f85e12) — a shared guild holds more than one Axi instance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_foreign_categories_are_neither_adopted_nor_deleted() -> None:
    """Another instance's categories must be invisible to this bot.

    Before 0f85e12 a second Axi in the same guild would adopt the first's
    categories, move its channels and delete its emptied categories.
    """
    foreign_axi = FakeCategory(1, "Axi", owner_id=None)
    foreign_chan = FakeChannel(10, "axi-master", foreign_axi)
    guild = FakeGuild([foreign_axi])

    with patch.object(channels, "_bot", FakeBot(guild)), \
         patch.object(channels.config, "DISCORD_GUILD_ID", 999), \
         patch.object(channels.config, "COMBINE_LIVE_CATEGORIES", False), \
         patch.object(channels.config, "AXI_CATEGORY_NAME", "Axi"), \
         patch.object(channels.config, "ACTIVE_CATEGORY_NAME", "Active"), \
         patch.object(channels.config, "KILLED_CATEGORY_NAME", "Killed"), \
         patch.object(channels.config, "ALLOWED_USER_IDS", []):
        await channels.ensure_guild_infrastructure()

    foreign_axi.delete.assert_not_awaited()
    assert foreign_chan in foreign_axi.text_channels, "another instance's channel was moved"
    assert foreign_axi not in channels.axi_categories, "adopted a foreign category"
    # It made its own 'Axi' instead of taking the existing one.
    created = [c.args[0] for c in guild.create_category.call_args_list]
    assert "Axi" in created


@pytest.mark.asyncio
async def test_own_categories_are_still_adopted() -> None:
    """The ownership check must not orphan the bot from its own categories."""
    own_axi = FakeCategory(1, "Axi")
    own_active = FakeCategory(2, "Active")
    own_killed = FakeCategory(3, "Killed")
    guild = FakeGuild([own_axi, own_active, own_killed])

    with patch.object(channels, "_bot", FakeBot(guild)), \
         patch.object(channels.config, "DISCORD_GUILD_ID", 999), \
         patch.object(channels.config, "COMBINE_LIVE_CATEGORIES", False), \
         patch.object(channels.config, "AXI_CATEGORY_NAME", "Axi"), \
         patch.object(channels.config, "ACTIVE_CATEGORY_NAME", "Active"), \
         patch.object(channels.config, "KILLED_CATEGORY_NAME", "Killed"), \
         patch.object(channels.config, "ALLOWED_USER_IDS", []):
        await channels.ensure_guild_infrastructure()

    guild.create_category.assert_not_called()
    assert own_axi in channels.axi_categories


@pytest.mark.asyncio
async def test_mixed_guild_adopts_only_our_own() -> None:
    """Both instances present: take ours, leave theirs alone."""
    theirs = FakeCategory(1, "Axi", owner_id=None)
    theirs_chan = FakeChannel(10, "axi-master", theirs)
    ours = FakeCategory(2, "Axi")
    ours_chan = FakeChannel(11, "axi-master", ours)
    guild = FakeGuild([theirs, ours, FakeCategory(3, "Active"), FakeCategory(4, "Killed")])

    with patch.object(channels, "_bot", FakeBot(guild)), \
         patch.object(channels.config, "DISCORD_GUILD_ID", 999), \
         patch.object(channels.config, "COMBINE_LIVE_CATEGORIES", False), \
         patch.object(channels.config, "AXI_CATEGORY_NAME", "Axi"), \
         patch.object(channels.config, "ACTIVE_CATEGORY_NAME", "Active"), \
         patch.object(channels.config, "KILLED_CATEGORY_NAME", "Killed"), \
         patch.object(channels.config, "ALLOWED_USER_IDS", []):
        await channels.ensure_guild_infrastructure()

    assert ours in channels.axi_categories
    assert theirs not in channels.axi_categories
    theirs.delete.assert_not_awaited()
    assert theirs_chan in theirs.text_channels, "their axi-master was touched"
    assert ours_chan in ours.text_channels
