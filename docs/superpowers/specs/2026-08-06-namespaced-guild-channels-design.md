# Namespace Isolation for Multi-Bot Guild Sharing

Date: 2026-08-06 · Supersedes: 2026-08-03-namespaced-guild-channels-design.md

## Problem

Multiple bot instances currently share a single Discord guild without any
namespacing. All agents live in channels and categories that carry no
instance ownership, so one bot can reply in another's channels, claim
another's manually-created channels, or reconstruct another's agents on
restart. The codebase also hardcodes the `axi-master` channel name in a
handful of places.

## Requirements

- Every bot instance must set `BOT_NAMESPACE` (required, validated, fail-fast
  at startup). `off` is a reserved keyword meaning "no prefix" (legacy mode).
- Channel names get a `{namespace}-` prefix: `axi-master` → `dev-axi-master`.
- Category names get a `{namespace}-` prefix: `Axi` → `dev-Axi`,
  `Active` → `dev-Active`, `Killed` → `dev-Killed` (and overflow, e.g.
  `dev-Axi 2`). Combined mode: `Combined` → `dev-Combined`.
- A bot processes messages only in channels that belong to its namespace,
  and reconstructs only its own agents on restart.
- A manually-created channel in one of the bot's categories that does not
  belong to the bot's namespace is not claimed as an agent.
- `#readme` is shared and un-namespaced (per decision).
- Session names (e.g. `axi-master`) are internal and never prefixed.

## Approach

Add a `BOT_NAMESPACE` config value and a small naming layer in
`axi/channels.py` that derives on-Discord channel and category names. The
ownership boundary is the category lists the bot maintains (which now only
contain its own namespaced categories); a name-parse predicate is used as
defense-in-depth and for deriving agent names from channel names.

`normalize_channel_name()` stays pure (it is property-tested for
idempotency). A new config-dependent wrapper applies the prefix.

## 1. Config

**File: `axi/config.py`**

Add `NAMESPACE_OFF = "off"` and export `BOT_NAMESPACE` (add to `__all__`):

```python
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

- Unset/empty → startup error. `off` → no prefix. Any other value must match
  `^[a-z0-9][a-z0-9-]*$` (lowercase alphanumeric + hyphens) and be ≤ 20 chars.
- `MASTER_AGENT_NAME` stays `"axi-master"` — the internal session name,
  never prefixed.

## 2. Naming layer

**File: `axi/channels.py`**

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

- `normalize_channel_name()` is unchanged (pure, idempotent).
- `parse_agent_from_channel_name` is the ownership predicate. Callers must
  pass the status-prefix-stripped name: status renames produce
  `🔴dev-axi-master`, so callers apply `strip_status_prefix()` first.
- `_category_name` is applied to every base name used in
  `ensure_guild_infrastructure()` (combined primary, legacy discover-only
  entries, killed) and inside `_get_category_with_room()` when building the
  overflow name (`"Axi 2"` → `"dev-Axi 2"`). `_match_category_group()` is
  unchanged — it already matches against the full on-Discord name.

## 3. Category infrastructure

**File: `axi/channels.py` — `ensure_guild_infrastructure()`**

Discovery, creation, and permission-sync of categories use
`_category_name(base_name)` for every entry in `group_map`. The config
constants (`AXI_CATEGORY_NAME`, `ACTIVE_CATEGORY_NAME`,
`KILLED_CATEGORY_NAME`, `COMBINED_CATEGORY_NAME`) stay raw; the prefix is
applied at use.

Result: `axi_categories`, `active_categories`, `killed_categories`, and
`combined_categories` contain only this bot's categories. Every channel the
bot touches (lookup, move, kill, reorder, status rename, reconstruction)
lives in its own namespace. This is the primary ownership boundary.

No changes needed in `ensure_agent_channel()`, `move_channel_to_killed()`,
`get_agent_channel()`, `deduplicate_master_channel()`,
`position_agent_channel_top()`, `ensure_master_channel_position()`,
`reorder_channels_by_recency()`, or the status-rename machinery: they all
already derive names via `normalize_channel_name()` and compare via
`_match_channel_name()`. Switch those to `namespaced_channel_name()`.

## 4. Message ownership filter

**File: `axi/main.py` — `on_message()`**

Insert after the guild-ID check and before the `channel_to_agent` lookup:

```python
# Only process channels in this bot's namespace. Category membership is the
# primary boundary (our channels always parse); the name parse also covers
# the uncategorized pinned-master case and foreign channels in our category.
if channels.parse_agent_from_channel_name(channels.strip_status_prefix(channel.name)) is None:
    observe_inbound_discord_event("message", "ignored_other_namespace")
    return
