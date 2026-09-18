# Spike: safe policy rollout

`rollout.py` is the empirical evidence for #14: the Stripe-style design question of
how to deploy a new routing policy without losing money for a week. A naive 100%
cutover routes live traffic through whatever the new policy does first; a 0% cutover
never ships. The ticket asks for four things: the rollout protocol (how long shadow,
the canary ramp, the abort criterion), the warm-start strategy, the policy
identity/version schema, and the automated rollback kill-switch.

The spike's frame is that the scary premise is mostly already fixed by earlier ADRs,
and what remains is measured, not assumed:

- **Warm start is inheritance, not approximation.** The fold over the WAL is
  policy-independent learned state (ADR-0006/0012); a candidate boots from the
  incumbent's newest snapshot and is warm at boot. [S1] prices the only case that
  can still hurt: a migration that loses the store.
- **Shadow is free but cannot clear margin.** A candidate folding the shared WAL
  (ADR-0008 C1's MAR argument) pays zero revenue risk and buys zero revenue
  evidence — its decisions are never executed. [S2] measures what shadow CAN clear
  (validity, plumbing, free warm-up) and how long it must run (a validation window,
  not a warm-up wait).
- **The canary gate is a SET, not one test.** [S3] measures the null distribution
  of the paired margin window (Y = 3σ of the live null, not a constant), then runs
  five regressions through the gate: the one the ticket fears (excluded processor)
  is caught by the routability member that the margin window is blind to; the
  rejected sampler of ADR-0006 walks past margin and coverage at a single stage —
  the honest boundary of any exposure-limited gate.
- **Rollout vs the drift detector.** ADR-0007 handed #14 a payload: share shifts
  must not false-trigger drift alarms. [S4] measures that they don't (δ=1e-3 does
  not trip on share shifts at processor level), prices suppression when the alarm
  is TRUE (the arming window delays, the re-check fires), and shows a real outage
  differences out of the paired window — both sides face the same world.
- **End to end on the improvement world.** [S5] runs the whole protocol
  (shadow → 1 → 5 → 25 → 50 → 100%, gate armed at every stage) against naive
  cold/warm/refresh cutovers, never-deploy, and an oracle, on
  `quiet-improvement-starved-v1` — the rollout the ticket fears, in the direction
  it hopes for.

It executes against the committed, content-addressed scenarios (`baseline-steady-v1`,
`outage-recovery-v1`, `quiet-improvement-starved-v1` from spike 0009) via the
ADR-0005 harness and the spike-0007/0008/0009 fold, detector, and censoring modules.

```bash
python3 rollout.py 60000                 # ~2.5 min, regenerates the committed RESULTS.md
python3 rollout.py 60000 --section=S3    # ~55 s, one section
python3 rollout.py 8000 --section=S0     # smoke
```

Findings:

- **[S1] A warm no-op cutover is free; a cold one costs 324 c/1k in its first 10k
  transactions** on a deploy with nothing wrong except missing state (same
  algorithm, same artifact). The warm-start strategy is therefore not a nice-to-have:
  it is the difference the ticket's premise turns on. The unit is transactions
  (10k = 1.2 days at the committed scenario's pace, 2 s at the 5,000 dps fleet
  budget).
- **[S2] Shadow ≥ 24 h of fleet traffic AND ≥ 50k decisions AND replay PASS AND
  the ADR-0009 gates** — a validation window over traffic diversity, not a learning
  wait. A warm-started shadow is at its decision-agreement plateau (75.9%) from the
  first window; a cold one claws up 59.9 → ~73% over 20k transactions and is the
  symptom of an R102 violation, not a stage to normalize. Cost while shadowing:
  13.0 GB/day at 5,000 dps (+29% on the DECISION_LOG row), a 135 KiB fold.
- **[S3] The gate: abort on 2 consecutive paired margin gate-windows < −Y or 1 <
  −1.5Y; Y = 3σ of the live null (1,869 c/1k routed at W=4,000/side here); plus
  routability, coverage, and the replay/dbl=0/I1=0 gates.** Pooled two-seed null:
  σ 1,754 c/1k at W=1,000 → 623 at W=4,000, zero breaches. The excluded-processor
  regression auto-aborts in 11,762 transactions at 25% share (routability +12.1
  pts; margin alone would MISS it — residual routing is only ~750 c/1k worse per
  routed decision). The ADR-0006 rejected sampler walks past a single 25% stage
  (coverage inside the 0.897–0.923 null band); accepted residual risk, checked
  cumulatively across stages. Below-Y regressions (λ_to 45→10: −1,305 c/1k) don't
  abort, they fail to promote. The same bad deploy gated vs not: 27,062.9 vs
  22,495.8 c/1k — the gate prevents, it does not repair.
- **[S4] Share shifts do not trip the drift detector; suppression is insurance
  sized for reshapes, and its arming window must stay short.** On the starved
  improvement world the true improvement alarm lands inside the 256-settled arming
  window (deploy and recovery overlap); the re-check fires it 4,485 transactions
  later — the alarm is late, not lost. During a real outage the Tier-1 fast path
  fires unsuppressed in 349 s and the paired margin window differences the world
  event out (mean −68 c/1k over 4 gate-windows against Y=1,869; 0 false aborts).
- **[S5] On a GOOD deploy the protocol is roughly free: 240.8 c/1k premium against
  an instant cutover of the same candidate** over the deploy window (5.8 s of wall
  clock at fleet pace), while the gate rode through a −5,041 c/1k window (vs
  Y=5,263) without a false abort. The refreshed artifact moves the direction
  (foxtrot share 15.3% vs 3.1% bare) but captures a fraction of the oracle headroom
  — artifact quality is the prior-artifact path's problem (ADR-0006 §2); the protocol's job was to ship it safely.

Magnitudes belong to the committed scenarios at n = 60,000 (incumbent policy seed
20260916, candidate 20260918, rollout assignment seed 20260919). Orderings,
mechanisms, and the gate arguments are the normative claims.
