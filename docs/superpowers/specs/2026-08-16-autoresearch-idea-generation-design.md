# Autoresearch: Mutator-Generated Research Ideas

Date: 2026-08-16. Status: approved design, pending implementation plan.

## Problem

The autoresearch loop has no way to originate a research idea. Every mutation
it performs is transcribed from a human-authored queue, and when that queue
empties the loop degrades rather than stopping.

Concretely:

- Both secretaries are pure `input`-block loops
  (`extensions/idea-secretary/commands/idea-secretary.json`, `timeout_seconds:
  604800`), waking only on a human Discord message. `schedules.json` is `[]`,
  so nothing else fires them. Neither generates ideas.
- With an empty queue, `orchestrator.json:41` (READ_IDEA) emits the literal
  string `idea-default-noop`. `run-experiment.json:65` (PREP_MUTATE_ARGS)
  fails to find it in the queue file and falls through to `IDEA="$2"`, so
  `@@IDEA_TEXT@@` becomes the bare token `idea-default-noop`. The mutation
  still runs, is still scored, and is still kept or discarded — an unguided
  coin flip, not a skip.
- `prompts/mutate-config.md:19` is a single `@@IDEA_TEXT@@` substitution, and
  §3 (`:21-38`) is a *selection* task over four enumerated targets. The
  sampled config is therefore scored on applying someone else's one-line
  instruction, not on producing a hypothesis.
- Nothing ever writes `[x]` to `research-ideas.md`. The teacher closes its
  loop (`teacher.json:108` seds `[~]`→`[x]` on `dataset-ideas.md`), but
  `run-experiment.json:143` (SIGNAL) only appends to `epoch-signals.log`. The
  "tried" state is unreachable.

The consequence for Phase 4: `run-experiment.json:4` claims *"the config being
optimized is therefore the agent that performs its own mutation, so a better
config mutates better."* With the idea injected, hypothesis quality is held
constant by the human queue and cannot be selected for. The only heritable
trait under selection is edit-application fidelity.

## Background facts (verified)

**Primary source.** The project spec is two Discord attachments in
`#spark-axi-master` (`1538057232246247466`): `message.txt` at
2026-08-15T05:52:09Z, superseded by an expanded respec at 2026-08-15T06:01:18Z
(attachment-only message). `PLAN.md:20`'s "Spec (06:01, verbatim)" quote checks
out against the second.

The spec does not describe a human-only idea source:

- Karpathy reference, step 2: *"Edit `train.py` with an experimental idea."*
  No queue, no human.
- *"We want an agent that humans can talk to that maintains a research ideas
  document **and talks with workers as needed**."*
- Secretary system prompt: *"Whenever **another agent** provides a query,
  that's generally **a question about what research idea to try next**…"*
- Worker loop steps 2-3: *"Ask the secretary queue to **provide** an
  experimental research idea… **choose** an experimental idea, and **update
  both the research idea document** and the soul configuration."*

**The plan, however, does encode secretary-as-sole-source.** `PLAN.md:51`
(loop step 2) and `PLAN.md:112` (parts table); `IMPLEMENTATION.md:69`
(READ-IDEA as a `head`) and `:107` (PLAN-EDIT "given parent + `idea_text`").
The build is faithful to the plan; the plan omits generation. Amending the
plan is therefore in scope (see below) — `PLAN.md:37-40`'s drift rule forbids
a builder from adding it unilaterally.

**Agent-to-agent messaging was specified and never built.**
`IMPLEMENTATION.md:70,93` specify `axi_send_message idea-secretary "claim
<id>"` and `"done <id>"`. There are zero `axi_send_message` calls in the
bundle. `orchestrator.json:4` records the substitution: *"Idea-secretary
signalling stays file-based."*

**The secretary prompt has a dead inbound case.** It instructs the agent to
respond to a `done`/`claim` signal *"via `epoch-signals.log` or a direct
note."* Nothing emits it, and an `input`-block agent cannot observe a log
file — it wakes only on a Discord message.

**The mutator is the sampled config running its own flowchart.**
`run-experiment.json:75-77` spawns `command_name={{parent_entry}}` with
`search_path={{parent_dir}}/flowchart`. For `cand-baseline-sonnet` that is
`/soul-lite`, a generic task flowchart, receiving `mutate-config.md` as its
message argument (`$5`). We author `run-experiment.json`; we do not author the
flowchart the mutator runs, and it changes as configs evolve.

