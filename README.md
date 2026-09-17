# switchback

A network-aware payment router: given a transaction (amount, currency, card BIN metadata,
3DS/SCA requirement, mandate flag, deadline, merchant floor margin), pick the processor and
fallback chain that maximize **authorization rate × margin** subject to latency, regulatory,
and network constraints — learning online with Thompson sampling and drift detection, and
explaining itself with recorded propensities.

## Status: design phase

We are resolving architecture decisions before writing the engine. Each decision is an issue
on this repo (`wayfinder:grilling` / `:research` / `:prototype`) and lands as an
[architecture decision record](docs/decisions/) with the evidence attached. The map is
[#1](https://github.com/Sehaan-1/switchback/issues/1).

Decided so far:

- [Engine core is Go; Python is a cold-path language](docs/decisions/0001-engine-language-go.md)
  ([#2](https://github.com/Sehaan-1/switchback/issues/2))
- [Reward function: two-part objective, end-to-end Bernoulli label, priced ambiguity]
  (docs/decisions/0002-reward-function.md)
  ([#4](https://github.com/Sehaan-1/switchback/issues/4))
- [Routing algorithm: stochastic bandit with Beta-Bernoulli Thompson sampling, not a
  static table](docs/decisions/0003-bandit-not-static-table.md)
  ([#3](https://github.com/Sehaan-1/switchback/issues/3))
- [ConstraintSet: a closed-vocabulary document, filtered before sampling, with the census,
  precedence and audit record that go with it](docs/decisions/0004-constraint-layer.md)
  ([#5](https://github.com/Sehaan-1/switchback/issues/5))
- [Simulation harness: one processor interface, scenarios as content-addressed documents,
  key-derived determinism](docs/decisions/0005-simulation-harness.md)
  ([#6](https://github.com/Sehaan-1/switchback/issues/6))
- [Thompson sampling implementation: arm space, update protocol, hot path, concurrency,
  cold start, persistence](docs/decisions/0006-thompson-sampling-implementation.md)
  ([#7](https://github.com/Sehaan-1/switchback/issues/7))
- [Drift detection: ADWIN at processor level, tiered decay, and onboarding exploration floor]
  (docs/decisions/0007-drift-detection.md)
  ([#8](https://github.com/Sehaan-1/switchback/issues/8))
- [Censored data and exploration: bandit feedback scope, no standing forced exploration,
  DECISION_LOG v1, delayed outcomes](docs/decisions/0008-censored-exploration.md)
  ([#9](https://github.com/Sehaan-1/switchback/issues/9))
- [Idempotency and double-charge prevention: the lease, the idempotent resend, and
  confirmed-terminal fallback](docs/decisions/0009-idempotency-double-charge.md)
  ([#10](https://github.com/Sehaan-1/switchback/issues/10))
- [3DS friction: the authentication record, the fee the ledger charges, and MIT's scope]
  (docs/decisions/0010-3ds-friction.md)
  ([#11](https://github.com/Sehaan-1/switchback/issues/11))
- [Engine module layout and interface contracts: one `internal/` tree, the acquirer seam,
  the fold-only write path](docs/decisions/0011-module-layout.md)
  ([#12](https://github.com/Sehaan-1/switchback/issues/12))

```
docs/decisions/     ADRs — one per resolved decision ticket
spikes/             small, reproducible probes that back a decision (not implementation)
constraints/        the ConstraintSet schema, the rule catalog, the examples and the gate over them
simulator/          the scenario schema, the worked scenarios, the golden files and the gate over them
```

The engine itself is not code yet: the module map it will be built against — the package
tree, the `acquirer.Client` seam, the `RoutingRequest`/`RoutingDecision` contracts, the
data-flow and the Python boundary — is
[ADR-0011](docs/decisions/0011-module-layout.md).

Implementation (engine, simulator, dashboard, benchmarks) starts after the map is complete.
Evidence for a decision lives next to it: see
[`spikes/0001-latency-budget/`](spikes/0001-latency-budget/) for the hot-path probe behind
ADR-0001, [`spikes/0006-simulation-harness/`](spikes/0006-simulation-harness/) for the
working reference harness behind ADR-0005,
[`spikes/0007-thompson-sampling/`](spikes/0007-thompson-sampling/) for the implementation
evidence behind ADR-0006,
[`spikes/0008-drift-detection/`](spikes/0008-drift-detection/) for the drift detection
evidence behind ADR-0007,
[`spikes/0009-censored-exploration/`](spikes/0009-censored-exploration/) for the
censored-feedback and exploration evidence behind ADR-0008, and
[`spikes/0010-idempotency/`](spikes/0010-idempotency/) for the idempotency and
double-charge prevention evidence behind ADR-0009, and
[`spikes/0011-sca-friction/`](spikes/0011-sca-friction/) for the 3DS/SCA logging and
scope evidence behind ADR-0010 — each regenerates its own committed results.

Two directories are artifacts rather than prose, and both are gated by a dependency-free
checker that runs in CI and refuses a document that should not exist:
[`constraints/`](constraints/) for the merchant's ConstraintSet, and
[`simulator/scenarios/`](simulator/scenarios/) for the world a benchmark runs in. A number
in this repository is cited against a scenario hash, not a filename.
