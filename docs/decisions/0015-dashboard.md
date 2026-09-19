# ADR-0015: Dashboard design — panel inventory, the analysis-API read path, grid-preview + async-exact counterfactual UX, and the React stack

- **Status**: Accepted
- **Date**: 2026-09-19
- **Resolves**: [#16 Dashboard design: revenue vs cost vs latency, policy regret, and counterfactual panel](https://github.com/Sehaan-1/switchback/issues/16)
- **Depends on**: [ADR-0002](0002-reward-function.md) (the two-part reward — the money panel *is* its decomposition; λ_to=45; no per-millisecond latency term), [ADR-0007](0007-drift-detection.md) (Tier-1/Tier-2 processor alarms — the traffic-light semantics), [ADR-0011](0011-module-layout.md) (`internal/api/` = routing + outcomes only; Python owns `analysis/`; `sim/oracle`), [ADR-0012](0012-state-store.md) (R91 partitions, R94 read classes, R96 the columnar tier, `[W10]`/`[W5]` reader costs, the store-metrics payload), [ADR-0013](0013-safe-policy-rollout.md) (the rollout-panel payload: stage, D-trace with live Y and null σ, gate verdicts, kill-switch state), [ADR-0014](0014-off-policy-evaluation.md) (the shift query/answer contract, the verdict enum with refusal semantics, the seal-time π₀ tables, the 43–408 s query-cost class)
- **Feeds**: the dashboard implementation ticket (endpoint contracts and component tree below, verbatim), #17 (benchmarks: the tail-read-vs-writer cost and the seal-job aggregate pass this ADR models), the engine implementation ticket (nothing — the engine's `/v1` surface is untouched by this ADR)
- **Evidence**: **no new spike — this is an integration ADR**, as ADR-0011 was: every contract it pins is owned by an accepted ADR, and every number is either cited or labelled a model. The UI performance figures (§4.3) are *requirements*, stated as such, on the ADR-0013 precedent ("the sub-second target is a labelled requirement, not a measurement"). Wireframe magnitudes use the committed scenario's values (`baseline-steady-v1@sha256:b49193b7e715`, the acquirer-catalog-2026.09.2 roster, n=60,000 per ADR-0014's 7-day window) and are labelled where they are illustrative.

---

## Decision