**The orchestrator's K claims are serial.** `orchestrator.json` connections
c4-c10 form a sequential in-process loop: `SAMPLE → READ_IDEA → CLAIM_IDEA →
FANOUT → INCR_I → LOOP_BRANCH → SAMPLE`. Only `FANOUT` is fire-and-forget, so
lineages run in parallel but claiming does not.

**Storage.** `user-data/autoresearch/` is ext4 on local nvme.
`research-ideas.md` is **not** git-tracked — no history, no recovery from a
clobbered write.

**Two latent bugs this design must not inherit:**

- `record_experiment.py:27` defaults the timestamp to the literal string
  `"2026-08-15"`, and `run-experiment.json:134` never passes one. Every
  `results.tsv` row carries that date, including rows written on 08-16.
- `run-experiment.json:65` strips the state marker with `sed -E 's/^- \[[ x]\]
  *//'`, whose class excludes `~`. Since `CLAIM_IDEA` always flips to `[~]`
  before `FANOUT`, idea text reaching the prompt is always prefixed `- [~] `.
  Resolved by removal — that code path goes away.

## Decisions taken

| # | Decision |
|---|---|
| 1 | Secretary ideas are claimed **exclusively** — one lineage per idea. |
| 2 | Mutator-generated ideas become **first-class entries** in the shared doc. |
| 3 | Storage is an **append-only event log** with the markdown as a rendered view. |
| 4 | The **orchestrator** claims the seed idea and passes it to the mutator; the mutator does not message the secretary. |
| 5 | A used seed enters a fourth state, **`used-as-seed`**, linked to the candidate it produced. |
| 6 | Bypass handling is **drift detection**, not toolset restriction. |

## Design

### 1. Storage model

```
$AUTORES_ROOT/ideas/events.jsonl    # append-only truth
$AUTORES_ROOT/ideas/.lock           # flock sidecar — claim + render only
$AUTORES_ROOT/research-ideas.md     # GENERATED view, path unchanged
```

Paths honour `AUTORES_ROOT` so sandboxed runs do not pollute the real pool.
(Contrast `models_lib.py:17`, which deliberately reads the real root because
models are a global fact of the box; ideas are not.)

One JSON object per line:

```json
{"ts":"…Z","op":"propose","idea_id":"…","origin":"lineage-2|secretary|human","text":"…"}
{"ts":"…Z","op":"claim","idea_id":"…","lineage":"lineage-2","purpose":"seed|test"}
{"ts":"…Z","op":"complete","idea_id":"…","candidate_id":"cand-…","score":0.9111,"kept":true}
{"ts":"…Z","op":"seeded","idea_id":"…","produced":"…"}
{"ts":"…Z","op":"retire","idea_id":"…","by":"secretary","reason":"…"}
```

State is a **fold** over the log, never an in-place edit:

| Ops seen | State |
|---|---|
| `propose` | `future` |
| `+ claim` | `present` |
| `+ complete` | `tried` (with a list of attempts — one idea may have N) |
| `+ seeded` | `used-as-seed` |
| `+ retire` | `retired` |
| `claim` older than the stale threshold, no terminal op | `stale` → auto-released |

Terminal ops (`complete`, `seeded`, `retire`) resolve **last-wins by log
order** — an idea used as a seed and later tested on its own merits ends
`tried`; one retired afterwards ends `retired`. No special-case precedence.

`tried` being derived is what makes the currently-unreachable state reachable
by construction rather than by adding another writer.

### 2. Operations and locking

| Command | Writers | Lock |
|---|---|---|
| `propose <text>… --origin X` | K mutators, concurrent | no |
| `complete --idea-id … --candidate … --score … --kept …` | K run-experiments, concurrent | no |
| `seeded --idea-id … --produced …` | K run-experiments, concurrent | no |
| `retire --idea-id … --reason …` | secretary | no |
| `claim --lineage L [--idea-id I] [--purpose seed\|test]` | orchestrator (serial, `seed`) + K mutators (`test`) + secretary | **yes** |
| `show [--json]` | any | no (read-only fold) |
| `render` | orchestrator at epoch boundary; secretary | **yes** |

Critical section, the only contended path:

```
flock(.lock) → fold log → verify target still in `future`
             → append claim record → release
```

