# Namespace Isolation for Multi-Bot Guild Sharing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each bot instance a `BOT_NAMESPACE` that prefixes its channel and category names, so multiple bots can share one Discord guild without cross-interference.

**Architecture:** Add a required `BOT_NAMESPACE` config value (`off` = legacy no-prefix mode). A naming layer in `axi/channels.py` derives on-Discord names (`namespaced_channel_name`, `parse_agent_from_channel_name`, `_category_name`); `normalize_channel_name` stays pure. Ownership = the bot's category lists (namespaced-only after `ensure_guild_infrastructure`) plus a name-parse predicate used as defense-in-depth in `on_message`, reconstruction, and channel-create auto-registration. Test harness lookups become namespace-aware via the `Discord` helper.

**Tech Stack:** Python 3.14, discord.py, discordquery, pytest, uv.

## Global Constraints

- `BOT_NAMESPACE` is REQUIRED: unset/empty → `ValueError("BOT_NAMESPACE is required. Set it to 'off' for un-namespaced (legacy) mode, or a lowercase alphanumeric+hyphen value.")`.
- Reserved keyword: `off` = no prefix (legacy mode). Any other value must match `^[a-z0-9][a-z0-9-]*$` and be ≤ 20 chars, else `ValueError`.
- `normalize_channel_name()` MUST stay pure (property-tested for idempotency — do not prefix inside it).
- Session names (`config.MASTER_AGENT_NAME == "axi-master"`, `session.name`) are NEVER prefixed — only on-Discord channel/category names.
- `#readme` is shared and un-namespaced — `sync_readme_channel()` is untouched.
- All callers of `parse_agent_from_channel_name` MUST pass the status-prefix-stripped name (`strip_status_prefix(ch.name)` first) — status renames produce `🔴dev-axi-master`.
- `bot_creating_channels` (the channel-create guard set) MUST hold namespaced channel names, or bot-created channels get auto-registered as new agents.
- `tests/unit/conftest.py` (Task 1) is load-bearing: `axi.config` validates `BOT_NAMESPACE` at import time and `load_dotenv()` does not override already-set vars, so the conftest's `setdefault` is what keeps every unit test importable.

---

### Task 1: Config — BOT_NAMESPACE validation + unit-test env conftest

**Files:**
- Modify: `axi/config.py` (add `NAMESPACE_OFF`, `_validate_namespace`, `BOT_NAMESPACE`; add both names to `__all__`)
- Create: `tests/unit/conftest.py`
- Modify: `.env.template`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `config.BOT_NAMESPACE: str` (module constant, validated at import), `config.NAMESPACE_OFF = "off"` (both exported in `__all__`). Later tasks read `config.BOT_NAMESPACE` at call time (patchable via `patch.object(config, "BOT_NAMESPACE", ...)`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_config.py`:

```python
from axi import config


class TestValidateNamespace:
    def test_off_allowed(self) -> None:
        assert config._validate_namespace("off") == "off"

    def test_valid_namespace(self) -> None:
        assert config._validate_namespace("dev") == "dev"

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError, match="BOT_NAMESPACE is required"):
            config._validate_namespace("")

    def test_too_long_rejected(self) -> None:
        with pytest.raises(ValueError, match="too long"):
            config._validate_namespace("a" * 21)

    @pytest.mark.parametrize("bad", ["Dev", "dev_1", "-dev", "dev-", "dev name", "déjà"])
    def test_invalid_chars_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError, match="lowercase alphanumeric"):
            config._validate_namespace(bad)

    def test_module_constant_defaults_to_off(self) -> None:
        # Set by tests/unit/conftest.py before import.
        assert config.BOT_NAMESPACE == "off"
```

Create `tests/unit/conftest.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_config.py -q`
Expected: FAIL — `ImportError: cannot import name '_validate_namespace'` (and the import of `axi.config` raises `ValueError: BOT_NAMESPACE is required` before the conftest exists).

- [ ] **Step 3: Implement**

In `axi/config.py`, in the "Environment variables" section (near the category constants), add:

```python
# ---------------------------------------------------------------------------
# Namespace (multi-bot guild isolation)
# ---------------------------------------------------------------------------

NAMESPACE_OFF = "off"


def _validate_namespace(ns: str) -> str:
    if not ns:
        raise ValueError(
            "BOT_NAMESPACE is required. Set it to 'off' for un-namespaced "
            "(legacy) mode, or a lowercase alphanumeric+hyphen value."
        )
    if ns == NAMESPACE_OFF:
        return ns
    if len(ns) > 20:
        raise ValueError(f"BOT_NAMESPACE too long ({len(ns)} chars, max 20)")
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", ns):
        raise ValueError("BOT_NAMESPACE must be lowercase alphanumeric + hyphens only")
    return ns