```

- DMs are already redirected before this point.
- `#readme` is un-namespaced, so it parses as `None` and is ignored — correct,
  it is bot-written, not user-interactive.

## 5. Channel reconstruction

**File: `axi/agents.py` — `reconstruct_agents_from_channels()`**

The category iteration already scopes to this bot's categories. Per channel,
derive the agent name via the parse predicate and gate on it:

```python
agent_name = _channels_mod.parse_agent_from_channel_name(
    _channels_mod.strip_status_prefix(ch.name)
)
if agent_name is None:
    continue  # foreign channel in our category — silently ignore
if agent_name == config.MASTER_AGENT_NAME:
    channel_to_agent[ch.id] = config.MASTER_AGENT_NAME
    continue
```

- Master check compares the parsed (un-namespaced) name.
- The prompt-substitution `{channel_name}` still uses
  `strip_status_prefix(ch.name)` (display only).

## 6. Channel-create auto-registration

**File: `axi/main.py` — `on_guild_channel_create`, `_handle_axi_channel_create`, `_handle_active_channel_create`**

Gate on the parse predicate and use the parsed name as the agent name:

```python
agent_name = channels.parse_agent_from_channel_name(channels.strip_status_prefix(channel.name))
if agent_name is None:
    return  # foreign channel in our category — don't claim
```

- `_handle_axi_channel_create` / `_handle_active_channel_create` receive the
  parsed name instead of `channel.name` (today a user-created
  `prod-something` inside our `dev-Axi` category would register an agent
  literally named `prod-something` and create a worktree for it).
- The `bot_creating_channels` guard and the master-name skip stay; the master
  check compares against the parsed name.

## 7. Test harness

**File: `tests/helpers.py` — `Discord`**

Read `BOT_NAMESPACE` from env (default `off`) and prefix lookups:

```python
def _ns(self, name: str) -> str:
    return f"{self.namespace}-{name}" if self.namespace != "off" else name
```

Prefix `find_channel()`, `find_category()`, and `find_channel_by_prefix()`.
The ~30 test call sites need no changes — they call
`discord.find_channel("smoke-x")` and get the namespaced lookup for free.
`conftest.py`'s `master_channel` fixture (`find_channel("axi-master")`) works
unchanged.

**File: `tests/mock/bot.py`**

Read `BOT_NAMESPACE` from env; prefix `MASTER_CHANNEL` and `_channel_name()`
so the mock mirrors the real bot's naming. `README_CHANNEL` stays
un-namespaced (readme is shared — matches the real bot).

**File: `axi_test.py`**

- `_normalize_channel_name`, `find_master_channel`, `_find_channel_by_name`,
  `_find_killed_category`: prefix when the instance's `BOT_NAMESPACE` is set
  (read via `get_instance_env(name)`).
- `_write_env`: emit `BOT_NAMESPACE=<instance name>` (or `off`) into each
  test instance's `.env` — required, since the bot refuses to start without
  it.

**File: `tests/unit/conftest.py` (new)**

`axi.config` validates `BOT_NAMESPACE` (and resolves `DISCORD_TOKEN`) at
import time, so every unit test that imports an `axi` module needs the env
set before import. Add a `conftest.py` that pytest loads before any test
module in `tests/unit` (this is the existing `DISCORD_TOKEN` pattern, made
uniform):

```python
import os

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")
os.environ.setdefault("BOT_NAMESPACE", "off")
```

The per-file `setdefault` lines already present (e.g.
`test_channels_combine_migration.py`, `test_discord_stream_*.py`,
`test_hub_wiring.py`, `test_proxy_auth.py`) stay — `setdefault` does not
override the conftest values. `patch.object(channels.config, ...)` patterns
keep working because the naming helpers read `config.BOT_NAMESPACE` at call
time.