Milliseconds, with no LLM turn inside it. `render` locks because it is a
whole-file rewrite, and is called **explicitly** (epoch boundary or by the
secretary) rather than after every append — otherwise K lineages race to
re-render on every `propose`.

Mutator `test`-claims take the same locked path, but **cannot contend**: the
id is freshly minted by that lineage and content-hashed, so no other writer
can hold it. They are serialized for uniformity, not for correctness. Real
contention exists only between the orchestrator's serial `seed`-claims and an
occasional secretary edit.

**Append safety.** POSIX makes `O_APPEND`'s seek-to-end-plus-write atomic, so
concurrent appenders cannot clobber each other's offsets; Linux holds the
inode rwsem across a single `write()`, so one-line records do not interleave
on ext4. This constrains the implementation: the tool must `os.write()` one
pre-encoded buffer to an `O_APPEND` fd, **not** Python's buffered
`open(…, "a")`, which may split a record across syscalls.

**Empty pool** is a normal state. `claim` exits nonzero and the mutator
proceeds unseeded — replacing the `idea-default-noop` degenerate path.

### 3. The mutator's contract

Everything the mutator does is instructed in prose and executed with its own
`Bash`, because we do not author its flowchart. That splits the work:

- **LLM-driven, best-effort:** generate, filter, `propose`, test-`claim`.
- **Deterministic, in `run-experiment`:** `complete` and `seeded`.

If the mutator misbehaves the pool simply does not grow (soft failure), but
the tried-record still lands. Completion must not depend on model compliance.

**Evidence pack** — a new `config_evidence.py`, the config-side analogue of
`find_blind_spots.py`, substituted as `@@EVIDENCE@@`:

- `ideas.py show` — folded tried/present/future/used-as-seed state
- the parent's `score.json` `per_task` — where this config is weak
- `results.tsv` rows on this parent's lineage — what was tried and what it scored
- the existing `@@AVAILABLE_MODELS@@` block

Prerequisite: fix `record_experiment.py:27`'s hardcoded timestamp. The
evidence pack is worthless if every row claims the same date.

**Rewritten `mutate-config.md` §2**, replacing the single `@@IDEA_TEXT@@` line:

| Step | Content |
|---|---|
| 2a Evidence | `@@EVIDENCE@@` |
| 2b Inspiration | `@@SEED_IDEA@@` — may be empty |
| 2c Generate | ≥`IDEA_GEN_MIN` distinct specific single-knob hypotheses, each naming exact target and predicted effect. May build on 2b; must not merely restate it. |
| 2d Filter | judge which are worth an eval, given 2a |
| 2e Deposit | `ideas.py propose` each survivor, `--origin lineage-N` |
| 2f Choose | `ideas.py claim --purpose test --idea-id <pick>`; nonzero exit ⇒ next pick |

Existing §3-§6 (apply / stamp / report) are unchanged.

**`idea_id` flows upward through the manifest.** `mutate-config.md` step 5
already has the mutator write `idea_id` into the candidate's `manifest.json`.
Change `compare_scores.py:28` to prefer that value, falling back to `argv[3]`
— backward compatible, and the only plumbing change needed.

Mint `idea_id` as a lineage prefix plus a content hash so two lineages
generating the same hypothesis cannot silently merge.

### 4. The secretary's contract

Three changes to `idea-secretary.json`'s RESPOND prompt:

1. **Add the missing case** from the spec: *an agent asks what to try next* →
   propose one, from the pool or freshly generated.
2. **Writes go through `ideas.py`** (`propose` / `retire`). The markdown is
   generated; a direct `Edit` is lost at the next `render`.
3. **Add the curation job.** Mutators deposit several hypotheses per lineage
   and consume one, so `future` grows monotonically. Retiring duplicates and
   dead ends is the secretary's new role — it stops being the source and
   becomes the editor.

**Delete** the `done`/`claim` signal case. It has no sender and names a
transport the agent cannot receive on; the tool records those itself.

### 5. Orchestrator changes

`READ_IDEA` and `CLAIM_IDEA` (`orchestrator.json:37-53`) stay in place and
swap grep/sed for `ideas.py claim --purpose seed --lineage lineage-{{i}}`.
Because the loop is serial, the K seeds are distinct without further
coordination, satisfying decision 1.

`LOG_EPOCH` gains `ideas.py render` as the explicit render point.

