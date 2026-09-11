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

Decided so far: [engine core is Go; Python is a cold-path language](docs/decisions/0001-engine-language-go.md)
([#2](https://github.com/Sehaan-1/switchback/issues/2)).

```
docs/decisions/     ADRs — one per resolved decision ticket
spikes/             small, reproducible probes that back a decision (not implementation)
```

Implementation (engine, simulator, dashboard, benchmarks) starts after the map is complete.
Evidence for a decision lives next to it: see
[`spikes/0001-latency-budget/`](spikes/0001-latency-budget/) for the hot-path probe behind
ADR-0001, which regenerates its own committed results.