1. **One page, six panels in a fixed 12-column grid: P1 money (margin earned vs cost, with a synchronized latency strip below it), P2 policy regret vs oracle (simulation runs only), P3 counterfactual (shift target + ρ slider + window), P4 processor health strip (one tile per acquirer: traffic light, auth rate, p99, volume share, sparkline), P5 rollout (ADR-0013's payload, verbatim shape), P6 engine/store health bar (ADR-0012's named metrics).** A single window switch (1h / 24h / 7d) drives P1 and P4; P3 carries its own date-range picker because its estimand is window-bound. The annotated page-shell wireframe is §1.0, the per-panel wireframes and data contracts are §1.1–§1.3 and §2, and the component tree is §5.4.
2. **The dashboard never reads the engine's SQLite/Parquet, and the engine never grows read endpoints: it talks to a FastAPI sidecar in `analysis/` ("the analysis API"), which owns exactly two read classes — long reads over sealed-day Parquet (the R96 columnar tier) and short bounded tail reads of today's open partition (R94's "short bounded reads" clause).** No WebSocket in v1: everything is batch (pre-aggregated at seal) or 60-second polling with a server-provided `next_poll_after`; the counterfactual is an on-demand async job (§3, §4).
3. **P1 answers "three y-axes or normalised overlay?" with neither: margin and cost share one money axis because they are the two parts of the ADR-0002 reward in one unit; latency gets its own axis on a separate strip with the same x-axis (a small multiple, hover-locked), because ADR-0002 puts no per-millisecond term in the objective and a normalised overlay would hide the absolute scale the 900 ms deadline lives on.** P2 answers "cumulative curve or per-arm breakdown?" with both: the cumulative regret line is primary, the per-arm missed-opportunity stack is the toggle view — exact in simulation because the committed scenarios record `all_arms`, and deliberately *absent* for live runs, which get a stated-unavailable card instead of a proxy.
4. **P3 is two-tier: slider scrubbing reads the seal-time π₀ grid — ADR-0014 decision 5 already computes per-target π₀ tables at partition seal "so a pane refresh is a sum" — and returns in well under a second with the full ADR-0014 answer contract; off-grid ρ or custom windows spawn an async job (202 + job id + 2-s polls) against `analysis/ope/`, with response targets ≤ 60 s p50 (7-day window, warm cache — the measured class is 43–49 s) and ≤ 10 min worst case (the measured ~408 s class), a progress state with ETA, and a page that never blocks.** No point is ever rendered without its verdict; a refused query renders the pinned refusal copy with N and no number; a wide-CI answer renders the band, the support diagnostics, and a low-informativeness badge (§4).
5. **Stack: React 18 + Vite static SPA served by the sidecar, Recharts for all panels, TanStack Query for server state, plain React state for the rest; no Next.js, no Zustand in v1, no hand-rolled D3.** Chart-library choice is argued on maintainability and contract surface, not performance: after the §3 pre-aggregation every chart receives ≤ 1,440 points (pinned arithmetic), which is below the threshold where any of the candidate libraries is at risk.

---

## Context

Three facts about the project's shape decide this design, and two of them mean the ticket's central question is half-answered before it is asked.

**The read path is already law.** R94 (ADR-0012) says long, interactive, or unbounded reads go to a sealed partition or a replica, never the live file, and prices the violation: a dashboard query on the live file costs the writer 66% of its throughput and 10 ms of commit tail (`[W10]`), and an interactive session that holds a read transaction pins ~119 MB of WAL per second. So "does the dashboard query the trace directly or go through an API" has only one admissible answer for the long reads — *through something that is not the live file* — and the real questions this ticket must answer are where that *something* lives, and what its refresh cadence honestly is.

**The counterfactual is a 43–408-second computation, measured.** ADR-0014 pins the shift interface, the answer contract, the verdict enum, the support gate (≥ 1,000 logged head-target rows), and its cost class: ≈ 43 s cold for an 8%-share 7-day target, ≈ 383 s for a 45%-share target, ≈ 49 s end-to-end warm, ~408 s worst class, single core [O1](3)/§4. The counterfactual panel is therefore not an estimator-design question (that was #15's); it is a question about what a UI does when its answer is a minute-scale job, and about using the seal-time π₀ tables ADR-0014 already builds.

**This is a prototype ticket.** It resolves the design question and hands the implementation ticket a contract it can build against; it does not contain React code. The wireframes below are annotated layouts — the format the ticket asks for — with the data contract per panel attached, so the implementation ticket never has to guess what a field means.

---

## 1. Panel inventory (consideration 1)

### 1.0 Page shell

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│◆ switchback · fleet view                                                                          [ 1h ] [ 24h ] [ 7d ] ①│
│world baseline-steady-v1@sha256:b491… (simulation) · policy 9f31…c4a2 (v1.4.0) · schema 1.0.0 ②                           │
│live tile: 42 s behind tail · sealed through 2026-09-18 00:00 UTC · all times UTC (browser: Europe/…) ③                   │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│P4 · PROCESSOR HEALTH · trailing 1h ④                                                                                     │
│┌────────────────────────────────────┐  ┌────────────────────────────────────┐  ┌────────────────────────────────────┐    │
││● alpha                             │  │● bravo                             │  │▲ charlie  (T2 ⚠)                   │    │
││auth 91.2%  n 16.4k                 │  │auth 88.6%  n 13.1k                 │  │auth 83.9%  n 20.3k                 │    │
││p99 631 / 900 ms                    │  │p99 897 / 900 ms                    │  │p99 1148 / 900 ms                   │    │
││share 24.1%                         │  │share 19.8%                         │  │share 31.2%                         │    │
││▁▂▅                                 │  │▁▂▇▅                                │  │▇▇▁                                 │    │
│└────────────────────────────────────┘  └────────────────────────────────────┘  └────────────────────────────────────┘    │
│┌────────────────────────────────────┐  ┌────────────────────────────────────┐  ┌────────────────────────────────────┐    │
││● delta                             │  │● echo                              │  │● foxtrot                           │    │
││auth 93.4%  n 7.6k                  │  │auth 89.7%  n 5.9k                  │  │auth 80.9%  n 3.0k                  │    │
││p99 512 / 900 ms                    │  │p99 755 / 900 ms                    │  │p99 1690 / 900 ms                   │    │
││share 11.4%                         │  │share 8.9%                          │  │share 4.6%                          │    │
││▂▃                                  │  │▂▃▂                                 │  │▂▃                                  │    │
│└────────────────────────────────────┘  └────────────────────────────────────┘  └────────────────────────────────────┘    │
├─────────────────────────────────────────────────────────────┬────────────────────────────────────────────────────────────┤
│P1 · MONEY · margin earned vs cost · 7d ⑤                    │P3 · COUNTERFACTUAL · what-if · own window ⑥                │
│margin ¢/1k 25,624.7 · cost ¢/1k 1,978.1 · net 23,646.6      │target [ foxtrot ▾ ]   = ▁▂●▃▃ 20%                          │
│volume 1.54M minor · authorized 85.9% · attempts 60,000      │window [ last 7d ▾ ] 2026-09-12 → 2026-09-19                │
│┌───────────────────────────────────────────────────────────┐│┌──────────────────────────────────────────────────────────┐│
││cost   ▂▂ ─────  (one money axis: minor units)             │││Δ margin ≈ −952.6 ¢/1k                                    ││
││margin ▁▂▇ (same axis — the two parts of the               │││95% CI [−1,905.2 , +0.2]                                  ││
││reward, ADR-0002)                                          │││verdict PASS · support 4,785 rows                         ││
││hover: 09-14 14:00 — margin 258.4k · cost 21.1k            │││ESS 300 (ess_frac 0.005) · Ē[w̃] 0.982                    ││
││attempts 96,312 · timeouts 41 · p99 897 ms                 │││clip 10 · clipped head-IPS · est. grade "grid"            ││
│└───────────────────────────────────────────────────────────┘││sealed 2026-09-18 · ≈ −57.2k minor (n 60,000)             ││
│P1b · LATENCY · same x, own axis · deadline 900 ms ┄┄ (ref)  │└──────────────────────────────────────────────────────────┘│
│┌───────────────────────────────────────────────────────────┐│Δ(ρ) grid ribbon · PASS zone shaded                         │
││p99  ▁▂▇▆▃                                                 ││  −3k ▒▒●▒▒ −1k   0  1k   · ρ .05 … .50                     │
││p50  ▁▁▂▁  (hover-locked with money chart)                 ││[ preview: grid ρ = 0.20 ]  [ run exact  est. 49 s ]        │
│└───────────────────────────────────────────────────────────┘│                                                            │
├─────────────────────────────────────────────────────────────┴────────────────────────────────────────────────────────────┤
│P2 · REGRET vs ORACLE · simulation only ⑦                    │P5 · ROLLOUT · candidate 4c77…d19 (v1.5.0) ⑧                │
│cumulative regret ¢/1k · 0 ─▁▃▇ (flattening ≈ 3.5k)          │stage: canary 25% · shares .25 / .75 · 348 windows          │
│per-arm missed: bravo 1,912 ▓▓ · charlie 804 ▒▒ · foxtrot 484│D +180 c/1k vs Y 5,262 (σ_null 1,754 · W = 1,000)           │
│policy 21,450 · oracle 24,981 · run baseline-steady-v1       │gates: margin ✓ · rout ✓ · coverage ✓ · promotion ✓         │
│(all-arms recording — oracle = world-model argmax;           │replay PASS · dbl 0 · I1 0                                  │
│ unavailable for live runs: stated-unavailable card)         │suppression: 1 hold (charlie, canary-arming) · 0 re-fire    │
│                                                             │kill-switch: ARMED                                          │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│P6 · ENGINE · tail 42 s behind · WAL peak 0.4 MB · stopped-short checkpoints 0 · dropped conflicts 3 ⚠                   ⑨│
│auth session unattributed 0 · last seal 2026-09-18 00:00 UTC · π₀ tables sealed through 09-18, all 6 targets              │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

① The window switch is page-level state (plain React state); it re-keys P1/P1b/P4 queries. ② Run identity is load-bearing: world hash + policy_id + schema version, so a reader can say *which* fleet this is at a glance — the ADR-0012 `boot` row hashes are the proof, surfaced not hidden. ③ The staleness line is a standing, honest statement of §3's cadence ("42 s behind tail"), never a "live" badge the data cannot support. ④ P4 spans the full width; it is the first thing read, so it is first — one tile per acquirer (two rows of three in the wireframe; one row at the real aspect ratio). ⑤ P1: the reward, decomposed into its two money series, with the latency strip (P1b) hover-locked below it — the layout answer to "three y-axes or normalised overlay?" (§1.1). ⑥ P3: the counterfactual, the grid-preview card and the exact-job action side by side (§4). ⑦ P2: sim-only regret, stated in the panel title — the gating is in the layout, not a tooltip (§1.2). ⑧ P5 exists because ADR-0013's payload to #16 names it; it is not optional inventory. ⑨ P6 is the ADR-0012 payload's "metrics worth a panel" — a one-line bar in v1, a full panel only if the implementation finds the line crowded (noted, not pre-built).

Wireframe magnitudes: the OPE card is the ADR-0014 measured headline class (foxtrot, ρ=0.20, −952.6 ¢/1k, 4,785 head rows [O3, O5]; CI values illustrative at the same order — ADR-0014's measured headline CI width is 946 ¢/1k); the P4 auth rates are the committed scenario's base rates (alpha 0.915, bravo 0.884, charlie 0.842, delta 0.935, echo 0.900, foxtrot 0.806) and p99s its recorded latency shape, shown as *trailing-window actuals* in the real UI; the P5 numbers are ADR-0013's measured null (σ 1,754 c/1k at W=1,000 ⇒ Y = 3σ = 5,262, arithmetic on the measurement) with D illustrative.

### 1.1 P1 — revenue vs cost vs latency over time

**The money chart is the reward, decomposed.** Per ADR-0002 the objective is `+win` on authorization, `−fee` on decline/abandonment, `−(fee + λ_to)` on timeout, with `fee` charged at submission. The panel's two series are exactly that, in one unit (minor currency):

- **margin earned** ("revenue"): Σ `win` over authorized attempts in the bucket, where `win = amount·(take_bps − cost_bps)/10⁴ + (take_fixed − fixed_fee)` — the platform's margin, not transaction GMV. (GMV is shown as a context stat, "volume", in the panel header, because a router reader wants both, but GMV is not what the reward optimizes and it does not get an axis.)
- **cost**: Σ `attempt_fee` over *all* attempts in the bucket + Σ `λ_to` over timeouts. "Costs are real on declines" (ADR-0002) is visible by construction: the cost series is positive even in a perfect-auth bucket.

**Three y-axes or normalised overlay? Neither.** The two money series share one axis because they are the same quantity in the two signs of one reward; adding a third axis for latency, or normalising everything to 0–1, is rejected for three independent reasons:

1. *Semantic.* ADR-0002 §3: "There is **no per-millisecond latency term**." Latency enters the objective only through the deadline (timeouts priced at λ_to) and the catalog's `max_response_ms`. Plotting ms on the same axis as money — in any scaling — depicts a trade the reward does not model.
2. *Scale.* The operational question is "p99 897 vs deadline 900". A normalised bar at 0.997 answers nothing; the absolute axis with a deadline reference line answers it.
3. *Ergonomics.* Three-axis charts are the standard readability failure. The replacement — a second strip on the same x-axis, hover-locked (Recharts `syncId`) — keeps 1:1 temporal alignment, which is the only property the third axis was ever providing.

**P1b latency strip:** p50/p95/p99 per bucket over attempt rows (`op.ms`, index-bounded per ADR-0012 `[W4]`), a dashed reference line at the route deadline (900 ms in the committed scenario's context mix; per-route-class deadlines where the mix splits), and timeout count in the tooltip. The strip and the money chart share the x-domain and the hover, so "cost jumped *and* p99 broke the deadline in the same hour" is one mouse motion.

**Windows and bucketing (pinned):** 1h → 10 s buckets (360 points), 24h → 1 min (1,440), 7d → 1 h (168). Pinned here, not derived: they come from §3's pre-aggregation, and Reopen trigger 3 is the only path that moves them.

**Data contract** — `GET /api/v1/money?window=1h|24h|7d&tz=UTC` and `GET /api/v1/latency?window=…`:

```json
{
  "window": "7d", "bucket_s": 3600, "tz": "UTC",
  "as_of": "2026-09-19T07:58:12Z", "source": "sealed-parquet+tail-cache", "next_poll_after": 60,
  "buckets": [
    { "t": 1758048000,
      "margin_minor": 153748, "margin_c_1k": 25624.7,
      "cost_minor": 11868, "cost_c_1k": 1978.1, "timeout_price_minor": 378,
      "attempts": 96312, "authorized": 79204, "timeouts": 41,
      "p50_ms": 231, "p95_ms": 512, "p99_ms": 897,
      "deadline_ms": 900 }
  ],
  "totals": { "margin_c_1k": 25624.7, "cost_c_1k": 1978.1, "volume_minor": 1540000, "authorized_rate": 0.859, "attempts": 60000 }
}
```

`margin_c_1k` / `cost_c_1k` are the ¢/1k estimand form (the unit ADR-0013/0014's gates and OPE speak); `*_minor` are the absolute form. Both are served; the UI picks per surface (charts: absolute; KPI chips: ¢/1k) and the reader never has to convert. Money and latency share one endpoint and one bucket grid in v1 so the hover-lock is guaranteed by construction; they split if the implementation finds one endpoint's cache semantics awkward (noted, not pre-built).

### 1.2 P2 — policy regret vs oracle

**Definition.** `regret(t) = Σ_{t′≤t} [ max_a win(a | t′) − win( policy(t′) | t′ ) ]` over settled decisions, in ¢/1k. The oracle is the world model's per-timestep argmax over all arms.

**Simulation only, by construction.** The oracle is computable only where the world is known — the harness. For live runs there is no oracle, and the panel says so instead of substituting: the "live run" state renders a card — *"Regret vs oracle is unavailable for live runs: the oracle is the world model's per-timestep argmax and only the simulation harness knows it. The production comparison is the rollout D-trace in P5 (canary vs incumbent, ADR-0013)."* A best-arm-in-data proxy is explicitly not shown: it is winner-biased (it credits the arm the data happened to observe) — the exact bias class ADR-0008 and ADR-0014 exist to price — and shipping it under the label "regret" would be an unpriced number, which the house rules delete rather than soften.

**Display — both options the ticket lists.** (a) *Primary: cumulative regret line*, because the question the curve answers is the convergence question — "is it still growing or has it flattened?" (a growing line is a policy problem; a flat line at 3.5k is the design working). A secondary toggle normalises to regret per 1k decisions for cross-run length comparison. (b) *Toggle: per-arm breakdown* — a stacked area of "missed opportunity by arm", `Σ_{t: a*=a} [win(a|t) − win(policy(t)|t)]`. This is **exact, not estimated**, in simulation: the committed scenarios run `recording.mode = all_arms` (ADR-0005), so every arm's outcome at every step is in the trace and the oracle argmax is a table lookup, not an OPE estimate. The per-arm view answers "who are we leaving money with", which the cumulative curve cannot.

**Data contract** — `GET /api/v1/regret?run=<run_id>` (static per completed run; no polling):

```json
{
  "run": "baseline-steady-v1@sha256:b49193b7e715", "available": true, "bucket_s": 3600,
  "buckets": [ { "t": 1758048000, "policy_cum_c_1k": 21450.2, "oracle_cum_c_1k": 24980.5, "regret_cum_c_1k": 3530.3 } ],
  "per_arm": [ { "arm": "bravo", "regret_cum_c_1k": 1912.4, "missed_decisions": 8123 } ],
  "oracle_basis": "world argmax over all-arms recorded outcomes (harness)"
}
```

Live run: `{ "run": "…", "available": false, "reason": "oracle requires the world model; see the rollout D-trace (P5)" }`.

### 1.3 P3 — counterfactual panel (design detail in §4)

Wireframe ⑥ above is the panel. Contract:

- `GET /api/v1/counterfactual/grid?window=7d&targets=<roster>` — the pre-computed preview surface (seal-time π₀ tables, ADR-0014 decision 5):

```json
{ "window": { "t0": 1757664000, "t1": 1758268800 },
  "grid": [ { "target": "foxtrot", "rho": 0.2,
             "value_c_1k": 24672.1, "auth_rate": 0.841, "delta_c_1k": -952.6, "ci95": [-1905.2, 0.2],
             "support": { "head_rows": 4785, "ess_frac": 0.005, "ew_bar": 0.982 },
             "verdict": "PASS", "est_grade": "grid", "clip": 10,
             "computed_at": "2026-09-18T00:41:00Z", "sealed_through": "2026-09-18T00:00:00Z" } ] }
```

- `POST /api/v1/counterfactual/jobs` `{ "target", "rho", "t0", "t1", "tz" }` → `202 { "job_id", "status": "queued", "eta_s": 49 }`; `GET /api/v1/counterfactual/jobs/{id}` → `{ "status": "queued|running|done|failed", "progress", "eta_s", "result": <ADR-0014 answer contract, verbatim>, "error" }`.

The `result` object on a done job is **ADR-0014's answer contract passed through unchanged** — `{ value_c_1k, auth_rate, delta_c_1k, ci95, support: { head_rows, ess_frac, ew_bar }, verdict }` — with two presentation fields added by the sidecar and nothing removed: `est_grade: "exact"` and `delta_minor` (the absolute form, arithmetic: `delta_c_1k × n_window / 1000`). The grid endpoint answers are the same shape with `est_grade: "grid"`.

---

## 2. Processor health at a glance (consideration 2)

One tile per acquirer (the committed roster: alpha, bravo, charlie, delta, echo, foxtrot — served from the catalog the store hands out, R98, never hard-coded in the UI):

```
┌───────────────────┐
│ ● charlie  (T2 ⚠) │   ① traffic light + alarm tier, never colour alone
│ auth 83.9%  n 20.3k│   ② trailing-window observed auth rate + its sample size
│ p99 1148 / 900 ms │   ③ trailing-window p99 vs the route deadline
│ share 31.2%       │   ④ share of decision volume in the window
│ ▇▇▆▅▃▂▁▁▁        │   ⑤ auth-rate sparkline, 1h at 5-min buckets (24 pts)
└───────────────────┘
```

**Traffic light — pinned to ADR-0007's tiers, not to taste.** The detector is processor-level ADWIN with two alarm tiers; the light is a direct mapping:

| light | condition | ADR-0007 source |
| --- | --- | --- |
| **red** | active Tier-1 alarm (CRITICAL): `te/settled > 0.20` (5+ consecutive transport errors) or ADWIN split delta `> 0.30`; or p99 ≥ 1.0 × route deadline sustained | Tier-1 definition |
| **amber** | active Tier-2 alarm (WARNING): ADWIN drift with split delta `≤ 0.30`; or processor in R49 onboarding (post-reset exploration, η=0.05, n_min=1,000); or p99 ≥ 0.8 × deadline (approaching it — the 0.8 is a pinned constant, not a derivation; it exists so the reader sees the approach before the red) | Tier-2 definition, reset protocol |
| **green** | none of the above, and the trailing window is not sample-starved (`n` shown, so a green over 40 rows reads as what it is) | — |

The alarm's `since` timestamp and tier render on the tile (the ADR-0007 alarm is an event with a time; a light without its "since" would be a mood). Colour is never the sole channel: tier text and glyph (●/▲/■) sit beside it, for colour-blind readers and for print (the repo's evidence culture exports panels to ADRs).

**The sparkline is auth rate, not p99** — it is the money signal, and the drift detector watches it too, so the reader's eye and the detector's eye agree. p99 is in the tile's second line and the hover.

**Data contract** — `GET /api/v1/processors/health?window=1h`:

```json
{ "window": "1h", "as_of": "2026-09-19T07:58:12Z", "next_poll_after": 60,
  "processors": [ { "id": "charlie", "traffic_light": "amber",
    "drift": { "state": "ALARM", "tier": 2, "since": "2026-09-19T06:12:40Z", "split_delta": 0.18, "te_over_settled": 0.04 },
    "onboarding": true,
    "auth_rate": 0.839, "auth_n": 20301, "p99_ms": 1148, "deadline_ms": 900,
    "volume_share": 0.312,
    "sparkline": [ { "t": 1758256200, "auth_rate": 0.861, "p99_ms": 1010 } ] } ] }
```

Sources, all already law: detector state and `DRIFT_RESET` ops (ADR-0007/0012, in the snapshot's detector buckets and the op log), auth rate and p99 from decision/outcome rows over the trailing window (the group-by fields are columns — ADR-0012's payload to #16: `op(ms)` is `op_ms`-indexed), volume share from decision volume, roster and `deadline_ms`/`max_response_ms` from the served catalog (R98).

---

## 3. Data contract between dashboard and engine (consideration 3)

**Question: direct trace reads or engine API? Answer: neither form the ticket imagines — a read API owned by `analysis/`, on exactly the two read classes R94 permits.**

- The engine's HTTP surface stays what ADR-0011 fixed it to be: `POST /v1/route`, `POST /v1/outcomes`. It grows no read endpoints. The engine is the writer's machine; a dashboard scan inside its process puts cold-path work in the binary that owns the 2 ms lease path, and the OPE side of the dashboard is Python that ADR-0011 decision 5 already houses in `analysis/` ("no subprocess, no gRPC sidecar, no embedded interpreter, no WASM module" *in the engine*; the dashboard is not the engine).
- The dashboard is a static SPA served by a **FastAPI sidecar in `analysis/`** (the analysis API). It is the same home ADR-0011 gives Python — `analysis/` is "OPE (#15), benchmark reports (#17)" and the dashboard read service is the third cold-path tenant — and it is where the OPE jobs it wraps already run. One process, one deploy unit; the SPA is static files from the same server.
- The sidecar's read classes are pinned (R94, restated for this consumer):
  1. **Long reads → sealed data only.** Everything older than today comes from sealed per-day partitions: the row-store copy via a `Reader` (sealed partitions or replica, never the live file) or, preferentially, the R96 columnar Parquet export that `analysis/` already owns. 7-day panels are pre-aggregated at seal (below), so a "7d" query touches a few thousand aggregate rows, not 994 M raw rows (ADR-0012's fleet-day figure).
  2. **Bounded reads → the live file, short and limited.** Today's partial day is served by a tail read: rows since the last poll, an index-bounded range (the `op_ms`/`seq` indexes ADR-0012 shipped for exactly the router's windowed queries), a short-lived transaction, then the sidecar folds the delta into its in-memory bucket cache. This is R94's own carve-out — "the live file is for the writer and for short bounded reads" — and it is the *only* live-file reader in the system, owned by the sidecar and sized in §3.1.
- **No WebSocket in v1.** The data has no stream: the sealed tier is a batch artifact and the tail is a bounded snapshot. Push would add stateful connections, reconnection, and fanout to buy zero data at these paces — the slowest displayed signal (the rollout D-trace) moves once per gate window, which is 0.8 s of fleet traffic at 5,000 dps (ADR-0013) and ~50 s at the committed scenario's 20 tps; 60-s polling resolves both. Push reopens only per Reopen trigger 1.

### 3.1 The tail read, sized (model on measured rates)

Fleet pace is 5,000 decisions/s ≈ 13,376 rows/s (ADR-0012, including auth rows); a 60-s poll therefore touches ≈ 0.8 M rows. The measured floor for this shape is ADR-0014's window scan, 2.5 M rows/s single-core stdlib sqlite [O6] (the sidecar has DuckDB over the columnar tier for everything sealed, whose *mechanism* measured 6.71 M rows/s pruned in ADR-0012 `[W5]` — real-format figures are labelled models, not measurements, per house rules). At the measured floor: ≈ 0.3 s of scan per 60 s per shard — **~0.5% duty** of one core, short transactions, nothing held. Against that, the rejected alternative's cost is measured, not modelled: an interactive reader on the live file costs the writer 66% of throughput and 10 ms of commit tail (`[W10]`). The tail read is the same *file* as `[W10]`'s hazard but a different *class* of read, and Reopen trigger 2 makes the distinction falsifiable: if the measured writer cost of the tail class exceeds 5% of throughput, or any R94 pinning signal appears (WAL peak > 10× autocheckpoint target, stopped-short checkpoints > 60 s), the design reopens to a dedicated replica tier rather than continuing on the carve-out.

### 3.2 Refresh cadence (the whole table — this is the answer to "real-time, polling, or batch?")

**All three, in that order of importance:** the bulk of what is displayed is *batch* (pre-aggregated at seal); *polling* at 60 s carries the live tiles; the counterfactual's exact path is *on-demand async*. There is no real-time path, and the header says so.

| data | producer | cadence | panels |
| --- | --- | --- | --- |
| sealed-day aggregates: (bucket × processor × outcome-class) counts, per-bucket latency quantiles, margin/cost sums | `analysis/` seal job, at day-seal + R96 export (one pass per sealed partition) | once per day per shard | P1 (≤ 7d), P2 (sim), P3 grid |
| seal-time π₀ tables for the registered query vocabulary (all 6 targets × pinned ρ grid) | ADR-0014 decision 5, at seal | once per day per shard | P3 preview |
| today's partial buckets, per-processor trailing windows, store tallies | sidecar bounded tail read → in-memory bucket cache | 60-s poll (server `next_poll_after`) | P1 (today), P4, P5, P6 |
| regret series | run artifact (static once the run completes) | loaded once per run | P2 |
| exact shift answers | `analysis/ope/` job, single-core, queued | on demand; 202 + 2-s polls | P3 |
| rollout gate state | tail cache (gate windows: W = 1,000 decisions per side) | 60-s poll | P5 |

The 60-s poll is a pinned constant, not a default: it equals the structural staleness of the tail cache, so the header's "42 s behind tail" (③) is the honest number the reader is shown, and `next_poll_after` lets the sidecar stretch the cadence when the tail is quiet (labelled requirement, not measurement; the stretch is an optimisation, the 60-s floor is the contract).

### 3.3 The remaining panels' contracts

**P5 rollout — `GET /api/v1/rollout/current`** — ADR-0013's payload to #16, shaped into JSON (per stage: share, windows formed, D-trace with live Y and the null σ it came from, per-member gate verdicts, suppression holds/refires, kill-switch state; "the Y display is load-bearing"):

```json
{ "active": true, "ladder": ["shadow","1%","5%","25%","50%","100%"], "stage": "25%",
  "policies": [ { "policy_id": "4c77…d19", "label": "v1.5.0", "share": 0.25 },
                { "policy_id": "9f31…c4a2", "label": "v1.4.0", "share": 0.75 } ],
  "gate": { "window_decisions": 1000, "windows_formed": 348,
    "margin": { "D_c_1k": 180.4, "Y_c_1k": 5262.0, "sigma_null_c_1k": 1754.0, "breaches": 0, "d_trace": [ … ] },
    "routability": { "delta_pts": 0.8, "threshold_pts": 5.0 },
    "coverage": { "ok": true, "cumulative": 0.97 },
    "replay_pass": true, "invariants": { "dbl": 0, "I1": 0 },
    "promotion": { "stage_mean_D_c_1k": 240.9, "se_c_1k": 61.2, "ok": true } },
  "suppression": { "holds": 1, "refires": 0, "last": { "arm": "charlie", "at": "2026-09-19T05:58:03Z", "reason": "canary-arming" } },
  "kill_switch": { "state": "armed" }
}
```

(`kill_switch.state` is `"armed"` or `"rolled_back"` with `{ "to": "<policy_id>", "seq": n }`. Y = 3σ is displayed with its σ, per ADR-0013: a gate whose threshold is invisible invites someone to tune it per deploy.)

**P6 engine/store health — `GET /api/v1/engine/health`** — ADR-0012's named metrics (WAL peak, stopped-short checkpoints, the R93 dropped-conflict count, R72's `session_unattributed` — "metrics worth a panel"):

```json
{ "tail": { "lag_s": 42, "last_read_ms": 310, "rows_read": 803112 },
  "store": { "wal_peak_mb": 0.4, "checkpoints_stopped_short": 0, "dropped_conflicts": 3, "session_unattributed": 0 },
  "partitions": { "hot": "trace-2026-09-19.sqlite", "last_seal": "2026-09-18T00:00:00Z", "last_export": "2026-09-18T00:41:00Z" },
  "ope": { "pi0_tables_sealed_through": "2026-09-18T00:00:00Z", "targets": ["alpha","bravo","charlie","delta","echo","foxtrot"],
           "rho_grid": [0.05,0.10,0.15,0.20,0.30,0.40,0.50] } }
```

**Envelope.** Every response carries `as_of` (when the source rows became visible), `source` (`sealed-parquet` | `tail-cache` | `run-artifact` | `job`), and `next_poll_after` where polling applies. The run-identity block (world hash, policy_id, schema version, roster) comes from `GET /api/v1/meta` and is displayed in the header (②). Time is epoch seconds on the wire, `tz` named, rendering local — the ADRs are UTC-end to end; the browser is the only local-time surface.

---

## 4. Counterfactual UX (consideration 4)

### 4.1 The interaction

Controls: **target** (a select over the roster, default the current highest-share acquirer), **ρ slider** 1%–100% in 5% steps with the grid positions (0.05…0.50) marked as snaps, **window** (presets 1d/3d/7d + custom `t0`/`t1`).

1. **Scrub → instant preview.** On slider input, the panel reads the grid: if the ρ is within 2.5 pts of a grid point *for that target and window*, the preview card renders the **full answer contract** (Δ + CI95 + support + verdict, §1.3) with `est_grade: "grid"` and a "grid" chip. This is fast because it is what ADR-0014 decision 5 built: the per-target π₀ table is computed at seal, "one quadrature pass serves the whole ρ grid — weights are arithmetic in ρ once π₀ is known, so a pane refresh is a sum."
2. **Off-grid → honest gap, no fake number.** A ρ between grid points, or a custom window the grid doesn't cover, renders *"off-grid — run exact"* and **no estimate at all** (R114: no point without a verdict; there is no verdict until a computation runs).
3. **Release / "run exact" → async job.** Debounced (600 ms) or explicit. `POST /counterfactual/jobs` → 202 → the card switches to a job state: progress bar, ETA, "the page stays interactive", 2-s polls. On `done`, the result card renders with `est_grade: "exact"` and *supersedes* the preview (exact is the same estimator, untruncated by the grid snap).
4. **Standing copy (pinned from ADR-0014, displayed under every answer):** *"Had we forced {ρ} onto {target} in this window, holding everything else the log froze (frozen-trajectory overlay). Does not include the re-learning composite — measured divergence −9.8 ¢/1k over 7 days [O2]; the ADR-0013 canary prices that."* The estimand is the UI's most important sentence: an overlay answer that reads as "if we switched 20% tomorrow" is a decision-support failure.

### 4.2 Response time targets (the ticket's question, answered)

| path | target | basis |
| --- | --- | --- |
| grid preview, end-to-end | ≤ 300 ms p95 | labelled requirement — JSON lookup over the seal-time sum; no compute at request time |
| exact, 7-day window, ρ ≤ 0.20 | ≤ 60 s p50, delivered as a job with ETA | measured class: ≈ 43 s cold (8%-share target), ≈ 49 s warm end-to-end, single core [O1](3), ADR-0014 §4 |
| exact, any registered ρ, 7-day window | ≤ 10 min worst case | measured worst class ≈ 408 s [O1](3)/ADR-0014 Consequences |

ETA on the job is a model, stated as one: the mixture identity (ADR-0014 [O1](3)) makes the query cost track the target's head share, not the window, so the sidecar interpolates between the two measured anchor points (43 s at the small-share class, 408 s at the worst). The job queue is **serial per shard** (labelled choice): the quadrature bottleneck is single-core (809 decisions/s [O6]), so two concurrent exact jobs share one core and both lie about their ETAs; the queue shows position instead. A human dragging a slider never waits for exact — that is what the preview tier is for — so the serial queue's user-visible cost is "the second analyst waits ~1 min", which is the price of an honest ETA.

### 4.3 What an uninformative estimate looks like (the ticket's second question)

Three states, all rendered with the number's position in the card — and none of them silent:

1. **REFUSED — no estimate exists.** The support gate (≥ 1,000 logged head-target rows in the window, ADR-0014 decision 4) runs before any estimator. The card renders **only** the pinned copy: *"Insufficient logged support for this shift over this window: {N} logged decisions headed {target} (gate: ≥ 1,000)."* No Δ, no CI, no extrapolated point — "a refused query is reported refused, never extrapolated." The card suggests the two moves that change the gate: widen the window (the 400-day retention makes this usually possible) or pick a higher-share target. The slider keeps working.
2. **PASS but uninformative — the wide CI.** The CI is a sampling statement about the clipped-IPS functional (ADR-0014 is explicit that it is *not* posterior coverage of truth), and the panel shows it next to the support diagnostics that price the missing mass it sits beside: `head_rows`, `ess_frac`, `Ē[w̃]`. The rule that makes "uninformative" visible is mechanical: **CI width > |Δ| ⇒ a low-informativeness badge** ("this band is wider than the effect it wraps"), the CI band drawn as the dominant visual (a ribbon the point sits inside), and the support diagnostics promoted to the same type size as the number itself. The measured data discriminates exactly as intended, on measured numbers: the headline-class Δ (−1,729.7 ¢/1k) is wider than the measured CI width of that query class (946 ¢/1k — width < |Δ|, no badge), while the foxtrot-class small-signal Δ (−226.7 ¢/1k) is narrower than that same width, so a CI of the measured class's order badges yes — the small-signal regime is precisely where the badge exists to be read. ESS is shown in human terms — `ESS = n_window × ess_frac` (arithmetic), "this window behaves like ~{ESS} independent logged rows for this shift" — because `ess_frac 0.005` alone is a number no one can act on.
3. **FAIL — the window isn't admissible.** On live runs FAIL means the computation refused to certify: the partition is not `strict` (a `batched`/`ephemeral` partition "is not admissible as production evidence", ADR-0012), or the seal-time π₀ table for the target is missing from the window (seal job pending). Red badge, reason text, and — for the missing-table case — the seal status from P6 so the reader sees *when* it will be runnable. (In simulation, FAIL is the replicate gate failing the estimator on a known world — the validation story, which ADR-0014's §4 owns; the panel just renders the verdict it is given.)

Standing rendering law (R114, the payload ADR-0014 handed #16, adopted verbatim): **no point without its verdict; no SNIPS without Ē[w̃]** — and v1 doesn't ship the second clause a single time, because the sidecar does not expose SNIPS at all (ADR-0014 prints it, doesn't ship it).

---

## 5. Tech decisions (consideration 5)

### 5.1 Charting — Recharts

The candidate set the ticket names: Recharts, Victory, Nivo, Observable Plot, raw D3. The choice is made on **contract surface, not speed**, because §3's pre-aggregation removes the speed argument from the table: every chart receives ≤ 1,440 points (1h: 360, 24h: 1,440, 7d: 168 — pinned arithmetic, not a fabricated benchmark), at which count every candidate library renders comfortably, and the reopen trigger is a *measured* render cost, not a feared one.

- **Recharts wins** on the two things these panels are: (i) *declarative React components* — the data contracts in §1–§3 are JSON in, SVG out, with the component tree as the whole implementation surface; (ii) *the two idioms these panels need out of the box*: `syncId` hover-locking (P1 money ↔ latency strip, the design's core ergonomic move) and `ComposedChart` composition (P3's Δ-line + CI-ribbon + verdict markers). Both are standard usage, not plugin surgery.
- **Nivo** — rejected: split canvas/visx rendering model and a weaker story for custom reference-line + band + marker composition in one chart; its accessibility strengths are real and are matched here by the §5.5 rules instead.
- **Victory** — rejected: the imperative chart-ref API is the wrong shape for a TanStack-Query-fed dashboard (the component fights the cache lifecycle), and its maintenance cadence lags the others.
- **Observable Plot** — rejected for this role: excellent one-off exploratory charts, but it is not a component library — embedding it under React state, syncing two charts, and carrying verdict badges means wrapping most of what Recharts already is. (It remains the right tool for `spikes/`, where the repo's analysis already lives.)
- **Raw D3** — rejected: maximum control in exchange for a hand-maintained chart layer (scales, axes, tooltips, resize, a11y) that no panel in this inventory requires at ≤ 1,440 points. The house rule is closed vocabularies and pinned constants with no untraceable surface; a hand-rolled chart layer is a standing liability with no panel demanding its benefits. It reopens per Reopen trigger 3 if the bucket pins are raised.

### 5.2 State — TanStack Query + local React state

Server state is 90% of the state (eight endpoints, 60-s polling, one 2-s job poll, cache invalidation on seal), and TanStack Query owns exactly that: polling via `refetchInterval`/`next_poll_after`, the job poll as a keyed query, stale-while-revalidate on window switch. UI state — window, target, ρ, panel toggles, tooltip pinning — is plain React state in the tree that uses it.

**Zustand is rejected for v1** as a second cache: it would duplicate what the query cache already holds and desync from it (two sources of truth for the same panel data is the failure mode the query library exists to prevent). The reopen trigger is concrete: when two panels must share a selection (the first candidate is a cross-panel processor filter — click a P4 tile, P1 splits by it), a shared store earns its keep, and until then it is surface to maintain.

### 5.3 Backend — the analysis sidecar, no Next.js

The sidecar is FastAPI + uvicorn, single process: static SPA + `/api/v1` + the OPE job queue (in-process, serial per shard, §4.2). DuckDB reads the sealed Parquet tier (ADR-0011's own flow: "traces flow out as SQLite the engine writes and DuckDB reads"); the tail read is stdlib sqlite3 against the open partition with the bounded-query shape ADR-0012 indexed for.

**Next.js API routes are rejected:** the API must be Python — the OPE layer, the columnar tier, and the `analysis/` home are all Python, and that is law, not preference (ADR-0011 decision 5). Next.js API routes would therefore proxy to the Python sidecar anyway; the Node tier would be a hop that does no work, plus a second deploy unit and a second language for the same JSON. The dashboard has no SSR or SEO need (internal ops surface, authenticated boundary), so the remaining Next.js argument — server components — has no purchase. It reopens per Reopen trigger 6 if the org adopts a React SSR platform for the whole product.

### 5.4 Component tree (the implementation ticket's file layout)

```
<DashboardPage>                              // owns: window (1h/24h/7d), run identity
├── <HeaderBar>                              // ①②③ — world/policy/schema, window switch, staleness line
│   └── <WindowSwitch>
├── <HealthStrip>                            // P4 — roster from /processors/health
│   └── <ProcessorTile> ×6                   // light+tier, auth rate + n, p99/deadline, share, <Sparkline>
├── <MoneyPanel>                             // P1a+P1b
│   ├── <KpiChips>                           // margin ¢/1k, cost ¢/1k, net, volume, authorized %, attempts
│   ├── <MoneySeriesChart>                   // margin + cost, one axis, shared x
│   └── <LatencyStripChart>                  // p50/p95/p99 + deadline ref, syncId-locked
├── <CounterfactualPanel>                    // P3
│   ├── <ShiftControls>                      // target select, ρ slider (grid snaps), window picker
│   ├── <EstimateCard>                       // Δ + CI + verdict badge + est_grade chip + pinned estimand copy
│   ├── <SupportDiagnostics>                 // head_rows, ESS, Ē[w̃]; same type size as the number when badge on
│   ├── <GridRibbonChart>                    // Δ(ρ) over the grid + CI ribbon + PASS zone
│   └── <JobStatus>                          // queued/running progress + ETA + queue position; done → EstimateCard
├── <RegretPanel>                            // P2 — sim-gated (R115)
│   ├── <RegretChart>                        // cumulative line (or per-1k-normalised toggle)
│   ├── <ArmBreakdownChart>                  // per-arm missed-opportunity stack (all-arms recording)
│   └── <UnavailableCard>                    // live runs: the pinned reason + pointer to P5
├── <RolloutPanel>                           // P5
│   ├── <StageLadder>                        // shadow → 1% → 5% → 25% → 50% → 100%, current marked
│   ├── <DTraceChart>                        // D with Y and its σ_null shown (load-bearing, ADR-0013)
│   ├── <GateVerdicts>                       // margin / routability / coverage / promotion / replay / dbl, I1
│   ├── <SuppressionState>
│   └── <KillSwitchState>                    // armed | rolled_back <policy_id> @ <seq>
└── <EngineHealthBar>                        // P6 — one line; full panel only if the line is crowded
```

### 5.5 Accessibility and export (pinned, not decorative)

Colour is never the only channel anywhere (traffic lights carry tier text; verdicts carry words; series carry labels in tooltips and a legend). Every chart has a data-table view toggle — the repo's evidence culture ships numbers into ADRs, and a panel whose numbers cannot be copied is not evidence. Contrast ratios are the WCAG AA floor as a labelled requirement (the palette in §5.6 is checked at implementation time, not assumed).

### 5.6 Theme tokens (pinned from the brand image)

The palette is expressed as semantic tokens only. The brand image referenced for this ticket — ghost-pale translucent mushrooms, icy blue-white, on a deep dark moss-green field with rare rust-brown specks (Yaga Maksi) — is a dark scheme, so the dark set is primary; a light set for print is derivable from the same semantic names if a reader asks for it. The mapping is the design and is stable: moss is the field and the healthy state, ghost-ice is ink and the money series, the droplet blue is latency, and rust — the image's one warm exception — is the alert family (warn/crit) and regret:

```css
--sb-bg: #0e130d;        /* deepest moss — page background */
--sb-panel: #151c13;     /* moss, one step up — panel surfaces */
--sb-ink: #dfe8e2;       /* ghost-white (the mushroom) — primary text */
--sb-ink-dim: #8fa093;   /* dim moss-grey — secondary text */
--sb-grid: #232d20;      /* chart gridlines */
--sb-margin: #b9cdd6;    /* P1 margin earned — the cap's ice */
--sb-cost: #6f7d5e;      /* P1 cost — moss olive */
--sb-latency-p99: #7fa6b8;  /* water-droplet blue */
--sb-latency-p50: #4e6a75;
--sb-deadline: #46523f;  /* dashed reference line — dim moss */
--sb-regret: #a5623c;    /* the rust speck — the one warm exception */
--sb-status-ok: #8fb573;   /* P4 traffic-light dots: moss green */
--sb-status-warn: #c98a52; /* rust amber */
--sb-status-crit: #c65b3d; /* ember rust */
--sb-verdict-pass: #8fb573;  --sb-verdict-fail: #c65b3d;  --sb-verdict-refused: #8fa093;
/* P4 sparklines, roster order — six distinct pulls from the same scheme */
--sb-spark-alpha: #e6eeef;  --sb-spark-bravo: #8fb573;  --sb-spark-charlie: #c98a52;
--sb-spark-delta: #b9cdd6;  --sb-spark-echo: #6f7d5e;   --sb-spark-foxtrot: #7fa6b8;
```

House-rule compliance, stated: the hex values are **eyeball extractions from the photograph** — the brand image arrived as a photo, not a spec, so these are approximate tones read from it, pinned so the implementation has a concrete palette and the mapping is reviewable. If the brand side publishes official values, they replace the hexes one-for-one without touching any token name, and the status-dot semantics (moss = ok, rust-amber = warn, ember = crit — §2's traffic-light law, R116) are unchanged either way. The §5.5 contrast floor still applies to this set at implementation time (dark-on-dark pairs — e.g. `--sb-cost` on `--sb-panel` — are the ones to check first).

---

## Payload for dependent tickets

- **Dashboard implementation ticket**: the endpoint contracts of §1.1/§1.2/§1.3/§2/§3.3 (verbatim JSON), the pinned buckets (10 s / 1 min / 1 h), the pinned ρ grid ([0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.50] for the six-target roster; the grid is data in `meta`, so the roster can grow without a UI change), the traffic-light derivation rules (§2 table), the counterfactual state machine (grid preview → off-grid → job → done/failed; §4), the rendering laws (R114), and the component tree (§5.4) as the file layout.
- **#17 (benchmarks)**: three gates this ADR models and cannot measure here. (i) *The tail-read class, priced against the writer*: the §3.1 figure (~0.5% duty at the measured 2.5 M rows/s floor) is a model on ADR-0014's scan rate; benchmark the actual bounded tail read against writer throughput at fleet pace on the production volume, with the 5% ceiling of Reopen trigger 2 as the pass line — `[W10]`'s 66% is the *held* class, not this one, and the distinction is only as good as its measurement. (ii) *The seal-job aggregate pass*: ADR-0014 already budgets the π₀ tables (≈ 46 s/day/shard for the richest class); add the (bucket × processor × outcome-class) count pass + latency quantiles this ADR requires, and certify the seal job end-to-end inside its day-boundary window. (iii) *Job-queue ETA calibration*: the §4.2 linear interpolation between the 43 s and 408 s anchors, checked against measured per-share-class costs on the committed scenario.
- **Engine implementation ticket**: nothing changes — `internal/api/` stays `POST /v1/route` + `POST /v1/outcomes`, and no read surface is added to the engine in v1. That is a consequence worth stating: this design costs the engine zero new surface.
- **ADR-0012 / ADR-0014**: no changes. R94 is honoured as written (the sidecar is the reader class it anticipated; the one live-file read is its carve-out, sized and made falsifiable in §3.1 and Reopen trigger 2). ADR-0014's answer contract is consumed verbatim; the sidecar adds only `est_grade` and `delta_minor` as presentation fields and removes nothing.

## Consequences and design rules that follow

- **Costs accepted, named**: (1) a second long-running process in the deployment — one container, and all of its state is derived and rebuildable (Parquet aggregates from sealed partitions, tail cache from the log, π₀ tables from ADR-0014's seal job), so the failure mode is "dashboard stale", never "engine impaired". (2) 60 s structural staleness on the live tiles — the header says "42 s behind tail" instead of "live"; rollback verification does not run through this panel (the engine's own gates and the kill-switch's sub-second propagation are the mechanism; the dashboard shows the state, it is not the mechanism). (3) a serial exact-job queue — the second analyst waits ~1 min; the preview tier is the design's answer to that, and it is why the preview is the first-class path and exact is a "run analysis" action. (4) a React surface maintained by a Go/Python org — pinned to Recharts + TanStack Query to keep the hand-rolled surface at zero, per §5.1.
- **Rules added** (sequence continues from R111):
  - **R112** — The dashboard reads only the analysis API. It never opens the engine's SQLite or Parquet, and the engine's `/v1` surface stays routing + outcomes. The single live-file reader in the system is the sidecar's bounded tail read (≤ one poll interval of rows, short-lived transaction); any other live-file read is an R94 violation.
  - **R113** — Every time series the browser renders is pre-aggregated (sealed buckets) or from the sidecar's tail cache; the browser never triggers a scan of the trace. Buckets are pinned 10 s / 1 min / 1 h for 1h / 24h / 7d (≤ 1,440 points per chart).
  - **R114** — Counterfactual answers render ADR-0014's answer contract whole: no point without its verdict; no SNIPS without Ē[w̃] (v1 exposes no SNIPS); a REFUSED renders the pinned refusal copy with N and no number; CI width > |Δ| renders the low-informativeness badge with `head_rows`, ESS, and Ē[w̃] at the number's type size.
  - **R115** — The regret panel is sim-gated: it renders only for runs whose world is known (the harness, all-arms recording); live runs get the stated-unavailable card pointing at P5. A non-oracle proxy is never labelled "regret".
  - **R116** — The traffic light maps to ADR-0007's tiers exactly (red = Tier-1 or deadline-sustained p99; amber = Tier-2, R49 onboarding, or p99 ≥ 0.8 × deadline; green otherwise) and always renders tier text + `since` beside the colour. A light without its timestamp is a defect.
  - **R117** — The refresh protocol is polling with server-provided `next_poll_after` (60 s floor); the browser opens no persistent connection in v1, and the header always displays the tail lag instead of a "live" badge.

## Reopen triggers

1. **The live-tile staleness budget drops below 10 s** (a product decision, or a gate whose windows form faster than the poll can show — note W = 1,000 decisions is 0.8 s of fleet traffic, ADR-0013). Then §3's no-WebSocket rule reopens toward push or engine-published aggregates.
2. **The tail read stops being the bounded class.** If the #17 payload's gate (i) measures it at > 5% of writer throughput, or any R94 pinning signal appears on the sidecar's connection (WAL peak > 10× autocheckpoint target, or stopped-short checkpoints > 60 s sustained), the carve-out reopens to a dedicated replica tier. The design has already priced the failure of the other class (`[W10]`: 66% of throughput, 119 MB/s of pinned WAL); this trigger exists so the tail class can never silently become that class.
3. **The bucket pins break.** A demanded granularity whose chart exceeds 5,000 points, or a *measured* Recharts render above 500 ms (the 5,000-point threshold is a labelled model, not a measurement — Recharts' SVG path has no published per-point cost this ADR can cite, so the trigger is the measurement, and the measurement is the point of #17). Then §5.1 reopens toward canvas (Nivo) or D3.
4. **ADR-0014's contract moves.** Any superseding ADR to the shift interface, the answer contract, or the verdict enum reopens P3 by construction — R114 binds the panel to the contract verbatim so the panel cannot silently render a stale shape.
5. **A production regret proxy is requested and accepted.** Best-arm-in-data or any other substitute that survives the ADR-0008 bias pricing reopens R115's sim-only gate. Until then the production comparison remains P5's D-trace.
6. **The engine grows a cold read surface** (a superseding ADR to ADR-0011's frozen `/v1`). Then the sidecar reopens toward a thin proxy over the engine API — the read classes and cadence of §3 survive; only the owner of the HTTP surface changes.
7. **Viewer fanout.** If concurrent dashboard sessions per shard grow such that per-viewer tail-read cost (the sidecar caches, so this is per-shard, not per-viewer — the trigger is the cache-miss case: many shards, few viewers each) approaches trigger 2's budget, the cache shape reopens (per-shard sidecar vs fleet-sidecar with a replica tier).

## Alternatives considered

- **Browser-side trace reads (DuckDB-WASM over Parquet in the page).** *Steelman:* zero extra service; DuckDB in the browser is real; at the committed scenario's scale a 7-day Parquet is a few MB and loads fine. *Rejected:* at fleet pace a fleet-day's Parquet is ≈ 205 GB / 21 ≈ 10 GB (ADR-0012's row-store bytes over the measured 21× export ratio — labelled arithmetic), and 7 days is ≈ 70 GB, which no page serves; even the scenario-scale case makes the *browser* the long-read holder over data it fetched, i.e. R94's pin hazard re-created client-side with no owner to bound it; and the sidecar is not an extra service — `analysis/` already exists, already runs the OPE the dashboard wraps, and already owns the columnar tier (R96). The browser gets JSON, and the read classes stay owned by one process.
- **Engine `/v1` read endpoints (the engine exposes the API).** *Steelman:* one binary, one auth boundary, the store's owner answers queries about the store; the dashboard skips a hop. *Rejected:* ADR-0011 fixed `api/` to routing + outcomes, and the read side of this design is Python (OPE, DuckDB, `analysis/`) — engine-side read endpoints would either embed the scans in the hot binary (putting cold-path work in the process that owns the 2 ms lease path and the R109 separation in reverse) or shell out to Python (exactly the seam ADR-0011 decision 5 closed). The sidecar adds zero surface to the engine, which is the cheapest possible version of "the engine exposes the API" that does not cross a fixed boundary.
- **WebSocket push for the live tiles.** *Steelman:* ops dashboards are expected to be live. *Rejected:* there is no stream to subscribe to — the sealed tier is batch and the tail is a bounded snapshot, so "real-time" is structurally 60 s stale either way, and WS would add stateful connections, reconnection, and fanout to deliver the same bytes. The slowest displayed signal moves per gate window (0.8 s of fleet traffic, ADR-0013); 60 s resolves it with 60× margin. Reopens per Reopen trigger 1, where the falsifying budget is stated.
- **Next.js API routes as the backend.** *Steelman:* one deploy, React-idiomatic, API routes next to the components they serve. *Rejected:* the API is Python by law (ADR-0011 decision 5: `analysis/` owns OPE and the cold path), so the Node routes would proxy to the Python sidecar — a hop that does no work, a second deploy unit, and a second language for the same JSON — while the only feature Next.js adds (SSR) has no purchase on an authenticated internal ops surface. See Reopen trigger 6 for the reopen.
- **Zustand as the state layer.** *Steelman:* one store, one mental model, selections shared freely. *Rejected:* server state is 90% of the state and TanStack Query already owns it (polling, job poll, invalidation on seal); a second store duplicates the cache and desyncs from it. The cross-panel-selection trigger is named (§5.2, Reopen triggers) instead of the store being built speculatively.
- **Three y-axes, or a 0–1 normalised overlay, for P1.** *Steelman:* one chart, three trends, less vertical space. *Rejected on three independent grounds* in §1.1 — the reward has no per-millisecond term (a shared axis depicts a trade that doesn't exist), the deadline question needs the absolute ms scale (normalisation hides "897 vs 900"), and three-axis charts are the canonical readability failure. The hover-locked small multiple keeps the only property a third axis provided: temporal alignment.
- **Production regret via best-arm-in-data.** *Steelman:* something is better than an unavailable card; the data contains the arms' realized outcomes. *Rejected:* best-arm-in-data is winner-biased — it credits arms the logging policy happened to observe, which is the bias ADR-0008 (censoring) and ADR-0014 (the weights) exist to price — and a number that large and unpriced, labelled "regret", would be the one figure in this dashboard an auditor could not trace. The honest production comparison already exists and is on the page: P5's D-trace. Reopen trigger 5 is the only path back.
- **Synchronous exact counterfactual (one `fetch`, 60-s timeout, spinner).** *Steelman:* simpler state machine — one request, one response. *Rejected:* the measured cost class is 43–408 s, single core; a blocking call sits at the edge of fetch/HTTP timeout budgets, shows no progress, dies with the connection, and serializes the tab against the rest of the page. The 202 + poll job state is ~10 lines more component and buys progress, ETA, queue position, and a page that stays interactive — which is the difference between an "analysis" and a "hang".
- **Pre-aggregation in the engine (fold tallies emit the buckets).** *Steelman:* the engine already folds every row; time-bucketed aggregates are just more tallies, and the hot process could publish them for free. *Rejected:* the fold's state is the 135 KiB posterior array + derived integers (ADR-0012) — a running summary, not a time-indexed series — and emitting (bucket × processor × outcome-class) series would put an analysis-tier query shape into the hot path, which is the R109 separation in reverse and would make the writer pay for every panel a reader might build. The seal job pays it once, offline, in the tier that owns cold reads.
