# Tests

How the axi-assistant test suite is organized and how to run each tier.

## Layout — the directory tells you what a test needs

```
tests/
  unit/    # instance-FREE. Pure Python: no bot, no Discord, no LLM. Parallelizable.
  live/    # needs a REAL Discord live instance (the real axi bot + a real LLM).
  mock/    # runs against the fake MockBot (canned replies, no axi logic, no LLM). GENERATED.
  conftest.py  helpers.py  llm_judge.py  axi_e2e.py   # shared infra ONLY — no test files here.
```

**The rule:** a test needs a live bot **iff** it lives in `tests/live/` or `tests/mock/`. Everything in `tests/unit/` is guaranteed instance-free.

## Where does a new test go?

| Your test… | Goes in |
|---|---|
| exercises a component in-process (no bot) | **`tests/unit/`** |
| drives the real axi bot over Discord (real spawn / LLM / tools / scheduling) | **`tests/live/`** |
| is a harness/smoke scenario for the deterministic mock bot | **`tests/mock/`** — but **don't hand-write it**, add a spec + regenerate (see below) |

## How the instance-dependency works (conftest layering)

- `tests/conftest.py` — the shared fixtures, including `discord` / `instance_env` / `warmup` and the autouse `_recover_after_failure`, which pulls in a live instance.
- `tests/unit/conftest.py` — **overrides** `_recover_after_failure` with a no-op, so unit tests skip all Discord/warmup → truly instance-free.
- `tests/live/` and `tests/mock/` — no own conftest; they **inherit** the real fixtures from `tests/conftest.py`.

So any test placed under `tests/unit/` stays instance-free automatically; anything under `live/`/`mock/` gets the bot fixtures.

## Running the tests

Recommended flags for every tier: `--timeout=300` (per-test, 5 min) · `--session-timeout=600` (whole run, 10 min). For reports/profiling add `--junitxml=… --html=… --durations=25`.

> **The e2e tiers are disabled by default.** `tests/live/` and `tests/mock/` are excluded from the default `pytest` collection (`collect_ignore` in `tests/conftest.py`) — they need a running bot and the live tier is currently unreliable (the `send_and_wait` sentinel/queuing work is still in progress). So a bare `pytest` runs only the instance-free `tests/unit/` tier. **To run an e2e tier, set `AXI_RUN_E2E=1`** (shown in the Live/Mock commands below).

### Unit — fast, no setup
```bash
uv run python -m pytest tests/unit/ --timeout=300
```
Runs serially, no instance needed (~1100 tests in a few minutes). **This is the default tier.**

### Live — needs a real test bot
Run **serially** (shared single bot; do **not** use `-n`).
```bash
# 1. reserve + start a test instance (real axi bot, real LLM from its .env AXI_MODEL)
uv run python axi_test.py up <name>
uv run python axi_test.py restart <name>

# 2. point the tests at it (AXI_RUN_E2E=1 re-enables the disabled-by-default tier)
AXI_RUN_E2E=1 AXI_TEST_INSTANCE_NAME=<name> AXI_TEST_INSTANCE_DIR=~/axi-tests/<name> \
  uv run python -m pytest tests/live/ --timeout=300 --session-timeout=600
```
Slow (real Discord round-trips + real model). Parallelism is capped by the number of test-bot slots.

### Mock — deterministic, no real LLM
The mock tests talk to a **standalone MockBot** run as its own process (it needs a test bot token + guild, which it reads from `DISCORD_TOKEN` / `DISCORD_GUILD_ID`). Use a reserved slot's creds — but run the MockBot instead of real axi (don't `restart` the instance).
```bash
# 1. start the mock bot with a test slot's creds
set -a; source ~/axi-tests/<name>/.env; set +a
uv run python tests/mock/bot.py &

# 2. run the tests in mock mode (AXI_RUN_E2E=1 re-enables the disabled-by-default tier)
AXI_RUN_E2E=1 AXI_MOCK_MODE=1 AXI_TEST_INSTANCE_NAME=<name> \
  uv run python -m pytest tests/mock/ --timeout=300
```
`AXI_MOCK_MODE=1` tells the conftest to send the MockBot a `[MOCK_RESTART]` control message instead of restarting real axi.

## `tests/mock/` is GENERATED — do not hand-edit

Each `test_*_generated.py` is emitted by the **`commands/discord/discord-test-generator.json`** flowchart from a spec in **`specs/*.md`** (1:1). The file header carries `# spec: specs/<name>.md` and a "Do not edit by hand" banner. To change a mock test, edit its **spec** (or the generator) and regenerate — never the `.py`.

## Flaky tests

Tag a genuinely unstable test (a timing or teardown race) with `@pytest.mark.flaky` (the marker is registered in `pytest.ini`). Then:

- Exclude known-flaky tests from a must-pass run: `pytest … -m "not flaky"`.
- **A `flaky` tag is a tracking label, not a fix.** It means "quarantined until root-caused," **not** "silenced forever." Do **not** retry-until-green, `xfail`, `skip`, or delete the assertion — open a follow-up to fix the underlying race.

Currently tagged:
- `tests/live/test_process_leak.py::test` — tears down a real `ClaudeSDKClient` subprocess and hits an asyncio `CancelledError` teardown race (failed 2/2 in the 2026-08-05 audit, serial *and* parallel). It spins up real claude + haiku, so it now lives in `tests/live/` (instance-gated — skips without a bot) instead of the instance-free `unit/` tier. Still open: root-cause the teardown race.

## Coming soon: a runner script

A runner/profiling **script** will wrap all of this — set up the instance, run each tier with the right flags, tee output to disk, and compile per-test speed/timeout reports across many runs. Until it lands, run the tiers manually as above.
