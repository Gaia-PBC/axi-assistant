# Axi 2.0 — E2E Test Tagging & Mock Strategy

Status: draft for Pride's review (2026-08-05, ux-audit). Not committed to a branch yet.

## TL;DR

1. **Three tiers, not two.** The current `tests/mock/bot.py` is a *separate fake bot* (strategy A) that runs **none** of axi's real orchestration code. It can only validate the e2e harness + the 13 `*_generated` tests. Pointing a queue/lifecycle/permission test at it would test the fake, not axi.
2. **"Replace the LLM usage" = strategy B: real bot + a deterministic stub model.** The `AgentHub` already takes an injectable `create_client` (`packages/agenthub/agenthub/runtime.py:49/68/438`). A stub `ClaudeSDKClient` injected there makes the **real** bot run **real** queue/lifecycle/spawn/permission logic with a fast, deterministic, $0 "LLM". This backend does **not** exist yet — building it is the main infra task.
3. **~48 of the ~68 live-bot tests → strategy B** (orchestration/plumbing). **~18–20 → real model** (LLM reasoning, tool use, planning, resume-memory, security). Plus the 13 `*_generated` stay on strategy A (harness validation).
4. **Pride's 6 critical functionalities:** #1 covered, #5/#6 partial, #2/#3/#4 are gaps (flowchart execution — shared with flowcoder/axi-core).

## Mock strategy — the key correction

| | Strategy A (current `tests/mock/bot.py`) | Strategy B (proposed) | Real e2e |
|---|---|---|---|
| What runs | A separate fake bot process | The **real** axi bot | The real axi bot |
| The "LLM" | canned string matcher | **deterministic stub `ClaudeSDKClient`** | real model (haiku) |
| Tests | e2e harness + 13 `*_generated` | real orchestration (queue, lifecycle, spawn, permissions, channels) | LLM reasoning/tools/planning/memory |
| Speed / cost | instant / $0 | ~instant / $0 | slow / real tokens |
| Trap | **can't test real axi logic** | must not stub the thing under test | — |

**Strategy B infra to build (the real "mock-extension list"):**
1. A deterministic stub `ClaudeSDKClient` that: (a) echoes `Say exactly: X` → `X`; (b) emits canned `tool_use` events for tool-visibility tests; (c) supports a **configurable response delay** so "busy → queue/interrupt/keep-latest" tests have a real busy window; (d) canned spawn/command acknowledgements.
2. An env flag (e.g. `AXI_STUB_MODEL=1`) that injects it via `AgentHub(create_client=stub_factory)`.
3. Smoke-test instance runs in stub-model mode for the B-tier suite; real haiku for the real-e2e suite.

**Do NOT stub** the behavior a real-e2e test exists to verify — tool invocation, plan generation, resume-restored memory, and permission denial on a real write attempt. A stub that "returns denied" greens a broken guard (SOUL: a mock supplying the missing behavior turns the suite green over a broken feature).

## Per-test tagging (~68 live-bot tests)

Tier: **B** = real-bot + stub-model · **B\*** = B but needs the stub's busy-delay mode · **REAL** = real model required.

### tests/test_core.py (16)
| Test | Tier | Reason |
|---|---|---|
| test_basic_response | B | message round-trip |
| test_status_command | B | `/status` dispatch |
| test_debug_toggle | B | `/debug` dispatch |
| test_clear_context | B | `/clear` dispatch |
| test_compact_context | B | `/compact` dispatch (real token reclaim not asserted) |
| test_model_warning | B | wake-banner (real bot) |
| test_agent_spawn_and_kill | B | real spawn/kill; rewrite llm_assert→string |
| test_killed_channel_protection | B | killed-channel logic |
| test_duplicate_live_agent_name | B | duplicate detection |
| test_emoji_reactions | B | reaction logic |
| test_debug_mode_visibility | REAL | real tool call surfaced as 🔧 |
| test_startup_notification | B | restart→ready |
| test_readme_channel_sync | B | readme sync |
| test_auto_sleep_and_wake | B | real auto-sleep/wake lifecycle |
| test_duplicate_name_spawn_killed | B | Killed/Active category moves |
| test_agent_resume | REAL | context preservation (stub is stateless) |

