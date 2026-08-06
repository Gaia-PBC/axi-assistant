"""Discord API helpers for smoke tests."""

import json

from discord_e2e import DiscordChannel, DiscordE2EClient

TEST_SENTINEL = "awaiting input"

# axi/discord_stream.py renders flowchart + agent UI chrome with these structural
# prefixes. None of it is the agent's own reply, so it must be stripped from the text
# the assertions / LLM-judge run against. (Mirrors the emit sites in discord_stream.py.)
_CHROME_PREFIXES = (
    "▶ ",  # "▶ **BLOCK** (`type`)"  block-progress marker
    "`\U0001f527",  # "`🔧 tool`"          tool-call marker
    "\U0001f504 ",  # "🔄 Compacting…"     context-compaction notice
    "-# ",  # "-# 12.3s"                   trailing timing line
)


def _is_renderer_chrome(content: str) -> bool:
    """True if the message is discord_stream UI chrome, not agent content."""
    return content.startswith(_CHROME_PREFIXES) or (
        content.startswith(">") and content.endswith("**FAILED**")
    )


def _is_structured_step_output(content: str) -> bool:
    """True if the message is *entirely* a fenced ```json block — i.e. a flowchart
    output_schema step (CLASSIFY / GATHER_NEXT_ACTION / TEST_COMPLETION) whose JSON is
    internal branching data, not a reply. Detected structurally (it parses as JSON), not
    by matching specific keys, so it never strips a genuine prose reply.
    """
    if not (content.startswith("```json") and content.endswith("```")):
        return False
    try:
        json.loads(content[len("```json") : -len("```")].strip())
    except ValueError:
        return False
    return True


def _ns_name(namespace: str, name: str) -> str:
    """Prefix a channel/category lookup name with the instance namespace."""
    if not namespace or namespace == "off":
        return name
    return f"{namespace}-{name}"


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

    def require_channel(self, name: str) -> DiscordChannel:
        return super().require_channel(name)

    def wait_for_bot(
        self,
        channel_id: str,
        after: str,
        timeout: float = 120.0,
        poll_interval: float = 2.0,
        sentinel: bool = True,
        check: str | None = None,
    ) -> list[dict]:
        result = self.wait_for_bot_response(
            channel_id,
            after=after,
            timeout=timeout,
            poll_interval=poll_interval,
            sentinel=TEST_SENTINEL if sentinel else None,
            check=check,
        )
        return result.messages

    def send_and_wait(
        self,
        channel_id: str,
        content: str,
        timeout: float = 120.0,
        sentinel: bool = True,
    ) -> list[dict]:
        result = super().send_and_wait(
            channel_id,
            content,
            timeout=timeout,
            sentinel=TEST_SENTINEL if sentinel else None,
        )
        return result.messages

    def bot_response_text(self, messages: list[dict]) -> str:
        """Join the agent's prose reply, dropping renderer chrome (block/tool/compaction/
        timing markers) and flowchart output_schema JSON step outputs. `*System:*` lines
        are already excluded upstream by the sentinel-mode capture.
        """
        parts: list[str] = []
        for message in messages:
            content = str(message.get("content", "")).strip()
            if content and not _is_renderer_chrome(content) and not _is_structured_step_output(content):
                parts.append(content)
        return "\n".join(parts)

    def poll_history(
        self,
        channel_id: str,
        after: str,
        check: str,
        timeout: float = 120.0,
        poll_interval: float = 3.0,
    ) -> str:
        result = self.wait_for_bot_response(
            channel_id,
            after=after,
            timeout=timeout,
            poll_interval=poll_interval,
            sentinel=None,
            check=check,
        )
        return result.text
