"""Discord-specific configuration — token, intents, guild, REST client.

Split from config.py (Phase 0.5) so that ``import axi.config`` works without
the ``discord`` package installed.  Everything here is Discord-only; core
modules should import from ``axi.config``, not this file.

During the transition period, ``config.py`` re-exports these names so
existing ``config.DISCORD_TOKEN`` etc. references continue to work.
"""

from __future__ import annotations

import json
import os

from discord import Intents
from dotenv import load_dotenv

from axi.egress_filter import scrub_secrets
from axi.metrics import observe_discord_rest_request

load_dotenv()

# ---------------------------------------------------------------------------
# Discord token resolution
# ---------------------------------------------------------------------------


def _resolve_discord_token() -> str:
    """Resolve Discord token from env or test slot reservation.

    For prime: reads DISCORD_TOKEN from .env as usual.
    For test instances: derives instance name from the bot directory,
    looks up the reserved token from ~/.config/axi/.test-slots.json
    and ~/.config/axi/test-config.json. No token in .env needed.
    """
    token = os.environ.get("DISCORD_TOKEN")
    if token:
        return token

    bot_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    instance_name = os.path.basename(bot_dir)
    config_dir = os.path.expanduser("~/.config/axi")
    slots_path = os.path.join(config_dir, ".test-slots.json")
    config_path = os.path.join(config_dir, "test-config.json")

    try:
        with open(slots_path) as f:
            slots = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        raise RuntimeError(
            f"DISCORD_TOKEN not set and cannot read {slots_path}: {e}\n"
            f"Set DISCORD_TOKEN in .env or reserve a slot: axi-test up {instance_name}"
        ) from None

    slot = slots.get(instance_name)
    if not slot:
        raise RuntimeError(
            f"DISCORD_TOKEN not set and no slot for '{instance_name}' in {slots_path}\n"
            f"Reserve a slot: axi-test up {instance_name}"
        )

    try:
        with open(config_path) as f:
            config = json.load(f)
        return config["bots"][slot["token_id"]]["token"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        raise RuntimeError(f"Cannot resolve token for bot '{slot.get('token_id')}': {e}") from None


DISCORD_TOKEN = _resolve_discord_token()

# ---------------------------------------------------------------------------
# Guild ID
# ---------------------------------------------------------------------------

DISCORD_GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])

# ---------------------------------------------------------------------------
# Discord-specific feature flags
# ---------------------------------------------------------------------------

CHANNEL_STATUS_ENABLED = os.environ.get("CHANNEL_STATUS_ENABLED", "").lower() in ("1", "true", "yes")
CHANNEL_SORT_BY_RECENCY = os.environ.get("CHANNEL_SORT_BY_RECENCY", "").lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Discord intents
# ---------------------------------------------------------------------------

intents = Intents(
    guilds=True,
    guild_messages=True,
    guild_reactions=True,
    message_content=True,
    dm_messages=True,
    voice_states=True,
)

# ---------------------------------------------------------------------------
# Discord REST API client
# ---------------------------------------------------------------------------

from discordquery import AsyncDiscordClient

from axi.discord_wire import emit_rest_audit_event

discord_client = AsyncDiscordClient(
    DISCORD_TOKEN,
    on_request_observer=lambda method, path, status, duration: observe_discord_rest_request(
        "discordquery",
        method,
        path,
        status,
        duration,
    ),
)

discord_client.content_filter = scrub_secrets
discord_client.audit_hook = emit_rest_audit_event