New unit tests:

- `namespaced_channel_name` / `parse_agent_from_channel_name` round-trip in
  both `off` and namespaced modes (including the `dev-` empty-remainder case
  and status-prefix stripping).
- `ensure_guild_infrastructure` creates `dev-Axi` / `dev-Active` /
  `dev-Killed` when namespaced (mirroring the existing FakeCategory tests).

## 8. Migration script

**File: `scripts/migrate_guild.py`**

Read `BOT_NAMESPACE` from env (default `off`) and build expected names
dynamically:

```python
ns = os.environ.get("BOT_NAMESPACE", "off")
def _cat(name: str) -> str:
    return f"{ns}-{name}" if ns != "off" else name

CATEGORY_NAMES = {_cat("Axi"), _cat("Active"), _cat("Killed")}
```

- `_is_axi_category` and the killed-category filter match prefixed names.
- The uncategorized-master special case becomes `ch["name"] == _cat("axi-master")`.

## 9. Documentation

**File: `.env.template`** — document `BOT_NAMESPACE` (required; `off` for
legacy, or a lowercase alphanumeric+hyphen value, max 20 chars).

## What Is Not Changed

- **Session names** (`session.name == "axi-master"`, ~15 sites across
  `main.py`, `tools.py`, `agents.py`, `shutdown.py`, `prompts.py`): internal,
  never prefixed.
- **`#readme`**: shared, un-namespaced. `sync_readme_channel()` untouched.
- **`scripts/status_tools.py`**: dead code (no callers in `axi/`), left as-is.
- **`BOT_DIR`, `DISCORD_GUILD_ID`, `ALLOWED_USER_IDS`, permission overwrites,
  agent sessions, session IDs, workflow logic**: unchanged.
- **`normalize_channel_name()`**: stays pure; idempotency property tests
  untouched.
- **`voice.py`, `hub_wiring.py`, `egress_filter.py`**: no changes (channel
  name uses are display-only).

## Future Work: Bot-to-Bot Messaging (explicitly out of scope)

The `{ns}-{agent}` naming is the addressing convention a cross-bot messaging
feature would build on, and v1 does not block it. A future feature would need:

1. **Permissions**: `_build_category_overwrites` sets `@everyone:
   view_channel=False` on live categories, so bot A's token cannot see bot
   B's channels unless granted (e.g. adding bot A's user ID to bot B's
   `ALLOWED_USER_IDS`). This is config, not code.
2. **Addressing**: `ns/agent` resolution via a guild-wide channel scan
   (a generic `split("-", 1)` — additive, nothing in v1 conflicts).
3. **Receive filter**: relaxing `on_message` to accept messages from other
   bots in their own namespaces (the namespace filter is independent of the
   permission gate, so granting visibility cannot accidentally defeat
   isolation).

## File Summary

| File | Change |
|------|--------|
| `axi/config.py` | `NAMESPACE_OFF`, `_validate_namespace()`, export `BOT_NAMESPACE` |
| `axi/channels.py` | `namespaced_channel_name()`, `parse_agent_from_channel_name()`, `_category_name()`; prefix category discovery/creation in `ensure_guild_infrastructure()`; channel-name call sites use `namespaced_channel_name()` |
| `axi/main.py` | Namespace filter in `on_message()`; parse-gate + parsed agent name in channel-create handlers |
| `axi/agents.py` | `reconstruct_agents_from_channels()` parse gate + parsed agent name |
| `tests/helpers.py` | `Discord` prefixes `find_channel` / `find_category` / `find_channel_by_prefix` |
| `tests/mock/bot.py` | Reads `BOT_NAMESPACE`, prefixes master/agent channel names (readme stays shared) |
| `tests/unit/conftest.py` | New: `setdefault("BOT_NAMESPACE", "off")` (with existing token/user/guild defaults) |
| `axi_test.py` | Prefix-aware helpers; `_write_env` emits `BOT_NAMESPACE` |
| `tests/unit/*` | New naming (round-trip) + namespaced `ensure_guild_infrastructure` tests |
| `scripts/migrate_guild.py` | Category names derive from `BOT_NAMESPACE` env |
| `.env.template` | Document `BOT_NAMESPACE` |