Accepted cost: the orchestrator picks the seed blind (top of pool) before any
evidence is read, where a mutator-side pick could match the seed to the
parent's weaknesses. Small, because the seed is only inspiration and the
tested hypothesis is generated fresh.

### 6. Migration

Reconstruct `events.jsonl` by joining `research-ideas.md` (proposals) with
`results.tsv` (completions) on `idea_id`. All six markdown entries have result
rows, and the markdown is wrong about every one of them:

| markdown | results.tsv |
|---|---|
| `[~]` idea-do-sonnet | 0.8889, discarded |
| `[~]` idea-main-opus | 0.8444, discarded |
| `[~]` idea-kb-concise | 0.8889, discarded |
| `[~]` opt-main-sonnet | 0.8889, kept |
| `[ ]` idea-do-haiku | ran twice — 0.9111 kept |
| `[ ]` idea-main-haiku | 0.9333, kept |

Two details the join forces: `swap-main-model-to-claude-sonnet-5` appears in
`results.tsv` with no markdown entry (synthesize a `propose`), and
`idea-do-haiku` has two completions (one idea, N attempts).

The migration doubles as the design's first test: `tried` populates on day one
from evidence, which is the memory the generator reasons from.

### 7. Failure modes

| Mode | Handling |
|---|---|
| Mutator skips `propose` | Pool does not grow; `complete` still fires. Self-heals next epoch. |
| Mutator dies between claim and complete | Idea stuck in `present`. **This is the current bug** — four `[~]` entries are exactly this. `claim` records `lineage` + `ts`; the fold marks entries older than `IDEA_STALE_SECONDS` and `render` auto-releases them. Without the reaper this design reproduces what it replaces. |
| Duplicate hypothesis across lineages | Content-hashed `idea_id` prevents silent merge. |
| Log growth | Fold cost is linear; irrelevant at current scale. Compaction is deferred, not built. |

### 8. Enforcement

**Drift detection, not toolset restriction.** `render` stores a hash of what
it wrote; on the next `render`, if the file no longer matches, it refuses to
overwrite and reports the divergence. Cheap, needs no coordination, and turns
a silently-lost edit into a visible error. Restricting the secretary's
`Edit`/`Write` would also work but is coarse and blocks unrelated file work.

## Spec amendments required

Not optional — left alone, the next builder reverts this design, and
`PLAN.md:37-40` forbids them from re-deriving it unilaterally.

- `PLAN.md:51` — loop step 2 is no longer "read the top unclaimed idea".
- `PLAN.md:112` — the secretary is not the idea source; and "the loop only
  *reads*/marks the file" is now false, since the loop proposes.
- `IMPLEMENTATION.md:69` — READ-IDEA is a claim, not a `head`.
- `IMPLEMENTATION.md:107` — PLAN-EDIT generates rather than receiving `idea_text`.
- `IMPLEMENTATION.md:70,93` — the `axi_send_message` lines describe a
  mechanism we are deliberately not building; restate as file-mediated so the
  gap stops reading as an oversight.

## Non-goals

- Agent-to-agent messaging between mutator and secretary. Decision 4 keeps the
  link file-mediated, preserving `PLAN.md:112`'s property that a message never
  interrupts a running epoch.
- Event-log compaction.
- Any change to the teacher / `dataset-ideas.md` half, which already has the
  autonomous-default pattern this design ports (`teacher.json:14,23,40`).
- Provider/model-registry gaps in the autoresearch model allowlist — a
  separate, unrelated finding.

## New tunables

Following the existing `tunables.json` pattern (file value overrides script
default; env var overrides the file — see `scripts/tunables.py`):

| Knob | Default | Env | Rationale |
|---|---|---|---|
| `IDEA_STALE_SECONDS` | `21600` | `AUTORES_IDEA_STALE_SECONDS` | Matches `orchestrator.json:80`'s join timeout. A lineage cannot outlive its own join, so a claim older than this is definitionally dead. |
| `IDEA_GEN_MIN` | `5` | `AUTORES_IDEA_GEN_MIN` | Minimum hypotheses generated at step 2c before filtering. Large enough that filtering is a real selection; small enough not to dominate the mutator's turn. |

## Open items

- `research-ideas.md` is untracked. Whether `events.jsonl` should be committed
  is undecided; it is the only durable record of the search once this lands.
  This is a repo-policy call, not a design one.