BOT_NAMESPACE = _validate_namespace(os.environ.get("BOT_NAMESPACE", ""))
```

Add `"BOT_NAMESPACE"` and `"NAMESPACE_OFF"` to `__all__` (alphabetical: `BOT_NAMESPACE` between `BOT_DIR` and `BOT_WORKTREES_DIR`; `NAMESPACE_OFF` between `MCP_SERVERS_PATH` and `QUERY_TIMEOUT`). `re` is already imported in `config.py`.

Add to `.env.template` (near the top, after `DISCORD_GUILD_ID`):

```
# Required: namespace prefix for this instance's channels and categories.
# 'off' = legacy un-namespaced mode. Otherwise a lowercase alphanumeric+hyphen
# value (max 20 chars), e.g. BOT_NAMESPACE=dev. Bots sharing a guild must use
# different namespaces.
BOT_NAMESPACE=off
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_config.py -q`
Expected: PASS (all tests including the new `TestValidateNamespace`).

- [ ] **Step 5: Sanity-check the full unit suite still imports**

Run: `uv run pytest tests/unit -q --co -q`
Expected: collection succeeds with no import errors (BOT_NAMESPACE=off now set for every unit test).

- [ ] **Step 6: Commit**

```bash
git add axi/config.py tests/unit/conftest.py tests/unit/test_config.py .env.template
git commit -m "feat: add required BOT_NAMESPACE config (off = legacy mode)"
```

---

### Task 2: Naming layer in channels.py

**Files:**
- Modify: `axi/channels.py` (add three functions in the "Pure helpers" section, after `normalize_channel_name`)
- Test: create `tests/unit/test_channel_namespace.py`

**Interfaces:**
- Consumes: `config.BOT_NAMESPACE`, `config.NAMESPACE_OFF`, `normalize_channel_name` (unchanged).
- Produces:
  - `namespaced_channel_name(agent_name: str) -> str` — Discord channel name for an agent (`"{ns}-{normalized}"`, bare when `off`).
  - `parse_agent_from_channel_name(namespaced: str) -> str | None` — stripped agent name, or `None` when not ours (foreign name, or bare prefix like `"dev-"`); returns input unchanged in `off` mode.
  - `_category_name(base: str) -> str` — on-Discord category name (`"{ns}-{base}"`, bare when `off`).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_channel_namespace.py`:

```python
"""Unit tests for the namespace naming layer (channels.py)."""

from __future__ import annotations

from unittest.mock import patch

from axi import channels, config


class TestNamespacedChannelName:
    def test_off_mode(self) -> None:
        with patch.object(config, "BOT_NAMESPACE", "off"):
            assert channels.namespaced_channel_name("My Agent") == "my-agent"

    def test_namespaced(self) -> None:
        with patch.object(config, "BOT_NAMESPACE", "dev"):
            assert channels.namespaced_channel_name("My Agent") == "dev-my-agent"

    def test_master(self) -> None:
        with patch.object(config, "BOT_NAMESPACE", "dev"):
            assert channels.namespaced_channel_name(config.MASTER_AGENT_NAME) == "dev-axi-master"


class TestParseAgentFromChannelName:
    def test_off_mode_identity(self) -> None:
        with patch.object(config, "BOT_NAMESPACE", "off"):
            assert channels.parse_agent_from_channel_name("axi-master") == "axi-master"

    def test_namespaced(self) -> None:
        with patch.object(config, "BOT_NAMESPACE", "dev"):
            assert channels.parse_agent_from_channel_name("dev-axi-master") == "axi-master"

    def test_foreign_namespace(self) -> None:
        with patch.object(config, "BOT_NAMESPACE", "dev"):
            assert channels.parse_agent_from_channel_name("prod-axi-master") is None

    def test_bare_prefix_returns_none(self) -> None:
        with patch.object(config, "BOT_NAMESPACE", "dev"):
            assert channels.parse_agent_from_channel_name("dev-") is None

    def test_roundtrip(self) -> None:
        with patch.object(config, "BOT_NAMESPACE", "dev"):
            assert (
                channels.parse_agent_from_channel_name(
                    channels.namespaced_channel_name("agent-v2")
                )
                == "agent-v2"
            )


class TestCategoryName:
    def test_off_mode(self) -> None:
        with patch.object(config, "BOT_NAMESPACE", "off"):
            assert channels._category_name("Axi") == "Axi"

    def test_namespaced(self) -> None:
        with patch.object(config, "BOT_NAMESPACE", "dev"):
            assert channels._category_name("Axi") == "dev-Axi"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_channel_namespace.py -q`
Expected: FAIL with `AttributeError: module 'axi.channels' has no attribute 'namespaced_channel_name'`.

- [ ] **Step 3: Implement**

In `axi/channels.py`, immediately after `normalize_channel_name()`:

