# ADR-0006: Thompson sampling implementation — arm space, update protocol, hot path, persistence

- **Status**: Accepted
- **Date**: 2026-09-15
- **Resolves**: [#7 Thompson sampling: posterior update protocol, arm space, forced exploration](https://github.com/Sehaan-1/switchback/issues/7)
- **Depends on**: [ADR-0001](0001-engine-language-go.md) (Go core, no-alloc `Decide()`, fixed-layout slices, atomic-pointer-swap artifacts), [ADR-0002](0002-reward-function.md) (the reward the posterior estimates, and the priced-ambiguity split this ADR inherits), [ADR-0003](0003-bandit-not-static-table.md) (the algorithm family, R15–R18 this ADR implements), [ADR-0004](0004-constraint-layer.md) (filter before sampling), [ADR-0005](0005-simulation-harness.md) (the harness, the scenarios, and the key-derived determinism this spike borrows for the policy)
- **Feeds**: #8 (drift detection: the `te` counter, processor-level aggregation, resets, no standing decay), #11 (offline estimates: the prior artifact format), #12 (`Decide()`'s return contract: chain, draws, propensity), #13 (storage: the WAL-as-trace contract, dedupe, snapshot cadence), #15 (off-policy evaluation: exact offline propensities), #17 (algorithm baselines: margin always paired with estimate quality and coverage)
- **Evidence**: [`spikes/0007-thompson-sampling/`](../../spikes/0007-thompson-sampling/RESULTS.md) — 60,000-transaction evidence run on the committed scenarios via the ADR-0005 harness, `python3 spikes/0007-thompson-sampling/posterior.py 60000` (~30 min, stdlib only, deterministic)

---

## Decision

1. **The arm is `(BIN class × region × SCA × mandate × amount band) × processor`** — categorical keys only (extends R18), fixed-layout preallocated arrays, 32 B/arm of mutable data plus a read-only 16 B/arm prior that swaps with the versioned artifact. Region is the geographic dimension; currency is derived (1:1 in every committed scenario). Band edges are a geometric ladder over the deployment's declared amount range, 2 significant figures, default 6 bands, and the edge vector versions *with the prior artifact* — with no offline table, the band dimension collapses (bare Jeffreys + fine bands starves; measured below).
2. **The label and its updates**: binary end-to-end `P(authorized | attempt, not timeout)` — `authorized → α+1`, decline/abandoned → `β+1`, **transport error → `β+1` and a separate `te` counter**, timeout → timeout counter only (already priced at `λ_to`). Updates are **pure float64 increments**; no standing decay. Ingest dedupes on `(seq, attempt)` — a protocol requirement, not a store option.
3. **The hot path**: exact gamma-ratio Beta draws, **key-addressed** — every draw a pure function of `(policy_seed, seq, arm, purpose)`. No normal/grid/order-statistic approximation in `Decide()`. The logged propensity is the **plug-in estimate** (reuses the decision's own draws), labelled with its method; anything IPS-grade (#15) recomputes exact propensities offline from the logged posteriors.
4. **Concurrency**: arm-index-sharded arrays, **one ingest writer per shard** fed by a bounded queue, readers are lock-free atomic loads; a torn `(α, β)` read is ≤ 1 count for ≤ 1 ingest interval — bounded staleness, not corruption. No mutex, CAS, or seqlock anywhere in the decision path. Ingest batching is bounded (K ≤ ~64) because staleness past that is measurably not free.
5. **Cold start and persistence**: a new (or #8-reset) processor enters an explicit **onboarding state**: while in it, with probability η (default 0.05) attempt 0 goes to a uniformly chosen eligible onboarding processor; the state clears at `n_min` (default 1,000) processor-level settled observations. There is **no standing threshold floor and no per-arm floor** (both measured and dominated). The **WAL — the trace itself — is the only writer of learned state**; the posterior is a fold over it, snapshots are checkpoints of that fold, and boot = snapshot + tail replay.

## Context

ADR-0003 chose the family (Beta-Bernoulli TS under ADR-0002's reward, coupled to #8); this ticket decides what the implementation actually *is*: the arm key and its granularity, the prior that seeds it, the update protocol (including how a transport error is labelled and what at-least-once delivery costs), the draw and its budget inside `Decide()`, the concurrency protocol behind ADR-0001 R5's shape, the forced-exploration story ADR-0003 [F3] promised, and the persistence contract #13 builds on. Every number below is from the 60k run of [`posterior.py`](../../spikes/0007-thompson-sampling/posterior.py) on the committed scenarios (`baseline-steady-v1` steady world, `outage-recovery-v1` event world, and this spike's gated `long-refused-v1` extension for the transport-error experiment), through the ADR-0005 harness — the same world generator the constraint spike used, so the numbers are comparable across ADRs. Magnitudes belong to those scenarios; **the orderings and mechanisms are the claim.**

Two methodology commitments that shape everything below, both inherited from the spike conventions: every margin number ships next to an estimate-quality number (MAE against the world's true per-arm rate, oracle-probed — the pairing #17 inherits), and every "cheap approximation wins margin" claim is checked against **coverage** (arms touched), because an approximation that stops exploring is ε-greedy with ε = 0 and *looks* profitable while it starves.

## 1. The arm space: which keys pay, and how much does the space cost? (consideration 1)

The ladder, same policy (TS, Jeffreys), `baseline-steady-v1` at n = 60,000:

| arm key | arms | med obs | margin c/1k | vs no-bands | MAE pts |
| --- | --- | --- | --- | --- | --- |
| proc only | 6 | 4,586 | 27,454.0 | +195 | 4.10 |
| +bin | 36 | 1,170 | 27,455.0 | +196 | 3.57 |
| +bin+region | 180 | 283 | 27,324.0 | +65 | 3.83 |
| +bin+region+sca | 360 | 199 | 27,056.0 | −203 | 3.72 |
| +…+mandate (no bands) | 720 | 28 | 27,259.5 | 0 | 3.96 |
| + 2 bands | 1,440 | 15 | 26,858.7 | −401 | 4.35 |
| + 6 bands (default) | 4,320 | 8 | 26,813.3 | −446 | 4.86 |
| + 8 bands | 5,760 | 7 | 26,562.0 | −697 | 5.09 |

Readings, in the order the table forces them:

- **`proc` and `bin` pay for themselves** — bin class is the biggest context effect in the fleet (ADR-0003 measured 9–22 pts spread), and one key level buys it. `region` is roughly free (+65/−65 across runs). `sca` costs −203 under Jeffreys — not because SCA doesn't matter (it changes the challenge probability, hence the effective rate) but because at 360 arms the Jeffreys posterior is already thin. `mandate` is kept for a reason the fixture cannot price: it is a *compliance* dimension (ADR-0004's vocabulary), and the marginal −0 margin at n = 60k is within run-to-run noise.
- **Bands are where Jeffreys starves.** Monotonically worse from no-bands to 8: each band level multiplies the space by ~1.4 and the median arm never leaves single-digit observations. This is *the prior's problem, not the key's* — the same sweep with the offline prior (m = 100, hierarchical, P2's construction):

| bands (informative prior) | margin c/1k | MAE pts |
| --- | --- | --- |
| none | 27,401.7 | 3.74 |
| 2 | 27,344.5 | 4.01 |
| 4 | 27,356.4 | 4.19 |
| 6 (default) | 27,376.2 | 4.25 |
| 8 | 27,293.6 | 4.47 |

  With a prior, 6 bands costs **−26 c/1k** — noise. This is why the band edges version with the *prior artifact* (R40): the fine space and the offline table are a package, and the fallback configuration is coarse, not fine.
- **Issuer granularity (the ticket's "6-digit BIN → IIN prefix" question), steelmanned.** On a spike-local world extension (`issuer-overlay-v1`, *not* a committed scenario) with a real lognormal(σ=0.06) issuer-group auth effect present, keying on issuer (51,840 arms, 61% never observed) loses to keying on region: **25,532.9 vs 27,371.7 c/1k, MAE 9.48 vs 6.48**. The effect is real and the arm still starves at this volume — the honest statement of ADR-0003's trigger 2, now measured. Issier→fleet-v2, with the shrink hierarchy doing the work (reopen trigger 3).

The scaling check (default and bin-only keys re-run at 2n = 120k) shows the band optimum does not rescue itself with volume on a week-scale window: bin-only stays ahead. The 6-band default is a *production-worlds* bet (amount-dependent 3DS exemptions, risk tranches — effects `baseline-steady-v1` deliberately understates), priced at ≤ ~26 c/1k with the prior, and **revisable without a cold start** (§7: re-bucketing is a re-fold of the WAL). That last property is what makes shipping a slightly-too-fine default rational: the cost is bounded and known, and the fix is cheap by construction.

## 2. The prior: Jeffreys, uniform, or offline-seeded — and how strong? (consideration 2)

The informative prior is seeded the way a real one arrives: a 2,000-transaction uniform-exploration prefix *outside the run window*, aggregated hierarchically (`proc × bin × region → proc × bin → proc → fleet`, deepest level with ≥ 4 settled observations), `m` = pseudo-attempt strength:

| prior | margin c/1k | cold c/1k | MAE pts |
| --- | --- | --- | --- |
| Jeffreys Beta(0.5, 0.5) | 26,813.3 | 26,121.9 | 4.86 |
| uniform Beta(1,1) | 26,789.6 | 25,833.8 | 5.01 |
| informative m=10 | 27,257.5 | 27,427.1 | 4.09 |
| informative m=30 | 27,341.7 | 27,637.8 | 4.08 |
| **informative m=100** | **27,376.2** | 27,661.0 | 4.25 |
| informative m=300 | 27,274.6 | 27,637.9 | 4.78 |
| informative m=1000 | 27,191.5 | 27,697.9 | 5.44 |
| m=100, rate ×0.90 | 27,344.9 | 27,647.7 | 5.46 |
| m=100, rate ×1.08 | 27,347.4 | 27,667.4 | 4.46 |
| m=100, foxtrot ×0.80 only | 27,322.8 | 27,409.0 | 4.11 |

- Jeffreys vs uniform is within noise (ADR-0003's prediction, held).
- **The prior's value is concentrated exactly where the ticket's cold-start worry lives**: the first 10k transactions, +1,539 c/1k over Jeffreys (27,661.0 vs 26,121.9), and the full-run MAE improves 4.86 → 4.25 even on a *stationary* world where there is nothing to be wrong about.
- **m beyond the cold window's own volume buys nothing and starts costing** (m=1000: 27,191.5, MAE 5.44) — the posterior is being held hostage to the prefix. m ~ 100 (the order of an arm's cold-window volume) is the knee.
- **Miscalibration is asymmetric**: rate ×0.90 (pessimistic) costs more MAE than ×1.08 (optimistic) at equal multiplicative error — 5.46 vs 4.46 — because TS under-explores arms it believes are confidently bad (ADR-0003 [F3], now measured at the prior level). This is why m is a **knob**, not a constant, and why #11's refresh matters more than #11's precision.

## 3. The update protocol: increments vs decay, the transport-error label, at-least-once delivery (consideration 3)

**(a) Standing decay.** Exponential decay (half-life H, applied to data counts so decay shrinks toward the prior, not toward 0.5) vs pure increments, steady world and event world at 2n:

| world | update | margin c/1k | mean settled/arm |
| --- | --- | --- | --- |
| baseline-steady-v1 | increments | 26,813.3 | 97 |
| baseline-steady-v1 | decay H=500 | 26,815.7 | 61 |
| outage-recovery-v1 | increments | 26,793.0 | 178 |
| outage-recovery-v1 | decay H=500 | 26,805.5 | 88 |
| outage-recovery-v1 | decay H=5000 | 26,781.0 | 157 |

Margin-flat on both worlds — the estimator is context-averaging-limited, not sample-limited — but decay **permanently caps the effective sample size** (H=500 halves mean settled/arm even on the steady world) and buys no adaptation the event world can measure (#8's resets own that job). Decay is a #8 tool, not a default (R43).

**(b) The transport-error label.** `long-refused-v1` (this spike's gated scenario document): foxtrot — cheapest, lowest-auth, slowest — refuses every connection for six hours at t = 0.286, then recovers in a step. Three labelings, bin×region arms:

| te label | share 1st half | share post-recovery | margin c/1k | outage-window c/1k | MAE pts |
| --- | --- | --- | --- | --- | --- |
| **beta** (β+1, te counter) | 8.74% | 6.70% | **27,034.7** | **21,522.2** | 3.94 |
| timeout (ambiguity) | 8.91% | 7.40% | 27,000.3 | 21,273.3 | 3.83 |
| exclude (invisible) | 9.77% | 9.99% | 26,948.6 | 20,978.3 | 3.83 |

`te → β` wins the outage window by **+544 c/1k** over exclude and +249 over timeout-laundering — a refusal is a settled failure, the estimand says so, and the router stops paying for a dead processor *during* the event. The cost is visible and honest: **re-entry hysteresis** — post-recovery share 6.70% vs 9.99% for the label that never learned anything — the outage's β-mass has to be un-learned. That hysteresis, and the reaction speed, are #8's problem statement (below). The separate `te` counter exists precisely so #8 can see "this arm's failures are connection-shaped" without re-deriving it.

At the **default fine arm space** the same event is diluted (share moves 9.95→9.41 under exclude vs 8.91→7.38 under beta): six hours of refusals spread over 4,320 arms moves each arm by a few counts. The margin gap shrinks from +86 to +38 c/1k. **The dilution is a finding, not a bug**: #8 must aggregate at the processor level to see transport-shaped events in a fine space; the posterior's job is the steady-state rate, which the fine space does estimate.

**(c) At-least-once delivery.** 1% of attempts duplicated during the run, then a Kafka-style rebalance that redelivers the last 10% of the log:

| dedupe | dupes dropped | attempts | counts learned | reconciles | margin c/1k | final MAE pts |
| --- | --- | --- | --- | --- | --- | --- |
| on | 5,682 | 51,693 | 51,693 | **PASS** | 26,813.3 | 3.74 |
| off | 0 | 51,689 | 57,422 | **FAIL (+11.1%)** | 26,807.6 | 3.81 |

On a stationary world the mean barely moves (the duplicated tail is unbiased) — the damage is **contractual**: state ≠ attempts (+11% volume), which silently breaks every downstream volume-based invariant (#8's detectors, #11's refresh denominators, capacity math). Dedupe on `(seq, attempt)` is therefore a property of the ingest *protocol* (R44), inherited by #13 as a requirement, not an option. (Methodology note, recorded because it cost a rerun: the first 60k pass of this table shared one world instance across the two drives and reported a −12,000 c/1k "dedupe cost" — a different outcome realization, not an effect. The harness's outcome stream continues across drives on a shared instance; every drive in the committed run gets a fresh world.)

**(d) Representation.** float64 counters: integer-exact to 2^53 (checked: 10⁶ increments from 0.0 land exactly; 5e9+1 is exact), and #8's shrink is a multiply — one representation serves increments, decay, and event-driven resets alike (R43).

## 4. The hot path: the draw, the discipline, the propensity (consideration 4)

**(a) Exactness first.** 40,000 draws per sampler, χ² over 32 equal-probability bins under the exact Beta CDF (df=31, reject at p<0.01 ≈ χ²>52.2):

| target | gamma-ratio (ours) | normal approx | grid 64 | order-stat |
| --- | --- | --- | --- | --- |
| Beta(0.5,0.5) | **PASS** (24.7) | FAIL (18,281; TV 28.7%) | PASS | FAIL (TV 96.9% — invalid there) |
| Beta(2,17) | **PASS** (27.1) | FAIL (8,941.5; TV 16.6%) | PASS | PASS |
| Beta(50,50) | **PASS** | PASS | PASS | PASS |
| Beta(300,1200) | **PASS** | PASS (49.8) | PASS | PASS |

The normal approximation fails exactly where cold arms live (skewed, small-count Betas).

**(b) Cost.** CPython µs/draw, with the Go projection on ADR-0001's 10/30/100× interpreter band: gamma-ratio **7.23** (→ 0.07–0.7 µs in Go), grid-prebuilt 3.16, normal 4.13, order-statistic 15.74. Against ADR-0001's ≤ 20 µs p99 in-engine budget at k ≈ 5 eligible arms, the exact draw is comfortably inside the budget in Go — there is nothing to save by approximating, and (a) says approximating is wrong anyway.

**(c) The draw discipline.** Every policy draw is a pure function of `(policy_seed, seq, arm, purpose)` — ADR-0005's key-derived determinism, applied to the policy. Permutation and repeat identity verified (PASS); the end-to-end consequence is P7's replay equality. The discipline costs ~5.8 µs/draw in CPython (one stream key per draw); in Go that is one FNV + splitmix64 chain, a model-estimated 5–15 ns (labelled a model, not a measurement).

**(d) Approximations at the *policy* level** — the check that matters, because a bad sampler can pass (a) in isolation and still distort a policy, at n = 20,000 (capped; the grid variant's table rebuilds — 33,663 of them — make it the expensive one, which is itself a finding):

| draw | margin c/1k | arms touched |
| --- | --- | --- |
| exact | 26,158.1 | 16,466 |
| normal | 26,240.9 | 16,107 |
| grid | 26,018.9 | 16,531 |

The normal approximation "wins" +83 c/1k **by touching 359 fewer arms** — it is ε-greedy with ε=0 wearing a margin costume. This is the coverage lesson #17 inherits: a margin number without an arms-touched number is not evidence.

**(e) The propensity** (TS propensity of arm i = P(i's sampled score is best)), k = 5, against a 200k-round MC reference over 24 recorded decision states:

| method | µs/decision (CPython) | chosen-arm \|Δp\| pts | any-arm max \|Δp\| pts |
| --- | --- | --- | --- |
| MC R=8 | 450.2 | 10.34 | 41.44 |
| MC R=64 | 3,633.2 | 2.70 | 8.95 |
| **plug-in** | **139.5** | 6.25 | 74.14 |

The plug-in (reuses the decision's own draws: `p_i = Π_j F_j(θ_i)`, k−1 CDF evals per arm, no extra sampling) is the cheapest by 3–26× and is per-row unbiased for the *chosen* arm — but **no cheap estimator is IPS-grade** (`E[1/p̂] ≠ 1/p`), and the any-arm column (74 pts for plug-in — cold arms that never win) is what forces the split: log the plug-in value labelled with its method (dashboards, alerting, audit trail), and **#15 recomputes exact propensities offline from the logged posteriors** (R46).

## 5. Concurrency: the sharded single-writer protocol, priced (consideration 5)

ADR-0001 R5 fixed the shape (shard by index, one writer per shard, lock-free reads); this section prices it and stress-tests the protocol claims with real threads (4 writers, 1 dispatcher, 2 readers, 100,000 ops from the run's WAL): **0 lost ops, per-arm FIFO order preserved, fold(WAL) == writers' final state bit-exact (PASS), 0 mid-pair writes observed in 387,500 reader samples**. CPython's GIL means the *timing* is not the claim; the *protocol* is.

What staleness costs the decision (posterior refreshed every K outcomes instead of every outcome):

| ingest batch K | auth% | margin c/1k | MAE pts |
| --- | --- | --- | --- |
| 1 | 69.30 | 26,813.3 | 4.86 |
| 64 | 69.15 | 26,696.8 | 4.83 |
| 1,024 | 68.38 | 26,667.9 | 5.23 |
| 8,192 | 60.24 | 24,690.6 | 9.75 |

Batches in the tens are free; batches in the thousands eat the cold window (the whole start of the run runs on the prior); batches in the ten-thousands are a different router. At ADR-0001's 5,000 decisions/s × 1.3 attempts over 8 shards, a 5 ms flush interval is K ≈ 4 — the design lands two orders of magnitude below the cliff, which is the right side to be on (R48's bound exists for the failure modes, not the steady state).

The Go contention model (constants labelled as a *model*): readers do 2 atomic loads per eligible arm (~12 ns at k=5), wait-free; a writer is ~0.005% busy at 810 events/s/shard; a per-arm mutex would put a lock in the read path and a global lock would serialize 6,500 updates/s through a 40 ns critical section — both "work" until they don't, which is why R48 rejects them by rule rather than by measurement.

## 6. Cold start: a processor added mid-run, and the exploration floor (consideration 6)

Charlie (the EEA/UK margin workhorse, eligible for 56% of traffic) withheld until t = 0.7, then onboarded cold; the world supported it all along, so always-available at the *same* prior is the ceiling and never-available prices the processor:

| prior | floor | share post | settled obs | margin c/1k |
| --- | --- | --- | --- | --- |
| always available (ceiling) | — | 51.89% | 25,528 | 27,376.2 |
| never available (its value) | — | 0% | 0 | 22,583.0 |
| jeffreys | none | 43.30% | 6,361 | 23,292.3 |
| calibrated | none | 47.26% | 6,978 | 23,904.8 |
| charlie-pessimistic ×0.85 | none | 23.43% | 3,338 | 23,614.5 |
| charlie-pessimistic ×0.60 | none | 12.44% | 1,739 | 23,512.8 |
| charlie-pessimistic ×0.60 | onboard η=0.05 n_min=200 | 12.77% | 1,783 | 23,505.7 |
| charlie-pessimistic ×0.60 | onboard η=0.10 n_min=1000 | 15.05% | 2,125 | 23,539.1 |
| charlie-pessimistic ×0.60 | arm-level η=0.05 | 13.21% | 1,878 | 22,929.5 |
| charlie-pessimistic ×0.60 | standing threshold η=0.05 | 12.78% | 1,785 | 23,395.8 |

- **Bare TS gives a new processor meaningful traffic immediately** — 43% share under Jeffreys, 47% under a calibrated table, with no floor at all. The wide prior *is* the exploration budget; ADR-0003's probability-matching does the onboarding.
- **A confidently wrong prior is the only starvation case**, and even it is bounded here: ×0.60 pessimism drops share 47%→12% but costs only −392 c/1k, because in a competitive fleet the starved processor's traffic goes to near-substitutes (and where there is *no* substitute — UK traffic is charlie-only — eligibility forces the traffic regardless of belief). The residual risk the floor insures against is a processor that is uniquely good on a contested segment; this fleet's flatness cannot exhibit that, which is stated rather than hidden.
- **The floor that ships is the onboarding-state floor** (R49): it exists only between "processor added / #8 reset" and "n_min settled observations", concentrates its entire budget on that processor (share 12.44→15.05, settled 1,739→2,125 at η=0.10/n_min=1,000), and moves full-run margin +26 c/1k — noise — while it does it. The two intuitively appealing alternatives are measured and rejected: a **standing threshold floor** ("any processor under n_min") taxes weak processors forever — echo never reaches 200 settled by legitimate TS demand, so the floor fires eternally at −117 c/1k with zero starvation benefit — and a **per-arm floor** on a fine space is diluted across rare buckets that are cold for *every* processor (−583 c/1k, no rescue). The selection probability of the floor is exactly computable in all variants (a known mixture), so propensities stay honest.
- The onboarding gap against the ceiling (23,904.8 vs 27,376.2) decomposes as ~3,355 from charlie simply being *absent* for 70% of the run (0.7 × its 4,793 value) and only ~116 from re-learning — cold start is a coverage problem, not an estimation problem, once the prior ships.

## 7. Persistence: the WAL is canonical, snapshots are checkpoints (consideration 7)

The outcome event is written **once** and serves as both the trace row and the write-ahead log of the learned state; the posterior is a **fold** over it (`fold(WAL) == live state` bit-exact: PASS). A snapshot at 50% + tail replay reproduces the live run's 23,367 tail **decisions** with 0 mismatches (the key-addressed draws of §4c are what make this true) and the final state bit-exact (PASS). Fold throughput 610,844 ops/s in CPython → 6–61 M ops/s on the Go band, i.e. a 10M-op WAL (a long week at 20 TPS × 1.3 attempts) replays in **0.2–1.6 s** — snapshot cadence bounds *replay time*, not data; a crash loses at most the un-fsynced tail (#13's group-commit knob).

Because every op row carries the raw context (bin, region, SCA, mandate, amount), changing the arm space is a **re-fold of the same log, not a cold start** — measured by collapsing the run to a 4-band space: the re-fold with the shipped prior reaches MAE **4.65** vs **6.03** for a cold start at the same prior on the new space (and 17.07 for a priorless re-fold — the prior ships, again). This is the property that makes R40's band default safe to revisit.

## Payload for dependents

- **#8 (drift)**: per-arm `te` counter (transport-shaped failures are distinguishable in the artifact); aggregate at the **processor level** to see events in a fine space (the dilution finding); resets enter the **onboarding state** (R49) so re-entry has the same bounded floor as entry; shrink = multiply on the same float64 arrays; **no standing decay** — the half-life, if ever shipped, is an #8-controlled response mode.
- **#11 (offline estimates)**: the artifact is per-`(proc × bin × region)` rates + strength m, with the hierarchy `proc×bin×region → proc×bin → proc → fleet` (deepest level ≥ 4 settled) as the fallback when a cell is thin; `α₀ = m·r̂, β₀ = m(1−r̂)`; **the band-edge vector versions with the artifact**; refresh beats precision (the m-knob and the asymmetry row).
- **#12 (Decide contract)**: returns the chain, the draws, and the plug-in propensity, labelled with its method — the same key-addressed values P7 replays.
- **#13 (storage)**: the WAL *is* the trace (one write, group-commit, fsync interval is yours); **dedupe on `(seq, attempt)` is a requirement**; snapshots are fold checkpoints (135 KiB at 4,320 arms × 32 B data + read-only prior swapped with the artifact); boot = snapshot + tail replay; every row carries raw context for re-bucketing; nothing else writes the posterior.
- **#15 (OPE)**: exact propensities are recomputed offline from logged posteriors; the logged plug-in value is dashboard-grade and labelled.
- **#17 (baselines)**: margin always ships with MAE *and* arms-touched; a margin win from an approximation is checked against coverage before it is believed.

## Consequences and design rules that follow

**Positive.** One fixed-layout array per shard, two float64 increments per settled outcome, no locks in the decision path; replay is bit-exact by construction (key-addressed draws + pure fold), which makes the audit trail and the WAL the same artifact; re-bucketing is a re-fold, so the arm-space decisions are revisable at the cost of a replay, not a cold start; the propensity story is honest at hot-path cost and exact offline.

**Accepted costs, named.**
1. The fine arm space is prior-dependent: without #11's table the band dimension collapses (R40) — a fleet decision that depends on an offline artifact shipping.
2. Re-entry hysteresis after a long transport outage is real (share 6.70% vs 9.99% for the label-blind alternative) until #8 lands; accepted because the in-window win (+544 c/1k) is larger than the post-window cost, and the alternative is not learning the outage at all.
3. The floor can only be validated as cheap insurance on a fleet whose margin surface is flat — the case it exists for (uniquely good processor, wrong prior, contested segment) is argued, not measured, because this fleet cannot exhibit it.
4. All magnitudes belong to `baseline-steady-v1` / `outage-recovery-v1` / `long-refused-v1` at n = 60,000 with the committed catalog; orderings and mechanisms are the load-bearing claims.

**Design rules (continuing ADR-0005's numbering):**
- **R39** — the arm key is `(BIN class × region × SCA × mandate × amount band) × processor`, categorical only (extends R18), fixed-layout preallocated; region is the geographic dimension, currency derived from it (a scenario family fact; the WAL records region).
- **R40** — band edges are a geometric ladder over the deployment's declared amount range, 2 significant figures, default 6 bands; the edge vector is part of the versioned prior artifact. With no artifact, the band dimension collapses; widening is a re-fold, never a cold start.
- **R41** — no MCC, attempt-index, or issuer dimensions in v1. Adding any reopens this ADR (triggers 2–3).
- **R42** — the label is binary end-to-end `P(authorized | attempt, not timeout)`: `authorized → α+1`; decline/abandoned → `β+1`; **transport error → `β+1` and `te+1`**; timeout → timeout counter only, priced at `λ_to` (extends ADR-0002). No other labeling ships.
- **R43** — updates are pure float64 increments; no standing decay, no scheduled shrink. Rate adaptation is #8's event-driven response; if a half-life ever ships, it is #8's knob on the same arrays.
- **R44** — ingest dedupes on `(seq, attempt)`; at-least-once delivery is the assumption, exactly-once learning is the protocol's job. #13 inherits this as a requirement.
- **R45** — policy draws are exact gamma-ratio Beta samples, key-addressed pure functions of `(policy_seed, seq, arm, purpose)`. No normal/grid/order-statistic approximation in `Decide()`, and no shared mutable draw stream anywhere.
- **R46** — the logged propensity is the plug-in estimate, labelled with its method; it is for dashboards, alerting, and the audit trail. Any IPS-grade use (#15) recomputes exact propensities offline from the logged posteriors.
- **R47** — learned state has exactly one writer: the fold over the WAL. Snapshots are checkpoints of that fold; boot = snapshot + tail replay; every op row carries the raw context (bin, region, SCA, mandate, amount) so re-bucketing is a re-fold.
- **R48** — concurrency: shard by arm index, one ingest writer per shard behind a bounded queue, readers are lock-free atomic loads. A torn `(α, β)` read is ≤ 1 count for ≤ 1 ingest interval — benign by rule. No mutex, CAS, or seqlock in the decision path. Ingest batch K is bounded (≤ ~64): staleness past the tens is measurably not free.
- **R49** — forced exploration exists only as the **onboarding state**: entered when a processor is added or #8 resets it, cleared at `n_min` (default 1,000) processor-level settled observations; while active, with probability η (default 0.05) attempt 0 goes to a uniformly chosen eligible onboarding processor. No standing threshold floor; no per-arm floor. The mixture probability is exactly computable and logged.
- **R50** — priors: Jeffreys when no table exists (band dimension collapsed per R40); with #11's table, `α₀ = m·r̂, β₀ = m(1−r̂)` per `(proc × bin × region)` with the hierarchical fallback, m default 100, a knob — strength beyond the arm's cold-window volume is a defect, not a feature.

## Reopen triggers

1. **Bands stop being free.** On production traces with the shipped prior, the 6-band space costs > 100 c/1k against a re-folded no-band/2-band space over two consecutive months → collapse the dimension (a re-fold; R40 anticipates exactly this).
2. **Amount effects are real and bigger than bands.** A measured amount-conditional rate effect (3DS exemption thresholds, risk tranches) that a 6-band ladder prices worse than a learned function → the band dim reopens as a modeling question (ADR-0003 trigger 2's local case).
3. **Issuer-level signal becomes affordable.** If fleet volume × issuer-cardinality × hierarchy (shrink toward the region key) makes issuer-keyed arms non-starving on production data — the steelman measured in §1 starving is evidence *for* the reopen condition, not against it.
4. **The floor's insurance case materializes.** A production onboarding where the prior is confidently wrong AND the processor is uniquely good on a contested segment → recalibrate η/n_min; if the onboarding floor measurably fails to rescue, the floor design (not TS) reopens.
5. **Torn reads stop being benign.** If a downstream consumer appears that cannot tolerate a ≤1-count-stale `(α, β)` pair (e.g. an exact-propensity hot path), the read protocol reopens (seqlock or 16-byte atomic) — with the burden of proof on the consumer, because the current protocol's cost is ~12 ns/read.
6. **The WAL outgrows the fold.** If WAL retention policy forces dropping raw context, re-bucketing dies with it and the arm space becomes immutable-in-practice — that is a #13 contract change and reopens R47.

## Alternatives considered

- **Finer keys (issuer / BIN prefix).** Steelmanned with a real effect present (§1): still loses 1,839 c/1k at 60k transactions — starvation beats signal at this volume. Reopen trigger 3 records the condition under which this flips.
- **No amount bands.** Margin-equivalent with the prior (−26 c/1k is noise) and better MAE on this world; rejected because `baseline-steady-v1` understates amount-dependence that production pricing (exemptions, risk tranches) will exhibit, and because R40 + re-fold make the bet cheap to unwind. The honest form of this alternative — "collapse bands until production proves them" — is trigger 1.
- **Normal-approximation draws.** 43% faster per draw and it *won margin* in the policy run (§4d) — by touching 359 fewer arms, i.e. by not exploring. Rejected on exactness (§4a: fails χ² exactly on cold-arm shapes) and on the coverage lesson: the win is a starvation artifact.
- **Grid/table draws.** Pass χ² and cheap per draw once built (3.16 µs) — but 33,663 table rebuilds in one 20k run, and rebuild time is unbounded in the tail (large-count arms). Rejected: the rebuild is a hidden p99 risk inside `Decide()`.
- **Transport error → timeout, or excluded.** The two alternatives the label experiment measured (§3b): timeout-laundering loses −34 c/1k and muddles the estimand (ADR-0002 already priced ambiguity); exclusion loses −86 c/1k full-run and −544 in-window by refusing to learn a dead processor. Both rejected; the `te` counter gives #8 everything the alternatives were reaching for.
- **Standing decay ("weighted update").** The ticket's own candidate: margin-flat on both worlds and permanently caps effective sample size (§3a). Rejected as a default; adopted as an #8-controlled tool on the same arrays.
- **Per-arm / standing-threshold exploration floors.** Both intuitively appealing, both measured and dominated (§6): the per-arm variant dilutes into rare buckets (−583 c/1k, no rescue), the standing threshold taxes legitimately weak processors forever (−117 c/1k, no benefit). The onboarding-state floor concentrates exactly where the risk is and switches itself off.
- **MC propensities on the hot path.** MC-64 is 26× the plug-in's cost and still not IPS-grade (any-arm 8.95 pts); MC-8 is cheap-ish and 41 pts off on cold arms. Rejected for the hot path; the exact-offline recompute (R46) is the design that survives.

## Appendix: reproduction

```bash
python3 spikes/0007-thompson-sampling/posterior.py 60000    # ~30 min, deterministic, stdlib only
python3 spikes/0007-thompson-sampling/posterior.py 5000 --section=P6   # ~12s smoke of any section
```

Deterministic (fixed policy seed 20260916; worlds and policy both key-addressed per ADR-0005); `RESULTS.md` is the committed output of exactly the first command (the P3 block from the equivalent `--section=P3` run of the same build — spliced once to fix a shared-world-instance confound in the dedupe table's first pass, documented in §3c). Limitations stated plainly: one catalog (six processors with the committed economics), three scenarios (steady, event, and the spike-local `long-refused-v1` gated behind `load_scenario` with zero errors), CPython timings projected to Go on ADR-0001's labelled 10/30/100× band (no Go in this sandbox), no #8 in the loop (drift findings are dependency claims, not simulations of #8), and the floor's insurance case argued from flatness rather than measured (§6, cost 3).
 rather than measured (§6, cost 3).
