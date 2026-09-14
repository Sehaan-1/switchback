# ADR-0001: Lock Go for the routing engine core; Python is a cold-path language

- **Status**: Accepted
- **Date**: 2026-09-11
- **Resolves**: [#2 Engine language: Go vs Rust for the routing core](https://github.com/Sehaan-1/switchback/issues/2)
- **Blocks / unblocks**: unblocks #12 (module layout), #13 (state store backends), #17 (benchmark toolchain); constrains #5 (constraint DSL is data, not code), #7 (bandit is native Go), #8 (drift detector is ported, not called)
- **Evidence**: [`spikes/0001-latency-budget/`](../../spikes/0001-latency-budget/RESULTS.md) — reproducible hot-path cost probe + GC model

---

## Decision

1. **Engine core: Go.** Minimum toolchain **Go 1.27** (current stable 1.27.1), pinned in `go.mod` with `toolchain` set. This covers `router/`, `bandit/`, `drift/`, `constraints/`, `trace/`, `simulator/`, `api/` and the state-store bindings. One language. No Rust.
2. **Python interop: there is no Python in the decision path.** The bandit (Thompson sampling over Beta posteriors) and the drift detector are closed-form statistics, implemented natively in Go — they are not models that need a runtime. Python owns training, research, off-policy evaluation, and benchmark report generation, and talks to the engine **only through files on a cold path**: traces flow out (engine → storage → Python), derived artifacts flow in (Python → versioned artifact on disk → engine reload). See [The Python boundary](#5-the-python-boundary-resolves-consideration-3).
3. **Secondary languages have non-runtime roles.** Python 3.12+ for `models/` + `analysis/` (never imported by the engine, never imported from the engine). TypeScript/React for the dashboard (#16). **Ruby: none.** The nod is honored in docs and an optional future client SDK, not in the build graph — see [The Ruby question](#7-the-ruby-question-resolves-consideration-6).

The latency budget the map left as `Y` is now proposed concretely (§1): **≤ 2 ms p99 added wall latency for a routing decision at 5k decisions/s/node on 4 vCPU**, with a **≤ 20 µs p99 in-engine CPU time** budget, measured at the API boundary. #17 owns the final wording; this ADR fixes the numbers the architecture is designed against.

## Context

The engine is the hot path, so the language feels like a performance decision. The spike says it is not one. The measured floor and ceiling of the actual per-decision work are two orders of magnitude inside the budget, in *both* directions, under a deliberately pessimistic assumption. What the language does decide is everything else: who can contribute, how fast the loop from question → merged change → committed benchmark number is, and how much surface area the deployment has.

Two facts about the project shape the reading of every consideration below:

- **Switchback's differentiator is decision science, not throughput per core.** Auth-rate learning, drift handling, OPE, and the audit trail are where the value is. Nobody adopts a routing engine because its decision took 0.3 µs instead of 1.2 µs; they adopt it because they can trust and explain its decisions.
- **The catastrophic failure modes here are logic bugs, not memory bugs.** A double charge (#10), a retry that violates a scheme rule (#5), a posterior corrupted by a concurrent update (#7), a benchmark that cannot be reproduced (#17). The borrow checker does not prevent one of these. This reframes the "Rust prevents entire classes of bugs" premise: the classes it prevents are largely not the ones we are exposed to.

## 1. Latency budget: is the GC a factor? (consideration 1)

**No, and it cannot be made a factor by choosing Rust either.** From the spike:

| Measurement (CPython 3.11, 2 vCPU, no perf isolation) | Value |
| --- | --- |
| One decision, 8 candidate arms (filter + 8 Beta draws + argmax + propensity) | 10.3 µs median, 4.3 µs floor |
| One decision, 32 candidate arms (if #7 picks finer arm granularity) | 37.2 µs median |
| Outcome ingest, batch of 8 arm updates | 1.64 µs (0.21 µs/update) |
| Full posterior live heap: 184,320 arms × 24 B, pointer-free | 4.4 MB |

Against a 2 ms p99 budget, the interpreted numbers consume **0.5%** of it. Extrapolating to compiled code — 10×/30×/100× faster for interpreter-dominated numeric loops — the compute is 1.0/0.34/0.10 µs. The verdict is deliberately reported at **1.0×: if Go and Rust were exactly as fast as CPython, we would still be two orders of magnitude inside budget on the arithmetic (the spike's own number: budget = 194× compute).** No Go-vs-Rust difference in this workload shape can matter, because neither language is what the budget is being spent on.

The GC question, same reasoning. Go's stop-the-world phases are mark setup and mark termination; on a small live heap they are tens of microseconds, and Green Tea (default since Go 1.26) reduces GC CPU by 10–40%. But the load-bearing point is what our design does to the collector's inputs, not what the collector's constant is:

- The posterior is a **fixed-layout, pointer-free, never-resized slice**. Go's GC traces pointers; a 4.4 MB slab of `float64`/`int64` is bulk-skipped, not walked object by object.
- The **allocation rate in `Decide()` is the whole story**: GC cycle frequency is `heap_growth / alloc_rate`. At 0 alloc/decision the collector does not run because of decisions, ever — no tuning needed, no GOGC argument, no ballast.
- The model in `RESULTS.md` [B1] shows the regime where it *would* matter: hundreds of MB of live heap **and** GB/s of allocation. That is what you get by putting the trace buffer or per-request JSON in the heap. It is a discipline problem, not a language problem — and a Rust engine with the same sloppy allocations pays for it in allocator and memcpy time instead.

Where the latency actually goes (spike [B2]) decides the two architectural rules that matter:

```
engine decision compute            ~0.3–10 µs      <- this ticket's whole debate
processor HTTPS call to acquirer   ~350 ms          <- the real cost, p99 multi-second
3DS challenge, when triggered      ~8000 ms         <- user interaction, unbudgetable
loopback gRPC to a sidecar         ~170 µs per call <- a tax we can simply refuse to pay
```

So: **≤ 2 ms p99 added latency at the API boundary, ≤ 20 µs p99 in-engine CPU.** The rules that make it achievable are (a) no synchronous network hop inside `Decide()` — no store round trip, no sidecar, no lock held across I/O, and (b) `Decide()` allocates nothing. Both hold in either language; (a) is an architecture decision this ADR makes, (b) becomes a CI gate in #17.

The honest asymmetry to record: at equal algorithm, Rust will beat Go on memory footprint (no GC headroom, tighter struct packing) and will win outright if the hot path ever grows real tensor math. We are trading a ~10× latency advantage we do not need for iteration speed and contributor surface area we do.

## 2. Concurrency model (consideration 2)

The engine's concurrency is not "lots of independent compute"; it is **deadline-bounded fan-out to external systems with cancellation that must be correct**. A decision awaits a processor call under `deadline_ms`; a fallback chain must not fire while an attempt is in-flight-but-unconfirmed (#10). That is the whole concurrency requirement, and Go's shape for it is the better fit:

- `context.Context` is an explicit deadline and cancellation contract threaded through every call frame, and it is visible to a reviewer at every call site. Rust has no equivalent default: cancellation is explicit (`select!`, `AbortHandle`, drop-future semantics) and `tokio::spawn` without a deadline is not a type error. A leaked goroutine that fires a late retry is exactly the double-charge bug — so this is a safety property, not a style one. Go's idiomatic answer is one the reviewer can see at every call site.
- Go 1.25+ makes `GOMAXPROCS` container-aware: on Linux the runtime reads the cgroup CPU quota, sets `GOMAXPROCS` to it, and re-checks as limits change — retiring `uber-go/automaxprocs` as a dependency. Note the activation condition, because it is a `go.mod` decision, not a toolchain one: the new default applies to modules declaring `go 1.25` or later, so pinning the language version high is what actually buys us this. Go 1.27 graduates the **goroutine leak profile** (`runtime/pprof` `goroutineleak`, exposed over `net/http/pprof`), which is a direct detector for leaked attempt-goroutines. That is a concrete, version-dated argument, not a vibe.
- Streamed outcome updates: a channel per shard, single-writer ownership. The alternative — Rust + tokio + broadcast channels — works, but the state machine for "outcome arrived after decision returned" is more natural to express and more natural to review in Go.

What Rust genuinely buys, and how we pay it back: `Send`/`Sync` bounds make cross-task sharing of non-thread-safe state a **compile error**, and `Result` + exhaustive `match` make missed error arms a compile error. Go's equivalents are process-level and test-level, so we commit to them as gates, not intentions (see [Consequences → design rules](#consequences-and-design-rules-that-follow)): `-race` on the full test suite in CI (not just unit tests), an "immutable value across goroutine boundaries, no shared mutable maps" rule for #12, and a lint against `interface{}` in the posterior path to keep boxing out of the hot path. `Rust prevents data races` is real; `Go with -race on an exercised suite` catches the ones tests can reach, which for a system this small is most of them — and the residual risk is accepted, explicitly.

## 3. Ecosystem: the ticket's premise does not survive contact with the repos — and it still does not flip the decision (consideration 4)

The strongest open-source routing-plane precedent is **not** Go. Two projects, both directly comparable to us, are Rust:

- [`juspay/hyperswitch`](https://github.com/juspay/hyperswitch) — composable payments platform, 300+ PSP connectors, 43.6k stars, active today, ~39 MB of Rust.
- [`juspay/decision-engine`](https://github.com/juspay/decision-engine) — "routing control plane for payment decisions," sits between an orchestrator and gateways, returns decisions over HTTP, ~3.9 MB of Rust, pushed the day this ADR was written. It is the closest thing in the wild to Switchback's shape, minus the learning layer.

Anyone citing "Stripe writes their API layer in Go" as a routing-core argument is citing a web-API precedent for a decision-plane problem. That argument does not survive contact with the repos, and I am recording it as *not* part of the justification.

What those two projects tell you instead is *which* properties they needed Rust for: a connector surface of hundreds of HTTP integrations where per-core density is a real cost centre, a mostly stateless rule/weight evaluator, and a company paying for senior Rust engineers full-time. Their routing is rule-based and success-rate-scored, not an online-learning core with an adjacent research stack — which is a *different* constraint profile from ours (bandit state, drift detectors, OPE, benchmark regeneration, community contribution from payments engineers and decision scientists rather than systems engineers).

The ecosystem items that do favour Go for our shape, all concrete:

| Need | Go | Rust |
| --- | --- | --- |
| Embedded store for model state (#13) | `modernc.org/sqlite`, pure Go, `CGO_ENABLED=0`, zero C toolchain; `ncruces/go-sqlite3` (wazero) as the low-latency option | `rusqlite` bundled compiles SQLite C — fine, but needs a linker and slower builds |
| Static single-binary deploy artifact | `CGO_ENABLED=0 GOOS=linux go build`, `FROM scratch`, no glibc version lock | same story, but `musl` targets and linker config add build variants |
| Parquet/Arrow for traces (#13, #15) | weaker (`segmentio/parquet-go` lineage) | **stronger — `arrow-rs` is the best Parquet implementation available.** Mitigation: the engine writes SQLite; DuckDB reads SQLite natively and exports Parquet from Python. This cost is real and it is why #13 should not ask the engine to write Parquet. |
| gRPC/protobuf HTTP surface (#12) | first-class, `buf` + `protoc-gen-go` | first-class (`tonic`) |
| Payments API precedent (Stripe/Adyen/Checkout.com SDKs) | official, idiomatic `stripe-go` et al. | community SDKs |
| In-process ONNX inference if #7/#4 ever demand it | `onnxruntime_go` (cgo wrapper, pinned to ORT 1.29 headers) — workable | `ort` 2.0 — notably better |
| Observability | OpenTelemetry Go, pprof built into the runtime | OTel Rust, no comparable built-in profiler story |
| CI iteration on a small codebase | seconds, one toolchain, `go vet`+`staticcheck`+`golangci-lint` | tens of seconds to minutes cold, more cross-target plumbing |

Two rows of eight are won by Rust, and both land on our roadmap: `arrow-rs`-class Parquet (#13) and `ort` for decision-time inference (#7's continuous-reward fork). Those are exactly the two items that get mitigation notes rather than being waved away. Everything else in the table is a wash or favours Go on the axes we actually use.

## 4. Contributor accessibility (consideration 5)

For an open-source engine whose value depends on outside contributors writing *constraint predicates, processor adapters, and policy variants* — not writing a runtime — the binding constraint is time-to-first-merged-PR. Go's is measured in an afternoon: one file, one test, no lifetime annotations on a processor adapter, no async-fn-in-trait or `Pin` ceremony between the contributor and a running trace. Rust's ownership model is a genuine filter that keeps out exactly the contributors we most want (payments engineers, and decision scientists fluent in Python).

This is not a claim that Rust developers are slower engineers — it is a claim about the pool we can draw from and the friction of a 3-PR-per-week cadence on a repo with one maintainer. Surveys and job-market data point the same direction (Go: larger, more payments-API-adjacent pool; Rust: scarcer, higher-compensated, longer ramp to a first async contribution), but I am treating those figures as directional only — the operational claim stands without them: for a one-maintainer OSS engine the second-order risk is a contributor barrier, and we get no compensating correctness win on our actual bug classes (§Context).

Counter-consideration, stated fairly: Rust's compiler is a senior reviewer that never sleeps, which matters when the maintainer is one person and review capacity is the scarce resource. We buy the closest available thing in Go with `-race`, `staticcheck`, `go vet` fieldalignment, exhaustive `golangci-lint`, and a rule that `Decide()` has no `interface{}`.

## 5. The Python boundary (resolves consideration 3)

The question "how tight is the coupling?" has a design answer, not a discovery answer: **we get to choose it, and the right choice is cold.**

The bandit is not ML in the sense that needs an inference runtime. A Beta posterior is two `float64`s; a draw is either the Marsaglia-Tsang gamma ratio (two gammas per arm) or, because `alpha`/`beta` are counts and therefore integers, exact Beta sampling via the order statistic of `alpha+beta-1` uniforms. Either way: ~8 arms, nanoseconds of arithmetic (§1). There is no Beta in `math/rand/v2`, so we own ~40 lines of it — which is a #7 decision, not an argument for Python. Thompson sampling, weighted discounting, ADWIN or two-window KL (#8) all fit in a few hundred lines of Go that we can unit-test, fuzz, and property-test. Putting them in Python and calling them per decision would be the only way to make this system's latency depend on Python, and it would buy nothing.

Rejected, with reasons (all three are real options; all three cost more than they return):

| Option | Verdict | Why |
| --- | --- | --- |
| **gRPC sidecar to Python** | **Rejected for the hot path.** Adopted nowhere in v1. | 1M-call histograms on an EPYC 7402P with a latency-tuned kernel ([Max Planck measurement](https://www.mpi-hd.mpg.de/personalhomes/fwerner/research/2021/09/grpc-for-ipc/)): local gRPC over a Unix socket costs **116 µs median / 142 µs p99** with client and server on different cores, **167 / 200 µs** on the same core. The transport is not the cost — bare blocking I/O on the same socket is **11 µs**. So the overhead is the framework, not the transport — gRPC is ~10× the raw syscall round trip — and it lands before Python computes anything, against a 20 µs in-engine budget. Add GIL-scheduling tail amplification, a second process to supervise, a second artefact to deploy, and an availability coupling: sidecar down ⇒ routing down. If we ever want it, it must be *optional and async*, i.e. for advice, not for decisions. |
| **Embed CPython (`cgo` in Go, PyO3 `embed` in Rust)** | **Rejected outright.** | A second runtime inside the process that holds a global lock, installs signal handlers, and cannot be unloaded. It fights the Go scheduler and GC for the same cores, makes `CGO_ENABLED=0` builds impossible, and turns any CPython crash into a routing outage. Note `onnxruntime_go`'s own README documenting that the CUDA EP **overwrites Go's default signal handlers** ([issue #140](https://github.com/yalue/onnxruntime_go/issues/140)) — that is the *class* of coupling we are refusing, from a library far more tamed than an embedded interpreter. Debuggability alone disqualifies it for money-moving code. |
| **Model export to native (ONNX)** | **Deferred; the designated escape hatch.** | The right answer *if and only if* decision-time inference becomes a real requirement (e.g. a learned auth-probability model over rich features, or a contextual bandit with a network encoder). Then: train in Python, export to ONNX, run in-process behind a `Scorer` interface. In Go today that means `onnxruntime_go` (cgo, pinned ORT version) or a purego load — workable, and it is the only place in the project where `cgo` will be permitted, behind a build tag. If that day comes, the `ort` gap is the strongest single argument for Rust and must be re-weighed (#7's "continuous reward" question is the same fork). |
| **Native implementation + file-based artifacts** | **Chosen.** | Zero hot-path coupling. Python keeps pandas/numpy/river/DuckDB where they are unreplaceable (OPE, drift research, benchmark generation) and loses nothing, because it was never in the loop at decision time. |

**The contract** (fields owned by #7 and #13; the *shape* is locked here):

- **Engine → Python (out)**: decision traces and posterior snapshots, append-only, in the store chosen by #13. Python reads them with DuckDB. The engine never knows a Python process exists.
- **Python → Engine (in)**: versioned artifacts on disk — prior tables, learned hyperparameters, drift thresholds, shrunk policy parameters. Atomic publish (`write tmp → fsync → rename`), validated against a schema at load, checksummed, with `schema_version` + `generated_at` + `ttl_seconds`.
- **Reload is cold**: the engine loads artifacts at boot and swaps them in on a reload tick (SIGHUP or interval) under an RWMutex on a pointer swap. A missing, stale, or invalid artifact **must never reject or delay traffic** — it falls back to in-memory learned state, emits a metric, and alerts. The engine is startable with zero Python-produced files. This is what makes the coupling genuinely loose rather than merely documented.
- **No Python process is a dependency of the routing path.** A crashed sidecar is a *degraded insight*, not an outage. This is also the deployment-topology answer the map listed as blocked on this ticket: **single binary**, one artifact, no sidecars.

## 6. What we tell #12/#13/#17 (the unblocking payload)

- **#12 (module layout)**: Go package layout, one `internal/` tree, `switchback` as the module path. `router.Decide` takes `context.Context` as first arg and returns a value type — no pointers in the request/response path, so nothing escapes and nothing is shared mutably. The `ProcessorClient` interface is a Go `interface` with `Do(ctx, req) (resp, error)`; the simulator implements the same interface, which is what makes #6's harness a compile-time guarantee rather than a protocol document. Numeric state lives in `[]ArmStat`, not `map[ArmID]*Arm`.
- **#13 (state store)**: pure-Go SQLite is a genuine deployment advantage here — `modernc.org/sqlite` (WAL mode) keeps `CGO_ENABLED=0` and a `scratch` image. And cgo is not forbidden: the Feb-2026 driver benchmark round shows the cgo driver (`mattn`) regaining ground under Go 1.26's ~30% cheaper cgo calls, so #13 should benchmark `modernc` vs `mattn` vs `ncruces/go-sqlite3` on our write profile instead of assuming the pure-Go tax is 25% and permanent. Engine writes SQLite; Python/DuckDB exports Parquet. Do not put a Parquet writer in the hot path.
- **#17 (benchmarks)**: `go test -bench` for the engine's own numbers, `-benchtime` with fixed seeds, committed output regenerated by one command, plus the CI gates this ADR promises: a `allocs/op == 0` check on the `Decide` benchmark path (or a documented, tiny constant), a goroutine-count-after-load check using the 1.27 leak profile, and a p99 assertion on the API-boundary harness. The spike in this repo is the seed of the format: script in, committed markdown out, environment recorded.
- **#5 (constraints)**: the ConstraintSet is data (schema-validated), and the predicate evaluator is a small closed Go interpreter over a fixed operator set — *not* an embedded scripting language. This is the same decision as "no Ruby," applied to #5.2 (constraint expression language), so #5 should design to it.

## 7. The Ruby question (resolves consideration 6)

Sentimental, and we are keeping it that way. The only place a dynamic language earns its keep here is a merchant-facing constraint DSL (#5.2), and that is precisely the place where it is disqualifying: constraints gate real money, must be *auditable and explainable after the fact* (#5.6), must be deterministic and replayable for OPE (#15), and are evaluated in the hot path. Every one of those properties argues for **data + a validated schema + a fixed evaluator**, and against an interpreter that can do anything, including the thing that costs you a basis point of auth rate at 3am. The cost of a Ruby layer is not just a second toolchain: it is a second semantic surface for the reward function to leak through, and a second thing to benchmark to prove parity.

So: no Ruby in the build graph. The nod is honored where it is free — the `docs/` example set and a possible `switchback-ruby` client SDK against the public API, which is exactly the language's historical home in payments integration. It is an SDK, which means it cannot break routing correctness if it is bad. Deferred, unlabelled, not on the critical path, and if nobody volunteers, it simply does not exist and nothing is lost.

## Consequences and design rules that follow

**Positive.** One toolchain, one build artifact, CI in seconds; a contributor pool that overlaps our actual audience; `context.Context` deadline discipline as the language-native answer to #10's cancellation problem; `CGO_ENABLED=0` static binary with an embedded store; no Python availability coupling in the payment path; pprof and race detector in every contributor's hands from day one; Go 1.25–1.27 runtime direction (Green Tea GC, container-aware `GOMAXPROCS`, ~30% cheaper cgo, `goroutineleak` profile, `encoding/json/v2`) is compounding in our favour rather than against us.

**Accepted costs, not glossed.**
1. Higher memory footprint and no deterministic destruction — we mitigate with `defer` discipline and fixed-size state, and accept that a Rust build would be leaner.
2. Data races are not a compile error. Mitigated as gates above; residual risk accepted.
3. `Decide()` must be hand-written allocation-free, and a contributor can regress that with one stray `fmt.Sprintf`. Mitigated by a CI `allocs/op` gate (#17), which is the only honest fix.
4. No `ort`. If decision-time tensor inference becomes a hard requirement, Go's ONNX path is cgo-shaped and one tier below Rust's. This is the most likely reason to reopen the ADR.
5. No `arrow-rs`-class Parquet writer in-engine. Mitigated by #13 writing SQLite and Python exporting.
6. Go's type system gives weaker modelling of arm state machines (no exhaustive `match` on decline classification for #10). Mitigated by convention + lint + table-driven tests over every state, and by keeping the state machine tiny.

**Non-negotiable rules (this ADR binds later tickets):**
- R1 — no synchronous network hop inside `Decide()`: no store round trip, no RPC, no lock held across I/O. Store writes are buffered/async.
- R2 — no Python in `Decide()`, and no Python in the process. Cold file contract only; missing artifacts never affect routing.
- R3 — `Decide()` allocates nothing measurable; enforced by benchmark assertion.
- R4 — every processor call takes `ctx` with a deadline derived from `deadline_ms`; no call may be issued without one (informs #10).
- R5 — posterior and drift state are fixed-layout, pointer-free slices owned by one writer per shard; readers never mutate, updates flow through a channel or an atomic CAS.
- R6 — `-race` on all integration tests, `GOEXPERIMENT` untouched (no opt-outs from Green Tea).
- R7 — `cgo` is permitted only behind a build tag for a future ONNX scorer. Nothing else.
- R8 — the deployable artifact is one static binary plus a schema-versioned config/artifact directory; no sidecars.

## Day-one validation (Go, when a toolchain exists)

The sandbox this ADR was written in has no Go and no network, so the extrapolation band in §1 is exactly that — a band. Three measurements retire it, and they belong in `benchmarks/` as `go test -bench` targets feeding #17, not in a throwaway spike:

```go
// B1: the allocation-free hot path. The assertion that matters is allocs/op, not ns/op.
//     Kill-criteria for R3: allocs/op > 0 on this path without a written exemption.
func BenchmarkDecide8Arms(b *testing.B)   // target: < 1 µs, 0 allocs
func BenchmarkDecide32Arms(b *testing.B)  // target: < 4 µs, 0 allocs  (fine-grained arms, #7)

// B2: the collector, under the load we actually run. Compare GOGC off vs default
//     vs GOMEMLIMIT-pinned; the claim being tested is "0 alloc/decision => GC is not
//     a latency input". If p99 moves between these rows, R3 is being violated somewhere.
func BenchmarkDecideUnderLoad(b *testing.B) // 5k decisions/s, 4 vCPU, p99 reported

// B3: ingest + posterior update contention (the #7.4 concurrency question, priced).
func BenchmarkOutcomeIngestSharded(b *testing.B)
```

Expected outcome, stated in advance so it can be wrong: B1 lands in the 0.3–1.5 µs range at 0 allocs, and B2's p99 is flat across GC settings. If instead B1 is >10 µs or B2 shows p99 sensitivity to GOGC, the §1 conclusion is under review and trigger 1 has fired — in which case the right response is first a memory-layout fix, and only then a language reconsideration.



## Reopen triggers

Revisit this ADR if any of the following becomes true — each is stated so it can be falsified by a number, not an opinion:

1. `go test -bench` at target load shows p99 **in-engine CPU > 20 µs**, and the allocation audit (pprof `-alloc_objects`) shows it is *not* fixable by buffer reuse. Then we have a real GC/allocator problem and Rust earns a re-weigh.
2. Decision-time latency p99 at the API boundary misses **2 ms** at 5k decisions/s on 4 vCPU after R1–R5 are demonstrably satisfied in code.
3. The bandit grows a **learned model at decision time** (contextual features, network encoder, continuous-reward model from #7/#4) whose inference exceeds ~100 µs per decision in Go's ONNX path, or where cgo constraints block the required ORT features. `ort` is then the deciding factor.
4. In-engine **Parquet** throughput becomes a requirement (> ~100 MB/s sustained from the hot path).
5. The engine becomes **multi-tenant and density-bound** (many tenants on one node at >50k decisions/s), where 2–4× memory efficiency converts directly into cost, or a memory-safety audit requirement lands.
6. Sustained contributor friction attributable to concurrency bugs — e.g. ≥2 production data races or leaks in 6 months that a borrow checker would have caught at compile time.

## Alternatives considered

- **Rust + tokio for the engine core.** Strongest form of the case: `ort`, `arrow-rs`, compile-time race freedom, 2–4× memory efficiency, sub-µs deterministic pauses, and the two closest OSS precedents (Hyperswitch, Juspay decision-engine) are Rust. Rejected because the latency argument is empty at our scale (§1), the safety argument is aimed at a bug class we are not primarily exposed to (§Context), the interop argument is inverted (the tight Python coupling is *ours* to avoid, and avoiding it is what makes Go fine), and the remaining advantages are worth less to a one-maintainer OSS decision-science engine than review surface and contributor throughput. Recorded as the decision most likely to be revisited via triggers 3 and 5.
- **Rust core + Go FFI (or a Rust `Decide()` linked into a Go process).** Rejected. The worst of both: two toolchains, two build matrices, and the FFI boundary sits exactly where the panic/abort semantics and allocation ownership are ambiguous, i.e. in the code that moves money.
- **Go engine + embedded CPython via cgo.** Rejected in §5; it makes the GC, the scheduler, and the GIL interact in the same process and forecloses `CGO_ENABLED=0`.
- **Go engine + Python gRPC sidecar for bandit scoring.** Rejected in §5 on measured transport cost and availability coupling. Retained as a possible *async advisory* channel if a future ticket needs Python-only model families; never on the decision path.
- **Python engine (the "it's just a bandit" option).** Rejected: the ingest path (§1 [M2]) and the trace volume put real sustained load on the process, and the interpretation of "≤Y ms p99" under a 30-concurrent-connection benchmark is not a claim anyone should have to defend.
- **Elixir / BEAM**, which has real deployments in payment orchestration for exactly the reasons #2's concurrency bullet favours Go. Rejected anyway: per-process heap isolation makes the posterior store awkward, tight numeric inner loops are the runtime's weak spot, and it deepens rather than removes the Python boundary. Not the map's question; recorded for completeness because it is the third answer a payments engineer will propose.

## Appendix: reproduction

```bash
python3 spikes/0001-latency-budget/hotpath.py
```

Deterministic (fixed seed, no network). Machine-readable numbers, not prose: `M1`/`M2` are measurements; `B1`/`B2` are analytic models with their constants named inline. The sandbox this ran in has 2 vCPUs and no perf isolation, so treat the µs as order-of-magnitude, and note the argument is built to survive a 10× error. It is *not* a Go-vs-Rust benchmark — that toolchain is unavailable here, and those numbers belong to #17 (`go test -bench`, regenerated by script, committed).

Primary sources: [Go 1.27 release notes](https://tip.golang.org/doc/go1.27) · [Green Tea GC](https://go.dev/blog/greenteagc) · [Go GC guide](https://go.dev/doc/gc-guide) · [local gRPC IPC latency histograms](https://www.mpi-hd.mpg.de/personalhomes/fwerner/research/2021/09/grpc-for-ipc/) · [onnxruntime_go](https://github.com/yalue/onnxruntime_go) · [ort](https://ort.pyke.io/) · [Go SQLite driver benchmarks, Feb 2026](https://www.reddit.com/r/golang/comments/1r36pwn/the_sqlite_drivers_benchmarks_game_feb_26_go_126/) · [juspay/decision-engine](https://github.com/juspay/decision-engine) · [juspay/hyperswitch](https://github.com/juspay/hyperswitch)