```python
def namespaced_channel_name(agent_name: str) -> str:
    """Discord channel name for an agent: '{ns}-{normalized}', or bare when BOT_NAMESPACE=off."""
    base = normalize_channel_name(agent_name)
    if config.BOT_NAMESPACE == config.NAMESPACE_OFF:
        return base
    return f"{config.BOT_NAMESPACE}-{base}"


def parse_agent_from_channel_name(namespaced: str) -> str | None:
    """Recover the agent name by stripping the '{ns}-' prefix.

    Returns None when the name is not in this bot's namespace or is exactly
    the bare prefix ('dev-' with empty remainder). When BOT_NAMESPACE=off,
    returns ``namespaced`` unchanged (no prefix to strip).
    """
    if config.BOT_NAMESPACE == config.NAMESPACE_OFF:
        return namespaced
    prefix = f"{config.BOT_NAMESPACE}-"
    if namespaced.startswith(prefix) and len(namespaced) > len(prefix):
        return namespaced[len(prefix):]
    return None


def _category_name(base: str) -> str:
    """On-Discord category name: '{ns}-{base}', or bare when BOT_NAMESPACE=off."""
    if config.BOT_NAMESPACE == config.NAMESPACE_OFF:
        return base
    return f"{config.BOT_NAMESPACE}-{base}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_channel_namespace.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add axi/channels.py tests/unit/test_channel_namespace.py
git commit -m "feat: namespace naming layer (namespaced_channel_name, parse_agent_from_channel_name, _category_name)"
```

---

### Task 3: channels.py call-site switch — channel names

**Files:**
- Modify: `axi/channels.py` — `ensure_agent_channel` (~line 532), `move_channel_to_killed` (~621), `get_agent_channel` (~657), `deduplicate_master_channel` (~673), `position_agent_channel_top` (~746), `ensure_master_channel_position` (~820), `_do_reorder` (~903).

**Interfaces:**
- Consumes: `namespaced_channel_name` (Task 2).
- Produces: all on-Discord channel names derived inside `channels.py` are namespaced. No signature changes.

- [ ] **Step 1: Make the edits**

Replace every channel-name derivation in `channels.py`. Each site currently computes `normalized = normalize_channel_name(X)` and then uses it as the on-Discord channel name; switch the derivation to `namespaced_channel_name(X)`:

1. `ensure_agent_channel`: `normalized = normalize_channel_name(agent_name)` → `normalized = namespaced_channel_name(agent_name)`
2. `move_channel_to_killed`: same replacement for `normalized = normalize_channel_name(agent_name)` (the `_match_channel_name(ch.name, normalized)` comparisons and `ch.edit(name=normalized)` then use the namespaced name).
3. `get_agent_channel`: same replacement.
4. `deduplicate_master_channel`: `normalized = normalize_channel_name(config.MASTER_AGENT_NAME)` → `normalized = namespaced_channel_name(config.MASTER_AGENT_NAME)`
5. `position_agent_channel_top`: `master_normalized = normalize_channel_name(config.MASTER_AGENT_NAME)` → `master_normalized = namespaced_channel_name(config.MASTER_AGENT_NAME)`
6. `ensure_master_channel_position`: `normalized = normalize_channel_name(config.MASTER_AGENT_NAME)` → `normalized = namespaced_channel_name(config.MASTER_AGENT_NAME)`
7. `_do_reorder`: `master_normalized = normalize_channel_name(config.MASTER_AGENT_NAME)` → `master_normalized = namespaced_channel_name(config.MASTER_AGENT_NAME)`

Do NOT touch `normalize_channel_name` itself, `_match_channel_name`, or `strip_status_prefix`.

- [ ] **Step 2: Verify no channel-name derivations remain**

Run: `grep -n "normalize_channel_name(" axi/channels.py`
Expected: only the function definition, `namespaced_channel_name`'s internal call, and the module docstring. No other call sites.

- [ ] **Step 3: Verify compilation and existing tests**

Run: `uv run python -m compileall -q axi/channels.py && uv run pytest tests/unit/test_channel_namespace.py tests/unit/test_agents.py tests/unit/test_channels_combine_migration.py -q`
Expected: PASS (BOT_NAMESPACE=off from the conftest keeps existing behavior identical).

- [ ] **Step 4: Commit**

```bash
git add axi/channels.py
git commit -m "feat: derive channel names via namespaced_channel_name in channels.py"
```

---

### Task 4: Namespaced category infrastructure

**Files:**
- Modify: `axi/channels.py` — `ensure_guild_infrastructure` (~317-470), `_get_category_with_room` (~204).
- Test: `tests/unit/test_channels_combine_migration.py` (add namespaced cases).

**Interfaces:**
- Consumes: `_category_name` (Task 2).
- Produces: `axi_categories` / `active_categories` / `killed_categories` / `combined_categories` contain only this namespace's categories when `BOT_NAMESPACE != "off"`. Overflow names get the prefix (`"dev-Axi 2"`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_channels_combine_migration.py` (reuses the existing `FakeGuild`/`FakeCategory`/`FakeBot`/`_reset_channel_state` helpers):

