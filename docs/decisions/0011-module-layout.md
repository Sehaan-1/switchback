# ADR-0011: Engine module layout and interface contracts — one `internal/` tree, the acquirer seam, the fold-only write path

- **Status**: Accepted
- **Date**: 2026-09-17
- **Resolves**: [#12 Engine module layout and interface contracts](https://github.com/Sehaan-1/switchback/issues/12)
- **Depends on**: all ten accepted ADRs — [ADR-0001](0001-engine-language-go.md) (Go core, `internal/` tree, module path `switchback`, the rules R1–R8 this layout enforces), [ADR-0002](0002-reward-function.md) (the observed request inputs, the trace must record the eligible set and the sampled scores), [ADR-0004](0004-constraint-layer.md) (the Go evaluator over compiled documents, five verdicts, the audit record), [ADR-0005](0005-simulation-harness.md) (`acquirer.Client` as specified, `Oracle` outside the engine's imports, the injected `Clock`), [ADR-0006](0006-thompson-sampling-implementation.md) (the `Decide` return contract, `[]ArmStat`, the WAL-as-fold), [ADR-0007](0007-drift-detection.md) (the `drift/` detector contract, resets as WAL ops), [ADR-0008](0008-censored-exploration.md) (DECISION_LOG v1, the score-based plug-in propensity, fold-on-arrival), [ADR-0009](0009-idempotency-double-charge.md) (the attempts manager, `ErrInFlight`, the resolution-window config this ticket owns), [ADR-0010](0010-3ds-friction.md) (AUTH_RECORD emission, path-accurate fees, the MIT scope predicate's place in the pipeline)
- **Feeds**: #13 (state store: which package owns which rows, the catalog-fact re-pin), #14 (safe rollout: where alarm suppression hooks in), #15 (OPE: the exact-propensity recompute reads the trace SQLite, lives in `analysis/`), #16 (dashboard: reads the same store, never the hot path), #17 (benchmarks: the in-package `Decide` benchmark, the dependency-lint gate, the poisoned-run port)
- **Evidence**: **no new spike — this is an integration ADR.** Every contract below is owned by an accepted ADR and cited to it; the new decisions are the package boundaries, the import DAG, and the two API types. Every µs figure is a labelled model extrapolating a measured CPython number on ADR-0001's 10–30× band; nothing here was timed in this sandbox (no Go toolchain, as in ADR-0001/0005/0006). The Go stubs are the normative specification, not compiled code; the first `go build` happens in an implementation ticket, and the day-zero validations this ADR hands it are listed in the appendix.

---

## Decision

1. **One Go module, one `internal/` tree.** Module path `switchback`, toolchain pinned at Go 1.27 in `go.mod` (ADR-0001). The engine lives entirely under `internal/` in thirteen packages — `core`, `acquirer`, `constraints`, `bandit`, `drift`, `trace`, `store`, `router`, `api`, `sim` (+ `sim/oracle`), `artifacts`, `model`, `config` — wired by a fixed, acyclic import DAG (§1). The repository-root `constraints/` and `simulator/` directories stay **artifact directories** (schema, examples, scenarios, golden files, Python gates): the engine reads them, hashes against them, and never writes them. The ticket's eight-module list is preserved one-to-one inside `internal/`; the five additions exist because an accepted ADR demanded each of them (§1.3).
2. **The seam is `acquirer.Client`, verbatim from ADR-0005 §1**: one method, `Authorize(ctx context.Context, req Request) (Response, error)`, value types throughout, no pointer in the request/response path, `ErrInFlight` as the sentinel an idempotent resend returns when the processor is still working the attempt (ADR-0009). The production HTTPS adapter and the synthetic fleet implement the same interface; the engine cannot tell them apart (R27). The one deviation from the ADR-0005 sketch is lexical: the closed vocabulary types (`Outcome`, `BinClass`, `AcquirerID`, …) are homed in a leaf package `core` so the DAG stays acyclic; the wire contract is unchanged (§2.2).
3. **The API contract is two value types in `router/`**: `RoutingRequest` (the observed inputs of ADR-0002's term table plus the arm-key context of ADR-0006 R39, the merchant document reference, and one-shot refinements) and `RoutingDecision` (the five-verdict constraint result, the merchant-visible answer `authorized | declined | processing`, the executed chain, the chosen-arm propensity tagged with its method, and the audit-record hash). `Router.Route(ctx, RoutingRequest) (RoutingDecision, error)` runs a transaction end to end; the allocation-free hot path is `Router.Decide(ctx, RoutingRequest) (Plan, error)` — ADR-0006's payload: the chain, the key-addressed draws (carried, never persisted — R60), and the propensity.
4. **Two write stories, one rule.** The route path is *decide → log → execute*: the constraint filter, the Thompson sample, and the DECISION_LOG v1 append all complete before any processor is called, and the attempts manager (the lease state machine of ADR-0009) is the only code that calls `acquirer.Client`. The learning path is *webhook → dedupe → WAL → fold*: ingest dedupes on `(seq, attempt)` (R44), appends the op row, and the fold is the **only writer of learned state** (R47) — bandit increments, drift observations, and drift resets (`DRIFT_RESET` rows) all flow through it. `drift/` never calls a mutating `bandit/` method; it alarms, and the fold applies.
5. **Python does not live anywhere on the path.** No subprocess, no gRPC sidecar, no embedded interpreter, no WASM module — the bandit and the drift detector are closed-form statistics in Go (ADR-0001 §5). Python owns `models/` (training, artifact production) and `analysis/` (OPE, benchmark reports) outside the engine; traces flow out as SQLite the engine writes and DuckDB reads, artifacts flow in as schema-versioned, checksummed files swapped under an RWMutex at boot or a reload tick, and a missing artifact degrades insight, never routing. `internal/model/` exists only as the designated escape hatch — a `Scorer` interface for a future ONNX export (ADR-0001 reopen trigger 3) — and nothing in v1 calls it.

## Context

Three facts shape how this document should be read.

1. **This ticket is the keystone, not a blank page.** Ten ADRs have each handed #12 a payload — eighteen concrete items, from "the module path is `switchback`" (ADR-0001 §6) to "emit AUTH_RECORD v1 per attempt at settlement" (ADR-0010). §6 is the checklist that proves every one landed somewhere; the new decisions this ADR makes are the ones no earlier ADR could make alone: the package list and its import DAG (§1), the caller-facing types (§3), the pipeline order where several ADRs each owned a step (§4), and the corrections to the ticket's own module list (§1.3).
2. **The repository has two kinds of directory, and the layout must not mix them.** `constraints/` and `simulator/scenarios/` are content-addressed artifacts with Python gates that run in CI today; `docs/decisions/` and `spikes/` are evidence. Engine code goes under `internal/` — which also resolves the only name collision in the ticket's tree: the Go `constraints` package *evaluates* the documents that live in the root `constraints/` directory, and the Go `sim` package *reads* the documents that live in `simulator/scenarios/` (ADR-0005 binds the name `internal/sim/`).
3. **Every number is either cited or labelled.** The house rule on fabricated benchmarks applies with full force to a layout ADR, because layouts get justified by performance folklore. Where this document quotes a cost — the constraint filter, the draw, the propensity — it quotes the measured CPython number and the extrapolation band, and says which ticket owns the Go measurement (#17). The one genuinely new quantitative observation (§4.4: the plug-in propensity is the largest single line in the 20 µs budget) is a model built from two committed measurements, and is flagged as the first thing the implementation must verify.

## 1. Top-level module breakdown (consideration 1)

### 1.1 The tree

The ticket's eight modules map one-to-one (its `router/`, `trace/`, `api/` arrive in the ticket body with encoding damage; the intended names are used). Five further packages exist because an accepted ADR demands each. One-line descriptions per module; the import DAG follows in §1.2.

```
switchback/                              go.mod: module switchback · Go 1.27 toolchain-pinned (ADR-0001)
│
├── cmd/switchback/                      main: one static binary (R8) — serves api/, runs ingest,
│                                        and hosts `switchback bench --scenario … --acquirer=sim`
│
├── internal/                            nothing below this line is importable outside the module
│   ├── core/                            the shared value vocabulary: Outcome, BinClass, Region,
│   │                                    RouteClass, AcquirerID, ProcessorBitmap, Catalog, and the
│   │                                    injected Clock. Imports nothing; carries no behaviour.
│   ├── acquirer/                        THE SEAM: Client interface + Request/Response value types
│   │                                    (ADR-0005 §1 verbatim) + ErrInFlight (ADR-0009).
│   │                                    Production HTTPS adapters live here too.
│   ├── constraints/                     ConstraintSet compiler + evaluator: six enforcement points,
│   │                                    five verdicts, the submit-time guard, the flat audit record
│   │                                    (ADR-0004). Consumes the root constraints/ artifacts.
│   ├── bandit/                          Thompson sampling: arm key (R39), []ArmStat posterior CRUD
│   │                                    (R48), exact key-addressed Beta draws (R45), score-based
│   │                                    plug-in propensity (R61), onboarding floor state (R49).
│   ├── drift/                           one ADWIN detector per processor (δ=10⁻³, K=32, ≤5 buckets
│   │                                    per row), tiered alarms (ADR-0007). Alarms out; it never
│   │                                    touches bandit state directly (§4.3).
│   ├── trace/                           the record schemas (DECISION_LOG v1, OUTCOME ops,
│   │                                    DRIFT_RESET, AUTH_RECORD v1) + the WAL writer + the ingest
│   │                                    pipeline + the fold. The fold is the only writer of learned
│   │                                    state (R47). SQLite via store/; NO Parquet here (ADR-0001 §6).
│   ├── store/                           the state-store binding (#13 owns the backend choice):
│   │                                    WAL-mode SQLite, snapshots (fold checkpoints), lease rows
│   │                                    (R71), the late-settlement scheduled-event queue keyed by
│   │                                    virtual time, catalog serving.
│   ├── router/                          the outer module: Route() orchestration, Decide() hot path,
│   │                                    the attempts manager + lease (ADR-0009), the fee model
│   │                                    (ADR-0010 R75). Sole caller of acquirer.Client.
│   ├── api/                             HTTP surface, versioned /v1: routing requests + outcome
│   │                                    webhooks + health/metrics. Stamps ArrivalMS with the wall
│   │                                    clock; nothing below it reads a clock (R84).
│   ├── sim/                             the synthetic acquirer fleet: fleet-v1 behaviour model,
│   │   │                                key-derived streams, scenario documents, injected Clock
│   │   │                                (ADR-0005). Implements acquirer.Client and nothing else (R27).
│   │   └── oracle/                      ground truth (Truth, Oracle). Benchmark/test-only: no engine
│   │                                    package may import it (R28), enforced by CI, not convention.
│   ├── artifacts/                       cold-path artifact loading: prior tables + band edges, λ_to,
│   │                                    drift thresholds. Atomic publish, schema+checksum validation,
│   │                                    boot load + reload tick, RWMutex pointer swap (ADR-0001 §5).
│   ├── model/                           the designated escape hatch and nothing else in v1: a Scorer
│   │                                    interface for a future ONNX export, cgo behind a build tag
│   │                                    (R7). Nothing calls it (§5).
│   └── config/                          the schema-versioned config directory (R8): resolution
│                                        windows (R67 — defaults this ticket owns: oneoff_cnp 30 s;
│                                        recurring_mit / card_on_file / installment 600 s), policy
│                                        seed, shard count, ingest batch K ≤ 64, probe interval,
│                                        store path, api bind address, artifacts directory.
│
├── constraints/                         [artifact dir, exists] ConstraintSet schema, rule catalog,
│                                        acquirer catalog, examples, check.py gate (ADR-0004)
├── simulator/                           [artifact dir, exists] scenario schema, worked scenarios,
│                                        golden files, check.py gate (ADR-0005)
├── models/                              [Python, cold path] training + artifact production. Never
│                                        imported by the engine; never imports it (ADR-0001 §1.2)
├── analysis/                            [Python, cold path] OPE (#15), benchmark reports (#17),
│                                        DuckDB over the engine's SQLite traces
├── docs/decisions/                      [exists] this record and the ten before it
├── spikes/                              [exists] the evidence base; regenerated by committed commands
└── benchmarks/                          [future, #17] go test -bench targets, committed outputs
```

### 1.2 The import DAG

The layout's load-bearing property is not the directory list but who may import whom. Layers, downward only:

```
package        may import (engine-internal)
─────────────  ──────────────────────────────────────────────────────────────
core           — (leaf: vocabulary + Clock)
acquirer       core
constraints    core
bandit         core
drift          core                                   (alarms out; nothing else)
store          core
model          core                                   (v1: interface only)
sim            core, acquirer                         (sim/oracle: core; benchmark-only)
trace          core, bandit, drift, store             ← the fold: only writer of learned state
artifacts      core, bandit (prior shapes)
config         — (pure data, parsed at boot)
router         core, acquirer, constraints, bandit, trace, store
api            core, router, trace (webhooks → ingest)
cmd/switchback everything, incl. sim behind --acquirer=sim
```

Rules the DAG encodes, each traceable to a prior decision:

- **`core` is the only shared-vocabulary package.** It exists so that `constraints`, `bandit`, `drift`, `trace`, and `router` can all name `Outcome`, `AcquirerID`, `BinClass`, and `ProcessorBitmap` without importing the acquirer seam or each other. It carries no behaviour except `Clock` — the injected time source ADR-0005 demands ("no `time.Now()` below the API boundary"), extended here from the harness to the whole engine (§4.5, R84).
- **`sim/oracle` is a compile-visible but contract-forbidden import for engine packages.** Go's compiler cannot express "benchmarks only"; the rule (R28, restated as R80) is enforced by a dependency-lint gate in CI (#17 owns the wiring, as it owns the `-race` and `allocs/op` gates ADR-0001 promised). A policy that can read the truth is not a policy under test.
- **`drift` imports `core` and nothing else internal.** It consumes observations from the fold and emits alarms; it cannot see `bandit`, which is what makes "the fold is the only writer of learned state" (R47) a property of the graph rather than of reviewer memory (§4.3).
- **Only `router`'s attempts manager calls `acquirer.Client`** (R85). `api` never calls processors; `sim` implements, never calls.

### 1.3 Additions and corrections to the ticket's list

| Ticket item | Disposition | Why |
| --- | --- | --- |
| `router/`, `bandit/`, `drift/`, `constraints/`, `api/` | kept, under `internal/` | — |
| `simulator/` | kept as `internal/sim/` | ADR-0005 binds the name; the root `simulator/` stays an artifact directory (§Context 2) |
| `trace/` — "logging, propensity recording, Parquet/SQLite writer" | kept, **minus Parquet** | ADR-0001 §6: the engine writes SQLite; "do not put a Parquet writer in the hot path". Parquet export is Python/DuckDB reading the SQLite the engine wrote (`analysis/`). Propensity recording is the DECISION_LOG v1 row (R60/R61), owned here |
| `model/` — "Python bridge or sidecar" | kept as an **empty seam** | ADR-0001 §5 rejected both for the decision path; §5 of this ADR answers the ticket's question 5 |
| `acquirer/` (new) | the seam package | ADR-0005: "`acquirer.Client` as specified in §1, in its own package" |
| `core/` (new) | vocabulary leaf | keeps the DAG acyclic (§1.2); the alternative (types in `acquirer`) makes `constraints` and `drift` import the seam |
| `store/` (new) | state-store binding | ADR-0001 §6 names "the state-store bindings" as Go-core work; #13 chooses the driver |
| `artifacts/` (new) | cold-path loader | ADR-0001 §5's contract (atomic publish, schema+checksum, reload tick, never-block-routing fallback) is a package, not a convention |
| `config/` (new) | operator config | R8: "one static binary plus a schema-versioned config/artifact directory"; ADR-0009 R67 explicitly hands the resolution-window defaults to this ticket |

### 1.4 The module interfaces, package by package

The seam (§2) and the API types (§3) get their own sections because the ticket names them; the four behavioural packages between them are specified here. Read together with §1.2, these stubs are the complete set of contracts an implementation writes against.

```go
// Package bandit is the Thompson-sampling model: the arm space (R39), Beta posterior CRUD on
// fixed-layout slices, exact key-addressed draws (R45), the score-based plug-in propensity
// (R61), and the onboarding floor state (R49). It owns no write path: its state mutates only
// through the fold over the WAL (R47/R82), which lives in trace/.
package bandit

// ArmContext is the context half of the arm key; ArmKey = context × processor (R39).
// Fixed size, derivable inside Decide at zero allocation (R3).
type ArmContext struct {
    BinClass   core.BinClass
    Region     core.Region
    SCA        bool
    Mandate    bool
    AmountBand uint8 // geometric ladder over the deployment's declared amount range, 2 sig figs,
                     // default 6 bands; the edge vector versions WITH the prior artifact (R40)
}

type ArmKey struct {
    Context   ArmContext
    Processor core.AcquirerID
}

type ArmIndex uint32

// ArmStat is the mutable per-arm posterior: 32 B, pointer-free, never resized (R5, R48).
// Numeric state lives in []ArmStat sharded by arm index — never map[ArmID]*Arm (ADR-0001 §6).
type ArmStat struct {
    Alpha, Beta float64 // auth posterior data counts (R42): authorized → α+1;
                        // decline/abandoned → β+1; transport error → β+1 and te+1;
                        // timeout → neither (priced at λ_to instead)
    TauA, TauB  float64 // separate timeout posterior (ADR-0002 §3): π̂ feeds the price, not θ
    TE          uint32  // transport-error counter (R42) — drift's window into refusal-shaped events
    Settled     uint32  // settled observations; processor-level sums drive the onboarding clock (R49)
}

// Prior is the read-only 16 B/arm half: α₀ = m·r̂, β₀ = m(1−r̂) per (proc × bin × region) with the
// hierarchical fallback (R50), swapped atomically with the versioned artifact (ADR-0001 §5).
type Prior struct{ A0, B0, TA0, TB0 float32 }

// LogPosterior is the f32 quad a DECISION_LOG row records per eligible processor
// (R60; 104 B/decision at k_max = 5 is ADR-0008 C4's sizing row).
type LogPosterior struct{ A, B, TA, TB float32 }

// FloorState is the R49 onboarding floor: which processors are onboarding and how many settled
// observations each still needs (n_min default 1,000; attempt-0 diversion η default 0.05).
// The mixture probability is exactly computable, so logged propensities stay honest.
type FloorState struct{ /* per-processor: active flag, settled count since entry/reset */ }

// Model owns the posterior slab. Readers are lock-free atomic loads; a torn (α, β) read is
// ≤ 1 count for ≤ 1 ingest interval — bounded staleness, not corruption (R48). No mutex, CAS,
// or seqlock anywhere in the decision path.
type Model struct{ /* shards []struct{ stats []ArmStat }; prior *PriorTable behind an atomic pointer */ }

func (m *Model) IndexOf(k ArmKey) ArmIndex
func (m *Model) Load(i ArmIndex) ArmStat

// Draw is exact gamma-ratio Beta sampling, a pure function of (policySeed, seq, arm, purpose)
// (R45): no normal/grid/order-statistic approximation in Decide, no shared mutable draw stream.
func Draw(policySeed uint64, seq uint64, arm ArmIndex, purpose uint8, s ArmStat, p Prior) float64

// The write surface. All three are called ONLY by the trace/ fold (R47/R82):
//   ApplyOutcome — one settled observation: the R42 increments (deduped upstream on (seq, attempt))
//   ApplyReset   — a DRIFT_RESET op row: γ·(data AND prior pseudo-counts) (R58), then enter
//                  the onboarding state (R49)
//   SwapPrior    — the reload-tick pointer swap; a missing/invalid artifact never reaches here
//                  with routing-affecting force (ADR-0001 §5)
func (m *Model) ApplyOutcome(i ArmIndex, success, transportError bool)
func (m *Model) ApplyReset(p core.AcquirerID, gamma float64)
func (m *Model) SwapPrior(t *PriorTable)

func (m *Model) Floor() FloorState
```

```go
// Package constraints compiles and evaluates ConstraintSet documents. The grammar, the closed
// rule catalog and the CI gate live in the root constraints/ artifact directory (ADR-0004);
// this package is the Go evaluator those documents run through. Every rule is a pure function
// of (document, request context, counter snapshot): no clock, no random, no I/O, no learned
// state — verdicts replay identically, which is what makes the audit record a replayable
// artifact rather than a narrative (ADR-0004 §2).
package constraints

// Verdict is ADR-0004's five outcomes, named distinctly because "nothing is legal" is not one
// thing. router re-exports it (type Verdict = constraints.Verdict) for the API types.
type Verdict uint8

const (
    Routed Verdict = iota
    Waived      // legal only after a bounded, recorded relaxation
    Deferred    // a velocity/reattempt rule handed the attempt to the scheduler
    Prohibited  // a hard stop or cumulative ceiling did exactly what it exists to do
    Unroutable  // empty candidate set that nothing may waive — a defect; escalate, never decline
)

// Context is the closed set of fields a predicate may reference (the scenario/context
// vocabulary, SV7). router copies the request into this fixed-size value — a copy, not a
// reference, so the evaluator cannot observe anything the audit record cannot replay.
type Context struct {
    AmountMinor int64
    Currency    [3]byte
    BinClass    core.BinClass
    CardCountry [2]byte
    CardRegion  core.Region
    MerchantCat core.MerchantCategory
    RouteClass  core.RouteClass
    EntryMode   core.EntryMode
    SCARequired bool
    Mandate     bool
}

// CounterSnapshot is the read-only view of the windowed counters velocity/reattempt rules
// consult (per-card/day paces, in-flight leases). The counters themselves live in store/.
type CounterSnapshot struct{ /* fixed-layout counters addressed by each rule's key */ }

// Audit is the flat, hashable record of one decision (ADR-0004 §6): the eligible set, a
// verdict for every rule that matched, the minimal conflict witness when the set is empty,
// which rule was waived at which rung, the document hash and the catalog hash.
type Audit struct{ /* fixed layout; hash rides RoutingDecision.AuditHash, the row rides the DECISION_LOG */ }

type FilterResult struct {
    Eligible core.ProcessorBitmap
    Verdict  Verdict
    Audit    Audit
}

// Compiled is one merchant's document plus the pinned catalog, compiled once at load.
type Compiled struct{ /* rules ordered by enforcement point (schema-declared, SV12); waiver ladder */ }

func Compile(doc []byte, catalog core.Catalog) (*Compiled, error)

// Filter runs the candidate + transaction enforcement points: the only two that may refuse an
// arm or empty the set (ADR-0004 decision 2). O(candidate arms); reads only the given values.
// The MIT scope predicate (R77) has already run before this call (§4.1 step 1).
func (c *Compiled) Filter(cv Context, counters CounterSnapshot) FilterResult

// Guard re-runs the SAME evaluator over the whole set at submit time — never a per-arm
// question: a per-arm guard diverges exactly where money is (ADR-0004 §3).
func (c *Compiled) Guard(cv Context, set core.ProcessorBitmap, counters CounterSnapshot) error

// Order applies the ordering point (preference.rank): may reorder the chain, never remove a
// link. The plan point (budget.chain_depth) and the lease point (budget.reservation) are
// consulted by the attempts manager during execution, not by Decide.
func (c *Compiled) Order(chain []core.AcquirerID, cv Context)
```

```go
// Package drift runs one ADWIN detector per processor (ADR-0007), aggregated at the processor
// level — never per arm, where a fine space dilutes an outage to 1–2 attempts per arm and
// starves the detector into silence (the dilution finding). δ = 10⁻³, check cadence K = 32
// settled attempts, ≤ 5 buckets per exponential-histogram row: ≤ 20 rows ≈ 1.5 KB per
// processor, preallocated, zero hot-path allocation, owned by the ingest writer (ADR-0001 R5).
package drift

type Detector struct{ /* fixed-layout bucket rows; window sums; since-check counter */ }

type Tier uint8

const (
    Tier1Abrupt Tier = iota + 1 // ≥5 consecutive transport errors (te/settled > 0.20) or split
                                // magnitude |μ̂₀ − μ̂₁| > 0.30 → γ = 0.1, CRITICAL alert
    Tier2Gradual                // ADWIN drift with |μ̂₀ − μ̂₁| ≤ 0.30 and te/settled ≤ 0.20
                                // → γ = 0.5, WARNING alert
)

type Alarm struct {
    Processor core.AcquirerID
    Tier      Tier
    Gamma     float64
    DeltaHat  float64 // |μ̂₀ − μ̂₁| at the split
    TEShare   float64
    AtMS      int64
}

// Observe consumes one settled attempt handed over by the fold. Returns at most one alarm; the
// fold turns it into a DRIFT_RESET WAL row — drift never touches bandit state directly (R82).
// A re-fired alarm on a processor still inside reset-onboarding compounds the decay but does
// not restart the n_min clock (R58).
func (d *Detector) Observe(outcome core.Outcome, atMS int64) (Alarm, bool)
```

```go
// Package trace owns the record schemas, the WAL, and the ingest pipeline. The WAL is the
// trace: one write per event, group commit; the fold over it is the only writer of learned
// state (R47). The engine writes SQLite through store/; Parquet is Python/DuckDB's export,
// never an engine writer (ADR-0001 §6).
package trace

// Op rows, fixed layout. Every row describing a simulated event carries the world's identity
// (R37): scenario hash, seed, model_version, harness_version — a row without its seed is
// history, not an experiment.

type ChainEntry struct{ Acquirer core.AcquirerID; ScoreMinor int64 } // the log's own chain cell;
// trace does not import router, so this mirrors router.ChainLink by construction, not by call

type DecisionLog struct { // DECISION_LOG v1 (R60): 104 B at k_max = 5, f32 posteriors
    Seq        uint64
    ArrivalMS  int64
    ContextKey bandit.ArmContext
    Eligible   core.ProcessorBitmap
    Posteriors [core.MaxEligible]bandit.LogPosterior // beliefs AT decision time — a snapshot,
                                                     // not the live arrays the ingest is mutating
    Chain      [core.MaxChain]ChainEntry             // (acquirer, score) the decision committed to
    Propensity float32
    Method     uint8        // plug-in-score-v1 (R61); an estimator change is a version bump
    Floor      bandit.FloorState
    AuditHash  [32]byte
    // Sampling draws are NEVER logged; they re-derive key-addressed from
    // (policy_seed, seq, arm, purpose) (R60). Run header, once per run: policy_seed and the
    // parameter hashes (document, catalog, prior artifact, config).
}

type OutcomeOp struct { // one settled attempt
    Seq          uint64
    Attempt      uint8
    Processor    core.AcquirerID
    Outcome      core.Outcome
    Code         core.DeclineCode
    DeclineClass core.DeclineClass
    LatencyMS    uint32
    SettledAt    int64
    Context      bandit.ArmContext // raw context rides every row so re-bucketing is a re-fold,
                                   // never a cold start (R47)
    World        WorldIdentity     // scenario hash/seed/model_version/harness_version (R37)
}

type DriftResetOp struct{ Processor core.AcquirerID; Tier uint8; Gamma float64; AtMS int64 }

type AuthRecord struct { // AUTH_RECORD v1 (ADR-0010 R72): one per attempt that touches an
    // authentication step, joined to the decision by Seq; never relabelled on late settlement (R62)
    Seq     uint64
    Attempt uint8
    SessionOutcome uint8 // not_started | frictionless | challenged_completed | challenged_abandoned
                         // | authentication_failed | authentication_unavailable
                         // | attempted_processing | decoupled | exempt_accepted | exempt_refused
    FlowVersion        uint8
    ChallengeIndicator uint8
    ExemptionClass     uint8
    ExemptionRequested bool
    ExemptionAccepted  bool
    ChallengePresented bool
    ChallengeCompleted bool
    LiabilityShift     bool
    FeesIncurredMinor  int64 // the fee the ledger charges, by path (R75): the session fee for
                             // in-session terminals, the submission fee only where submitted
}

// Writer appends with group commit. Decide's append is buffered — R1: no synchronous I/O on
// the decision path; the ingest path acks only once the append is durable (R83).
type Writer interface {
    AppendDecision(DecisionLog)
    AppendOutcome(OutcomeOp) error
    AppendDriftReset(DriftResetOp) error
    AppendAuthRecord(AuthRecord) error
    Snapshot() error // fold checkpoint; boot = snapshot + tail replay (ADR-0006 §7)
}

// Ingest is the outcome pipeline: webhook → dedupe on (seq, attempt) (R44) → WAL append →
// fold. The fold applies op rows to bandit and drift and is bit-exact on replay. Late
// settlements on a non-authorized terminal increment the late counter and hit the void queue —
// never the posterior (R62); their durable scheduled-event queue, keyed by VIRTUAL time, is
// store/'s (#13).
type Ingest struct{ /* writer, dedupe index, bounded shard queues (K ≤ 64, R48), the fold */ }

func (in *Ingest) Submit(ev core.OutcomeEvent) error
```

## 2. The ProcessorClient interface (consideration 2)

### 2.1 The contract, in the target language

ADR-0005 §1 specified this interface and this ADR reproduces it as the normative stub, with the vocabulary types qualified into `core` (§2.2) and ADR-0009's sentinel added:

```go
// Package acquirer is the seam between the engine and whatever moves money. Exactly two
// implementations exist: the production HTTPS adapter (this package) and the synthetic
// fleet (internal/sim). The engine cannot tell them apart and must never need to (R27):
// no field on Request or Response is one a real acquirer could not accept or return, no
// Reset/Inject*/Set* methods exist, ctx carries the deadline and cancellation and nothing
// else (ADR-0001 R4), and fault injection is a scenario events[] entry, not an interface.
package acquirer

import (
    "context"
    "errors"

    "switchback/internal/core"
)

// ErrInFlight is the answer an idempotent resend gets when the processor is still working
// the attempt (ADR-0009 §2). It arrives on the error channel of the same Authorize call —
// the resend IS the status query; there is no second method (R27 holds). It is not a
// transport failure: the lease treats it as "still ambiguous, re-probe at the interval".
var ErrInFlight = errors.New("acquirer: attempt in flight")

// Value types throughout: no pointers in the request/response path, so nothing escapes
// and nothing is shared mutably (ADR-0001 §6, R5).
type Request struct {
    TxnSeq         uint64             // the transaction's identity in this run
    IdempotencyKey [16]byte           // the same key on a retry of the same attempt (ADR-0009);
                                      // production keys are CSPRNG nonces persisted in the lease
                                      // row BEFORE the call goes out (R71); harness keys are
                                      // key-derived from (seed, txn, attempt)
    Attempt        uint8              // 0-based position in the chain
    AmountMinor    int64
    Currency       [3]byte
    BinClass       core.BinClass      // consumer_credit | consumer_debit | premium_credit
                                      // | corporate | prepaid
    CardCountry    [2]byte
    CardRegion     core.Region        // EEA | UK | US | LATAM | APAC
    MerchantCat    core.MerchantCategory
    RouteClass     core.RouteClass    // oneoff_cnp | recurring_mit | card_on_file | installment
    EntryMode      core.EntryMode
    SCARequired    bool
    Mandate        bool
    DeadlineMS     uint32             // the caller's deadline, honoured by ctx as well
}

type Response struct {
    Acquirer     core.AcquirerID
    Attempt      uint8
    Outcome      core.Outcome         // ADR-0002's closed taxonomy + TransportError (ADR-0005 §1)
    Code         core.DeclineCode     // scheme response code; empty unless Outcome is a decline
    Scheme       core.Scheme          // iso8583 | nacha: which catalog Code came from
    DeclineClass core.DeclineClass    // soft | hard | none — a scheme/catalog fact, never an
                                      // inference (ADR-0004 R34); the state machine branches on
                                      // the class, never on the code (ADR-0009 §3)
    LatencyMS    uint32               // what the caller observed, capped at the deadline on timeout
    SettledAt    int64                // virtual-clock ms; 0 when the outcome is UNKNOWN
}

type Client interface {
    Authorize(ctx context.Context, req Request) (Response, error)
}

// A fleet is the registry Route() draws chains from. In production it is the onboarded
// adapters; under `--acquirer=sim` it is sim.NewFleet's return value and the engine runs
// unchanged (ADR-0005 §1, "three consumers, one interface").
type Fleet map[core.AcquirerID]Client
```

The closed vocabulary, homed in `core`:

```go
package core

type Outcome uint8 // ADR-0002's closed enum, plus TransportError (ADR-0005 §1)

const (
    Authorized Outcome = iota + 1
    DeclinedSoft
    DeclinedHard
    Abandoned
    Timeout // deadline passed, result UNKNOWN: excluded from the auth posterior, priced at λ_to
    TransportError // confirmed no-auth; β+1 and te+1 (R42)
)

type BinClass uint8      // consumer_credit | consumer_debit | premium_credit | corporate | prepaid
type Region uint8        // EEA | UK | US | LATAM | APAC
type RouteClass uint8    // oneoff_cnp | recurring_mit | card_on_file | installment
type MerchantCategory uint8
type EntryMode uint8
type Scheme uint8        // iso8583 | nacha
type DeclineClass uint8  // none | soft | hard — a catalog fact (R34)
type DeclineCode string  // closed catalogs only; the gate owns the list (SV4)
type AcquirerID string   // catalog identity: "alpha"…"foxtrot" in fixtures, onboarded ids in prod

// ProcessorBitmap addresses processors (not arms): at a fixed decision context the eligible
// set varies only over processors, so ≤ 64 bits encode it (the DECISION_LOG v1 "eligible-set
// bitmap" of R60). Arms = processor × context are derived, never logged as a 4,320-bit set.
type ProcessorBitmap uint64

// The fixed-layout ceilings. They live here because they size BOTH sides of the boundary:
// router's Plan/RoutingDecision arrays and trace's DECISION_LOG row. Changing either is a
// log schema version bump (R60), not an edit. DECISION_LOG v1 is sized at k_max = 5
// (ADR-0008 C4); the default chain ceiling is the ticket's 2-attempt world with headroom.
const (
    MaxChain    = 4
    MaxEligible = 8
)

// OutcomeEvent is the webhook payload api/ decodes and trace.Ingest consumes: the closed
// taxonomy, the attempt's identity, and what the processor observed. At-least-once delivery
// is assumed; dedupe on (seq, attempt) makes redelivery safe (R44/R83).
type OutcomeEvent struct {
    Seq          uint64
    Attempt      uint8
    Processor    AcquirerID
    Outcome      Outcome
    Code         DeclineCode
    DeclineClass DeclineClass
    LatencyMS    uint32
    SettledAt    int64
}

// Catalog is the read-only fact base the constraint filter and the fee model both read:
// capability (currencies, markets, 3DS) + price (cost_bps, fixed_fee_minor, attempt_fee_minor,
// auth_fee_minor) + the ADR-0009/0010 processor-contract facts (max_response_ms, key_lifetime_ms).
// Served by store/, hashed into every audit record (ADR-0004); the committed EXAMPLE catalog
// gains the contract facts in the ticket that first consumes them — with the scenario re-pin
// in the same PR (§7, payload to #13).
type Catalog struct{ /* Acquirers []Acquirer — value type, swapped atomically on onboarding */ }

// Clock is the only time source below the api boundary (ADR-0005, extended by R84).
type Clock interface {
    NowMS() int64
    AdvanceTo(ms int64) // virtual clocks only; the wall clock is injected at api/, never read below
}
```

### 2.2 The one deviation from the ADR-0005 sketch

ADR-0005's sketch declares `BinClass`, `Region`, `Outcome`, `AcquirerID` inside `package acquirer`. This ADR moves the closed vocabulary to `core`, because the sketch as written would make `constraints` (predicate fields), `bandit` (arm keys), `drift` (detector identity), and `trace` (row schemas) import the seam package to name a decline — and the seam would then sit below half the engine in the DAG instead of at its edge. What ADR-0005 §1 actually binds is the *interface*: "the `ProcessorClient` interface is a Go `interface` with `Do(ctx, req) (resp, error)`" (ADR-0001 §6), refined to `Authorize` with these exact `Request`/`Response` shapes, in its own package (ADR-0005, "What this binds on later tickets"). That contract is reproduced here byte-for-byte in spirit; only the import path of the shared enums moves. Recorded as a deviation so a future reader does not read the two files as disagreeing.

### 2.3 What implementations owe the interface

- **The synthetic fleet** (`internal/sim`): implements `Client` and nothing else (R27); its constructor is ADR-0005's, `func NewFleet(sc Scenario, clock core.Clock) (acquirer.Fleet, oracle.Oracle)`; the `Oracle` half is returned to benchmark code only, from a package no engine file imports (R28/R80). It honours the resend contract — a re-call with the same key is a status query, never a new authorization (ADR-0009 decision 5) — and emits `late_settlement` events on the virtual clock, because a world that cannot produce a late authorization cannot test the double-charge invariant at all.
- **A production adapter**: one per processor, same interface; per-adapter credentials and endpoint config are `config/` data, not interface surface. A timeout is returned as `Response{Outcome: Timeout}` at the derived deadline, and a mid-attempt resend answers `ErrInFlight` while the processor's own state says "working" — adapters whose processors cannot commit to `max_response_ms` are onboarded as **opaque** (ADR-0009 decision 4): their timeouts go straight to `GAVE_UP`, no chain.

## 3. The RoutingRequest and RoutingDecision types (consideration 3)

The API contract between callers and the engine, in `internal/router`. Both are value types passed by value across the boundary (ADR-0001 §6).

```go
package router

import "switchback/internal/core"

type MerchantID string

// RoutingRequest carries exactly the observed decision-time inputs of ADR-0002's term table
// (amount, currency, sca_required, deadline_ms, floor_margin_bps) plus the context the arm
// key is built from (R39) and the constraint layer reads (ADR-0004). No estimated field may
// be added: anything learned lives behind the decision, never in front of it.
type RoutingRequest struct {
    TransactionID string        // the caller's identity for correlation and the reconciliation
                                // ledger. NOT an idempotency token anywhere (ADR-0009 §2): the
                                // per-attempt keys are minted by the engine, one per attempt.
    ArrivalMS     int64         // stamped by api/ from the injected wall clock (R84); DECISION_LOG
                                // field 2 — Decide never reads a clock
    Merchant      MerchantID    // selects the compiled ConstraintSet; the document hash and the
                                // catalog hash are echoed into the audit record (ADR-0004 §6)
    AmountMinor   int64
    Currency      [3]byte
    BinClass      core.BinClass
    CardCountry   [2]byte
    CardRegion    core.Region
    MerchantCat   core.MerchantCategory
    RouteClass    core.RouteClass // selects the resolution-window default (R67): oneoff_cnp 30 s;
                                  // recurring_mit / card_on_file / installment 600 s
    EntryMode     core.EntryMode
    SCARequired   bool            // the MIT scope predicate (R77) is evaluated BEFORE this field
                                  // is taken at face value (§4.1 step 1)
    Mandate       bool
    DeadlineMS    uint32          // the caller's deadline; every processor call's ctx deadline is
                                  // derived from it (R4)
    FloorMarginBPS int32          // optional per-transaction floor; the STANDING floor and its
                                  // required basis live in the merchant's econ.floor_margin rule
                                  // (ADR-0004 §1), which this value refines, never weakens
    Refinements   []Refinement    // one-shot, request-scoped constraints (ADR-0004 §2): required
                                  // request_ref, TTL ≤ PT1H, never relaxable. Compiled at admission
                                  // in api/ — they are not Decide's allocation business
}

type Refinement struct {
    Head       string // a closed rule head from the schema; unknown heads reject at admission
    ParamsJSON []byte // typed by the head, exactly as standing rules are
    RequestRef string
    TTLMS      int64
}

// Verdict is re-exported from the constraint layer, which owns the five-outcome census
// (ADR-0004): routed, waived, deferred, prohibited are the layer working; only unroutable is
// a defect, and it escalates rather than declining (ADR-0002 R10).
type Verdict = constraints.Verdict

// Answer is the merchant-visible state at the resolution deadline (ADR-0009 R67). Processing
// is a first-class state, not an error: an ambiguous timeout is never reported as a decline.
type Answer uint8

const (
    Processing Answer = iota
    AuthorizedAnswer
    DeclinedAnswer
)

type ChainLink struct {
    Acquirer core.AcquirerID
    ScoreMinor int64 // θ̂·win − (1−θ̂)·fee − π̂·λ_to at decision time, minor units (ADR-0002 §3);
                     // carried so the attempts manager can re-run the EV gate after a confirmed
                     // soft decline without re-sampling
}

type Propensity struct {
    Value  float32 // the chosen arm's score-based plug-in estimate (R61): P(arm's score is
                   // argmax) with score = θ·win − (1−θ)·fee − π·λ_to — never P(θ is max)
    Method uint8   // "plug-in-score-v1"; any estimator change is a method/version tag (R46, R60)
}

// RoutingDecision is what the caller gets when Route returns: either a confirmed terminal
// outcome, or Processing at the resolution window's expiry with the ambiguity retired to
// reconciliation (GAVE_UP) or still probing under an opaque processor's M.
type RoutingDecision struct {
    Seq       uint64
    Verdict   Verdict
    Answer    Answer
    Outcome   core.Outcome     // terminal class of the attempt that closed the transaction;
                               // 0 while Answer == Processing
    Chain     [core.MaxChain]ChainLink // the executed prefix is meaningful; fixed layout (R3)
    ChainLen  uint8
    Attempts  uint8            // attempts actually dispatched (the harness's 1.06–1.18 range)
    Propensity Propensity      // provenance + sanity for dashboards and audit (R46); IPS-grade
                               // propensities are recomputed offline from the logged posteriors
                               // (#15), never read off this field
    AuditHash [32]byte         // hash of the flat audit record (ADR-0004 §6): eligible set, every
                               // matched rule's verdict, the minimal conflict witness if empty,
                               // waivers by rung, document hash + catalog hash. The full record
                               // rides the DECISION_LOG row; this hash is what a merchant disputes
                               // with
    EngineLatencyMS uint32     // the engine's own added latency — the ≤ 2 ms budget's instrument,
                               // never a reward input
}
```

Field-by-field provenance, because a contract with ten parents must show each of them:

| Field(s) | Owned by | Rule |
| --- | --- | --- |
| `AmountMinor, Currency, SCARequired, DeadlineMS, FloorMarginBPS` | ADR-0002 term table — "observed at decision time (request), owner #12" | — |
| `BinClass, CardRegion, Mandate` + amount→band | ADR-0006 R39 arm key: `(BIN class × region × SCA × mandate × amount band) × processor` | R39 |
| `RouteClass, EntryMode, MerchantCat, CardCountry` | ADR-0005 `Request` context fields; the scenario context vocabulary (SV7) is the closed list | — |
| `Merchant, Refinements` | ADR-0004 §2: document per merchant; one-shot refinements with `request_ref`, TTL ≤ PT1H, never relaxable | SV10 |
| `TransactionID` vs engine-minted attempt keys | ADR-0009 §2: correlation identity crosses processors; idempotency keys do not | R71 |
| `Verdict` (five values) | ADR-0004 decision | — |
| `Answer` (`processing` first-class) | ADR-0009 R67 | R67 |
| `ChainLink.ScoreMinor`, the EV gate | ADR-0002 §3: a retry is an EV test, not "retry on soft decline" | R13 |
| `Propensity` (score-based, tagged) | ADR-0008 R60/R61, correcting ADR-0006's θ-only plug-in (measured 83.6 pts mean error on a margin-skewed fleet) | R46, R61 |
| `AuditHash` | ADR-0004 §6 | — |

**The in-engine sibling type.** `Decide` returns a `Plan`, not a `RoutingDecision` — ADR-0006's "payload for dependents: #12 (Decide contract): returns the chain, the draws, and the plug-in propensity, labelled with its method":

```go
// Plan is Decide's contract with the rest of the engine. It carries the per-eligible
// posterior SNAPSHOT the decision actually used (so the DECISION_LOG row logs beliefs at
// decision time, not beliefs mutated by the ingest that races it), and it carries the draws
// for execution — but draws are NEVER persisted: they re-derive from (policy_seed, seq, arm,
// purpose) (R60). 104 B at k_max = 5 with f32 posteriors is the measured sizing (ADR-0008 C4).
type Plan struct {
    Seq        uint64
    ContextKey bandit.ArmContext                        // (bin, region, sca, mandate, band)
    Eligible   core.ProcessorBitmap
    Posteriors [core.MaxEligible]bandit.LogPosterior    // f32 (α, β, τa, τb) per eligible processor
    Chain      [core.MaxChain]ChainLink
    ChainLen   uint8
    Draw       [core.MaxChain]float64                   // key-addressed; carried, never logged (R60)
    Propensity Propensity
    Floor      bandit.FloorState                        // onboarding state (R49)
    Audit      constraints.Audit                        // the flat record; hashed into the DECISION_LOG row
}
```

and the two entry points themselves:

```go
// Router wires the modules: the compiled constraint registry, the bandit model, the trace
// writer, the lease store, and the acquirer fleet. Constructed once at boot by cmd/switchback;
// no per-decision state lives here.
type Router struct{ /* dependencies only */ }

// Route executes one transaction end to end: Decide, log the decision, then run the attempts
// manager over the Plan under the lease (ADR-0009). It blocks until a confirmed terminal
// outcome or the resolution window's expiry (R67).
func (r *Router) Route(ctx context.Context, req RoutingRequest) (RoutingDecision, error)

// Decide is the hot path and ADR-0001's benchmark subject: MIT scope predicate (R77) →
// constraint filter → Thompson sample → plan. Zero allocation, zero I/O (R1, R3); ctx is taken
// for cancellation only. The attempts manager (§4.1 steps 5–7) executes the Plan it returns.
func (r *Router) Decide(ctx context.Context, req RoutingRequest) (Plan, error)
```

## 4. Data flow (consideration 4)

### 4.1 The route path — sequence diagram

```
caller        api/        router/                constraints/      bandit/           trace/          store/        acquirer.Client
  │             │             │                       │               │                │               │               │
  │ POST /v1/route            │                       │               │                │               │               │
  ├────────────▶│ Route(ctx, req)                     │               │                │               │               │
  │             ├────────────▶│ 1. scope predicate (R77): MIT with an authenticated mandate is out of │               │
  │             │             │    SCA scope — evaluated before the eligible set exists               │               │
  │             │             │ 2. Filter(context, counters)           │                │               │               │
  │             │             ├──────────────────────▶│               │                │               │               │
  │             │             │◀── eligible, verdict, audit ──────────┤                │               │               │
  │             │             │    empty + nothing waivable → UNROUTABLE → escalate, never decline      │               │
  │             │             │ 3. Decide: arm key (R39); exact key-addressed Beta draws (R45);         │               │
  │             │             │    score = θ̂·win − (1−θ̂)·fee − π̂·λ_to; chain = EV-gated argmax order  │               │
  │             │             ├──────────────────────────────────────▶│                │               │               │
  │             │             │◀── Plan {chain, draws, propensity, floor, posteriors} ──┤               │               │
  │             │             │ 4. append DECISION_LOG v1 — buffered; Decide never waits on I/O (R1)    │               │
  │             │             ├──────────────────────────────────────────────────────▶ │               │               │
  │             │             │ 5. attempts manager (ADR-0009): lease free? mint K₀ → lease row (R71)   │               │
  │             │             ├───────────────────────────────────────────────────────────────────────▶│               │
  │             │             │ 6. Authorize(ctx(deadline from req), {K₀, attempt 0})   │               │               │
  │             │             ├──────────────────────────────────────────────────────────────────────────────────────▶│
  │             │             │◀── Response {outcome, code, latency} ────────────────────────────────────────────────┤
  │             │             │ 7. classify on the CLASS, never the code (R34):                         │               │
  │             │             │    authorized → sale · hard/abandoned → chain HALTS                     │               │
  │             │             │    soft/transport → EV gate over remaining arms → dispatch next         │               │
  │             │             │    timeout → AMBIGUOUS: NO dispatch of any kind; probe same (P, K₀)     │               │
  │             │             │    — the idempotent resend; ErrInFlight = re-probe at the interval      │               │
  │             │             │    — until confirmed terminal, M (R66), or the resolution window (R67)  │               │
  │             │◀────────────┤ answer at the deadline: authorized | declined | processing              │               │
  │◀────────────┤ RoutingDecision {verdict, answer, chain, propensity, audit hash}                      │               │
```

Annotated: steps 1–3 are the body of `Router.Decide`, and steps 1–4 are the ≤ 20 µs in-engine budget; all of it completes **before** any processor is touched — the constraint layer filters before sampling (ADR-0003 R17, ADR-0004 decision 2), the bandit samples only the legal set, and the audit record exists before the money moves. Steps 5–7 are the execution path where wall time is spent (a processor call is ~350 ms, p99 multi-second — ADR-0001 §1); the lease is the gate on every dispatch and the state machine branches on the decline class, never the code. The submit-time guard (ADR-0004 §3: the *same* evaluator over the whole set, not a per-arm question) runs inside step 6, before each dispatch leaves.

### 4.2 The learning path — outcomes, folds, resets

```
processor webhook    api/         trace/ (ingest + fold)      bandit/       drift/        artifacts/ (Python-produced)
  POST /v1/outcomes   │                   │                     │             │                     │
  ───────────────────▶│ Submit(ev) ──────▶│                     │             │                     │
                      │                   │ dedupe (seq, attempt) (R44)        │                     │
                      │                   │ append OUTCOME op → WAL; group commit                    │
                      │◀── ack ONLY after the append (R83) ────┤             │                     │
                      │                   │ fold: ApplyOutcome ▶ α/β/τ increments, te counter (R42)  │
                      │                   │         Observe ─────────────────▶ ADWIN, K=32 cadence   │
                      │                   │◀── alarm (tier 1: γ=0.1 · tier 2: γ=0.5) ─┤              │
                      │                   │ append DRIFT_RESET op → fold: ApplyReset — γ·(data AND   │
                      │                   │ prior pseudo-counts) (R58) + enter onboarding (R49)      │
                      │                   │ late settlement on a non-authorized terminal:            │
                      │                   │ void/reconcile + late counter only — never relabel (R62) │
                                                                                                     │
  boot / reload tick ◀──────────────────────────────────────────────────────── load, validate ──────┤
  (schema_version, checksum, ttl) → RWMutex pointer swap; missing/invalid artifact = metric + alert, │
  never a rejected or delayed decision (ADR-0001 §5)                                                 ┘
```

Three properties this flow is built to keep, each paid for by a module boundary:

- **Fold-only mutation (R82).** `drift/` holds no reference to `bandit/` state: an alarm becomes a `DRIFT_RESET` WAL row, and the fold applies it through the same `ApplyReset` a replay applies. This is what keeps `fold(WAL) == live state` bit-exact across restarts (ADR-0006 §7) — a reset that bypassed the fold would be the only op replay cannot see.
- **Dedupe is protocol, not store option (R44).** At-least-once delivery is assumed; the 1%-dupes + Kafka-rebalance experiment measured +11.1% phantom counts with dedupe off and a failed reconciliation with it on (ADR-0006 §3c).
- **Update on arrival, no holding window (R62).** Ingest lag is margin-neutral to ~256 transactions and harmful after (ADR-0008 C5); a deadline-terminal is `timeout` forever — late resolutions increment the late counter, never the posterior.

### 4.3 Why drift and bandit are neighbours, not callers

The ticket lists `drift/` as "integrates with bandit to trigger resets". The integration is deliberately indirect: both packages sit beside the fold, and the fold is the only thing that writes. Direct `drift → bandit` calls would create a second mutation path that (a) is invisible to WAL replay, (b) needs synchronization the single-writer protocol (R48) exists to avoid, and (c) was measured unnecessary — the detector needs the `te` counter and settled counts the fold already carries per op row (ADR-0007 decision 5: detector state "owned by the ingest writer"). The alarm → op row → fold path costs one buffered append and buys crash-consistent resets for free.

### 4.4 The budget ledger (model, labelled)

Where the ≤ 20 µs p99 in-engine budget (ADR-0001) goes, per decision, extrapolating the committed CPython measurements on the 10–30× band:

| Step | Measured (CPython) | Go band (10–30×) | Source |
| --- | --- | --- | --- |
| Constraint filter (northwind doc) | 35.0 µs | 1.2–3.5 µs | ADR-0004 §3 [M1] |
| Thompson draws + argmax, 5–8 eligible | 10.3 µs median | 0.3–1.0 µs | ADR-0001 spike [M1] |
| Score-based plug-in propensity, k = 5 | 139.5 µs | **4.6–14.0 µs** | ADR-0006 §4e |
| DECISION_LOG append | buffered; 0 on the decision path | 0 | R1 |
| **Total** | | **~6–18.5 µs** | vs 20 µs budget |

Two readings. First, the layout fits the budget at the 30× end and is tight at 10× — exactly the extrapolation uncertainty ADR-0001 labelled a band. Second, **the propensity is the largest single line** — up to ~70% of the budget in the pessimistic case — because the score-based plug-in needs quadrature over the τ posterior per eligible arm (ADR-0008, "Consequences: harder"). The Go implementation's first obligation is a CDF representation (precomputed incomplete-Beta table, per ADR-0005 §2.2's Φ⁻¹ precedent) that lands this line under ~8 µs; the reopen trigger below covers the case where it cannot. The 2 ms API-boundary budget itself is dominated by admission + serialization; the decision compute is two orders of magnitude inside it, as ADR-0001 §1 established.

### 4.5 The clock

One rule covers both paths: **the wall clock is read exactly once per request, in `api/`, and everything below operates on the injected value** (R84, extending ADR-0005's R31 from the harness to the engine). `ArrivalMS` rides the request; attempt deadlines ride `ctx`; the simulator's `AdvanceTo` and production's wall clock are both `core.Clock` implementations. This is what makes `TestHarnessReadsNoAmbientState` (ADR-0005, the poisoned-run port) a test over engine code as well as harness code.

## 5. Where Python lives (consideration 5)

**Nowhere on the decision path — not a subprocess, not a gRPC sidecar, not embedded WASM, not an embedded interpreter.** ADR-0001 §5 made the call with measurements; this ADR restates it as the layout, since the ticket asks the question directly:

| The ticket's option | Verdict | Why, in one line |
| --- | --- | --- |
| Subprocess per decision | Rejected | A fork/exec per decision against a 20 µs budget is disqualified before the protocol is discussed |
| gRPC sidecar | Rejected for the hot path | Measured local gRPC is 116–200 µs round trip — ~10× the raw syscall, before Python computes anything — plus a second supervised process and sidecar-down ⇒ routing-down (ADR-0001 §5). Retained only as a possible *async advisory* channel, never on the decision path |
| Embedded interpreter (cgo/CPython) | Rejected outright | A second runtime holding a global lock in the process that moves money; breaks `CGO_ENABLED=0`; a CPython crash becomes a routing outage (ADR-0001 §5) |
| Embedded WASM module | Rejected | Same coupling class as an embedded interpreter — a second runtime inside the binary (R8: one static binary, no second runtime) — and it buys nothing: the bandit and the drift detector are closed-form statistics (~40 lines of Beta sampling, a few hundred lines of ADWIN), not models that need a runtime |
| ONNX export, in-process | **The designated escape hatch** | If decision-time inference ever becomes a requirement (contextual features, a learned auth-probability model): train in Python, export ONNX, run behind `internal/model`'s `Scorer` interface, cgo behind a build tag (R7). This is the only place cgo is permitted, and v1 ships no caller — ADR-0001 reopen trigger 3 is the tripwire |

**The boundary that replaces them is a file contract, in two directions:**

```
engine ──SQLite traces──▶ storage ◀──DuckDB── analysis/     (out: DECISION_LOG v1, OUTCOME rows,
                                                              AUTH_RECORD v1, DRIFT_RESET, snapshots;
                                                              append-only; the engine never knows a
                                                              Python process exists)

models/ ──versioned artifacts──▶ artifacts/ directory ──▶ engine reload
         (in: prior tables α₀/β₀ + the amount-band edge vector (R40/R50), λ_to, drift thresholds,
          shrunk policy parameters; atomic publish write-tmp→fsync→rename; schema_version +
          generated_at + ttl_seconds + checksum; the engine boots with zero Python-produced files)
```

So the Python directories are: `models/` — training, research, and artifact production (the prior table arrives the way ADR-0006 §2 measured it: a uniform-exploration prefix aggregated hierarchically, m ≈ 100, band edges versioned with it); `analysis/` — OPE (#15: exact propensities recomputed by WAL replay), benchmark report generation (#17), drift research. Neither is imported by the engine, neither imports it, and a crashed or absent Python stack is a degraded insight, never an outage (ADR-0001 §5). `internal/model/` is the stub the ticket asked for:

```go
// Package model is the seam for non-trivial inference, should it ever exist. v1 has no
// implementation and no caller; the interface is here so the day an ONNX export lands
// (ADR-0001 trigger 3) it lands behind a contract, not a refactor. cgo only behind a build
// tag (R7); the build without the tag must stay CGO_ENABLED=0 (R8).
package model

import "context"

type Scorer interface {
    // Score fills out with one score per row of features, in process, with no allocation
    // beyond out. The features vector and its version are a logged artifact, like the
    // posteriors: a scorer change is a parameter-hash change in the run header (R60).
    Score(ctx context.Context, features []float32, out []float32) error
}
```

## 6. What the ten ADRs handed #12, and where each item landed

The integration checklist the Context promised — every payload item a prior ADR addressed to this ticket, and the place in this document (or the rule number) that absorbs it. A reviewer checking this ADR should find no row without a home:

| # | Handed by | Item | Lands at |
| --- | --- | --- | --- |
| 1 | ADR-0001 §6 | Go package layout, one `internal/` tree, module path `switchback` | Decision 1, §1.1 |
| 2 | ADR-0001 §6 | `router.Decide` takes `context.Context` first, returns a value type; no pointers in the request/response path | §3: `RoutingRequest`/`RoutingDecision`/`Plan` by value, `Router.Decide` signature |
| 3 | ADR-0001 §6 | `ProcessorClient` is a Go interface with `Do(ctx, req) (resp, error)` | §2, refined to `Authorize` per ADR-0005 |
| 4 | ADR-0001 §6 | numeric state in `[]ArmStat`, not `map[ArmID]*Arm` | §1.4 `bandit.Model`/`ArmStat` |
| 5 | ADR-0002 term table | observed decision-time inputs owned by #12 (`amount`, `currency`, `sca_required`, `deadline_ms`, `floor_margin_bps`) | §3 `RoutingRequest` + provenance table |
| 6 | ADR-0002 R14 | trace records the eligible set, the sampled scores, the chosen action | §1.4 `trace.DecisionLog` (eligible bitmap, per-eligible posteriors, chain) |
| 7 | ADR-0004 | Go evaluator over compiled documents; enforcement points read from the schema (SV12); five verdicts; submit-time guard re-runs the same evaluator over the whole set; flat audit record | §1.4 `constraints` stub |
| 8 | ADR-0005 | `acquirer.Client` as specified, in its own package, `Request`/`Response` value types | §2.1 |
| 9 | ADR-0005 | `Oracle` in a package the engine does not import | §1.1 `internal/sim/oracle`; R80 |
| 10 | ADR-0005 | `Clock` interface, no ambient time, poisoned-run gate | §2.1 `core.Clock`; §4.5; R84; appendix item 4 |
| 11 | ADR-0006 | `Decide` returns the chain, the draws, and the plug-in propensity labelled with its method | §3 `Plan` (draws carried, never persisted — R60) |
| 12 | ADR-0006 | the WAL is the only writer of learned state; boot = snapshot + tail replay | §1.4 `trace.Writer`/`Ingest`; R82; appendix item 5 |
| 13 | ADR-0007 | the `drift/` package and detector contracts; processor-level aggregation; resets | §1.4 `drift` stub; §4.3 |
| 14 | ADR-0008 | DECISION_LOG v1 field list; score-based plug-in propensity with method tag; fold on arrival, no holding window | §1.4 `trace.DecisionLog`; §3 `Propensity`; §4.2 |
| 15 | ADR-0009 | the attempts manager and `ErrInFlight`; the lease; the resolution-window config this ticket owns | §2.1 sentinel; §4.1 steps 5–7; §1.1 `config/` (R67 defaults: 30 s / 600 s) |
| 16 | ADR-0010 | emit AUTH_RECORD v1 per attempt at settlement | §1.4 `trace.AuthRecord` |
| 17 | ADR-0010 R75 | the fee model reads `auth_fee_minor` and charges by path | §1.1 `router/` (fee model); §2.1 `core.Catalog` |
| 18 | ADR-0010 R77 | the MIT scope predicate is computed with the ConstraintSet, before the eligible set | §4.1 step 1; §1.4 `constraints.Filter` comment |

## 7. Payload for dependent tickets

- **#13 (state store)**: `store/` is your package; the row owners are fixed here — WAL tail and snapshots (`trace/`), lease rows with the persisted idempotency key (`router` writes through `store`, R71), the late-settlement scheduled-event queue keyed by **virtual** time, catalog serving. The committed EXAMPLE catalog (`constraints/catalog/acquirer-catalog.example.json`) does **not** yet carry ADR-0009/0010's contract facts (`max_response_ms`, `key_lifetime_ms`, `auth_fee_minor`); adding them changes the catalog hash that scenarios pin (SV3), so the re-pin lands in the ticket that first consumes them — here that is #13 — with the scenario digests re-committed in the same PR. Do not split that change.
- **#14 (safe rollout)**: drift-alarm suppression during routing transitions hooks between `drift/`'s alarm emission and the fold's `DRIFT_RESET` append — the one queue between them is the suppression point; no other seam exists by construction (R82).
- **#15 (OPE)**: the exact-propensity recompute is an `analysis/` job over `trace/`'s SQLite (R46/R61); the logged plug-in field on `RoutingDecision` is provenance, never an IPS weight. `all_arms` counterfactuals come from the harness's `Oracle`, which `analysis/`/benchmark code may import and engine code may not (R80).
- **#16 (dashboard)**: reads `store/` (SQLite) and nothing else; the drift alarm ledger, floor-clock state, late counters and AUTH_RECORD panels (ADR-0007/0008/0009/0010) are views over the same rows the fold writes. No dashboard query touches the hot path.
- **#17 (benchmarks)**: three gates this layout promises and #17 wires — (a) `BenchmarkDecide*` runs in-package (`internal/router`) so the unexported hot path is reachable, asserting `allocs/op == 0` (R3); (b) the dependency lint that makes `sim/oracle` unreachable from engine packages and the import DAG of §1.2 acyclic (R80); (c) the poisoned-run port `TestHarnessReadsNoAmbientState` covering engine paths too (R84). `switchback bench --acquirer=sim` is the contributor's first-evening on-ramp ADR-0005 paid for. The §4.4 propensity line is the first number to verify.
- **#5 (constraints, reopened items)**: the Go evaluator this ADR stubs reads the enforcement point from the schema (SV12), not from a table beside it; the scope predicate (R77) is the pipeline's step 1, before the eligible set exists.

## Consequences and design rules that follow

**Positive.** Every interface the implementation will write is now a cited, reviewable contract; the import DAG makes the three load-bearing invariants (one writer of learned state, one caller of processors, no truth visible to the policy) properties of the graph instead of promises in review. The ticket's module list survived contact with ten ADRs with only a Parquet correction and an empty `model/` package — evidence the earlier decisions compose. The engine remains one binary, one toolchain, startable with zero Python files.

**Accepted costs, named.**

1. **Thirteen packages for a small engine is ceremony.** Accepted: the DAG is the documentation, and four of the thirteen (`core`, `store`, `artifacts`, `config`) are under ~200 lines each at first implementation.
2. **`Plan` duplicates data the DECISION_LOG also carries.** Accepted: the duplication is the point — the logged posterior snapshot must be the beliefs at decision time, and a shared mutable structure would race the ingest (bounded staleness, R48, is benign for decisions but not for audit rows).
3. **The propensity's budget share (§4.4) is a model, not a measurement.** Accepted and flagged as reopen trigger 1; the pessimistic end leaves ~1.5 µs of headroom, which is why the trigger exists before the code does.
4. **Two type vocabularies to keep aligned** (`core` enums vs scenario schema vocabularies vs the constraint schema's closed fields). Mitigated: the artifact directories' gates own the word lists, and the Go side generates from them at implementation time rather than retyping them; the alignment check belongs to #17's gates.
5. **No gRPC in v1** where the ticket said "HTTP/gRPC". Accepted: JSON-over-HTTP first, protobuf shapes mirror the Go structs 1:1 so gRPC is additive; trigger 4 covers the reverse case.

**Design rules (continuing ADR-0010's numbering):**

- **R79 — One module, one tree.** The engine is Go module `switchback`, all engine packages under `internal/`; repository-root `constraints/` and `simulator/` remain artifact directories the engine reads, hashes against, and never writes. The deployable stays one static binary plus a schema-versioned config/artifact directory (R8).
- **R80 — The DAG is the contract.** Imports follow §1.2, downward only; `core` carries vocabulary and `Clock` and no other behaviour; no engine package imports `internal/sim/oracle` — enforced by a CI dependency check (#17), because R28's "package the engine does not import" must be a gate, not a grep.
- **R81 — The API boundary is two value types.** `RoutingRequest` in, `RoutingDecision` out, passed by value, on a versioned `/v1` surface. Adding a field is a minor revision; changing a field's semantics is a new version. `Plan` (with the draws) never crosses the boundary and never reaches the log (R60).
- **R82 — The fold is the only writer of learned state** (R47, given an address): outcomes, drift resets, and prior swaps are all WAL-visible ops applied by `trace/`'s fold; `drift/` alarms, never mutates; `api/` never writes posteriors; a mutation path that bypasses the fold is a replay-breaker and is refused in review.
- **R83 — Outcome webhooks ack after the WAL append, never before.** Delivery is at-least-once; dedupe on `(seq, attempt)` (R44) makes redelivery safe. An ack is a durability claim, and this is the only place in the engine where one is made synchronously.
- **R84 — One clock read per request, at the boundary.** `api/` stamps `ArrivalMS` from the injected wall clock; every package below takes time as a value (`core.Clock`), production or virtual. No `time.Now()` below `api/` — the poisoned-run test (ADR-0005 [M2]) is ported to cover engine paths, not just harness paths.
- **R85 — The attempts manager is the only caller of `acquirer.Client`.** It dispatches only from a lease-free state, mints the idempotency key into the lease row before the call goes out (R71), probes with the idempotent resend, and honours the classification table of ADR-0009 §3. No other package issues an authorization — the simulator's `NewFleet` return value and the production fleet are both consumed here and only here.

## Reopen triggers

1. **The propensity line breaks the budget.** `go test -bench` shows the score-based plug-in > 8 µs p99 at k = 5 after table/CDF optimisation, and `Decide` p99 misses 20 µs because of it. Then the plug-in moves out of `Decide` (the DECISION_LOG row carries the posterior snapshot and a labelled estimate computed on the ingest path), and R46/R61's *placement* — not the estimator — reopens.
2. **A production adapter needs a seam change.** A real processor integration requires a second `Client` method — a synchronous 3DS session primitive, a mandate-setup call — that the idempotent resend genuinely cannot express (ADR-0009 covered status queries; it did not cover challenge initiation). The seam reopens with the adapter's evidence in hand, and whatever lands, `sim` implements it too (R27 extends) or the benchmark stops testing the thing production uses.
3. **The fold falls behind.** Sustained ingest (5k decisions/s × ~1.3 attempts) plus ADWIN observation exceeds one fold's throughput. Then `trace/` splits into records + sharded ingest packages and the fold shards per WAL partition; the fold-only-writer rule (R82) survives, the single-fold assumption does not.
4. **gRPC proves load-bearing.** The #17 API-boundary harness shows JSON serialization > 10% of the 2 ms p99, or a consumer lands that needs streaming outcomes. Then protobuf/gRPC moves into v1 — additive over the existing structs, not a rewrite.
5. **The constraint filter's Go cost misses the model.** #17 measures the compiled evaluator at > 30% of the Decide budget (the pessimistic CPython band said 8.8–26.4%). Then per-rule specialization lands; the pre-sampling filter itself does not move — filter-before-sampling is ADR-0003/0004 territory and this trigger explicitly does not reach it.

## Alternatives considered

- **Top-level packages (`router/`, `bandit/` at repo root), as the ticket sketches.** The steelman: visibility — the architecture reads off `ls`. Rejected: the repo root is the artifact surface (schema, scenarios, spikes, decisions), and mixing gated artifacts with code under one roof gives the CI gates two masters. `internal/` also buys the compile-time guarantee that engine internals are not imported by a future SDK or tool — the same reasoning ADR-0005 applied to `sim/oracle`, applied to the whole engine. The ticket's tree is preserved one-to-one under `internal/`, so the visibility cost is one directory level.
- **Vocabulary types in `acquirer`, per the ADR-0005 sketch.** Steelman: one less package, and the sketch said so. Rejected in §2.2 on the DAG: it puts the seam below `constraints`, `bandit`, `drift`, and `trace`. The deviation is recorded there, not hidden.
- **A separate `ingest/` package beside `trace/`.** Attractive — ingest has its own protocol (dedupe, batching, fold). Rejected for now: "the WAL — the trace itself — is the only writer of learned state" (ADR-0006 decision 5) reads cleanest as one package owning the record, the writer, and the fold; the split is reopen trigger 3's prescribed remedy, not a never.
- **`drift` calling `bandit` directly on alarm.** The steelman is latency: a reset lands one queue-hop sooner. Rejected: the hop is one buffered append against a detector whose cadence is 32 settled attempts, and the price of the shortcut is a mutation path WAL replay cannot see — the exact corruption class ADR-0006 §7's bit-exact fold exists to make impossible.
- **RoutingDecision as a pointer, or a streamed decision.** Rejected: ADR-0001 §6 fixed value types so nothing escapes and nothing is shared mutably; a streamed variant would make the lease's "one answer" (R67) a protocol question. gRPC streaming remains a v2 question under trigger 4.
- **A Go client SDK in `pkg/` now.** Deferred: the public surface is HTTP, and an SDK is a separate module (as ADR-0001 §7 said of Ruby) — shipping `pkg/` before a second consumer is API design by speculation.

## Appendix: day-zero validation, handed to the implementation ticket

The sandbox this layout was written in has no Go toolchain (continuing ADR-0001/0005/0006's position: state the gap, don't paper over it). The stubs above are the specification the first implementation PR is checked against, and these are its entrance exams, all owned by #17's gate set:

1. `go build ./...` at `CGO_ENABLED=0`, `go vet`, `staticcheck` — the module compiles with zero cgo (R7's build tag off).
2. `go test -bench BenchmarkDecide -benchmem` asserting **0 allocs/op** on the filter→sample→plan path (R3), on the fixture fleet and the two committed merchant documents.
3. The dependency lint: acyclic §1.2 DAG; zero engine imports of `internal/sim/oracle` (R80).
4. `TestHarnessReadsNoAmbientState` (the [M2] poison port) covering harness **and** engine paths: rigged wall clock and global RNG raise; the run digest is unchanged (R84).
5. Replay equality: `fold(WAL)` after a crash-restart reproduces the live posterior bit-exact, including a `DRIFT_RESET` in the tail (R82, ADR-0006 §7).

Until those run, every µs in §4.4 is a labelled model, and the layout's claims are structural, not measured — which is exactly the state every prior ADR left its Go projections in.
