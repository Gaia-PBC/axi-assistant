You are mutating your OWN config bundle to produce one candidate for an autoresearch optimizer.

You are the config at `@@PARENT_DIR@@` (id `@@PARENT_ID@@`), running as `@@LINEAGE@@`.
Do every step below yourself, using your Bash / Read / Write / Edit tools. Touch nothing
outside the paths named here.

**You generate the research idea.** You are not being handed one to apply. A seed may be
offered below as inspiration, but the hypothesis you test is yours, and the quality of your
hypotheses is part of what this loop is selecting for.

Single-edit discipline still holds: whatever you choose, exactly ONE knob changes, so the
experiment's effect on the score is attributable to that knob.

## 1. Copy yourself into the candidate directory

    mkdir -p "@@OUT_DIR@@"
    cp -a "@@PARENT_DIR@@/." "@@OUT_DIR@@/"

Then list the candidate tree and read `@@OUT_DIR@@/manifest.json` so you know what you are
working with. Everything from here on happens inside `@@OUT_DIR@@` — never edit the parent.

## 2. Decide WHAT to try

### 2a. The evidence

@@EVIDENCE@@

### 2b. Seed idea (inspiration only — may be empty)

@@SEED_IDEA@@

If that is empty, the pool had nothing to offer; generate unseeded. That is a normal state,
not an error.

### 2c. Generate at least @@IDEA_GEN_MIN@@ distinct hypotheses

Each one must be a SINGLE knob, must name its exact target, and must state the effect you
predict and why the evidence in 2a supports it. Vary the kind of knob — do not produce
@@IDEA_GEN_MIN@@ variations of a model swap. Valid targets:

- **model** — `manifest.main_model`, ONE entry of `manifest.spawn_block_models`, or ONE
  flowchart spawn-block model.
- **flowchart** — ONE block in ONE `flowchart/*.json` file.
- **kb** — `kb/SYSTEM_PROMPT.md` or ONE `kb/refs/*.md` file.
- **extension** — ONE entry of `manifest.extensions[]`.

You may build on the 2b seed, but do not merely restate it. Do not re-propose anything
listed as already tried in 2a unless you are changing it in a specific, stated way.

For a **model** target the value MUST be one of these exact ids — a candidate referencing
anything else is rejected before its eval is paid for, so the lineage is wasted. `rel_cost`
is a coarse ordering hint (higher = pricier); the loop optimizes quality-per-cost.
Null/empty means "inherit the process default".

**The model does not have to be Claude.** The soul being optimized is Axi's, whatever serves
it. Entries with a non-`anthropic` provider are locally served (ollama / vLLM): `rel_cost=0`
because there is no marginal token cost, traded against quality and latency. Routing is
automatic from the id alone — you do not write the provider anywhere. A local model is a
genuinely different region of the search space from a cheaper hosted tier, and the evidence
in 2a will not contain it until someone tries it.

@@AVAILABLE_MODELS@@

### 2d. Filter

State which of your hypotheses are worth spending an eval on, and why the others are not.

### 2e. Deposit the promising ones in the shared pool

For EACH hypothesis that survived 2d, run:

    @@PY@@ @@SCRIPTS@@/ideas.py propose "<the one-line hypothesis>" --origin "@@LINEAGE@@"

Each call prints the `idea_id` it minted — keep them. The ones you do not test stay queued
for later lineages, so deposit every survivor, not just your favourite.

### 2f. Claim the one you will test

Pick the single most promising, then:

    @@PY@@ @@SCRIPTS@@/ideas.py claim --lineage "@@LINEAGE@@" --purpose test --idea-id "<its id>"

If that exits non-zero another lineage got there first — pick your next choice and retry.
Record the id you successfully claimed; it is `<CLAIMED_ID>` below.

## 3. Apply that one edit

Apply it inside `@@OUT_DIR@@` only. Change ONLY that one thing — nothing else.

- Model edits: edit the JSON field in `@@OUT_DIR@@/manifest.json`. Keep valid JSON.
- Flowchart / kb edits: edit the named file under `@@OUT_DIR@@/`. Flowchart files must
  remain valid JSON.
- Extension edits: edit `manifest.extensions[]`.

Do not touch `manifest.id` / `parent_id` / `idea_id` here — step 4 sets those.

## 4. Stamp the lineage

Merge these three fields into `@@OUT_DIR@@/manifest.json`, leaving every other field intact
and the file valid JSON (indent 2):

    id        = @@CAND_ID@@
    parent_id = @@PARENT_ID@@
    idea_id   = <CLAIMED_ID>        # the id you claimed in 2f — NOT the seed id

That `idea_id` is how your hypothesis gets attributed in `results.tsv`. If it is wrong or
missing, your experiment is recorded against the wrong idea.

## 5. Report

Print, in order:

1. The hypotheses you generated and which you filtered out.
2. The id you claimed and the one-line reason you chose it.
3. A one-line before/after of the single value you changed.
4. Confirmation that `@@OUT_DIR@@/manifest.json` still parses.