```python
@pytest.mark.asyncio
async def test_namespaced_categories_created() -> None:
    """Namespaced mode creates '{ns}-Axi/Active/Killed' categories."""
    _reset_channel_state()
    guild = FakeGuild([])
    with patch.object(channels, "_bot", FakeBot(guild)), \
         patch.object(channels.config, "DISCORD_GUILD_ID", 999), \
         patch.object(channels.config, "COMBINE_LIVE_CATEGORIES", False), \
         patch.object(channels.config, "AXI_CATEGORY_NAME", "Axi"), \
         patch.object(channels.config, "ACTIVE_CATEGORY_NAME", "Active"), \
         patch.object(channels.config, "KILLED_CATEGORY_NAME", "Killed"), \
         patch.object(channels.config, "COMBINED_CATEGORY_NAME", "Axi"), \
         patch.object(channels.config, "ALLOWED_USER_IDS", []), \
         patch.object(channels.config, "BOT_NAMESPACE", "dev"):
        await channels.ensure_guild_infrastructure()

    assert [c.name for c in channels.axi_categories] == ["dev-Axi"]
    assert [c.name for c in channels.active_categories] == ["dev-Active"]
    assert [c.name for c in channels.killed_categories] == ["dev-Killed"]
    assert guild.create_category.call_count == 3


@pytest.mark.asyncio
async def test_namespaced_discovers_existing() -> None:
    """Namespaced mode discovers existing prefixed categories without creating."""
    _reset_channel_state()
    axi = FakeCategory(1, "dev-Axi")
    active = FakeCategory(2, "dev-Active")
    killed = FakeCategory(3, "dev-Killed")
    guild = FakeGuild([axi, active, killed])
    with patch.object(channels, "_bot", FakeBot(guild)), \
         patch.object(channels.config, "DISCORD_GUILD_ID", 999), \
         patch.object(channels.config, "COMBINE_LIVE_CATEGORIES", False), \
         patch.object(channels.config, "AXI_CATEGORY_NAME", "Axi"), \
         patch.object(channels.config, "ACTIVE_CATEGORY_NAME", "Active"), \
         patch.object(channels.config, "KILLED_CATEGORY_NAME", "Killed"), \
         patch.object(channels.config, "COMBINED_CATEGORY_NAME", "Axi"), \
         patch.object(channels.config, "ALLOWED_USER_IDS", []), \
         patch.object(channels.config, "BOT_NAMESPACE", "dev"):
        await channels.ensure_guild_infrastructure()

    assert [c.name for c in channels.axi_categories] == ["dev-Axi"]
    assert [c.name for c in channels.active_categories] == ["dev-Active"]
    assert [c.name for c in channels.killed_categories] == ["dev-Killed"]
    guild.create_category.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_channels_combine_migration.py -q`
Expected: the two new tests FAIL (categories are created/discovered as `"Axi"`, not `"dev-Axi"`); existing tests PASS.

- [ ] **Step 3: Implement**

In `ensure_guild_infrastructure`, wrap every on-Discord category base name with `_category_name`:

1. In the combined-mode `group_map`, wrap the names:
   ```python
   group_map: list[tuple[str, list[tuple[int, CategoryChannel]], bool]] = [
       (_category_name(config.COMBINED_CATEGORY_NAME), axi_found, True),
   ]
   legacy_names: list[str] = []
   if config.AXI_CATEGORY_NAME != config.COMBINED_CATEGORY_NAME:
       legacy_names.append(_category_name(config.AXI_CATEGORY_NAME))
   if (
       config.ACTIVE_CATEGORY_NAME != config.COMBINED_CATEGORY_NAME
       and config.ACTIVE_CATEGORY_NAME != config.AXI_CATEGORY_NAME
   ):
       legacy_names.append(_category_name(config.ACTIVE_CATEGORY_NAME))
   group_map.extend((name, axi_found, False) for name in legacy_names)
   group_map.append((_category_name(config.KILLED_CATEGORY_NAME), killed_found, True))
   ```
2. In the non-combined `group_map`:
   ```python
   group_map = [
       (_category_name(config.AXI_CATEGORY_NAME), axi_found, True),
       (_category_name(config.ACTIVE_CATEGORY_NAME), active_found, True),
       (_category_name(config.KILLED_CATEGORY_NAME), killed_found, True),
   ]
   ```
3. The killed-overwrites selection compares the group_map base name: `desired = killed_overwrites if base_name == config.KILLED_CATEGORY_NAME else overwrites` → `desired = killed_overwrites if base_name == _category_name(config.KILLED_CATEGORY_NAME) else overwrites`.
4. In the combine-categories migration block, replace `_match_category_group(cat.name, config.COMBINED_CATEGORY_NAME)` with `_match_category_group(cat.name, _category_name(config.COMBINED_CATEGORY_NAME))` (both occurrences) and `_get_category_with_room(combined_cats_list, config.COMBINED_CATEGORY_NAME)` with `_get_category_with_room(combined_cats_list, _category_name(config.COMBINED_CATEGORY_NAME))`.

