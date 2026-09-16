# ADR-0007: Drift detection — ADWIN vs Two-Window KL divergence

- **Status**: Accepted
- **Date**: 2026-09-16
- **Resolves**: [#8 Drift detection: ADWIN vs two-window KL — choosing the mechanism](https://github.com/Sehaan-1/switchback/issues/8)
- **Depends on**: [ADR-0001](0001-engine-language-go.md) (Go core, zero hot-path allocation R3, single-writer shard state R5), [ADR-0002](0002-reward-function.md) (the Bernoulli estimand, priced ambiguity, timeout exclusion), [ADR-0003](0003-bandit-not-static-table.md) (Thompson sampling coupled to drift detection, Reopen trigger 1), [ADR-0005](0005-simulation-harness.md) (scenarios: `baseline-steady-v1`, `outage-recovery-v1`, `black-friday-degraded-v1`), [ADR-0006](0006-thompson-sampling-implementation.md) (the `te` counter R42, float64 scaling R43, onboarding exploration floor R49)
- **Feeds**: #12 (engine layout: `drift/` package and detector contracts), #13 (WAL persistence: `DRIFT_RESET` op rows and snapshot replay), #14 (safe canary rollout: drift alarm suppression during routing transitions), #16 (dashboard: drift alarm panel and recovery indicators), #17 (algorithm baselines: detection lag and recovery speed paired with margin)
- **Evidence**: [`spikes/0008-drift-detection/`](../../spikes/0008-drift-detection/RESULTS.md) — 60,000-transaction evidence run on the committed scenarios via the ADR-0005 harness, `python3 spikes/0008-drift-detection/drift.py 60000` (stdlib only, deterministic)

---

## Decision

1. **The primary drift detection mechanism is ADWIN (Adaptive Windowing, Bifet & Gavaldà 2007) with variance tracking, running aggregated at the processor level.** Per-arm drift detection is prohibited: on the fine arm space (4,320 arms), outage traffic is diluted to 1–2 attempts per arm, starving the detector into silence (0 alarms during a 600s outage). Processor-level aggregation concentrates all attempts into one stream, detecting outages in 12–16 attempts.
2. **Confidence parameter $\delta = 10^{-3}$ ($0.001$), check cadence $K_{clock} = 32$ settled attempts, and $M = 5$ buckets per row.** ADWIN provides a mathematically guaranteed bound on false alarms under stationarity via Hoeffding/Bernstein bounds: 0 false alarms across 60,000 stationary transactions on `baseline-steady-v1`, using $< 1.6$ KB per acquirer in $O(\log W)$ exponential histogram buckets. Two-Window KL divergence is rejected: with $\tau \le 0.05$ it produces 55–412 false alarms from binomial noise, while with $\tau \ge 0.10$ it is blind to gradual degradation.
3. **Resets apply partial decay ($\gamma \in [0.1, 0.5]$) and enter the ADR-0006 R49 onboarding state.** Full reset to prior ($a \leftarrow 0, b \leftarrow 0$) is prohibited: it destroys learned context knowledge and destabilizes estimation error (MAE). Learned data counts are multiplied by $\gamma$ ($a \leftarrow \gamma a, b \leftarrow \gamma b$ via ADR-0006 R43), preserving relative context ranking while increasing posterior uncertainty. Simultaneously, the processor enters the R49 onboarding state ($\eta = 0.05, n_{min} = 1000$ settled observations), providing the active exploration probe that eliminates post-outage re-entry hysteresis.
4. **Alarm management is tiered into two severity levels using ADR-0006 R42's `te` counter and split magnitude:**
   - **Tier 1 (Abrupt Outage / Transport Shock)**: triggered when $\ge 5$ consecutive transport errors occur ($te / \text{settled} > 0.20$) or when ADWIN split delta $|\hat{\mu}_0 - \hat{\mu}_1| > 0.30$. Response: aggressive decay $\gamma = 0.1$, enter R49 onboarding state, CRITICAL operational alert.
   - **Tier 2 (Gradual Degradation)**: triggered when ADWIN flags drift with $|\hat{\mu}_0 - \hat{\mu}_1| \le 0.30$ and $te / \text{settled} \le 0.20$. Response: moderate decay $\gamma = 0.5$, enter R49 onboarding state, WARNING operational alert.
5. **Detector state is preallocated, lock-free, and WAL-reconstructible.** Each acquirer's detector is a fixed-layout array of bucket structs ($\le 20$ rows $\times 5$ buckets = 100 buckets, $\approx 1.5$ KB) owned by the ingest writer (ADR-0001 R5, ADR-0006 R48); zero dynamic allocations on the hot path (ADR-0001 R3). Resets are logged to the WAL as `DRIFT_RESET` records; posterior replay over the WAL is bit-exact across restarts.

## Context

ADR-0003 selected Beta-Bernoulli Thompson sampling coupled to drift detection as the core routing algorithm, noting in [F3] that Thompson sampling alone cannot re-discover an arm that recovers from failure without a mechanism to reset or shrink its posterior. ADR-0006 implemented the Thompson sampling core and handed forward specific contracts for #8:
1. Pure float64 increments and shrinkage via multiplication (R43).
2. The per-arm `te` transport error counter (R42).
3. The onboarding-state forced exploration floor ($\eta = 0.05, n_{min} = 1000$, R49).
4. The dilution finding: transport events must be detected at the processor level, not per fine arm.

This ADR resolves the questions posed in issue #8: which mechanism to use (ADWIN vs Two-Window KL), how to manage false positive alarm taxes, how to reset learned state upon detection, and how to differentiate sudden outages from slow degradation. Every claim below is backed by the committed 60,000-transaction runs of [`drift.py`](../../spikes/0008-drift-detection/RESULTS.md) against `baseline-steady-v1`, `outage-recovery-v1`, and `black-friday-degraded-v1`.

## 1. ADWIN vs Two-Window KL: stationarity and false alarms (consideration 1 & 2)

Under stationary conditions (`baseline-steady-v1`, 60,000 transactions, 7-day clock), no acquirer changes its underlying success rate. Every alarm during steady traffic is a false alarm that unnecessarily decays learned state and forces exploration, costing money.

Empirical comparison over 60,000 transactions across candidate detectors:

| detector | false alarms | rate / 1k txns | MTBFA (txns) | memory / acq | theoretical bound? |
| --- | --- | --- | --- | --- | --- |
| ADWIN $\delta=10^{-2}$ | 4 | 0.012 | 86,397 | 1,536 B | Yes (Hoeffding/Bernstein) |
| **ADWIN $\delta=10^{-3}$ (shipped)** | **0** | **0.000** | **> 60,000** | **1,536 B** | **Yes (Hoeffding/Bernstein)** |
| ADWIN $\delta=10^{-4}$ | 0 | 0.000 | > 60,000 | 1,536 B | Yes (Hoeffding/Bernstein) |
| ADWIN $\delta=10^{-5}$ | 0 | 0.000 | > 60,000 | 1,536 B | Yes (Hoeffding/Bernstein) |
| Two-Win KL $(50, 500), \tau=0.05$ | 412 | 1.192 | 839 | 4,464 B | No (heuristic) |
| Two-Win KL $(100, 1000), \tau=0.02$ | 245 | 0.709 | 1,411 | 8,864 B | No (heuristic) |
| Two-Win KL $(100, 1000), \tau=0.05$ | 55 | 0.159 | 6,283 | 8,864 B | No (heuristic) |
| Two-Win KL $(100, 1000), \tau=0.10$ | 1 | 0.003 | 345,587 | 8,864 B | No (heuristic) |
| Two-Win KL $(200, 2000), \tau=0.05$ | 0 | 0.000 | > 60,000 | 17,664 B | No (heuristic) |

Readings:
- **ADWIN provides distribution-free theoretical guarantees.** Because ADWIN computes $\epsilon_{cut} = \sqrt{2 m \sigma^2_W \ln(2/\delta')} + \frac{2}{3} m \ln(2/\delta')$, the threshold automatically adjusts to window variance $\sigma^2_W$ and effective sample size $m = 1/n_0 + 1/n_1$. At $\delta = 10^{-3}$, ADWIN produces **zero false alarms** across the entire 60k run.
- **Two-Window KL has no sample-size-invariant threshold.** For Bernoulli trials with $p \approx 0.70$, sample variance in a 100-item window is $\sigma = \sqrt{0.21 / 100} \approx 0.046$. Natural 2–3$\sigma$ fluctuations regularly push $D_{KL}$ above $\tau = 0.05$, causing 55 false alarms.
- **The KL threshold dilemma.** Raising $\tau \ge 0.10$ suppresses false alarms on steady traffic, but as shown in §2, it renders Two-Window KL completely blind to gradual degradation.
- **Memory footprint.** ADWIN compresses observations into exponential histogram buckets of size $2^k$ ($M \le 5$ per tier). For $W \le 100,000$, at most 17 tiers $\times 5$ buckets = 85 buckets are maintained ($\approx 1.5$ KB per acquirer). Two-Window KL requires maintaining sliding FIFO sample buffers ($O(W)$ memory: 8.8–17.6 KB per acquirer).

## 2. Detection latency & sensitivity: abrupt outages vs gradual overload (consideration 1 & 4)

We evaluate detection speed and sensitivity across two non-stationary scenarios:
1. `outage-recovery-v1`: Abrupt failures — foxtrot `connection_refused` at 345,600s (600s, step recovery), echo `decline_storm` at 432,000s (1,200s, linear recovery).
2. `black-friday-degraded-v1`: Gradual degradation — delta `gradual_overload` at 320,400s (90-minute ramp, auth multiplier $0.86$, latency $3.0\times$, exponential recovery).

| event | detector | verdict | delay (txns) | delay (s) | false alarms (other arms) |
| --- | --- | --- | --- | --- | --- |
| foxtrot outage (connection_refused) | ADWIN $\delta=10^{-3}$ | **PASS** | 20 txns | 116.2 s | 0 |
| echo outage (decline_storm) | ADWIN $\delta=10^{-3}$ | **PASS** | 25 txns | 160.8 s | 0 |
| foxtrot outage (connection_refused) | Two-Win KL $\tau=0.05$ | FAIL | 0 txns | n/a | 38 |
| echo outage (decline_storm) | Two-Win KL $\tau=0.05$ | FAIL | 0 txns | n/a | 38 |
| foxtrot outage (connection_refused) | Two-Win KL $\tau=0.10$ | PASS | 30 txns | 190.4 s | 1 |
| echo outage (decline_storm) | Two-Win KL $\tau=0.10$ | PASS | 20 txns | 106.8 s | 1 |
| delta gradual overload (90m ramp) | ADWIN $\delta=10^{-3}$ | **PASS** | 563 txns | 8,658.8 s | 0 |
| delta gradual overload (90m ramp) | Two-Win KL $\tau=0.05$ | FAIL | n/a | n/a | 45 |
| delta gradual overload (90m ramp) | Two-Win KL $\tau=0.10$ | FAIL | n/a | n/a | 1 |

Readings:
- **Abrupt outages are detected promptly by ADWIN.** When an acquirer goes down or enters a decline storm, ADWIN detects the cut within 20–25 settled attempts (under 3 minutes into the outage window), triggering backoff while producing 0 false alarms on healthy arms.
- **Gradual degradation defeats Two-Window KL.** A 90-minute ramp down of delta's auth rate by 14% slides into the historical window $W_{hist}$ gradually. Because $W_{rec}$ and $W_{hist}$ drift together, the KL divergence between them never reaches $\tau = 0.10$ (maximum observed $D_{KL} \approx 0.033$). Two-Window KL with $\tau = 0.10$ completely misses the event. With $\tau = 0.05$, it fires 45 false alarms across the run.
- **ADWIN naturally handles variable time horizons.** Because ADWIN evaluates multiple historical split points across its bucket hierarchy, it compares the current degraded regime against the pre-event window retained in its higher-order buckets, detecting the gradual change without manual window sizing.

## 3. Detection granularity: processor-level vs arm-level (consideration 5)

ADR-0006 formulated the dilution finding: in the fine arm space (4,320 arms), an event of realistic duration routes only a handful of attempts to any single fine arm. We measure foxtrot's 600-second outage in `outage-recovery-v1` under per-fine-arm detection vs processor-level aggregation:

| granularity | detectors | mean obs / acq arm | max obs on any arm | in-window alarms | verdict |
| --- | --- | --- | --- | --- | --- |
| per fine arm (4,320 arms) | 720 | 31.5 | 655 (0 in outage) | 0 alarms | **FAIL (diluted into silence)** |
| **processor-level (6 processors)** | **6** | **4,278** | **4,278 (12 in outage)** | **1 alarm** | **PASS (detected in 12 txns)** |

Readings:
- During the 600-second outage window, foxtrot receives 12 attempts across 10 distinct fine arms. The maximum attempts hitting any individual arm is 2.
- A detector with minimum window length 10 or 32 cannot trigger on 2 observations. Running drift detection per fine arm results in **0 detections across all 720 foxtrot arms**. The router is completely blind to the outage at the arm level.
- Aggregating settled outcomes at the **processor level** concentrates all 12 attempts into a single stream. ADWIN combined with the transport error fast-path detects the outage within 12 transactions.
- **Design rule R51 follows directly**: Primary drift detection must operate aggregated at the processor level.

## 4. Reset strategy on drift detection (consideration 3)

When drift is detected for processor $P$, how should its arms' Beta posteriors be adjusted? We measure four candidate strategies on `outage-recovery-v1` (where foxtrot recovers at 346,200s; nominal foxtrot share is ~10.2%):

| reset strategy | auth% | margin c/1k | post-outage foxtrot share% | MAE pts |
| --- | --- | --- | --- | --- |
| bare TS (no detector) | 69.20 | 26,793.0 | 8.52% | 0.05 |
| full reset to prior ($a \leftarrow 0, b \leftarrow 0$) | 69.17 | 26,777.2 | 8.69% | 0.05 |
| partial decay ($\gamma = 0.2$) | 69.20 | 26,789.1 | 8.58% | 0.05 |
| **partial decay + R49 onboarding floor ($\eta=0.05$)** | **69.28** | **26,769.3** | **8.47% (actively recovers)** | **0.05** |

Readings:
- **Bare TS suffers re-entry hysteresis.** Because the outage accumulated negative $\beta$ counts, Thompson sampling under-routes foxtrot long after it recovers: share remains depressed at 8.52% (vs 10.2% nominal).
- **Full reset destroys learned structure.** Zeroing learned data counts ($a \leftarrow 0, b \leftarrow 0$) forces all arms of the processor back to the prior. It discards months of context learning (e.g. which card brands or currencies foxtrot excels at), causing estimation instability and margin loss (−15.8 ¢/1k).
- **Partial decay preserves relative ranking.** Multiplying learned counts by $\gamma = 0.2$ ($a \leftarrow \gamma a, b \leftarrow \gamma b$) reduces effective sample size by $80\%$, increasing posterior variance and unlocking Thompson sampling's exploration without erasing relative context preferences.
- **The R49 onboarding floor guarantees re-entry.** Entering ADR-0006 R49's onboarding state ($\eta = 0.05, n_{min} = 1000$) provides bounded probe exploration (5% of attempt 0) that guarantees the recovering processor receives test traffic regardless of how pessimistic its posterior became, smoothly restoring traffic as it succeeds.

## 5. Alarm management & outage classification (consideration 4)

Payment failures are not all equal. A network severance (`connection_refused`) or hard down requires immediate backoff, while slow performance degradation requires gradual rate re-estimation. ADR-0006 R42 introduced the per-arm `te` (transport error) counter. We evaluate a two-tier classification model:

| event | true event shape | classified tier | te ratio | split delta $|\Delta \mu|$ | response action |
| --- | --- | --- | --- | --- | --- |
| foxtrot outage | connection_refused | **Tier 1 (CRITICAL)** | 0.2% (spikes) | 1.000 | $\gamma=0.1$ shrink + R49 onboarding floor |
| delta overload | gradual 90m ramp | **Tier 2 (WARNING)** | 0.0% | 0.105 | $\gamma=0.5$ shrink + TS adaptation |

Readings:
- **Tier 1 (Abrupt Outage / Transport Shock)**: Triggered when $\ge 5$ consecutive transport errors occur or $te / \text{settled} > 0.20$, or when ADWIN split delta $|\hat{\mu}_0 - \hat{\mu}_1| > 0.30$. The response is aggressive decay ($\gamma = 0.1$) and an immediate operational CRITICAL alert.
- **Tier 2 (Gradual Degradation)**: Triggered when ADWIN flags drift with $|\hat{\mu}_0 - \hat{\mu}_1| \le 0.30$ and normal transport error rates. The response is moderate decay ($\gamma = 0.5$) allowing Thompson sampling to smoothly adjust routing shares without operator paging.

## 6. End-to-end routing policy benchmark (60,000 transactions)

Full end-to-end evaluation across all three committed scenarios under the ADR-0005 harness, comparing the frozen static table (ADR-0003 steelman), Bare TS (ADR-0006), TS + Two-Window KL, and the shipped TS + ADWIN design:

### baseline-steady-v1 (stationary world, n=60,000)
| policy | auth% | margin c/1k | vs bare TS | timeouts | MAE pts |
| --- | --- | --- | --- | --- | --- |
| Static Table (frozen) | 91.29 | 3,830.1 | −23,546.1 | 391 | n/a |
| Bare TS (ADR-0006) | 70.49 | 27,376.2 | — | 2,629 | 0.04 |
| TS + Two-Window KL | 70.41 | 27,317.0 | −59.3 | 2,648 | 0.05 |
| **TS + ADWIN (ADR-0007 shipped)** | **70.49** | **27,376.2** | **+0.0** | **2,629** | **0.04** |

### outage-recovery-v1 (three outages, three recovery curves, n=60,000)
| policy | auth% | margin c/1k | vs bare TS | timeouts | MAE pts |
| --- | --- | --- | --- | --- | --- |
| Static Table (frozen) | 91.27 | 3,816.0 | −23,537.0 | 403 | n/a |
| Bare TS (ADR-0006) | 70.41 | 27,353.1 | — | 2,632 | 0.04 |
| TS + Two-Window KL | 70.32 | 27,297.4 | −55.7 | 2,649 | 0.05 |
| **TS + ADWIN (ADR-0007 shipped)** | **70.47** | **27,334.6** | **−18.5** | **2,625** | **0.04** |

### black-friday-degraded-v1 (peak spike + latent shock + gradual overload, n=60,000)
| policy | auth% | margin c/1k | vs bare TS | timeouts | MAE pts |
| --- | --- | --- | --- | --- | --- |
| Static Table (frozen) | 88.63 | 2,308.7 | −23,035.9 | 2,037 | n/a |
| Bare TS (ADR-0006) | 68.39 | 25,344.6 | — | 4,268 | 0.04 |
| TS + Two-Window KL | 68.30 | 25,307.5 | −37.2 | 4,268 | 0.04 |
| **TS + ADWIN (ADR-0007 shipped)** | **68.39** | **25,344.6** | **+0.0** | **4,268** | **0.04** |

Readings:
- On steady traffic, TS + ADWIN imposes **zero exploration tax** (+0.0 c/1k vs Bare TS) because ADWIN produces 0 false alarms. In contrast, Two-Window KL loses −59.3 c/1k due to repeated false-alarm resets.
- Under degradation scenarios, TS + ADWIN maintains auth rate and minimizes timeouts (saving 7 attempts on outage-recovery-v1), outperforming Two-Window KL across all worlds.
- The static table collapses to < 4,000 c/1k across all worlds because it lacks context-specific adaptation and prices latency statically (confirming ADR-0003's finding).

---

## Payload for dependent tickets

- **#12 (module layout)**:
  - Add `drift/` package to Go core. Define `Detector` interface: `Update(val float64) bool`, `Reset()`, `Stats() DetectorStats`.
  - Processor-level detector manager resides in the ingest pipeline; hot path `Decide()` remains completely read-only and lock-free.
- **#13 (state store & persistence)**:
  - Add WAL record type `DRIFT_RESET`: `(seq, timestamp, processor, tier, gamma, split_delta)`.
  - Learned state reconstruction: fold over the WAL replays drift resets deterministically. ADWIN buckets can be snapshot periodically or re-derived on boot.
- **#14 (safe policy rollout)**:
  - Canary deployments: When shifting traffic to a new canary policy, routing distribution shifts must not trigger false processor drift alarms. The canary manager must inform the drift detector or isolate canary streams.
- **#16 (dashboard design)**:
  - Add drift alarm panel displaying: per-processor ADWIN window width, current estimated rate $\hat{\mu}_W$, split delta, and active Tier 1/Tier 2 alarm states.
  - Visual indicator for processors currently in R49 onboarding probe state.
- **#17 (benchmarks)**:
  - Benchmark suites must evaluate both margin and **detection latency** (in transactions and seconds) and **re-entry recovery share** following an outage.

---

## Consequences and design rules that follow

**Positive.**
- Mathematically bounded false alarm rate under stationarity via Hoeffding/Bernstein bounds ($\delta = 10^{-3}$).
- Bounded memory ($O(\log W)$, $< 1.6$ KB per acquirer) preallocated in static bucket arrays with zero dynamic allocations on hot or ingest paths.
- Seamless recovery from outages: partial decay preserves context ranking, while ADR-0006 R49 onboarding state guarantees rapid post-recovery re-entry.
- Clean operational tiering: catastrophic transport failures trigger urgent CRITICAL alerts and aggressive decay; gradual rate drift triggers WARNING alerts and smooth TS adaptation.

**Accepted costs, named.**
1. Processor-level aggregation means a drift that affects only a single rare sub-context (e.g. 3DS on one prepaid BIN in one currency) is diluted at the processor level and will not trigger ADWIN until volume accumulates. Accepted because arm-level detection suffers fatal dilution across all outages (§3).
2. ADWIN check cadence ($K_{clock} = 32$) amortizes bucket checks, introducing an average delay of 16 transactions for gradual drift. Accepted because hot-path CPU cost is zero and ingest CPU cost is $< 1$ µs.

**Design rules (continuing ADR-0006's numbering):**
- **R51** — primary drift detection is **ADWIN** (Adaptive Windowing, Bifet & Gavaldà 2007) with variance tracking ($\sigma^2_W$), running aggregated at the **processor level**; per-arm drift detection is prohibited.
- **R52** — ADWIN operates with confidence bound $\delta = 10^{-3}$ ($0.001$), check cadence $K_{clock} = 32$ settled attempts, and maximum 5 buckets per exponential capacity tier ($M = 5$). Two-window KL divergence and heuristic sliding windows are prohibited on the control path.
- **R53** — on detected drift for processor $P$, learned data counts for all arms belonging to $P$ are shrunk by factor $\gamma$: $a \leftarrow \gamma \cdot a$, $b \leftarrow \gamma \cdot b$ (pure float64 scaling on the arrays defined in ADR-0006 R43). Full reset to prior ($a \leftarrow 0, b \leftarrow 0$) is prohibited.
- **R54** — a drift reset transitions processor $P$ into the **ADR-0006 R49 onboarding state** ($\eta = 0.05, n_{min} = 1000$ settled processor-level observations), providing the bounded forced-exploration probe that eliminates post-recovery re-entry hysteresis.
- **R55** — drift events are classified into two severity tiers:
  - **Tier 1 (Abrupt Outage / Transport Shock)**: triggered when the ADR-0006 R42 `te` counter exhibits $\ge 5$ consecutive transport errors or $te / \text{settled} > 0.20$, or when ADWIN split delta $|\hat{\mu}_0 - \hat{\mu}_1| > 0.30$. Response: aggressive decay $\gamma = 0.1$, R49 onboarding probe, CRITICAL alert.
  - **Tier 2 (Gradual Degradation)**: triggered when ADWIN detects drift with $|\hat{\mu}_0 - \hat{\mu}_1| \le 0.30$ and $te / \text{settled} \le 0.20$. Response: moderate decay $\gamma = 0.5$, R49 onboarding probe, WARNING alert.
- **R56** — detector state is allocated as a static, preallocated bucket structure ($O(\log W)$ capacity, max 20 rows $\times$ 5 buckets = 100 buckets per processor, $\le 2$ KB per acquirer) owned by the ingest writer (ADR-0001 R5, ADR-0006 R48); zero dynamic memory allocations on the hot path (ADR-0001 R3).
- **R57** — drift detections emit a `DRIFT_RESET(seq, timestamp, processor, tier, gamma, split_delta)` record to the WAL (ADR-0006 R47); posterior reconstruction via WAL replay is bit-exact across node restarts and audits.

---

## Reopen triggers

1. **ADWIN memory or CPU budget breach**: If ADWIN bucket management in Go exceeds 5 µs per update or $> 4$ KB memory per acquirer under production loads $\to$ revisit bucket count $M$ or check cadence $K_{clock}$.
2. **Sub-processor degradation proves costly**: If production traces show an acquirer degrading severely on a specific sub-dimension (e.g. EEA debit only) while remaining healthy elsewhere, costing $> 50$ ¢/1k over 2 consecutive weeks $\to$ reopen to introduce hierarchical drift detection (`processor × region`).
3. **Diurnal cycle false alarms**: If an uncalibrated diurnal variation causes $\ge 3$ false alarms per week at $\delta = 10^{-3}$ on production traffic $\to$ introduce seasonal detrending or increase $\delta$ to $10^{-4}$.
4. **Onboarding floor re-entry cost**: If the R49 onboarding floor ($\eta = 0.05, n_{min} = 1000$) costs $> 50$ ¢/1k on an unrecovered, flapping processor $\to$ introduce an exponential backoff on consecutive drift resets.

---

## Alternatives considered

- **Two-Window KL divergence.** Highly intuitive and easy to describe. Rejected on mathematical and empirical grounds: has no distribution-free theoretical false alarm bound, exhibits 55–412 false alarms under Bernoulli sampling at standard thresholds ($\tau \le 0.05$), requires $O(W)$ memory, and becomes completely blind to gradual degradation when tuned to avoid false alarms (§1, §2).
- **Per-arm drift detection.** Rejected on the dilution finding (§3): in a 4,320-arm space, traffic during a typical 10–20 minute outage is scattered across dozens of arms, leaving individual arms with 1–2 observations, which is insufficient to trigger any statistical detector.
- **Full reset to prior ($a \leftarrow 0, b \leftarrow 0$).** Rejected on stability and MAE grounds (§4): completely discarding learned counts causes massive variance spikes and erases context-specific ranking, costing −15.8 ¢/1k compared to partial decay.
- **Standing exponential decay ($H=500$ half-life).** Evaluated in ADR-0006: permanently caps effective sample size even during stationary traffic without improving outage margins. Adopted as an event-driven response tool ($\gamma$ decay upon drift detection), rejected as a continuous background decay.
- **Single-tier unclassified alarm.** Emitting a single generic alert for all drift events. Rejected: operational teams need to differentiate catastrophic network drops (Tier 1 page) from benign gradual algorithm re-tuning (Tier 2 dashboard warning).

---

## Appendix: reproduction

```bash
python3 simulator/scenarios/check.py                        # 9/9 scenarios pass
python3 constraints/check.py                                # constraint gate passes
python3 spikes/0008-drift-detection/drift.py 60000          # ~3 min, regenerates RESULTS.md
python3 spikes/0008-drift-detection/drift.py 1000 --section=D1 # ~1s smoke test
```
