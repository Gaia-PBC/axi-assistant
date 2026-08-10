# Stress Testing Reference

Instructions for stress testing the Axi test infrastructure. Read this when asked to stress test, verify test infra health, or after making changes to test infrastructure code.

## Levels

- **Smoke test** — one pass through each subsystem, happy path only. Answers: "does it work at all?"
- **Stress test** — repeated rounds, concurrent load, failure injection, cross-subsystem interactions. Answers: "does it break under pressure?"

When asked to "stress test," run the full stress test. When asked to "verify" or "check" the infra, a smoke test is sufficient.

## Subsystems

Every test run (smoke or stress) should cover ALL of these, not just the one you're debugging:

1. **Message processing** — single message, queued messages, rapid-fire
2. **Flowchart execution** — full /soul flowchart completes, all blocks fire
3. **Queued flowcharts** — second message triggers a new flowchart after the first completes (the deadlock bug scenario)
4. **Agent spawning** — spawn, verify channel, verify wake, verify response
5. **Agent lifecycle** — task → response → kill → cleanup
6. **Concurrent agents** — multiple agents active, no cross-talk
7. **Cross-subsystem interactions** — spawn during active flowchart, message agent while master is busy, agent-to-master messaging
8. **Edge cases** — respawn killed agent name, wake sleeping agent, messages during bridge reconnection

## Procedure

1. **Set up**: `uv run python axi_test.py up <name> --wait` to create a test instance
2. **Start**: `uv run python axi_test.py restart <name>`
3. **Verify startup**: Check journal logs for startup notification and "Bridge connection established"
4. **Run test rounds** for each subsystem:
   - `post_message` to send test messages
   - `wait_for_message` to verify responses
   - `discord_list_channels` to verify channel creation/cleanup
   - Check journal logs for errors between rounds (`journalctl --user -u axi-test@<name> --since "N min ago"`)
5. **Record results**: Log pass/fail per subsystem with error details
6. **Tear down**: `uv run python axi_test.py down <name>`

## Test Details

### Message Processing
- Send a single message, verify response completes (STREAM_END result=ok in logs)
- Send 2 messages rapidly so the second queues, verify both complete with correct answers
- Send 3+ messages rapidly, verify all process in order with correct answers

### Flowchart Execution
- Verify the /soul flowchart runs end-to-end: CLASSIFY block outputs JSON, RESPOND block produces a response, GATHER_NEXT_ACTION and SET_STATUS blocks fire
- Check logs for flowchart completion: `Flowchart started` → `STREAM_END result=ok`

### Queued Flowcharts
- This is the deadlock bug scenario: each queued message triggers its own full flowchart. The second flowchart must not consume stale control_responses from the first.
- Send 2+ rapid messages, verify each gets its own complete flowchart pass (each should have CLASSIFY → RESPOND → GATHER_NEXT_ACTION → SET_STATUS)
- Check logs for `Drained N stale control_response(s)` — if this appears, the drain is working. If it doesn't appear, that's fine too (no stale responses to drain).

### Agent Spawning
- Ask the master to spawn an agent
- Verify a new Discord channel appears in the Axi/Active category
- Send a message in the agent's channel, verify it wakes and responds
- If wake fails, check stderr in logs — common issues: effort level, CLI auth, PATH

### Agent Lifecycle
- Send a task to a spawned agent, verify response
- Kill the agent via master, verify channel moves to Killed category
- Respawn with the same name, verify channel moves back to Axi/Active and agent responds

### Concurrent Agents
- Spawn 2+ agents simultaneously
- Send messages to all agents at the same time (parallel `post_message` calls)
- Verify each agent responds correctly with no cross-talk (agent-1's response doesn't appear in agent-2's channel)

### Cross-Subsystem Interactions
- **Spawn during active flowchart**: Send a spawn request while the master is still processing a previous message's flowchart. Verify the spawn queues and completes after the current flowchart.
- **Agent-to-master while busy**: Use `axi_send_message` from an agent to master while master is processing. Verify the message interrupts or queues correctly (check for `interrupt()` in logs).
- **Schedule during processing**: If schedules are configured, trigger a scheduled event while the master is busy. Verify it queues without deadlock.

### Edge Cases
- **Respawn killed name**: Kill an agent, spawn a new one with the same name. Verify no channel conflicts.
- **Wake sleeping agent**: Wait for auto-sleep (60s idle), then send a message. Verify the agent wakes and responds.
- **Rapid spawn/kill cycles**: Spawn an agent, immediately kill it, immediately spawn again. Verify no orphaned channels or zombie processes.

## Failure Injection (Stress Test Only)

These tests probe error handling. Skip for smoke tests.

- **Rate limit simulation**: Send many messages rapidly to trigger Discord API rate limits. Verify the bot doesn't crash and resumes after the limit clears.
- **Invalid flowchart input**: Send a message that causes a flowchart block to produce invalid JSON. Verify the error is handled gracefully (check for error messages in logs, not a hang or crash).
- **Bridge reconnection**: Restart the test instance (`axi_test.py restart`) while agents are sleeping. Verify the bridge reconnects and agents can be woken after restart.

## Success Criteria

- **Smoke test**: All subsystems pass on happy path.
- **Stress test**: All subsystems pass including cross-subsystem interactions and at least one failure injection scenario.
- A partial test (e.g., only message processing) is never a complete test. If any subsystem is skipped, note it explicitly in the results.