In `_get_category_with_room`, build the overflow name from the prefixed base:

```python
    # All full — create overflow
    next_num = len(categories) + 1
    overflow_name = f"{_category_name(base_name)} {next_num}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_channels_combine_migration.py tests/unit/test_channel_namespace.py -q`
Expected: PASS — existing tests (BOT_NAMESPACE=off) plus the two new namespaced tests.

- [ ] **Step 5: Commit**

```bash
git add axi/channels.py tests/unit/test_channels_combine_migration.py
git commit -m "feat: namespace category discovery/creation in ensure_guild_infrastructure"
```

---

### Task 5: main.py — message filter + channel-create gate + bot_creating_channels

**Files:**
- Modify: `axi/main.py` — `on_message` (after guild-ID check, ~line 301), `on_guild_channel_create` (~2440), `_handle_axi_channel_create` (~2414), `_handle_active_channel_create` (~2390), `_do_spawn` guard add/discard (~1424, ~1433).

**Interfaces:**
- Consumes: `channels.parse_agent_from_channel_name`, `channels.strip_status_prefix`, `channels.namespaced_channel_name` (Task 2).
- Produces: `on_message` ignores foreign-namespace channels (`ignored_other_namespace` metric); manually-created foreign channels in our categories are not claimed; `bot_creating_channels` holds namespaced names.

- [ ] **Step 1: Make the edits**

**Edit 1 — `on_message` ownership filter.** In `on_message`, after the block that checks `message.guild.id != config.DISCORD_GUILD_ID` / `not isinstance(message.channel, TextChannel)` / `message.author.id not in config.ALLOWED_USER_IDS` (i.e. after `channel = message.channel` is assigned, before `channels.mark_channel_active`), insert:

```python
    # Only process channels in this bot's namespace. Category membership is the
    # primary boundary (our channels always parse); the name parse also covers
    # the uncategorized pinned-master case and foreign channels in our category.
    if channels.parse_agent_from_channel_name(channels.strip_status_prefix(channel.name)) is None:
        observe_inbound_discord_event("message", "ignored_other_namespace")
        return
```

**Edit 2 — `bot_creating_channels` namespaced.** In `main.py` (the `/spawn` path), replace:

- `channels.bot_creating_channels.discard(channels.normalize_channel_name(agent_name))` (~1424) → `channels.bot_creating_channels.discard(channels.namespaced_channel_name(agent_name))`
- `channels.bot_creating_channels.add(channels.normalize_channel_name(agent_name))` (~1433) → `channels.bot_creating_channels.add(channels.namespaced_channel_name(agent_name))`

**Edit 3 — `on_guild_channel_create` gate.** Replace the handler body's channel-name logic:

```python
@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel) -> None:
    """Auto-register agent when a user manually creates a channel in Axi or Active category."""
    if not isinstance(channel, discord.TextChannel):
        return
    if channel.name in channels.bot_creating_channels:
        return
    agent_name = channels.parse_agent_from_channel_name(channels.strip_status_prefix(channel.name))
    if agent_name is None or agent_name == config.MASTER_AGENT_NAME:
        return  # foreign-namespace channel or master — not claimable

    if channels.is_axi_channel(channel):
        await _handle_axi_channel_create(channel)
    elif channels.is_active_channel(channel):
        await _handle_active_channel_create(channel)

    # Ensure master stays at top after any channel creation
    if _startup_complete:
        try:
            await channels.ensure_master_channel_position()
        except Exception:
            log.exception("Failed to re-enforce master channel position after channel create")
```

**Edit 4 — handlers use the parsed name.** In `_handle_axi_channel_create` and `_handle_active_channel_create`, replace `agent_name = channel.name` with:

```python
    agent_name = channels.parse_agent_from_channel_name(channels.strip_status_prefix(channel.name))
    if agent_name is None:
        return
```

(the gate in `on_guild_channel_create` already guarantees non-None; this keeps the handlers safe as standalone functions).

- [ ] **Step 2: Verify**

Run: `uv run python -m compileall -q axi/main.py`
Expected: compiles. Then confirm no un-namespaced `bot_creating_channels` callers remain:

Run: `grep -n "bot_creating_channels" axi/main.py axi/tools.py axi/agents.py`
Expected: every `.add(` / `.discard(` uses `namespaced_channel_name(...)`; `on_guild_channel_create`'s `channel.name in channels.bot_creating_channels` stays as-is.

- [ ] **Step 3: Commit**

```bash
git add axi/main.py
git commit -m "feat: namespace ownership filter in on_message and channel-create gate"
```

---

### Task 6: agents.py reconstruction + spawn guard, tools.py spawn guard

**Files:**
- Modify: `axi/agents.py` — `reconstruct_agents_from_channels` (~1227-1245), `spawn_agent` (`bot_creating_channels` add, ~1649-1650).
- Modify: `axi/tools.py` — `bot_creating_channels` add/discard in the spawn tool (~310, ~340).

