# AGENTS: READ THIS FIRST

**There is no "optimizer." There is no "recursion phase." There is no second population.
If you are about to build, score, migrate, sample, or reason about an "optimizer bundle,"
you are repeating a mistake that has now burned ~20 hours across at least four sessions.**

This file exists because agents keep re-deriving the same wrong architecture from
plausible-looking artifacts in this repo. Every artifact that supports the wrong version is
listed in §5 so you can recognize it and discard it. The human's verbatim words are in §1–§3.

> **STATUS as of 2026-08-16 15:0x UTC.** The cleanup ("R0") is **done**. `dict/configs/` now
> holds exactly four real soul-configs; `optimizer-seed` and `cand-…855` are gone. The loop's
> own flowcharts/scripts/prompts moved **out** of the dictionary to the autoresearch root:
> `user-data/autoresearch/{flowchart,scripts,prompts}/`. Verified: `ucb_pick` now returns real
> configs (20 draws → 11/5/3/1 across the four), and all four pass `PREP_PARENT_ENTRY` and
> `PREP_PARENT_MODEL`. **`dict/configs/optimizer-seed/` no longer exists — references to it
> below are historical.** Not yet committed.

---

## 1. What the recursion actually is

The human stated it exactly, twice, on 2026-08-16. Quote, verbatim:

> **06:38** — "Okay, we're going to do a simpler redesign. Our configs accept a prompt as an
> argument, not a flowchart. `mutate-config.json` needs to be reduced to a single prompt. In
> `run-experiment.json`, the command call to mutate-config needs to be replaced with a call to
> the sampled config with the mutate-config prompt as the arg. **THIS IS THE RECURSION.**
> Orchestrator spawning run-experiment is correct."

And nine minutes earlier, the correction that produced it:

> **06:29** — "Here's how recursion is supposed to work. Currently orchestrator spawns
> run-experiment and passes sampled config to run-experiment. **THAT IS FUCKING WRONG.** It's
> supposed to spawn the sampled config and pass run-experiment to it. **THAT'S THE FUCKING
> RECURSION.**"

Two refinements the human added:

> **06:43** — "the mutation prompt should thoroughly capture everything that was in the
> mutate-config flowchart, including the deterministic bits. mutate can become spawn."

> **07:03** — "when passing the mutate prompt to the sampled config, we should append a message
> like `Knowledge base: {KB_FILE}` so that the KB part of the config is not lost."

### In one sentence

**A soul-config improves itself by being spawned and handed the mutation prompt.**
The config under test *is* the mutator. That is the entire recursion. It is a
**runtime call direction**, not a data-model trick.

### This is already implemented and correct

`user-data/autoresearch/flowchart/run-experiment.json`, block `mutate`:

```json
"mutate": {
  "type": "spawn",
  "agent_name": "mutator",
  "command_name": "{{parent_entry}}",   <- the SAMPLED CONFIG's own entry flowchart
  "search_path": "{{parent_dir}}",      <- resolved from the sampled bundle
  "model": "{{parent_model}}",          <- the sampled config's own model
  "arguments": "{{mutate_args}}"        <- prompts/mutate-config.md + its KB appended
}
```

**Do not "fix" this. It is the thing the human asked for.** If you are tempted to change it,
you have misread this document.

---

## 2. What the recursion is NOT — three wrong versions, all killed

Three different wrong answers have been shipped. Learn all three, because the artifacts for
all three are still on disk and they contradict each other, so "the code says X" is not a
defense.

### WRONG v1 — "the optimizer is a scored bundle in the dictionary" (2026-08-15 10:30)

A session re-derived recursion as: *"everything the loop touches is one artifact: a scored
config bundle... 'optimize the optimizer' falls out the moment you drop the optimizer's own
bundle into the dictionary."* It then hit the obvious wall — how do you score a
config-producing config against a coding dataset? — and invented a sandboxed nested mini-loop
(`score_optimizer.py`, §4.3) plus a `kind: "target" | "optimizer"` split and a `SAMPLE_KINDS`
filter to keep the two apart.

Human's verdict:

> **08-16 03:00** — "what you just described about targets and the optimizer makes absolutely
> no fucking sense so im convinced the implementation is wrong. **the recusion is from feeding
> the autoresearch flowchart directly into soul. it is not from treating the optimizer
> flowchart as a soul config.** what the actual fuck"

> **08-16 03:22** — "why the fuck did 10:30 and 11:28 happen? why didnt you just fucking follow
> directions?"

