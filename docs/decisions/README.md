# Architecture decision records

Every resolved wayfinder ticket (`wayfinder:grilling`, `wayfinder:research`,
`wayfinder:prototype`) lands here as one numbered file. The map in
[#1](https://github.com/Sehaan-1/switchback/issues/1) tracks *that a decision exists*;
this directory holds *why*, with the evidence attached so a reader in six months can
disagree with the reasoning instead of the conclusion.

```
NNNN-short-slug.md      #12 -> 0002-...  (one per resolved ticket, numbered in merge order)
```

Format, in this order, with nothing skipped:

1. **Decision** — imperative, specific, ≤ 5 bullets. If a reader only reads this section they
   must be able to implement from it.
2. **Context** — the two or three facts about the project's shape that change how the
   evidence should be read.
3. **Consideration-by-consideration analysis** — one section per bullet in the ticket, and
   every number either measured (with a reproducible command) or labelled as a model with
   its constants inline. A claim that cannot be traced is deleted, not softened.
4. **Payload for dependent tickets** — what later tickets must design against. This is the
   point of an ADR: it is a constraint handed forward, not a retrospective.
5. **Consequences and design rules that follow** — including the costs accepted, named, and
   the rules (R1, R2, …) later code review enforces.
6. **Reopen triggers** — the falsifiable numbers that would reverse this decision. A decision
   with no reopen trigger is a preference, not a decision.
7. **Alternatives considered** — including the strongest steelman of the option not chosen.
   If the winning argument here is weaker than the losing one, say so.

House rules:

- **No fabricated benchmarks.** If the environment cannot run it, the file says so and states
  the claim as a bound or a model. Order-of-magnitude reasoning is welcome and marked;
  invented precision is not. Synthetic-scenario magnitudes are labelled as properties of the
  scenario, never as expected production impact.
- **Counter-evidence is part of the record.** If research contradicts the framing of the
  ticket, the ADR says the framing was wrong (see ADR-0001 §3 on Rust prior art in payment
  routing) rather than quietly dropping the citation.
- **Placeholders become numbers.** Where the spec says `≤Y ms` or `+X pts`, a resolving ADR
  proposes the concrete value and names the ticket that owns the final wording.
- Superseding a decision means a new file that links the old one and marks it
  `Superseded by ADR-NNNN`. Never edit an accepted decision's conclusion in place.

| # | ADR | Resolves | Status |
| --- | --- | --- | --- |
| [0001](0001-engine-language-go.md) | Lock Go for the routing engine core; Python is a cold-path language | [#2](https://github.com/Sehaan-1/switchback/issues/2) | Accepted |
| [0002](0002-reward-function.md) | Two-part reward: end-to-end Bernoulli label, priced ambiguity, no 3DS multiplier | [#4](https://github.com/Sehaan-1/switchback/issues/4) | Accepted |
| [0003](0003-bandit-not-static-table.md) | Routing algorithm: stochastic bandit, Beta-Bernoulli Thompson sampling, not a static table | [#3](https://github.com/Sehaan-1/switchback/issues/3) | Accepted |
| [0004](0004-constraint-layer.md) | ConstraintSet: a closed-vocabulary document, filtered before sampling | [#5](https://github.com/Sehaan-1/switchback/issues/5) | Accepted |
| [0005](0005-simulation-harness.md) | Simulation harness: one processor interface, scenarios as content-addressed documents, key-derived determinism | [#6](https://github.com/Sehaan-1/switchback/issues/6) | Accepted |
| [0006](0006-thompson-sampling-implementation.md) | Thompson sampling implementation: arm space, update protocol, hot path, concurrency, cold start, persistence | [#7](https://github.com/Sehaan-1/switchback/issues/7) | Accepted |
| [0007](0007-drift-detection.md) | Drift detection: ADWIN at processor level, tiered decay, and onboarding exploration floor | [#8](https://github.com/Sehaan-1/switchback/issues/8) | Accepted |
| [0008](0008-censored-exploration.md) | Censored data and exploration: bandit feedback scope, no standing forced exploration, DECISION_LOG v1, delayed outcomes | [#9](https://github.com/Sehaan-1/switchback/issues/9) | Accepted |