**Interfaces:**
- Consumes: `parse_agent_from_channel_name`, `strip_status_prefix`, `namespaced_channel_name` (Task 2).
- Produces: reconstruction derives agent names via the parse predicate (never `dev-foo`); `bot_creating_channels` holds namespaced names everywhere.

- [ ] **Step 1: Make the edits**

**Edit 1 — `reconstruct_agents_from_channels`.** Replace the per-channel loop head (currently ~1235-1240):

```python
    for cat in categories:
        for ch in cat.text_channels:
            agent_name = _channels_mod.parse_agent_from_channel_name(
                _channels_mod.strip_status_prefix(ch.name)
            )
            if agent_name is None:
                continue  # foreign channel in our category — silently ignore
            if agent_name == config.MASTER_AGENT_NAME:
                channel_to_agent[ch.id] = config.MASTER_AGENT_NAME
                continue
```

(Delete the old `agent_name = _channels_mod.strip_status_prefix(ch.name) ...` line and the old master comparison `agent_name == normalize_channel_name(config.MASTER_AGENT_NAME)` — the master check now compares the parsed, un-namespaced name. The prompt-substitution `{channel_name}` below keeps using `_channels_mod.strip_status_prefix(ch.name)`.)

**Edit 2 — `spawn_agent` guard.** Replace (~1649-1650):

```python
        normalized = normalize_channel_name(name)
        _channels_mod.bot_creating_channels.add(normalized)
```
with:
```python
        normalized = namespaced_channel_name(name)
        _channels_mod.bot_creating_channels.add(normalized)
```
(`normalized` is used later for `_channels_mod.bot_creating_channels.discard(normalized)` — both now namespaced. The channel created by `ensure_agent_channel` will also be namespaced.)

**Edit 3 — `tools.py` spawn tool.** Replace `channels.bot_creating_channels.add(agents.normalize_channel_name(agent_name))` (~340) with `channels.bot_creating_channels.add(agents.namespaced_channel_name(agent_name))`, and the `discard` at ~310 with `agents.namespaced_channel_name(agent_name)`.

- [ ] **Step 2: Verify**

Run: `uv run python -m compileall -q axi/agents.py axi/tools.py`
Expected: compiles.

Run: `uv run pytest tests/unit -q`
Expected: full unit suite PASSES (existing `test_agents*.py` tests unaffected; reconstruction/spawn are not unit-covered — verified by import + compile here, and by the live suite in Task 10).

- [ ] **Step 3: Commit**

```bash
git add axi/agents.py axi/tools.py
git commit -m "feat: namespace-aware reconstruction and spawn guards"
```

---

### Task 7: Test harness — Discord helper, conftest fixture, mock bot

**Files:**
- Modify: `tests/helpers.py` (add `_ns_name`, namespace param to `Discord`)
- Modify: `tests/conftest.py` (`discord` fixture passes namespace)
- Modify: `tests/mock/bot.py` (read `BOT_NAMESPACE`, prefix master/agent channel names)
- Test: create `tests/unit/test_helpers_ns.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (env-driven).
- Produces: `Discord` instances prefix `find_channel` / `find_category` / `find_channel_by_prefix` by `namespace`; mock bot mirrors real-bot naming.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_helpers_ns.py`:

```python
"""Unit tests for the test-harness namespace prefix helper."""

from __future__ import annotations

from tests.helpers import _ns_name


def test_off_mode_identity() -> None:
    assert _ns_name("off", "axi-master") == "axi-master"


def test_namespaced() -> None:
    assert _ns_name("dev", "axi-master") == "dev-axi-master"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_helpers_ns.py -q`
Expected: FAIL with `ImportError: cannot import name '_ns_name'`.

- [ ] **Step 3: Implement**

**`tests/helpers.py`:**

```python
def _ns_name(namespace: str, name: str) -> str:
    """Prefix a channel/category lookup name with the instance namespace."""
    if not namespace or namespace == "off":
        return name
    return f"{namespace}-{name}"
```

Update `Discord`:

```python
class Discord(DiscordE2EClient):
    """Axi-specific test adapter built on the generic Discord E2E client."""

    def __init__(self, bot_token: str, sender_token: str, guild_id: str, namespace: str = "off"):
        super().__init__(reader_token=bot_token, sender_token=sender_token, guild_id=guild_id)
        self._bot = self._reader
        self._sender = self._sender_client
        self.namespace = namespace

    def _ns(self, name: str) -> str:
        return _ns_name(self.namespace, name)

    def find_channel(self, name: str) -> str | None:
        return super().find_channel(self._ns(name))

    def find_category(self, name: str) -> str | None:
        return super().find_category(self._ns(name))

    def find_channel_by_prefix(self, prefix: str) -> dict | None:
        return super().find_channel_by_prefix(self._ns(prefix))
```