**Note the mechanism of this failure**, because you are susceptible to it: the drift used
*the same vocabulary* as the spec — config, bundle, dictionary, optimizer — so it felt like
elegantly restating the human rather than replacing their mechanism. It swapped a **runtime**
mechanism for a **data-model** one and no alarm fired.

### WRONG v2 — "recursion = a config is fed into `/soul`" (2026-08-16 03:19–03:54)

The audit that correctly diagnosed v1 then invented its own wrong answer and wrote it into
`PLAN.md` §3/§6 and `TODO.md` as the ground rule. The human killed it on sight:

> **08-16 06:29** — *"Recursion = a config runs by being fed into /soul (soul-flow style)."*
> **"No, dude, holy fucking shit, how many fucking times are you going to get this wrong in so
> many stupid fucking ways."**

v2 inverts the nesting. `/soul` as an outer harness running the config as payload is
**backwards**. The config is the thing that gets spawned and runs the research work; the
mutation prompt is the payload handed *to* it.

**`PLAN.md` and `TODO.md` still assert v2 as of this writing.** See §5.

### WRONG v3 — "Phase 4 / P4 — Recursion" as a build phase

`TODO.md` §P4 instructs: *"Package the `worker-loop` flowchart ... as a soul-config bundle in
the SAME `dict/configs/`."* **That single line is why `dict/configs/optimizer-seed/` existed.**
It is still present in `PLAN.md` line 187 — see §5.

The human on whether P4 was ever wanted:

> **08-16 13:04** — "what is phase-4 recursion? is that actually something i want or is that an
> artifact of your past 24 hours of intense stupidity?"

> **08-16 13:10** — "Okay, so it is literally *exactly* your stupidity down to the word, thanks
> for wasting my time"

**P4 is agent invention. It was never requested. Do not execute it. Do not restore it.**

---

## 3. Why an "optimizer bundle" in the dictionary is not merely unwanted — it is broken

**This is now fixed. It is kept because it is the strongest argument against reinstating it.**
It broke the loop totally, which is how this file came to be written.

1. `optimizer-seed` sits in `dict/configs/` and is therefore sampled as a mutation parent.
2. It has never been evaluated (`score.json`: `n_evals: 0`).
3. `ucb_pick.py` gives never-scored bundles absolute priority (must-explore branch).
4. So it is selected on **100%** of draws — verified, 20/20 — and every epoch dies in
   `run-experiment`'s `PREP_PARENT_ENTRY`.
5. `epoch.log`: **217 of 220 epochs ever recorded** read `parent=optimizer-seed`.

The `SAMPLE_KINDS=["target"]` filter used to hide this. R0 correctly deleted the filter but
**left the bundle**, which promoted a dormant drift artifact into a 100% failure. Deleting the
guard without deleting the thing it guarded is worse than either alone.

**Resolution:** the bundle was removed from the dictionary entirely — its flowcharts, scripts
and prompts were moved to the autoresearch root. **No filter was re-added.** The optimizer
cannot be sampled because it is not in the dictionary, not because something skips it. If you
ever find yourself adding a filter, a `kind` field, or a skip-list to keep the optimizer out of
the sampler, you have put it back in the dictionary and should undo that instead.

> **08-16 14:45** — "what's stopping us from delete it altogether?"
> **08-16 14:48** — "wait, you're saying that optimizer-seed is a superdirectory? but like why?
> why do we need this?"
> **08-16 14:22** — "**WE'RE NOT SUPPOSED TO HAVE THIS STUPID RECURSION OPTIMIZER SHIT**"

---

## 4. Rationalizations you will reach for. All of them are wrong.

You will encounter evidence that seems to authorize the optimizer-bundle design. Here is that
evidence and why it is worthless. **Every item below was actually used by an agent, in good
faith, to justify the wrong thing.**

