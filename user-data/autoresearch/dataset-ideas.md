# Dataset Ideas — benchmark-growth queue

Maintained by the **dataset-secretary** (a human-facing agent); read by the **teacher**.
`teacher.json`'s READ step reads this file to decide how to co-evolve gaia-testbench: new
judge *kinds*, multi-turn / simulated-user cases, and harder / held-out tasks aimed where
the top configs all currently pass (PLAN.md §1, §2e; IMPLEMENTATION.md §2.8). This
co-evolution is the anti-overfitting engine and the validity precondition for recursion
(PLAN.md §3.1).

Entry format (one benchmark change per idea, so the teacher's evolutions stay attributable):
`- [ ] <idea-id>: <one concrete benchmark-growth change>`
States: `- [ ]` unclaimed · `- [~]` in-progress (claimed) · `- [x]` done.

Seeded 2026-08-16 from the blind-spot scan: the top configs score identically (no
discrimination) on `fix-balanced-brackets`, `refactor-user-validation`, and
`flaky-api-retry` — those are the saturated tasks these ideas harden.

- [x] harden-brackets: Author a held-out variant of the saturated fix-balanced-brackets task with adversarial nesting and required empty-string / unmatched-tail edge cases, so it separates configs that only handle the easy cases.
- [ ] strict-refactor-verify: Author a refactor task whose verification rubric scores 3 only if the tool-call trace shows a behavior-equivalence test was actually run — the saturated refactor-user-validation gives verification credit too easily.
- [ ] concurrency-retry-holdout: Author a harder flaky-api-retry variant that requires correct exponential backoff + jitter + a max-retry cap, judged by recomputing behavior under repeated simulated failures.
- [ ] numeric-precision-oracle: Author a new high-precision numeric task (a different constant than sqrt-67-digits) with a strict last-digit correctness oracle, to keep the numeric-precision dimension discriminating.
- [ ] stateful-parser-task: Author a new build-from-scratch task (a small stateful tokenizer/parser) with a deterministic oracle, adding a from-scratch dimension beyond the current subset.
