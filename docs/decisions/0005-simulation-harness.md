# ADR-0005: Simulation harness — one processor interface, scenarios as content-addressed documents, key-derived determinism

- **Status**: Accepted
- **Date**: 2026-09-15
- **Resolves**: [#6 Simulation harness architecture: synthetic acquirer fleet design](https://github.com/Sehaan-1/switchback/issues/6)
- **Depends on**: [ADR-0001](0001-engine-language-go.md) (Go core; `ProcessorClient` is a Go interface, no Python in `Decide()`, the cold file contract), [ADR-0002](0002-reward-function.md) (the closed outcome taxonomy the harness must emit), [ADR-0003](0003-bandit-not-static-table.md) (the arm space and the baseline set the harness must be able to run), [ADR-0004](0004-constraint-layer.md) (the context fields the harness must emit and the catalog contract it must honour)
- **Feeds**: #7 (arm granularity, forced exploration), #8 (drift detection: the degradation shapes it is graded on), #10 (idempotency: late settlement and the three failure modes), #11 (3DS: the funnel it must estimate into), #12 (module layout: the interface and the package boundary), #13 (state store: the trace row contract), #15 (OPE: `all_arms` recording and the counterfactual), #16 (dashboard: the same document drives the demo), #17 (benchmarks: scenario ids, targets T1–T3, the CI gate)
- **Evidence**: [`spikes/0006-simulation-harness/`](../../spikes/0006-simulation-harness/RESULTS.md) — a working reference implementation of this design, `python3 spikes/0006-simulation-harness/harness.py 20000 --full=1000000`; and [`simulator/scenarios/`](../../simulator/scenarios/) — the schema, four worked scenarios, four negative fixtures, two golden files and the gate over all of them, `python3 simulator/scenarios/check.py`

---

## Decision

1. **One interface is the harness contract, and it is the acquirer's.** `acquirer.Client` has one method — `Authorize(ctx, Request) (Response, error)` — implemented both by the production HTTPS client and by the synthetic fleet. Nothing simulator-only may appear on a `Response`; the model's private knowledge (the true approval probability, whether the issuer would have approved) is reachable only through a separate `Oracle` handle that benchmark code holds and engine code cannot see. The engine is never told which one it is talking to.
2. **A scenario is a versioned JSON document, resolved and content-addressed.** `simulator/scenarios/schema/scenario.schema.json` v1.0.0 defines the grammar; `extends` overlays are resolved *before* hashing; a stdlib-only gate (checks SV0–SV10) validates the document against the schema, against the pinned acquirer catalog and against four negative fixtures; and `golden/scenario-hashes.json` pins the result. Every benchmark number is cited as `<scenario-id>@sha256:<12>`, never as a filename.
3. **Determinism is key-derived, index-addressed randomness plus an injected virtual clock — not a seeded stream.** `u = float64(mix64(stream(seed, domain, keys…) + index·φ) >> 11) · 2⁻⁵³`, specified in 25 lines and pinned by 26 golden vectors that any implementation must reproduce. The world's answer for `(transaction, acquirer, attempt)` is a total function of the scenario; it does not depend on the policy, the query order, the shard count, the fleet membership, the process, the hash seed or the timezone ([M2], [M3]). The harness path provably never reads wall time, the global RNG or the environment — the spike rigs all eight of them to raise and the digest is unchanged.
4. **The behavioural model is `fleet-v1`: six parameter blocks and five event types.** Per acquirer: `auth` (base rate × BIN × region × amount kink), `decline_mix` (weights over catalogued codes; retryability is a scheme fact, never a scenario parameter), `latency` (three declared quantiles plus a tail index: lognormal body through p50/p95, generalised-Pareto tail pinned at p99), `three_ds` (frictionless rate, abandonment, liability-shift uplift, per-BIN and per-category multipliers), `late_settlement`, and fleet-level `correlation` (AR(1) latent factors). Scheduled events: `gradual_overload`, `outage` (four failure modes), `recovery` (three curves), `traffic_spike`, `fleet_shock`.
5. **Replay is two features with two record contracts, and speed is a target with three numbers.** Re-running needs `(scenario_hash, seed, model_version, harness_version)`; counterfactual replay additionally needs every arm recorded or the propensity logged, which is why `recording.mode` is a scenario field and why a trace source is *required* to be `all_arms` (SV9). Targets: **T1** 1M transactions × 6 arms, `all_arms`, ≤ 30 s on one core; **T2** 10M over 8 shards ≤ 60 s; **T3** ≤ 2 µs of harness per attempt. Measured in CPython: 7.06M model evaluations for T1 in 64–79 s across four runs of the same command (9.4–11.2 µs/attempt), projecting to 2.1–3.2 s natively — 9–14× headroom before sharding. #17 owns the final wording and the CI gate.

## Context

Three facts about the project's shape change how the evidence below should be read.

- **The harness is not a test double; it is the instrument that produces the project's only numbers.** Every claim this repository will make — "+X pts of authorization rate at equal effective cost", "the bandit re-discovers a degraded acquirer in N minutes" — is a harness output. A test double only has to be convenient. An instrument has to be *calibrated, versioned and reproducible*, because a reader in six months cannot re-derive the claim from the code; they can only re-run the world.
- **The thing under test learns, so the world must not respond to it.** A router that adapts and an environment that adapts back is a feedback loop, and the loop is invisible in aggregate: it shows up as noise. This is not a hypothetical — [M3] measures it. With the ordinary "seed the RNG" design, **26.6% of the world's answers change when the policy asks the arms in a different order**, and **26.5% change when a seventh acquirer is added to the fleet**. Two policies in the same benchmark table were being scored against two different worlds.
- **ADR-0001 already made the interface a compile-time guarantee in Go and left everything else open.** It fixed `ProcessorClient` as a Go interface with `Do(ctx, req) (resp, error)` and said "the simulator implements the same interface, which is what makes #6's harness a compile-time guarantee rather than a protocol document". What it did not say is what may cross that interface, what the world behind it must be able to do, or how a run is identified. Those are this ADR's job, and §1 answers the first of them because it constrains all the rest.

## 1. The harness interface: one method, and a hard line around it (consideration 5)

The ticket calls this the architectural crux, and it is — but the crux is not "does the simulator implement the same interface". ADR-0001 already decided that, and in Go the compiler decides it again every build. The crux is **what is allowed to cross the interface**, because a simulator that can tell the engine something a real acquirer cannot is a benchmark that grades the engine on a skill it will never use.

### The contract

```go
package acquirer

// Value types throughout: no pointers in the request/response path, so nothing escapes
// and nothing is shared mutably (ADR-0001 §6, R5).

type Request struct {
    TxnSeq        uint64        // the transaction's identity in this run
    IdempotencyKey [16]byte     // #10: the same key on a retry of the same attempt
    Attempt       uint8         // 0-based position in the chain
    AmountMinor   int64
    Currency      [3]byte
    BinClass      BinClass      // uint8: consumer_credit | consumer_debit | premium_credit
                                //        | corporate | prepaid
    CardCountry   [2]byte
    CardRegion    Region        // uint8: EEA | UK | US | LATAM | APAC
    MerchantCat   MerchantCategory
    RouteClass    RouteClass
    EntryMode     EntryMode
    SCARequired   bool
    Mandate       bool
    DeadlineMS    uint32        // the caller's deadline, honoured by ctx as well
}

type Response struct {
    Acquirer     AcquirerID
    Attempt      uint8
    Outcome      Outcome       // ADR-0002's closed taxonomy, plus TransportError
    Code         DeclineCode   // scheme response code; empty unless Outcome is a decline
    Scheme       Scheme        // iso8583 | nacha: which catalog Code came from
    DeclineClass DeclineClass  // soft | hard | none — a scheme fact, not an inference
    LatencyMS    uint32        // what the caller observed, capped at the deadline on timeout
    SettledAt    int64         // virtual-clock ms; 0 when the outcome is UNKNOWN
}

type Client interface {
    Authorize(ctx context.Context, req Request) (Response, error)
}
```

`Outcome` is ADR-0002's enum with one addition:

| Outcome | A real acquirer produces it when… | The harness produces it from… |
| --- | --- | --- |
| `Authorized` | the issuer approved | `u_auth < p` and the deadline held |
| `DeclinedSoft` | a retryable code (`51`, `05`, `91`, …) | the decline mix, code class `soft` |
| `DeclinedHard` | a terminal code (`14`, `43`, `54`, …) | the decline mix, code class `hard` |
| `Abandoned` | the 3DS challenge was never completed | `challenged ∧ u_abandon < abandon_rate` |
| `Timeout` | the deadline passed, result **unknown** | modelled latency > `DeadlineMS`, or `failure_mode: timeout` |
| `TransportError` | connection refused, 5xx, TLS failure | `failure_mode: connection_refused \| http_503` |

`TransportError` is not in ADR-0002's five, and adding it is deliberate: an outage is not a decline, and a harness that can only express an outage as a timeout cannot test the difference between "unknown, hold the lease" and "certainly failed, release it" — which is #10's whole subject. It maps onto the reward as ADR-0002 maps `timeout` minus the ambiguity price: the attempt fee is spent, the outcome is known, so there is no λ_to. #7 owns the counter; this ADR only insists the harness can emit it.

### The line that may not be crossed

**R27 (below) is the rule, and [M1] is the test.** The spike's driver is probed with a wrapper that records every attribute it touches; over a full run it touches exactly one — `authorize`. And no field on `Response` is something a real acquirer could not return.

The tempting violations, and why each is disqualifying:

- `Response.TrueApproveProbability`. It makes an oracle out of the response, so a policy can be written that reads it and still "pass". The benchmark would then be measuring whether the author remembered not to look.
- `Response.Simulated bool`. Now the engine can branch, and somebody will.
- `Client.Reset()`, `Client.InjectFailure()`, `Client.SetLatency()`. These are *scenario* operations, and they belong to the scenario document, not to the interface. A test that calls `InjectFailure()` mid-run has performed an edit to the world that no scenario hash describes, which is exactly the silent drift §4 exists to prevent. Fault injection is an `events[]` entry with an `at_s`; a unit test that needs it at a specific transaction uses a scenario with a short clock.
- A `context.Context` value carrying simulator hints. `ctx` carries the deadline and cancellation (ADR-0001 R4) and nothing else.

### Truth, behind a different handle

Benchmarks need ground truth — regret is defined against it, and ADR-0003's baseline table cannot be computed without it. So the simulator's constructor returns two values:

```go
// NewFleet returns the clients the engine may see and the oracle only benchmarks may hold.
func NewFleet(scenario Scenario, clock Clock) (map[AcquirerID]Client, Oracle)

type Truth struct {
    PApprove     float64  // the model's conditional approval probability, end to end
    Challenged   bool     // whether a 3DS challenge was presented
    WouldApprove bool     // whether the issuer approved, independent of the deadline
    SettlesLateAt int64   // 0 unless a timed-out attempt is later authorized (#10)
}

type Oracle interface {
    Truth(txnSeq uint64, a AcquirerID, attempt uint8) (Truth, bool)
}
```

`Oracle` lives in a package the engine does not import (`internal/sim/oracle`), which in Go is a real boundary rather than a convention. Because the world's draws are index-addressed (§3.1), `Truth` can be computed for an arm that was never called — that is what makes the counterfactual in §5.2 free rather than approximate.

Note the honesty requirement this creates: `Truth.PApprove` is the *conditional* approval probability, while the number a benchmark reports is usually the *end-to-end* authorization rate, which is lower because timeouts and abandoned challenges are in the denominator. [M6c] shows the gap and, better, shows why you cannot read a liability shift off it (§2.5).

### Three consumers, one interface

| Consumer | What it plugs in | Why the same interface matters |
| --- | --- | --- |
| Unit tests (`#12`) | a 20-line scenario with one acquirer and a 1-second clock | the test exercises the real decision path, not a stub of it |
| Benchmarks (`#17`) | a committed scenario, `all_arms` recording | the number is reproducible from a hash |
| Local development | the same binary, `--acquirer=sim` | a contributor with no processor credentials can run the engine end to end |

The third is the one that pays for the discipline: "run the engine against a simulator with zero code changes" is not an abstraction argument, it is the difference between a contributor's first evening and their first week.

## 2. What the fleet must simulate (consideration 1)

Five asks, five parameter blocks. Each subsection states the model, the evidence, and what is deliberately *not* modelled.

### 2.1 Decline codes: a mix over a catalog, with retryability taken away from the author

The ticket asks for "configurable decline codes per acquirer (R01 insufficient funds, R03 do-not-honour, R12 invalid card, etc.)". **The framing mixes two schemes, and the difference matters.** R01/R03/R12 are NACHA ACH *return* codes — R01 insufficient funds, R03 no account/unable to locate, R12 account sold to another DFI. Card authorization declines are ISO 8583 response codes: `51` insufficient funds, `05` do not honour, `14` invalid card number. A harness that accepted `R03` as a card decline would be modelling a payment method it does not have, and an engine that learned "R03 is retryable elsewhere" would be learning a fact about ACH from a card fixture.

So: `decline_mix.scheme` is `iso8583` or `nacha`, the codes are validated against a catalog in the gate (SV4), and **the catalog also fixes each code's retryability**. A scenario may invent frequencies; it may not invent codes, and it may not declare a hard decline soft — because that flag is what decides whether the engine spends another attempt and pays another attempt fee. The card path ships first; the NACHA branch exists so that adding ACH later is a catalog change and not a model change.

Per-acquirer weights, normalised at load. The fixture fleet's calibration ([M6a], n = 20,000 transactions × 6 arms):

| Family | Codes | Fixture | Published band |
| --- | --- | --- | --- |
| insufficient funds | `51`, `61`, `65` | 36.68% | 25–40% |
| do not honour | `05` | 19.21% | 15–25% |
| card invalid | `14`, `54`, `41`, `43`, `62` | 12.40% | 10–15% |
| fraud / security | `59`, `01`, `N7` | 5.78% | 5–10% |
| technical | `91`, `96`, `12` | 9.66% | 5–10% |
| not permitted | `57` | 16.26% | 10–20% |

Six families, six bands, all six inside. The bands are cited in the appendix, not measured here, and the fixture's soft/hard split comes out at 65.6% / 34.4% — which is the number that decides how much a fallback chain is worth, and therefore the number a benchmark conclusion is most sensitive to. That is why it is a scenario parameter with a calibration target and a printed comparison, rather than a constant in a model file.

**Not modelled:** issuer-specific decline behaviour (the same issuer declining differently by acquirer), decline-code *drift* as a scheme changes its codes, and the interaction between a decline code and the network's reattempt counters — that is ADR-0004's `network.reattempt_limit`, evaluated by the engine, not by the world.

### 2.2 Latency tails: three quantiles and a tail index, because a distribution name cannot hit three quantiles

The ticket asks for "p50/p95/p99 per acquirer, diurnal patterns, correlated spikes". Those are three different mechanisms and they belong in three different places:

- **The per-acquirer tail** is a property of the acquirer: `latency.{p50_ms, p95_ms, p99_ms, tail_index}`.
- **Diurnal patterns** are a property of the *traffic*, not of the acquirer: `traffic.arrivals.diurnal` modulates the arrival rate. Latency follows load through `congestion_coefficient`, which is one number and one honest assumption (`latency_mult = 1 + κ·(rate(t)/nominal − 1)`), rather than a queueing model nobody would believe.
- **Correlated spikes** are a property of the *fleet*: `correlation.factors[]`, each an AR(1) latent process over `bucket_s` windows that multiplies the latency of its member acquirers, plus `fleet_shock` events that displace a factor by N standard deviations. Without them a fleet-wide event is N independent events, which is not what a scheme outage looks like.

The distribution is a **lognormal body fitted through (p50, p95) with a generalised-Pareto tail above p95 pinned to p99**, shape ξ = `tail_index`:

```
u ≤ 0.95 :  x = p50 · exp(σ_body · Φ⁻¹(u)),      σ_body = (ln p95 − ln p50) / 1.6449
u > 0.95 :  x = p95 + (σ_gpd/ξ)·((0.05/(1−u))^ξ − 1),   σ_gpd = ξ·(p99 − p95)/(5^ξ − 1)
```

Why this and not a named distribution with two parameters: **[M4] shows a two-parameter family cannot reproduce three declared quantiles.** Four shapes fitted to alpha's declared (190, 420, 640) ms, 120,000 draws each:

| shape | p50 | p95 | p99 | err p95 | err p99 | p99.9 | max | P(>900 ms) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **lognormal + GPD (fleet-v1)** | 189.8 | 418.6 | 643.1 | −0.3% | +0.5% | 1187.5 | 6270.4 | **0.263%** |
| lognormal fitted p50/p99 | 189.8 | 446.7 | 642.3 | +6.4% | +0.4% | 968.8 | 2221.1 | 0.147% |
| lognormal fitted p50/p95 | 189.8 | 418.6 | 585.4 | −0.3% | −8.5% | 855.7 | 1841.6 | 0.071% |
| the shape `spikes/0004` hand-rolled | 189.9 | 569.2 | 625.9 | +35.5% | −2.2% | 638.7 | **640.0** | **0.000%** |

Three findings, in increasing order of inconvenience:

1. Fit p50/p99 and p95 is 6.4% wrong; fit p50/p95 and p99 is 8.5% wrong. The author thinks they declared a tail and the model has quietly moved it.
2. Nothing in the declared triple constrains **p99.9, and p99.9 is where a 900 ms deadline lives**: the four shapes span 639–1188 ms there and 0.000–0.263% in breach rate — a 3.7× spread in the timeout rate of the *same declared acquirer*, and one shape with no timeouts at all.
3. The last row is this repository's own prior art. `spikes/0004-reward-function/fleet.py` needed a latency model and hand-rolled a bounded one whose maximum draw is exactly its declared p99. Every timeout rate in ADR-0002's evidence table was therefore produced by a world in which a slow acquirer cannot be slower than its p99. The conclusion survives — it is about reward terms, not tails — but it is a demonstration of the failure mode: **when the harness does not own the latency model, every experiment invents one, and invents a convenient one.**

So the model belongs to the harness, is named in `model_version`, and is changed by bumping it. ξ defaults to 0.25 and is a scenario parameter, because "how bad is the tail beyond the worst quantile we can see" is precisely the question a p99-based dashboard cannot answer and a deadline-based router must.

Φ⁻¹ is Acklam's rational approximation, max |error| 2.9 × 10⁻⁹ against `statistics.NormalDist` ([M4]) — five orders of magnitude below the sampling error of any quantile a benchmark reports, so the closed form is fine and a 4096-entry table is the Go implementation's optimisation, not a correctness requirement.

### 2.3 Health degradation: gradual, sudden, recovery — and the three ways "down" differs

Five event types, all scheduled in virtual time and all resolved by a fold in `(at_s, content-hash)` order, so document order cannot change the world ([M2], row 6):

| Event | Parameters | It models |
| --- | --- | --- |
| `gradual_overload` | `ramp_s`, `auth_multiplier_to`, `latency_multiplier_to` | an acquirer sliding: the hard case, because a step change is detectable by anything and a ramp is detectable only by a detector that knows what it is looking for (#8) |
| `outage` | `duration_s`, `failure_mode`, `auth_multiplier` | a processor outage — with four failure modes, below |
| `recovery` | `curve` ∈ step/exponential/linear, `tau_s`/`duration_s`, `auth_multiplier_from`, `latency_multiplier_from` | what happens *after*, which is where benchmarks are quietly optimistic |
| `traffic_spike` | `duration_s`, `rate_multiplier` | Black Friday: more arrivals, and through `congestion_coefficient`, worse latency |
| `fleet_shock` | `factor_id`, `magnitude`, `duration_s` | a displacement of a latent factor: the correlated spike that is nobody's fault |

**`failure_mode` is the point of the whole block.** Three outages, three engine-visible worlds ([M5], 400 transactions per 10-minute bucket, clock set directly):

| `failure_mode` | auth% | soft% | timeout% | transport% | median latency | late authorizations per 400 |
| --- | --- | --- | --- | --- | --- | --- |
| `timeout` (alpha, 900 s) | 0.00 | 0.00 | **100.00** | 0.00 | 900 ms (the deadline) | **95** |
| `connection_refused` (foxtrot, 600 s) | 0.00 | 0.00 | 0.00 | **100.00** | 46 ms | 0 |
| `decline_storm` (echo, 1200 s, auth ×0.02) | 1.50 | **56.25** | 1.00 | 0.00 | 222 ms | 0 |

- `timeout` is the only mode with an **unknown result**, so it is the only one that can produce a late authorization and therefore the only one that can double-charge. Over a full week of the fixture fleet, 24.4% of timeouts settle late: 1,178 attempts in 20,000 transactions where the engine's answer was "unknown, we gave up" and the issuer's answer was "approved" ([M5]). A mock that returns a timeout and forgets the transaction cannot produce that event, so it cannot test #10 at all. The harness emits each one as a scheduled event on the virtual clock inside `recording.late_settlement_window_s`.
- `connection_refused` has the **same routing consequence and the opposite money consequence**: release the lease immediately, retry now, no ambiguity to price. An engine that treats the two identically is right about where to send the next attempt and wrong about whether it may.
- `decline_storm` is not an outage from the engine's side at all. Every response is well-formed, prompt, and a *soft* decline — the class the engine is supposed to retry. A health model built on failures-to-respond sees a perfectly healthy acquirer and keeps feeding it traffic while it burns attempt fees and network reattempt budget. This is the failure mode ADR-0002's outcome taxonomy exists for, and it is only testable if the harness can express it.

**Recovery shape is a benchmark assumption wearing a scenario's clothes.** The fixture's alpha recovers exponentially with τ = 900 s from a 2× latency residual:

| minutes after the outage ends | latency multiplier | p99 |
| --- | --- | --- |
| +0 | 2.000 | 1280 ms |
| +5 | 1.717 | 1099 ms |
| +15 | **1.368** | **875 ms** |
| +30 | 1.135 | 727 ms |
| +60 | 1.018 | 652 ms |
| +120 | 1.000 | 640 ms |

A `step` recovery reports the arm fully healthy the instant the event ends. A router that returns traffic early — which is what a drift detector is *for* — is then never punished for it, and the benchmark concludes the detector is free. That is why SV6 requires an exponential or linear recovery to state its residual (`auth_multiplier_from` / `latency_multiplier_from`): a curve with no stated residual is a step recovery wearing a curve's name.

### 2.4 BIN-level variation: multipliers, and the arm-space consequence

`auth.bin_class_multiplier` and `auth.region_multiplier` are per-key multipliers on the base rate, closed-vocabulary (SV7) so a typo cannot silently create a stratum. `auth.amount_sensitivity` is a two-kink linear penalty above a ticket threshold, capped. Across the fixture fleet ([M6b], 120,000 arm-observations):

| BIN class | mean P(approve \| reached) | vs consumer credit |
| --- | --- | --- |
| premium_credit | 89.94% | +1.47 pts |
| consumer_credit | 88.46% | — |
| consumer_debit | 88.46% | −0.00 pts |
| corporate | 85.22% | −3.24 pts |
| prepaid | 79.54% | **−8.92 pts** |

The 8.9-point spread between premium and prepaid is the reason the arm space is bucketed on `bin_class` at all (ADR-0003 R18): a router with one arm per acquirer cannot see it, and a benchmark that reports only the fleet-average authorization rate cannot see that it cannot. Per arm the spread is not uniform — charlie is *better* on corporate (88.08% vs 85.62% on consumer credit) because the fixture gives it a 1.03 corporate multiplier, which is the shape a real acquirer with a corporate-card agreement has. That interaction (arm × segment) is the thing #7's granularity decision turns on, and it only exists if the harness can express it.

**Not modelled:** per-issuer variation *within* a BIN class. The acquirer's `bin_class_multiplier` is the projection of an issuer mix onto an arm, which is the right granularity while the arm is `(processor, flow)`. If #7 grows an issuer dimension, this model has to grow with it — reopen trigger 4.

### 2.5 3DS: a funnel with three independently settable numbers

`three_ds.{frictionless_rate, challenge_abandon_rate, liability_shift_uplift}`, with `bin_class_multiplier` and `merchant_category_multiplier` on the frictionless rate. The funnel: `P(challenge) = 1 − frictionless × multipliers`; a challenged transaction is abandoned with `challenge_abandon_rate`; a completed challenge multiplies approval by `liability_shift_uplift`. On the fixture fleet ([M6c/d], SCA-applicable traffic only):

| | fixture | published |
| --- | --- | --- |
| SCA-applicable share of traffic | 29.7% | PSD2/EEA+UK mix |
| challenged | 23.17% | 15–20% |
| frictionless | 76.83% | 80–85% |
| abandoned, of those challenged | 21.33% | 10–15% |
| challenge rate, digital goods | 5.45% | — |
| challenge rate, travel | 9.03% | — |
| challenge rate, gaming | 9.23% | — |

Two of the three sit outside the published bands, deliberately and on the record: the fleet's frictionless rates span 0.60–0.92 because `spikes/0004`'s sign-flip finding requires a fleet containing both 3DS-strong-but-auth-weak and 3DS-weak-but-auth-strong acquirers, and the 0.22 abandonment rate is inherited from that spike so the two evidence tables are comparable. Both are #11's to estimate for real. The harness's obligation is that they are **independently settable**, because a benchmark that ties abandonment to frictionless rate cannot distinguish a good 3DS treatment from a lucky one.

And one finding that is only visible because the funnel is modelled rather than asserted ([M6c]): the fixture applies a **1.055 uplift** to the challenged branch, and the challenged branch's conditional authorization rate still comes out **below** the frictionless one (83.65% vs 85.97%). That is selection, not a bug — the contexts that get challenged are the higher-risk ones, and the amount sensitivity has already taken a bite out of their approval probability. You cannot read a liability shift off an aggregate table. That is ADR-0002's argument against a 3DS multiplier, reproduced as data instead of asserted as reasoning.

## 3. Determinism: streams, clock, and a proof by poisoning (consideration 2)

### 3.1 The stream is specified, not implemented

```
mix64(z)     = splitmix64 finaliser (Stafford variant 13):
               z ^= z >> 30; z *= 0xBF58476D1CE4E5B9
               z ^= z >> 27; z *= 0x94D049BB133111EB
               z ^= z >> 31                                    (all mod 2⁶⁴)
key64(k)     = FNV-1a/64(k.utf8)              if k is a string
               (k + 0x9E3779B97F4A7C15) mod 2⁶⁴  if k is an integer
stream(seed, k₁…kₙ) = fold: h = seed; h = mix64(h ^ key64(kᵢ))
draw(s, index)      = float64(mix64((s + index·0x9E3779B97F4A7C15) mod 2⁶⁴) >> 11) · 2⁻⁵³
```

Four domain tags, so the same integer can never mean two things: `arr` (arrival gaps), `ctx` (context fields), `att` (attempt outcomes), `fac` (latent factors). The attempt stream is `stream(seed, "att", txn_seq, fnv1a64(acquirer_id), attempt)` — **the acquirer enters as a hash of its id, never as its position in the fleet**, which is what makes §3.3's second result possible. Draw indices within a stream are fixed constants (0 = latency, 1 = approval, 2 = decline code, 3 = challenge, 4 = abandonment, 5 = late settlement, 6 = late delay) and are part of `model_version`: reordering them changes every number a scenario produces, which is exactly what a version bump is for.

This is 25 lines of integer arithmetic with no library, no floating-point state and no platform dependence, and it is committed twice: as code in `simulator/scenarios/check.py` (the normative statement, imported by the spike rather than duplicated) and as **26 golden vectors** in `simulator/scenarios/golden/stream-vectors.json` that the gate verifies. A perturbation of 10⁻⁹ in any vector fails the gate. So #12's Go implementation is checked against a fixture rather than against a Python file, and #15's analysis can regenerate a counterfactual in a notebook without running the engine at all.

### 3.2 Two RNGs, and the boundary between them

**The world's randomness and the policy's randomness are separately seeded, and this is a rule, not a preference.** The world's seed is `scenario.seed`; the policy's (Thompson draws, ε-greedy noise, tie-breaks) comes from a run seed that #17 records per run in the trace header. If they share a seed, changing the policy changes the world — which is the failure §3.3 measures, arriving through a door nobody would think to check.

### 3.3 Why not "seed the RNG"

The obvious design is one PRNG per run, consumed in call order. It is what every spike in this repository does today, including the ones behind ADR-0002/0003/0004. [M3] holds the model, the contexts and the clock identical and varies only the RNG discipline (n = 20,000 transactions × 6 arms = 120,000 comparisons per row):

| Test | Shared seeded PRNG | Key-derived streams |
| --- | --- | --- |
| Same fleet, queried in reverse order | **26.645% of answers change** | **0.000%** |
| A seventh acquirer added (sorting first, so every ordinal shifts) | **26.544% of answers change** | **0.000%** |
| Per-arm P(authorized) reported by two runs of the same seed and scenario | up to **0.345 pts** apart | **0.000 pts** apart |

The second row is the one that decides the design. Under a shared stream you cannot add an acquirer to a benchmark fleet and keep the old numbers comparable, so **every fleet change silently invalidates the baseline** — and a benchmark suite that cannot accumulate is a benchmark suite that gets rewritten before each release. Under derived streams the six existing arms answer bit-for-bit as before and the new arm is a pure addition.

The third row is why this survives code review: 0.35 pts is smaller than most fleet-level effects #17 will report and larger than most per-arm differences it is trying to resolve. It does not look like a bug. It looks like noise.

The cost ([M3] test 4): in CPython a derived draw is 583 ns against 48 ns for a Mersenne step, 12.2×. That ratio is an artifact of Python's arbitrary-precision integers — `& M64` and a 64-bit multiply are heap operations there, while `random.random()` is one C call. In Go, `splitmix64` is three xors, two multiplies and a shift against a table lookup and a temper, i.e. the same order of magnitude, a few ns either way (labelled a model: no Go toolchain here, per ADR-0001's day-one-validation note). [M7] puts the whole attempt at 8–11 µs in CPython with the PRNG at 21–25% of it, so even the pessimistic reading does not decide the runtime.

### 3.4 The clock is injected, and the claim is tested by poisoning

Virtual time: `clock.advance_to(ms)`, nothing else. Simulated latency is arithmetic on that clock, which is why a 604,800-second scenario runs in a minute instead of a week and why a `traffic_spike` can be tested without a load generator. `clock.start` is a UTC instant with a literal `Z` — no local time, because a scenario that shifted with the runner's timezone would not be a scenario.

"No `time.Now()` in harness paths" is usually a lint rule and a hope. Here it is an experiment ([M2]): rig `time.time`, `time.monotonic`, `time.perf_counter`, `time.sleep`, `datetime.now`, `random.random`, `random.betavariate` and `os.urandom` to raise, then run a whole scenario. Twelve runs — ten that must produce one digest, and two controls that must not:

| Perturbation | Expected | Result |
| --- | --- | --- |
| reference, 1 shard, in process | — | `sha256:0a60ead7…` |
| run again in the same process | identical | PASS |
| batch boundaries changed (97 shards) | identical | PASS |
| arms queried in reverse order | identical | PASS |
| both at once (97 shards, reversed) | identical | PASS |
| events listed in reverse document order | identical | PASS |
| **poisoned clock and global RNG** | identical | **PASS** |
| fresh process, `PYTHONHASHSEED=random`, TZ=America/Los_Angeles (×2) | identical | PASS |
| fresh process, `PYTHONHASHSEED=0`, TZ=Asia/Kolkata | identical | PASS |
| CONTROL: virtual clock perturbed on every read | **must differ** | PASS |
| CONTROL: seed changed by one | **must differ** | PASS |

The two controls are what make the other ten mean something: a perturbation that *should* change the world does change it, so "identical" is not the result of a digest that ignores its inputs.

Two model bugs were found by this table while it was being written, and both are the kind that a single-run test cannot see:

- The AR(1) latent factor was computed lazily from whatever bucket the caller asked for first. Under sharding, shard *k* visits buckets 0, 97, 194…, so the chain restarted constantly and a sharded run produced different latencies than a serial one. The fix is that the chain is always extended from bucket 0 — an invariant, not an optimisation.
- Event tie-breaking used document order. Reversing `events[]` changed the world. The fix is a tie-break on the event's own canonical bytes, so two events at the same instant are resolved by content.

Neither would have been caught by "run it twice and compare", which is the determinism test most projects write.

## 4. Scenario composition: documents, overlays, and a gate (consideration 3)

### 4.1 JSON documents, not YAML, not a builder API

The ticket offers "YAML/JSON config files? A builder API?" and the answer is **JSON documents as the primary form, with a builder permitted only as a generator of documents**.

- **Not YAML.** A content-addressed artifact needs one canonical byte string. YAML has several parse models (anchors, implicit typing, `no` → `false`, the Norway problem), so "the same scenario" can be several byte strings and one byte string can be two scenarios. JSON has one. The constraint layer already chose JSON for the same reason (ADR-0004), and a project with two configuration grammars has two review problems.
- **Not a builder API as the primary form.** A builder produces a world that exists only inside a process, so it cannot be hashed, diffed, reviewed or cited. "Which world produced this number" becomes "read the test". Builders are still useful — a parameter sweep should be able to emit 200 scenarios — so the rule is that **a builder writes documents to disk and the documents are what run**. Everything downstream sees a hash either way.
- **Canonical form is shared with the constraint layer**: `json.dumps(doc, sort_keys=True, separators=(",", ":"))` → sha256. This is already how `policy.catalog_hash` is computed in `constraints/`, and one canonical form in a project is worth two good ones. A Go implementation must reproduce it byte for byte, which is a real constraint on the Go side (sorted keys, no whitespace, UTF-8, no HTML escaping) and is the reason `check.py --hash` exists as a command.

### 4.2 Overlays: `extends` is resolved before it is hashed

`black-friday-degraded-v1.json` is **51 lines and resolves to 575** ([M1]). It inherits the entire baseline fleet and adds four events and one congestion coefficient. Because the *resolved* document is what gets canonicalised and hashed, an overlay and a hand-written full document describing the same world produce the same hash — nothing downstream can tell how a scenario was authored, and there is no "effective config" that differs from the checked one.

Constraints on overlays: single parent, no cycles, and a child may not change `seed` or `model_version` (SV2) — an overlay may describe a different world, but not a different *kind* of world or a different identity.

### 4.3 Adding "Black Friday spike + Stripe degraded": five steps

The ticket's literal example, against the fixture fleet (which has no Stripe; the target is `delta`, the acquirer a router learns to prefer — degrading the best arm is the case worth simulating, because it is the one where a stale policy keeps paying):

1. `simulator/scenarios/examples/black-friday-degraded-v1.json`, `"extends": "baseline-steady-v1"`.
2. Add the events: `traffic_spike` ×3.0 for 6 h, `fleet_shock` +1.8σ on the EEA issuer factor for 2 h, `gradual_overload` on delta ramping to auth ×0.86 and latency ×3.0 over 90 min, then `recovery` exponential τ = 1800 s **with the residual stated**. Raise `congestion_coefficient` from 0.35 to 0.55, because a traffic spike that slows nobody down is not a traffic spike.
3. `python3 simulator/scenarios/check.py --resolve black-friday-degraded-v1` — read the world you actually wrote. Inheritance is a convenience and a way to be surprised.
4. `python3 simulator/scenarios/check.py --pin` — commit the hash diff. If SV6 complains that the recovery follows no degradation, or that two outages overlap, that is the gate earning its keep.
5. Run it: `python3 spikes/0006-simulation-harness/harness.py 20000` reads these documents directly, so a new scenario is immediately executable. In Go it is `switchback bench --scenario black-friday-degraded-v1`.

Total elapsed for a competent contributor: minutes. The four events are correlated in time on purpose — in reality the spike, the congestion and the degradation of the busiest acquirer arrive together, and an engine that survives them separately may not survive them together.

### 4.4 The gate

`check.py` runs a JSON Schema subset validator (stdlib only, unknown keywords fail loudly), then ten semantic checks the schema cannot express, then the golden files:

| Check | It refuses… | Why it is a check and not a comment |
| --- | --- | --- |
| SV1 | a missing id, an unknown `model_version`, a non-integer seed | a number cited without an identity is not reproducible |
| SV2 | an unresolved `extends`, a cycle, a child that moves `seed`/`model_version` | an overlay may not change the world's identity |
| SV3 | a `catalog_hash` that does not match the catalog, an acquirer not in it | the fleet and the catalog it was written for move together (ADR-0004's rule, applied here) |
| SV4 | a decline code not in the scheme's catalog | a scenario invents frequencies, never codes |
| SV5 | non-monotone quantiles, `floor_ms ≥ p50`, ξ outside [0.05, 0.6] | an unsatisfiable tail fit produces negative latencies, silently |
| SV6 | an event past the run's end, an unknown target, overlapping outages, a recovery that follows nothing, a curve with no stated residual | the last one is a step recovery wearing a curve's name (§2.3) |
| SV7 | a context-mix key outside the closed vocabulary, an inverted amount range | a typo creates a stratum nobody reports on |
| SV8 | a correlation factor with fewer than two distinct members | a factor with one member is a typo, not a correlation |
| SV9 | an absolute or `..` trace path; a trace replay recorded `chosen_only` | a benchmark must run anywhere, and a replay that cannot answer a counterfactual must not look like it can |
| SV10 | `$ENV`, `${`, `file://` anywhere in the document | a run that reads the environment is not a run |
| SV0 | a resolved hash that differs from the committed golden | "the world changed" must be a line in a pull request |

Plus the stream vectors (§3.1). Four negative fixtures exist, one per family of mistake, each rejected for the reason it was written to demonstrate — and a fixture that starts passing means a check was weakened, which is the same discipline `constraints/check.py` already runs.

## 5. Replay and audit: two features that need different records (consideration 4)

The ticket asks whether "replay and get reproducible numbers" means replaying recorded real events, purely synthetic scenarios, or both. **Both — and they are different features with different record contracts, which is the part the spec's phrase hides.**

### 5.1 Re-running: reproducibility

Given `(scenario_hash, seed, model_version, harness_version)`, a run reproduces exactly. That is [M2]'s digest, and it needs nothing but the document.

### 5.2 Replaying a recording: counterfactual replay

A trace source (`source.kind: trace`) supplies **contexts and attempt keys** from a recording — #13's Parquet export — and the outcomes come from the same key-derived streams a synthetic run uses. That is what makes a counterfactual on a real event *reproducible* rather than merely plausible: "what would delta have done on the transactions alpha took last Tuesday" is a well-defined question with one answer, not a resample.

Two measurements decide the record contract ([M8], n = 20,000):

**Test 1 — what a trace must carry.** Record a `chosen_only` trace under one policy (21,181 rows), then reconstruct the counterfactual for all six arms (120,000 answers):

| The row carries… | Counterfactual answers that differ from the truth |
| --- | --- |
| `(scenario_hash, seed, model_version, harness_version, seq, attempt, acquirer)` | **0 (0.000%)** |
| the same, minus the seed | **35,043 (29.203%)** |

A trace without its seed is not replayable; it is merely historical. So the row contract is fixed here and handed to #13: **every trace row carries the world's identity, not just the event.**

**Test 2 — what a trace must record.** If `bin_class` is not among the recorded context fields, a replay has to model it, and the per-stratum estimates — the ones #15 reweights — are wrong by:

| Cell | p(cell) | p(marginal) | error |
| --- | --- | --- | --- |
| foxtrot × prepaid | 69.74% | 79.97% | **10.23 pts** |
| charlie × prepaid | 76.22% | 85.29% | 9.06 pts |
| delta × prepaid | 84.75% | 93.03% | 8.28 pts |
| worst of 30 cells / mean | | | **10.23 / 3.06 pts** |

An unrecorded field is not a missing column; it is estimate error concentrated in exactly the strata the router segments on. So **the harness records the full context vector by default**, and `recording.context_fields` exists to make dropping one an explicit, reviewable decision rather than an accident of what the engine happened to log.

**Test 3 — the recording mode is a scenario field.** `chosen_only` is what production can record; `all_arms` is what only a simulation can record, and it is what #15's counterfactual needs. They differ by 6.64× in attempts and ~3× in wall time ([M7]b), so it cannot be a global default: a nightly benchmark run wants `all_arms`, a 10M-transaction soak test does not. SV9 requires a *trace* source to be `all_arms`, because replaying reality through a `chosen_only` recorder cannot answer a counterfactual — and a scenario that looks like it can is a trap for whoever reads the table. Where `all_arms` is unavailable, the fallback is ADR-0002/0003's logged propensity over the eligible set, and IPS rather than direct replay; that is #15's choice, and this ADR only guarantees the harness can produce both.

### 5.3 What replay is not

Replaying the *world* is not replaying the *engine*. Re-running a recorded context stream through a different policy is a counterfactual experiment. Reconstructing a past decision from its audit record — both hashes, the eligible set, the pass trace, the waiver — is ADR-0004 §6's job and needs no harness at all. The two are often conflated under "replay"; they share a format and nothing else.

**Parquet:** the scenario schema names `parquet` and `jsonl` as trace formats. The spike reads neither, because `pyarrow` is not installable here and a spike with a dependency is a spike that does not run in CI; the `replay-trace-v1` document is therefore the *shape* of the contract with #13, and its `content_hash` is a placeholder that SV3 does not yet verify against a real file. That gap is stated rather than papered over: #13 owns the writer, and the first real trace replay is the test that closes it.

## 6. Speed: a target with three numbers (consideration 6)

The ticket proposes "1M transactions in < 30s". Accepted, with the unit corrected: **the budget that matters is per attempt**, because an attempt is what the engine pays for and what the harness has to produce, and because ADR-0001's engine budget (≤ 20 µs p99 in-engine CPU per decision) is per decision. A harness slower than the engine measures itself.

Measured ([M7], 2 vCPU, no perf isolation; the model-only row is the best of 3, the rest are single runs):

| | CPython | at ADR-0001's 25–30× native band |
| --- | --- | --- |
| one attempt, model only | 7.0–7.9 µs | 0.23–0.32 µs |
| — of which the derived PRNG (3 draws) | 1.7 µs (21–25%) | — |
| — of which the latency sampler (Φ⁻¹) | 1.7–3.5 µs (24–44%) | — |
| full loop, `chosen_only` (1.06 attempts/txn) | 19.4–22.6 µs/attempt | — |
| full loop, `all_arms` (7.06 attempts/txn) | 9.7–11.2 µs/attempt | — |
| `all_arms` + trace rows formatted | +6–13% on top | — |
| **the T1 run: 1,000,000 transactions, 7.06M attempts** | **64–79 s** | **2.1–3.2 s** |

Four runs of the same command on this box gave 64.2, 66.2, 67.6 and 78.9 s for the T1 row,
which is the honest spread for 2 vCPU with no perf isolation and no pinned frequency. The
rows above are bands for that reason; the *ratios* — attempts per transaction, the share of
an attempt that is randomness, the cost of recording — are stable to a few percent and are
the part the design rests on.

Targets, stated so they can be missed:

- **T1** — 1,000,000 transactions, 6-acquirer fleet, `all_arms` recording, **≤ 30 s on one core** of a CI runner (≥ 33k txn/s, ≥ 235k attempts/s). Met with 9–14× headroom on the projection, before sharding.
- **T2** — 10,000,000 transactions over 8 shards **≤ 60 s**. "Linear in shards" is the claim, and [M2]'s 97-shard row is what makes it testable rather than hopeful: shard boundaries cannot change the answer, so a sharded run needs no reconciliation logic and no locking.
- **T3** — **≤ 2 µs of harness per attempt** in Go, so a two-attempt transaction costs ≤ 4 µs of harness against the engine's 20 µs decision budget. Above that, the benchmark is timing the harness, not the router.

Why 1M and not 100k: a benchmark suite is scenarios × seeds × policies. At 20 × 3 × 6 that is 360 runs of 1M transactions — 3 core-hours at T1, i.e. a nightly job rather than a research project. At 100k transactions the per-arm cell counts get thin enough that the estimate-quality column #17 must publish next to margin (ADR-0002, ADR-0003) is dominated by sampling noise rather than by the policy difference being measured.

Where the time actually goes, and the warning that comes with it: the counterfactual pass multiplies attempts by 6.64× but roughly *halves* µs/attempt (22.6 → 11.2), because the per-transaction fixed cost — arrival draw, context draw, policy call — is amortised over six arms instead of one. So `all_arms` is much cheaper per unit of information than it looks, and the expensive thing is **recording**: formatting trace rows already costs 6–13% of the model in CPython, and that is with the rows thrown away rather than written. If the Go implementation misses T1, the first suspect is the trace writer, not the model — which is ADR-0001 R1 (no synchronous I/O on the decision path) applied to the harness, and #13's buffered writer staying off the model's critical path.

## What this binds on later tickets

- **#12 (module layout).** `acquirer.Client` as specified in §1, in its own package, with `Request`/`Response` as value types. `internal/sim/` implements it and exports `Oracle` from a package the engine does not import. `Clock` is an interface with `NowMS() int64` and `AdvanceTo(int64)`; there is no `time.Now()` below the API boundary, and the CI gate that enforces it is the poisoned-run test in [M2], ported to Go (`TestHarnessReadsNoAmbientState`).
- **#13 (state store).** The trace row contract of §5.2: `(scenario_hash, seed, model_version, harness_version, txn_seq, attempt, acquirer, outcome, code, decline_class, latency_ms, settled_at)` plus the **full context vector** by default. Parquet export carries `source.trace.content_hash` so a replay can be pinned to a file. The late-settlement event needs a durable scheduled-event queue keyed by virtual time, not wall time.
- **#7 (arm granularity).** The harness emits the context fields that make an arm; §2.4's arm × segment interaction (charlie better on corporate, foxtrot 9 pts worse on prepaid) is the input the granularity decision needs, and it only exists if `bin_class_multiplier` is per acquirer. The forced-exploration budget ADR-0003 requires can be exercised against `gradual_overload`, which is the case where an excluded arm's estimate goes stale.
- **#8 (drift detection).** Three degradation shapes to be graded on — `gradual_overload` over 90 minutes, `outage` + exponential recovery with a stated residual, and `fleet_shock` — and the honest comparison is against the step-recovery version of the same scenario, because that is the version where a naive detector looks free. Detection latency, false-positive rate under the AR(1) factor, and re-discovery time are all measurable from the same document.
- **#10 (idempotency).** §2.3's three failure modes are the test matrix, and `late_settlement` is the only way to produce a late authorization at all. A harness that cannot emit one cannot test the double-charge bug; 24.4% of timeouts settling late, 1,178 exposures in 20,000 transactions, is the volume the lease logic has to be right about.
- **#11 (3DS friction).** `frictionless_rate`, `challenge_abandon_rate` and `liability_shift_uplift` are independently settable, and §2.5's selection effect is the warning: the challenged branch's conditional auth rate comes out *below* the frictionless one despite a 1.055 uplift, so an estimate read off an aggregate is an estimate of the wrong quantity.
- **#15 (OPE).** `recording.mode: all_arms` gives the exact counterfactual; `chosen_only` plus logged propensities gives IPS. Both are producible from the same scenario, and the harness's `Oracle` supplies the ground-truth propensity for validating the estimator itself.
- **#16 (dashboard).** The same scenario document drives the demo, so the dashboard is never showing a world the benchmarks did not run in. `events[]` is a timeline the UI can render directly.
- **#17 (benchmarks).** Every reported number cites `<scenario-id>@sha256:<12>`; the committed scenario set is the benchmark's fixture list; T1–T3 are CI gates; and the determinism digest is a regression fixture — a Go implementation that produces a different digest for `baseline-steady-v1@16661ded…` at a given n is wrong, and the correct answer is committed.

## Consequences and design rules that follow

**Positive.** A benchmark number becomes citable ("`black-friday-degraded-v1@7e939d9d…`, seed 20260915, 1M transactions") rather than attributable to a test file. A new scenario is a 50-line overlay plus a re-pin, so the scenario set can grow with the questions instead of being frozen at whatever the first author imagined. The counterfactual is exact and free rather than resampled, which is what makes #15's estimator checkable against ground truth. Sharding is safe by construction, so throughput scales with cores and not with coordination. And the harness is a usable local development backend, which is the difference between a contributor's first evening and their first week.

**Accepted costs, named.**

1. **A synthetic world is not the real one.** Every rate in the fixture is invented to span a plausible range; the decline families are calibrated to published bands and the 3DS funnel deliberately is not (§2.5). A number from this harness is a property of the scenario, and the ADR house rule about synthetic magnitudes applies with full force. The mitigation is not better guessing, it is that the parameters are visible, versioned and diffable.
2. **A closed model cannot surprise you.** `fleet-v1` has no issuer-level entity, no network-token effect, no chargeback lag, no acquirer-side velocity throttling. Real fleets have all four. The answer is a version bump with a migration, not an expression language in the scenario — the same trade ADR-0004 made for constraints.
3. **Two golden files make editing a scenario a two-step operation.** Accepted: the friction is the point, and `--pin` is one command.
4. **`all_arms` recording costs 6.64× the attempts.** Accepted for benchmarks (it is the cheap way to buy exact counterfactuals) and refused for soak tests, which is why it is a scenario field.
5. **Index-addressed draws waste nothing but look wasteful.** Every arm's outcome is computed whether or not the arm is called; the draws are cheap and the alternative (lazily deriving only what is asked) reintroduces order dependence.
6. **The Python reference and the Go implementation can drift.** Mitigated by the golden vectors and the digest fixture, and accepted as residual: the vectors pin the stream, the digest pins the model's use of it, and neither pins the Go code's structure.

**Design rules (continuing ADR-0004's numbering):**

- **R27** — the synthetic fleet implements `acquirer.Client` and nothing else. No simulator-only field on `Request` or `Response`, no `Reset`/`Inject*`/`Set*` on the interface, no simulator hints in `ctx`. Fault injection is an `events[]` entry with an `at_s`.
- **R28** — ground truth lives behind `Oracle`, in a package the engine does not import. A policy that can read the truth is not a policy under test.
- **R29** — the world's randomness is key-derived and index-addressed per §3.1, with the domain tags and index assignment fixed by `model_version`. A shared, call-order-consumed PRNG is forbidden in the harness path: [M3] measures what it costs.
- **R30** — the world's seed and the policy's seed are separate, and the policy's run seed is recorded in the trace header. Changing a policy must not change the world.
- **R31** — no harness path reads wall time, the global RNG, the environment or the locale. Time is `Clock`, injected. The poisoned-run test is the gate, not a lint rule.
- **R32** — a scenario is a JSON document, resolved before hashing, cited as `id@sha256:<12>`. A builder may generate documents; it may not replace them. Canonical form is the constraint layer's.
- **R33** — a scenario pins its catalog by hash and may only name acquirers in it. Capability and price are catalog facts; behaviour is scenario data.
- **R34** — a scenario invents frequencies, never codes and never retryability. Decline codes come from the scheme catalog (SV4).
- **R35** — a recovery event states its residual. A degradation without a recovery curve, or a curve without a stated residual, is a step recovery, and a step recovery makes a benchmark optimistic about early return.
- **R36** — the harness records the full context vector by default. Dropping a field is an explicit `recording.context_fields` entry with a reviewer, because [M8] prices an unrecorded field at up to 10.2 pts of stratum error.
- **R37** — every trace row carries the world's identity (`scenario_hash`, `seed`, `model_version`, `harness_version`). A row without its seed is history, not an experiment.
- **R38** — `model_version` bumps invalidate every committed benchmark number, and the bump is the migration. Latency shape, draw-index order, outcome semantics and stream algorithm are all inside it.

## Reopen triggers

1. **The Go implementation misses T3** (≤ 2 µs per attempt) and profiling shows the cost is in the model rather than the trace writer. Then the model is too expensive to be a benchmark instrument, and the fix is a cheaper latency sampler or a precomputed quantile table — not a smaller world.
2. **A determinism divergence appears between the Go implementation and the committed digest** for the same scenario hash, seed and n, after the golden vectors pass. That means the vectors under-specify the stream (floating-point order, Φ⁻¹ implementation, tie-breaking), and the contract needs tightening before any benchmark number is trusted.
3. **A benchmark conclusion flips when the latency tail index moves inside its plausible range** (ξ ∈ [0.15, 0.35]) or when the soft/hard decline split moves ±5 pts. Then the conclusion is a property of the fixture, and the scenario set needs a sensitivity sweep before the claim is published — this is the harness equivalent of ADR-0004's permutation test.
4. **#7 grows an issuer or network-token dimension into the arm space.** `bin_class_multiplier` is a projection of an issuer mix onto an arm (§2.4); an arm that *is* an issuer needs an issuer entity in the model, which is a `fleet-v2`.
5. **Real traces become available and the synthetic and replayed fleets disagree by more than the sampling error** on the same contexts. Then the fixture is miscalibrated in a way the published bands did not catch, and calibration against measured data replaces calibration against cited ranges — the good kind of reopen.
6. **A scenario needs something the grammar cannot express** — three requests in a quarter, or one that cannot be expressed as a parameter block at all. Then the model grows a version, with the same migration discipline ADR-0004 applies to rule heads. Not before: an expression language in a scenario is an unauditable world.

## Alternatives considered

- **Per-test mocks and fixtures (the status quo, and the default in most Go projects).** The steelman is strong: mocks are local, obvious, and free. Rejected because a mock is a world with no identity — it cannot be hashed, cited, diffed or reused, so every experiment invents its own and the inventions drift. `spikes/0004` is the exhibit: it needed a latency model, hand-rolled one, and hand-rolled a bounded one whose maximum draw is its declared p99 ([M4]). Nobody chose that; it is what happens when the harness does not own the model.
- **Recorded-response replay only (VCR/cassettes against real acquirers).** Realistic by construction and useless here for three reasons: it cannot answer a counterfactual (the arm you did not call has no recorded response), it cannot be graded against ground truth (there is none), and it is not reproducible under a changed policy — the recording is a function of the policy that made it. Retained as the `source.kind: trace` *context* provider, which is the part of a recording that is genuinely valuable (§5.2).
- **A Python harness, permanently.** It would run today and it is what the spike is. Rejected for the production harness by ADR-0001: the engine is Go, a Python harness would be a second toolchain and a serialization boundary between the thing under test and the thing testing it, and `Decide()` must not allocate or cross a runtime. The spike stays Python because a spike with a Go toolchain requirement is a spike nobody runs.
- **YAML scenarios.** Rejected in §4.1 on canonicalisation: a content-addressed artifact needs one byte string per world.
- **A builder API as the primary form.** Rejected in §4.1: an in-process world cannot be cited. Kept as a generator that emits documents.
- **A discrete-event simulator with real concurrency and a real (scaled) clock.** The steelman is fidelity: real goroutines, real queueing, real contention — the harness would exercise the engine's concurrency rather than assume it. Rejected as the *primary* mode because it trades away the two properties this ADR exists to buy (determinism under sharding, 1M transactions in seconds) and because contention is #17's `go test -bench` job, not the world's. Retained as a possible `clock.mode: realtime` for soak tests, unlabelled and off the critical path.
- **A seeded shared PRNG, with careful call discipline.** "Just always query the arms in the same order." Rejected on measurement: [M3] shows 26.5% of answers move when an acquirer is *added*, which no amount of call-order discipline prevents, and 0.345 pts of movement in the reported per-arm statistic on the same seed and scenario. Discipline is a rule reviewers must remember; index addressing is a property the code has.
- **A learned generative model of acquirer behaviour** (fit a model to real traces, sample from it). The right answer once real traces exist, and the wrong answer now: it needs data we do not have, it introduces a second determinism argument (the training run), and it cannot be audited by reading it. The parameter blocks in §2 are the shape a fitted model would have to fill, which is the point of designing them first.
- **Modelling issuers as first-class entities behind the acquirers.** More realistic — approval is an issuer decision and the acquirer is a route. Rejected for `fleet-v1` because the engine's arm is `(processor, flow)` (ADR-0003 R18) and it never chooses an issuer, so an issuer entity would add state the decision cannot act on. The BIN-class multipliers are the projection of the issuer mix onto the arm, which is the right granularity while the arm space is what it is. Reopen trigger 4 is the tripwire.
- **Making `all_arms` the only recording mode.** Simpler, and it would remove a scenario field. Rejected on cost: 6.64× the attempts and ~3× the wall time, and a 10M-transaction soak test does not need exact counterfactuals. A field with a documented default beats a mode nobody can afford.

## Appendix: reproduction

```bash
python3 simulator/scenarios/check.py                  # 9/9: 4 scenarios accepted, 4 negative
                                                      # fixtures rejected for their stated
                                                      # reason, 26 stream vectors reproduced
python3 simulator/scenarios/check.py --json           # the same verdict for CI
python3 simulator/scenarios/check.py --resolve black-friday-degraded-v1
python3 simulator/scenarios/check.py --hash baseline-steady-v1

python3 spikes/0006-simulation-harness/harness.py 20000 --full=1000000
                                                      # ~3 min; RESULTS.md is this output
python3 spikes/0006-simulation-harness/harness.py --section=M3   # the PRNG comparison alone
python3 spikes/0006-simulation-harness/harness.py --stream-hash 2000
                                                      # the determinism digest at n/10, which
                                                      # is what [M2]'s subprocess rows compare
```

Deterministic: fixed seeds, stdlib only, no network. `M1`–`M6` and `M8` are measurements; the Go projections in `M3` test 4 and `M7`(c)/(d) are a labelled model using ADR-0001 §1's 25–30× band, and the sandbox has 2 vCPU with no perf isolation, so treat the microseconds as a band and the ratios as the finding.

Primary sources for the calibration bands quoted in §2 — cited as ranges, not as measurements of this fixture: [decline-code category shares](https://paymentsandrisk.com/docs/reference/decline-codes/) · [code 51 share of declines (Mastercard/Ethoca via industry reporting)](https://beastinsights.com/blog/credit-card-decline-code) · [soft vs hard decline taxonomy](https://icetree.co.uk/knowledge-hub/card-decline-codes/) · [3DS2 frictionless and challenge rates](https://www.keytransact.com/guides/3d-secure) · [frictionless 80–85%, challenge abandonment 10–15%](https://www.pci-proxy.com/blog-posts/3ds-frictionless-vs-challenge-flow) · [3DS1 vs 3DS2 approval and abandonment](https://beastinsights.com/blog/3ds1-vs-3ds2) · [Go `math/rand/v2` PCG and ChaCha8 sources](https://github.com/golang/go/issues/61716) · [splitmix64 finaliser](https://xorshift.di.unimi.it/splitmix64.c)