| You will think | Why it is wrong |
|---|---|
| "`ucb_pick.py` says *'The dictionary is one uniform population: every bundle is sampled, with no target/optimizer split'* — so sampling the optimizer is deliberate." | That is an **agent-authored docstring**, written *while half-stripping the drift*. It is the drift describing itself. A comment that explains *why* is rationale wearing the authority of code that executes. The code's **behaviour** is verifiable; its stated **justification** is not evidence of anything. |
| "The human said at 11:28 they wanted *one uniform population* — so the optimizer belongs in it." | Read what 11:28 was answering. It rejected a **human gate on promoting a mutated optimizer**. It is not a ruling that the optimizer is a dictionary member. The human's actual complaint was that a *gate* was over-engineered, not that the optimizer should be sampled. |
| "The human said at 10:27 the loop pointing at itself *'should be its design from the start'* — so recursion is wanted." | **Correct — recursion IS wanted.** But §1 is the recursion. Self-improvement happens because the sampled config *is* spawned as its own mutator. It does not require the optimizer to be a scored dictionary entry. Wanting recursion does not license v1/v2/v3. |
| "`cand-1786842874097707855` has `parent_id: optimizer-seed`, so the optimizer has been mutated before — it must be intended." | That candidate is a **drift artifact**. It still ships `score_optimizer.py` and still carries `"kind": "optimizer"` in its `score.json`. It is evidence the mistake happened, not evidence it was wanted. `TODO.md` R0 explicitly lists it for deletion. |
| "`PLAN.md`/`TODO.md` say recursion is a config fed into `/soul`." | Both documents predate the human's 06:29 correction and are **contaminated**. See §5. |
| "`TODO.md` Phase P4 tells me to put the worker-loop flowchart in `dict/configs/`." | P4 is agent invention, confirmed by the human at 13:04–13:10. See §2 WRONG v3. |
| "The optimizer's improvement score was 0.889 vs 0.8 baseline — it works." | The measured noise floor for this benchmark is **0.1333** (`null-controls.json`). That "improvement" of 0.0889 is **below noise**. It is not evidence of anything. |
| "It's already built, so ripping it out is a bigger change than making it work." | Making it work is what four sessions did. That is the trap. |

---

## 5. Contaminated sources — do not build from these

| Artifact | Status |
|---|---|
| `user-data/agents/hackathon-rnd/PLAN.md` **line 187** | **STILL WRONG (v3), verified 08-16 15:0x.** The phase roadmap still reads: *"**P4 — Recursion.** Put the worker-loop flowchart into the dictionary as a soul-config."* **This is the exact instruction that created `optimizer-seed`.** §6 was corrected; this line was missed. Do not execute it. |
| `user-data/agents/hackathon-rnd/TODO.md` **lines 8–9** | **STILL WRONG (v2), verified 08-16 15:0x.** Ground rule #2 still reads: *"Recursion = the worker-loop flowchart is a soul-config fed into `/soul`, **in the ONE dictionary**, optimized by the SAME loop."* Phase P4 itself was correctly deleted at line 86, but this ground rule still asserts the loop belongs in the dictionary. |
| `user-data/agents/hackathon-rnd/PLAN.md` §6 line 152 | **PARTLY FIXED.** Still opens *"The worker loop is a flowchart fed into `/soul`… That is the entire recursion"* (v2 phrasing the human rejected at 06:29), then correctly describes the MUTATE spawn block. Trust the second half, not the first sentence. |
| `user-data/agents/hackathon-rnd/IMPLEMENTATION.md` | Marked contaminated by the 03:19 audit; never fully retired. |
| `user-data/agents/hackathon-rnd/REBUILD-PLAN.md`, `REBUILD-TODO.md` | Superseded interim docs, same v2 error. R2/R3 deleted; residual v2 phrasing remains. |
| `ucb_pick.py` module docstring (the "one uniform population" line) | Agent-authored rationale for the drift. Ignore the justification. The behaviour it describes was real and was exactly the bug. |
| `dict/configs/cand-1786842874097707855/` | **DELETED by R0.** Was a drift artifact shipping `score_optimizer.py` and `kind: "optimizer"`. Do not restore it from git history. |
| `dict/configs/optimizer-seed/` **as a dictionary member** | **DELETED by R0.** The v3 artifact. Its flowcharts were the live loop and were fine — they now live at `user-data/autoresearch/flowchart/`. Its *presence in `dict/configs/`* was the bug. |

**General rule that would have prevented all of this:** a document or code comment written by
an agent is not evidence of what the human wants — however authoritative its tone, and even
when it claims to quote them. Find the human's actual words in Discord history and cite those.
The full message log is at `logs/discord-wire.jsonl` (`user_name == "px_chyper"`); it covers
the project from 2026-08-15 05:11 onward and is the primary source.

---

## 6. If you think this document is wrong

You are probably experiencing the exact failure it describes: a cleaner-feeling reframing that
uses the same vocabulary and therefore feels like agreement rather than replacement.

Before acting on that feeling:

1. Read `logs/discord-wire.jsonl`, filtered to `px_chyper`, for 2026-08-16 06:29 and 06:38.
2. Quote the human's words back and diff them against what you are about to build.
3. If your change alters the *mechanism* the human specified, say so out loud as an explicit
   swap and get approval **of that specific swap**. A terse "proceed" or "update the plan" is
   not approval of an unflagged mechanism change.

> **08-16 13:13** — "Think you've done enough research to answer a question? Well you haven't.
> Think you already saw the answer? No that's you making a snap decision. Do more research and
> work harder."
