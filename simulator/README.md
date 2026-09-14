# The simulation harness

The deterministic simulation harness is the artifact that makes every other claim in this
repository checkable: the engine is graded against a world it did not choose, and the world
is a document. This directory holds the canonical artifacts of
[ADR-0005](../docs/decisions/0005-simulation-harness.md) — the scenario grammar, the
worked scenarios, the negative fixtures, the golden hashes and the golden randomness
vectors, and the gate over all of them. It is data and a validator, not a simulator: the Go
harness that consumes a resolved document is [#12](https://github.com/Sehaan-1/switchback/issues/12)'s
job, and the reference implementation that proves the design is
[`spikes/0006-simulation-harness/`](../spikes/0006-simulation-harness/).

```
scenarios/schema/scenario.schema.json     the scenario grammar, v1.0.0 (JSON Schema draft 2020-12)
scenarios/examples/baseline-steady-v1.json          the reference world: six acquirers, no events
scenarios/examples/black-friday-degraded-v1.json    the ticket's own example, as a 51-line overlay
scenarios/examples/outage-recovery-v1.json          three failure modes, three recovery curves
scenarios/examples/replay-trace-v1.json             the trace-replay source and its record contract
scenarios/examples/invalid/*.json                   four documents that MUST fail, for stated reasons
scenarios/golden/scenario-hashes.json               the content hash of every resolved scenario
scenarios/golden/stream-vectors.json                the normative randomness, as reproducible vectors
scenarios/check.py                                  the gate: schema subset + SV1-SV10 + goldens
```

## Run it

```bash
python3 simulator/scenarios/check.py                # human output, non-zero exit on failure
python3 simulator/scenarios/check.py --json         # the same verdict, machine-readable (CI)
python3 simulator/scenarios/check.py --resolve ID   # print the resolved (extends-applied) document
python3 simulator/scenarios/check.py --hash ID      # print its content hash
python3 simulator/scenarios/check.py --pin          # re-pin golden/scenario-hashes.json (a reviewable diff)
```

Every example must be accepted, every `examples/invalid/*.json` must be rejected *for the
reason it exists to demonstrate* (the expected error fragment is in `check.py`), both golden
files must reproduce, and the exit code is the verdict. Like the constraint gate next door,
the checker has no dependencies: it validates the JSON Schema subset the schema actually
uses and fails loudly on a keyword it does not understand, because a silently-unchecked
keyword is a rule that looks enforced and is not.

## Why a scenario is a document and not code

A benchmark number is only worth the paper it is printed on if a reader can tell *which
world* it came from. So:

- **A scenario is content-addressed.** `check.py` resolves `extends`, canonicalises
  (`json.dumps(sort_keys=True, separators=(",",":"))` — the same canonical form the
  constraint layer already uses for `policy.catalog_hash`, because one canonical form in a
  project is worth two good ones) and hashes the result. A reported number cites
  `baseline-steady-v1@sha256:16661ded…`, not a filename.
- **The hash is pinned.** `golden/scenario-hashes.json` is committed, so editing a scenario
  fails the gate until somebody runs `--pin`, and the re-pin is a line in the pull request.
  "The world changed" stops being silent drift in the baseline.
- **The fleet is pinned to its catalog.** `fleet.catalog_hash` must equal the hash of the
  acquirer catalog the scenario names (checker SV3), so a scenario and the capability and
  economics it was written against move together — the same discipline ADR-0004 applies to
  a ConstraintSet.
- **Nothing may read the environment.** No `$ENV`, no absolute paths, no shell
  interpolation (SV9/SV10). A scenario that behaves differently in a different timezone or
  on a different runner is not a scenario.

## The taxonomy, in one table

Everything a scenario can say, and where the ADR argues for it.

| Block | It can say | ADR-0005 |
| --- | --- | --- |
| `seed`, `model_version` | the master seed and the behavioural contract those parameters mean | §4 |
| `clock` | virtual start, duration, tick resolution — the harness never sleeps and never reads wall time | §4 |
| `source.kind: synthetic` | arrival process (Poisson/fixed), diurnal shape, congestion coefficient, and the full context mix | §3 |
| `source.kind: trace` | a recorded stream to replay, its hash, and the context fields it actually carries | §6 |
| `fleet.acquirers[].auth` | base approval rate, BIN-class and region multipliers, amount sensitivity | §3.4 |
| `fleet.acquirers[].decline_mix` | weights over catalogued decline codes, per scheme (iso8583 or nacha). Retryability is a scheme fact, never a scenario parameter | §3.1 |
| `fleet.acquirers[].latency` | p50/p95/p99 and a tail index: a lognormal body plus a generalised-Pareto tail | §3.2 |
| `fleet.acquirers[].three_ds` | frictionless rate, challenge abandonment, per-BIN and per-category multipliers, liability-shift uplift | §3.5 |
| `fleet.acquirers[].late_settlement` | how often an attempt the engine gave up on is authorized later, and how much later | §3.3 |
| `fleet.correlation` | AR(1) latent factors coupling acquirers, so a fleet-wide event is one event and not N | §3.2 |
| `events[]` | `gradual_overload`, `outage` (four failure modes), `recovery` (three curves), `traffic_spike`, `fleet_shock` | §3.3 |
| `recording` | `chosen_only` vs `all_arms`, the trace path, the context fields to persist, the late-settlement window | §6 |

Capability and price are **not** here: they live in the acquirer catalog
([`constraints/catalog/`](../constraints/catalog/)) and are read through `fleet.catalog`.
A scenario describes how an acquirer *behaves*; the catalog describes what it *is*.

## Adding a scenario

1. Write `examples/<id>.json`. If it is a variation on an existing world, `extends` it —
   the resolved document is what gets hashed, so an overlay and a hand-written full
   document are indistinguishable to everything downstream. `black-friday-degraded-v1.json`
   is 51 lines and resolves to 575.
2. `python3 simulator/scenarios/check.py --resolve <id>` and read the world you actually
   wrote; inheritance is a convenience and a way to be surprised.
3. Add a negative fixture to `examples/invalid/` if the scenario exercises a new check, and
   the expected error fragment to `EXPECTED_INVALID` in `check.py`. A fixture that starts
   passing means a check was weakened.
4. `python3 simulator/scenarios/check.py --pin` and commit the hash diff.
5. Run it: `python3 spikes/0006-simulation-harness/harness.py 20000` reads these documents
   directly, so a new scenario is immediately executable.

## Two things this directory is not

- **Not calibrated to reality.** The fixture fleet's rates are invented to span a realistic
  range and to be comparable with `spikes/0003` and `spikes/0004`; the decline mix *is*
  calibrated against published family bands, and
  [`spikes/0006-simulation-harness/RESULTS.md`](../spikes/0006-simulation-harness/RESULTS.md)
  `[M6]` shows both the fixture and the bands so the difference is visible. Magnitudes
  belong to the scenario; orderings and sign flips are the findings.
- **Not the harness.** There is no simulator here, no clock loop and no engine. The
  executable reference is the spike; the production harness is Go, behind the same
  `ProcessorClient` interface as a real acquirer, and it is #12's.