**`tests/conftest.py`** — `discord` fixture: pass the instance namespace:

```python
    d = Discord(
        bot_token=bot_token,
        sender_token=sender_token,
        guild_id=guild_id,
        namespace=instance_env.get("BOT_NAMESPACE", "off"),
    )
```

**`tests/mock/bot.py`:**

```python
BOT_NAMESPACE = os.environ.get("BOT_NAMESPACE", "off")


def _ns(name: str) -> str:
    return f"{BOT_NAMESPACE}-{name}" if BOT_NAMESPACE != "off" else name


MASTER_CHANNEL = _ns("axi-master")
README_CHANNEL = "readme"  # shared, un-namespaced
```

And in `_channel_name(raw)`, prefix the result:

```python
def _channel_name(raw: str) -> str:
    cleaned = re.sub(r"[^a-z0-9\-]", "-", raw.lower())
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    cleaned = cleaned or "agent"
    return _ns(cleaned)
```

Note: the mock bot reads `BOT_NAMESPACE` from its own process env — whoever launches it in mock mode must set the same value the instance `.env` has (the instance `.env` now carries `BOT_NAMESPACE`; see Task 8).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_helpers_ns.py -q`
Expected: PASS.

Run: `uv run python -m compileall -q tests/helpers.py tests/conftest.py tests/mock/bot.py`
Expected: compiles.

- [ ] **Step 5: Commit**

```bash
git add tests/helpers.py tests/conftest.py tests/mock/bot.py tests/unit/test_helpers_ns.py
git commit -m "feat: namespace-aware test harness (Discord helper + mock bot)"
```

---

### Task 8: axi_test.py — prefix-aware helpers + instance env

**Files:**
- Modify: `axi_test.py` — `_normalize_channel_name` callers (`_find_channel_by_name` ~726, `_find_killed_category` ~746, `find_master_channel` ~770), `_write_env` (~379).

**Interfaces:**
- Consumes: nothing new.
- Produces: `axi_test.py` CLI commands (msg/clean) resolve the right channels for a namespaced instance; generated instance `.env` files include `BOT_NAMESPACE`.

- [ ] **Step 1: Make the edits**

Add a namespace resolver near the existing helpers (uses `get_instance_env` which already exists):

```python
def _instance_namespace(name: str) -> str:
    """Return the instance's BOT_NAMESPACE (default 'off')."""
    return get_instance_env(name).get("BOT_NAMESPACE", "off") or "off"


def _ns(name: str, namespace: str) -> str:
    return f"{namespace}-{name}" if namespace != "off" else name
```

- `_find_channel_by_name(client, guild_id, name)`: this is called by `cmd_clean` without the instance name. Change the signature to `_find_channel_by_name(client, guild_id, name, namespace="off")`, and prefix both the normalized name and the `"active"` category lookup with `_ns(...)`:
  ```python
  normalized = _ns(_normalize_channel_name(name), namespace)
  ...
  if ch.get("type") == 4 and ch.get("name", "").lower() == _ns("active", namespace).lower():
  ```
- `_find_killed_category(client, guild_id, namespace="off")`: prefix the base matches:
  ```python
  base = _ns("killed", namespace).lower()
  if name.lower() == base:
      killed_cats.append((1, ch))
  elif re.match(rf"^{re.escape(base)}\s+\d+$", name.lower()):
      killed_cats.append((int(name.split()[-1]), ch))
  ```
- `find_master_channel(client, guild_id, namespace="off")`: prefix the master name lookup and the `"active"` fallback category.
- `cmd_msg` (~847) and `cmd_clean` (~1182) call sites: pass the instance namespace:
  - `cmd_msg`: `namespace = _instance_namespace(name)` then `channel_id = find_master_channel(client, guild_id, namespace)`.
  - `cmd_clean`: `namespace = _instance_namespace(name)` then `_find_channel_by_name(client, guild_id, name, namespace)` and `_find_killed_category(client, guild_id, namespace)`.
- `_write_env`: add a line to `env_content`:
  ```python
  f"BOT_NAMESPACE=off\n"
  ```
  Rationale: existing test guilds are un-namespaced. Emitting `off` keeps every current reservation working (the bot refuses to start without the var). Instances that want to exercise namespaced mode set `BOT_NAMESPACE` explicitly in their instance `.env` (and pass the same value to the mock bot's process env); the harness helpers in Task 7 and `_instance_namespace` here pick it up automatically.

- [ ] **Step 2: Verify**

Run: `uv run python -m compileall -q axi_test.py`
Expected: compiles.

Run: `uv run python axi_test.py --help`
Expected: help prints, no import errors.

- [ ] **Step 3: Commit**

```bash
git add axi_test.py
git commit -m "feat: namespace-aware axi_test.py helpers and instance env"
```

---

### Task 9: Migration script

**Files:**
- Modify: `scripts/migrate_guild.py` (`CATEGORY_NAMES`, `_is_axi_category`, killed filter, uncategorized-master case).

**Interfaces:**
- Consumes: `BOT_NAMESPACE` env var (default `off`).
- Produces: category matching and the uncategorized-master case accept namespaced names.

- [ ] **Step 1: Make the edits**

```python
NS = os.environ.get("BOT_NAMESPACE", "off")


