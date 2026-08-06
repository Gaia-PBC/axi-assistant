# spec: specs/agent-tool-visibility.md
"""Generated-style live test for Agent tool visibility in Discord."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from tests.axi_e2e import AxiDiscordEntrypoints
from tests.conftest import agent_cwd

if TYPE_CHECKING:
    from tests.helpers import Discord


def test_agent_tool_visibility_in_parent_channel(
    discord: Discord, master_channel: str, warmup: None
) -> None:
    name = f"agentvis-{int(time.time() * 1000)}"
    master = discord.channel(master_channel, name="axi-master")
    axi = AxiDiscordEntrypoints(master, discord=discord)
    try:
        axi.spawn_agent(
            name=name,
            cwd=agent_cwd(name),
            prompt="You are a test agent. Keep responses concise.",
            command="prompt",
            timeout=180.0,
        )
        agent = discord.wait_for_channel(name, timeout=60.0)
        baseline = agent.latest_message_id() or "0"

        axi.send_direct_to_agent(
            name,
            "Use the Agent tool exactly once. Ask a subagent to answer with exactly SUBAGENT_OK and nothing else.",
            timeout=45.0,
        )

        deadline = time.monotonic() + 180.0
        cursor = baseline
        saw_agent_announcement = False
        saw_task_lifecycle = False
        saw_final_response = False

        while time.monotonic() < deadline and not (
            saw_agent_announcement and saw_task_lifecycle and saw_final_response
        ):
            messages = discord.history(agent.channel_id, limit=100, after=cursor)
            if messages:
                for message in reversed(messages):
                    cursor = str(message["id"])
                    content = message.get("content", "") or ""
                    if "`🔧 Agent" in content and "toolu_" in content:
                        saw_agent_announcement = True
                    if "`🔧 task " in content and "task completed" in content:
                        saw_task_lifecycle = True
                    if "SUBAGENT_OK" in content:
                        saw_final_response = True
            time.sleep(2.0)

        assert saw_agent_announcement, "Did not see the Agent tool announcement in the parent channel"
        assert saw_task_lifecycle, "Did not see the subagent lifecycle message in the parent channel"
        assert saw_final_response, "Did not see the final SUBAGENT_OK response"
    finally:
        try:
            axi.kill_agent(name, timeout=60.0)
        except Exception:
            pass