### tests/test_advanced.py (10) — all **B** (real orchestration)
concurrency_limit_bypass (MAX_AWAKE enforcement · **#1**), packs_default/empty/custom (pack loading), record_updater_spawns, record_updater_excluded_for_master, shutdown_rejection (shutdown state), manual_channel_auto_register, channel_reconstruction, stranded_message_recovery (scheduler safety net).

### tests/test_messaging.py (11)
| Test | Tier | Reason |
|---|---|---|
| test_message_queuing | B | queue order · **#6** |
| test_max_length_input | B | input handling |
| test_unicode_emoji | B | formatting |
| test_code_blocks | B | formatting |
| test_status_while_busy | B\* | needs busy window |
| test_inter_agent_message_idle | B | inter-agent routing · **#5** |
| test_queue_stress | B | queue · **#6** |
| test_long_output_splitting | B | message splitting |
| test_clear_while_busy | B\* | needs busy window |
| test_inter_agent_message_busy | B\* | busy-interrupt · **#5** |
| test_concurrent_multi_agent | B | 2 agents at once · **#1** |

### tests/test_validation.py (8)
| Test | Tier | Reason |
|---|---|---|
| test_reserved_name_axi_master | B | name validation |
| test_disallowed_cwd | B | cwd validation |
| test_empty_agent_name | B | name validation |
| test_cwd_write_enforcement | REAL | **security** — real permission guard on a real write |
| test_special_chars_in_name | B | name normalization |
| test_ask_user_question | REAL | LLM generates structured question (AskUserQuestion) |
| test_todo_write_display | REAL | real TodoWrite tool use |
| test_spoofed_system_message | B | spoofed-input rejection (bot logic) |

### tests/test_edge_cases.py (5)
empty_text_command **B**, unknown_text_command **B**, invalid_debug_arg **B**, context_clear_doesnt_erase_channel_history **REAL** (agent reads history via tool), race_message_during_kill **B** (lifecycle race).

### tests/test_plan_mode.py (4) — all **REAL**
enter/approve/reject/feedback — LLM plan generation & revision (quicksort+type-hints).

### tests/test_mcp_tools.py (4) — all **REAL**
discord_send_file, discord_list_channels, discord_read_messages, discord_send_message — real tool invocation.

### tests/test_scheduling.py (3) — all **REAL**
one_off / recurring / skip — agent-driven schedule-tool use + real firing.

### tests/test_agenthub_live.py (3) — all **B**
queue_contract (**#6**), reuse_channel_after_kill, restart_then_wake_existing_agent.

### tests/test_stop_ux_regressions.py (2) — **B\***
master_skip_prefers_latest_message, master_rapid_fire_busy_messages_keep_only_latest — keep-latest queue dedup; needs busy window (**#6**).

**Totals:** ~48 B (of which ~5 are B\*, needing the stub busy-delay) · ~18–20 REAL.

## Pride's 6 critical functionalities — coverage

| # | Functionality | Status | Tests / gap |
|---|---|---|---|
| 1 | Multiple simultaneous sessions | ✅ covered | test_concurrent_multi_agent, test_concurrency_limit_bypass |
| 2 | Multithreaded flowcharts (spawn blocks) | ❌ gap | none — write one; flowchart engine is flowcoder/axi-core |
| 3 | Flowcharts w/ command blocks (stack) | ❌ gap | none — write one |
| 4 | Author flowchart → spawn agent to execute it | ❌ gap | mechanism exists (`axi_e2e.py:31` `command=`), but every test uses `command="prompt"` |
| 5 | Two sessions back-and-forth | ⚠️ partial | idle/busy one-way delivery only; no A↔B multi-turn round-trip |
| 6 | Multiple requests → queue | ⚠️ partial | single-source covered; **multi-source** (several sessions→one) untested |

## Gap tests to write (REAL-tier, these need real flowchart execution)

- **#2** — a flowchart with ≥2 `spawn` blocks + a `wait` join; assert both sub-agents ran and the parent resumed after the join.
- **#3** — a flowchart with a `command` block (inline composition); assert the inner flowchart ran and returned into the stack.
- **#4** — author/modify a flowchart file, then `axi_spawn_agent(command=<that flowchart>, command_args=...)` (NOT a prose prompt); assert the spawned agent executed the flowchart.
- **#5 round-trip** — A sends to B, B replies to A, A replies to B; assert the full multi-turn exchange.
- **#6 multi-source** — 2+ sessions send to one agent concurrently; assert all requests queue and process (none dropped).

## Plumbing `llm_assert` → deterministic-string rewrites (B-tier)

Under a stub model these responses are deterministic, so replace the LLM-judged assertion with a substring check:
- test_core: spawn/kill/duplicate confirmations.
- test_messaging: status_while_busy, inter_agent_message_idle send-confirmation.
- test_validation: reserved_name, disallowed_cwd, empty_name, special_chars.
- (Keep llm_assert only where the REAL model's free-form output is the point.)

## Suggested gating

- **Push-gate (fast, deterministic):** unit/integration (1093) + 13 `*_generated` (strategy A) + the ~48 B-tier (strategy B, once the stub model exists).
- **On-demand / nightly (real tokens):** the ~18–20 REAL-tier + the 5 new gap tests.