def _cat(name: str) -> str:
    return f"{NS}-{name}" if NS != "off" else name


CATEGORY_NAMES = {_cat("Axi"), _cat("Active"), _cat("Killed")}
MASTER_CHANNEL_NAME = _cat("axi-master")
```

- `_is_axi_category` is unchanged (it iterates `CATEGORY_NAMES`, now prefixed).
- Killed-category filter: `if name == "Killed" or (name.startswith("Killed ") and name[7:].isdigit())` → use `_cat("Killed")`:
  ```python
  killed_base = _cat("Killed")
  killed_cat_ids = {cid for cid, name in source_cats.items()
                   if name == killed_base or (name.startswith(killed_base + " ") and name[len(killed_base) + 1:].isdigit())}
  ```
- Uncategorized master: `elif parent is None and ch["name"] in ("axi-master",):` → `elif parent is None and ch["name"] == MASTER_CHANNEL_NAME:`.

- [ ] **Step 2: Verify**

Run: `BOT_NAMESPACE=dev uv run python -c "import os; os.environ['BOT_NAMESPACE']='dev'; import scripts.migrate_guild as m; assert m._is_axi_category('dev-Axi'); assert m._is_axi_category('dev-Killed 2'); assert not m._is_axi_category('prod-Axi')"`
Expected: no output, exit 0. Also check default mode:
Run: `uv run python -c "import scripts.migrate_guild as m; assert m._is_axi_category('Axi'); assert m._is_axi_category('Active 3')"`
Expected: no output, exit 0.

- [ ] **Step 3: Commit**

```bash
git add scripts/migrate_guild.py
git commit -m "feat: derive migration-script category names from BOT_NAMESPACE"
```

---

### Task 10: Full verification

**Files:** none (verification only).

- [ ] **Step 1: Run the full unit suite**

Run: `uv run pytest tests/unit -q`
Expected: all unit tests PASS (including new `test_channel_namespace.py`, `test_helpers_ns.py`, namespaced infrastructure cases, `TestValidateNamespace`).

- [ ] **Step 2: Compile all touched packages**

Run: `uv run python -m compileall -q axi tests scripts axi_test.py`
Expected: compiles clean.

- [ ] **Step 3: Run the repo linters if available**

Run: `uv run ruff check axi tests scripts axi_test.py` (if `ruff` is installed in the venv; otherwise skip with a note).
Expected: no new violations (follow existing style).

- [ ] **Step 4: Confirm the spec's grep-level invariants**

Run: `grep -rn "normalize_channel_name(" axi | grep -v "def normalize_channel_name\|namespaced_channel_name\|test"` and `grep -rn "channel.name ==\|ch.name ==" axi`
Expected: no un-namespaced channel-name derivations remain in `axi/` (the first grep returns only `normalize_channel_name`'s definition and the call inside `namespaced_channel_name`). The second grep returns at most `channels.py:1079`'s `channel.name == desired_name` (status-rename comparison against a namespaced-derived `desired_name` — correct) plus `main.py`'s `on_guild_channel_create` guard `channel.name in channels.bot_creating_channels` (correct — set holds namespaced names) and the `#readme` handling (`ch.name == "readme"` — shared, correct).

- [ ] **Step 5: Commit any stragglers**

```bash
git status --short
# If anything remains, add + commit with an appropriate message.
```

- [ ] **Step 6: Report**

Summarize: `BOT_NAMESPACE` semantics, the three naming helpers, the ownership filter locations, harness changes, and which checks passed (unit suite + compile + ruff).

---

## Self-Review Notes (run after writing, before handoff)

- Spec §1 (config) → Task 1; §2 (naming layer) → Task 2; §3 (infrastructure) → Tasks 3-4; §4 (message filter) → Task 5; §5 (reconstruction) → Task 6; §6 (channel-create) → Task 5; §7 (harness) → Tasks 7-8; §8 (migration) → Task 9; §9 (docs) → Task 1 (.env.template). `tests/unit/conftest.py` (spec §7) → Task 1 (load-bearing for every later task).
- Placeholder scan: every step contains concrete code or an exact command; no TBDs.
- Type consistency: `namespaced_channel_name(agent_name) -> str`, `parse_agent_from_channel_name(str) -> str | None`, `_category_name(str) -> str`, `config.BOT_NAMESPACE: str`, `config.NAMESPACE_OFF = "off"` used identically across tasks. `_ns_name(namespace, name)`, `_ns(name)` in the harness, and `_cat(name)` in the migration script are task-local and never cross-referenced.
