# Research Ideas — GENERATED, do not edit

Rendered from `ideas/events.jsonl` by `ideas.py render`. Hand edits are
detected and refused on the next render — use `ideas.py propose` /
`ideas.py retire` instead.

## Tried (past) (7)

- `idea-do-sonnet` (human) — Swap the spawn_do (child/doer) model from claude-opus-4-8 to claude-sonnet-5 — test whether a cheaper doer keeps quality (quality-per-cost). — best 0.8889, discarded, 1 attempt(s)
- `idea-main-opus` (human) — Swap main_model to claude-opus-4-8 — strongest classifier, tests the quality ceiling regardless of cost. — best 0.8444, discarded, 1 attempt(s)
- `idea-kb-concise` (human) — Add one directive to kb/SYSTEM_PROMPT.md — prefer concise answers and always self-verify any numeric output before finalizing. — best 0.8889, discarded, 1 attempt(s)
- `opt-main-sonnet` (human) — Set main_model to claude-sonnet-5 (main-session model knob) — a single-knob edit that applies to a target OR an optimizer bundle (Phase-4 recursion). — best 0.8889, kept, 1 attempt(s)
- `idea-do-haiku` (human) — Swap the spawn_do (doer) model to claude-haiku-4-5-20251001 — cheapest doer, tests the cost floor. — best 0.9111, kept, 2 attempt(s)
- `idea-main-haiku` (human) — Swap main_model to claude-haiku-4-5-20251001 — the lightweight-classifier baseline knob. — best 0.9333, kept, 1 attempt(s)
- `swap-main-model-to-claude-sonnet-5` (human) — (recovered from results.tsv — original text not in research-ideas.md) — best 0.8889, kept, 1 attempt(s)

## In flight (present) (13)

- `swap-spawn-do-model-from-75de70` (lineage-1) — Swap spawn_do model from claude-opus-4-8 to claude-sonnet-5 — sonnet-5 has reasoning + lower cost, may handle numeric tasks better than opus
- `swap-main-model-from-cla-3eb5dd` (lineage-1) — Swap main model from claude-haiku-4-5-20251001 to claude-sonnet-5 — sonnet-5 has reasoning, targets sqrt-67-digits weakness (0.67) at classification stage
- `swap-main-model-from-cla-218897` (lineage-1) — Swap main model from claude-haiku-4-5-20251001 to claude-sonnet-4-5 — reasoning tier with different cost-quality tradeoff than sonnet-5
- `swap-spawn-do-model-from-cb7bae` (lineage-2) — Swap spawn_do model from claude-opus-4-8 to nemotron-3.5-lightning — local cost-free reasoning model, unexplored region with high upside if quality holds
- `add-numeric-self-verific-ee12e8` (lineage-3) — Add numeric self-verification directive to SYSTEM_PROMPT.md — sqrt-67-digits (0.67) is weakest task; instruct to verify numeric outputs before finalizing
- `fix-ref-loading-path-in-cc3eb4` (lineage-4) — Fix ref-loading path in soul-lite-do.json from /home/pride/ to kb/refs/ to enable reference context for ref-dependent tasks like flaky-api-retry
- `add-step-by-step-numeric-ef0682` (lineage-5) — Add step-by-step numeric verification directive to SYSTEM_PROMPT.md to improve weak sqrt-67-digits task (0.67) with explicit calculation discipline
- `upgrade-spawn-do-model-f-a72fb4` (lineage-1) — Upgrade spawn_do model from claude-opus-4-8 to claude-opus-5 for stronger numeric reasoning on weak tasks like sqrt-67-digits
- `swap-spawn-do-to-claude-dac381` (lineage-6) — Swap spawn_do to claude-sonnet-4-5 with reasoning enabled for better cost-quality tradeoff while maintaining reasoning capability for numeric tasks
- `swap-spawn-do-to-nemotro-705f5b` (lineage-7) — Swap spawn_do to nemotron-3.5-lightning local model with zero marginal cost to explore unexplored model family and cost-quality frontier
- `downgrade-spawn-do-model-c01497` (lineage-1) — Downgrade spawn_do model from claude-opus-4-8 to claude-haiku-4-5-20251001 to test prompting vs compute bottleneck
- `flowchart-in-soul-lite-d-545e3d` (lineage-1) — Flowchart: in soul-lite-do.json bare_do block, replace the trailing 'Be concise' with an explicit verify-before-finalizing step (trace code against the given examples/edge cases, recompute numeric answers, fix failures) — targets the weakest measured criterion (verification 2.6/3, correctness already 3.0) at zero added model cost.
- `in-soul-lite-do-json-app-14f48c` (lineage-1) — In soul-lite-do.json, append a self-verification step to the BARE_DO_IT doer prompt (re-check every requirement, trace code against edge cases, recompute numbers before finalizing) — targets the weakest rubric criterion, verification (2.6/3.0).

## Queued (future) (10)

- `enhance-system-prompt-md-af2ba2` (lineage-1) — Enhance SYSTEM_PROMPT.md with validation and API error handling guidance for weak tasks
- `create-kb-refs-validatio-334b1b` (lineage-1) — Create kb/refs/validation.md with user validation and API reliability patterns
- `downgrade-spawn-do-model-997bdb` (lineage-1) — Downgrade spawn_do model from claude-opus-4-8 to claude-sonnet-4-5 for cost-efficient reasoning
- `try-local-model-nemotron-cd23f5` (lineage-1) — Try local model nemotron-3.5-lightning as main classifier for zero-cost region exploration
- `model-set-the-main-sessi-c4057a` (lineage-1) — Model: set the main-session model in config-meta.json to nemotron-3.5-lightning (local vLLM, rel_cost 0, reasoning) — the main session only classifies ref-topics, so a zero-cost reasoning model that matches classification quality wins on quality-per-cost versus haiku.
- `model-swap-the-spawn-do-142ed5` (lineage-1) — Model: swap the spawn_do doer model from claude-opus-4-8 to nemotron-3.5-lightning (local vLLM, rel_cost 15 -> 0) — replaces the cost center with a zero-cost reasoning model; large quality-per-cost win if doer quality holds.
- `system-prompt-add-one-di-5e1b6b` (lineage-1) — System prompt: add ONE directive to SYSTEM_PROMPT.md telling the doer to verify code by tracing the given test cases and to recompute numeric outputs before finalizing (no 'concise' clause) — targets verification 2.6/3 from the config-level prompt locus.
- `in-soul-lite-do-json-bar-f25fc7` (lineage-1) — In soul-lite-do.json BARE_DO_IT, replace 'Be concise' with a correctness-and-completeness-first directive — remove brevity pressure that suppresses edge-case handling and verification.
- `in-soul-lite-do-json-add-7f4230` (lineage-1) — In soul-lite-do.json, add a dedicated REVIEW prompt block between BARE_DO_IT and END that re-examines the draft answer for correctness and edge cases before finalizing (single structural flowchart knob).
- `swap-the-spawn-do-doer-m-00bef3` (lineage-1) — Swap the spawn_do (doer) model override in flowchart-main.json from claude-opus-4-8 to nemotron-3.5-lightning (local vLLM, rel_cost=0) — probe the unexplored local-model region for quality-per-cost.

## Used as seed (0)

- (none)

## Retired (0)

- (none)
