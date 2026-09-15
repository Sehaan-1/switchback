# Spike: what does implementing ADR-0003's Thompson sampler actually take?

`posterior.py` is the implementation-side evidence for #7: the arm key and its granularity,
the prior that seeds it, the update protocol (decay, the transport-error label, at-least-once
delivery), the Beta draw and its discipline inside `Decide()`, the concurrency protocol, the
cold-start floor, and the persistence contract. It runs on the committed scenarios through the
ADR-0005 harness (the spike-local `long-refused-v1` scenario document sits next to it, gated
through `load_scenario`). Read the module docstring first: it states the P1–P7 map and the
coupled-determinism discipline (the world and the policy are both key-addressed, so any run
replays bit for bit).

```bash
python3 posterior.py 60000                 # ~30 min, the committed RESULTS.md
python3 posterior.py 5000 --section=P6     # ~12s smoke of one section
```

Five things this is designed to catch, because they are the ones that survive review:

- **A cheap approximation that "wins" margin by starving.** The normal-approximation Beta draw
  fails χ² exactly on cold-arm shapes, and at the policy level it *out-margins* the exact draw
  while touching 359 fewer arms. Every margin number here ships next to MAE and arms-touched;
  #17 inherits the pairing.
- **Priors that pay in the wrong place, or cost in the wrong direction.** The informative
  prior's value is concentrated in the cold window (+1,539 c/1k over Jeffreys), strength past
  the cold-window volume is a defect, and pessimistic miscalibration costs more than optimistic
  at equal error (TS under-explores what it believes is confidently bad).
- **Labels that change the estimand.** A transport error labelled as ambiguity or excluded
  from learning changes *what is estimated*, not just the estimate: measured against the world
  (not just margin) on a six-hour refusal event, `te → β` wins the outage window by +544 c/1k
  and pays a named re-entry hysteresis — the honest trade #8 inherits.
- **"The protocol works" claims that only hold at one n.** The threaded sharded-writer
  protocol is exercised at 100k ops (0 lost, bit-exact fold, 0 mid-pair reads), and ingest
  staleness is priced as a decision cost: batching in the tens is free, in the thousands it
  eats the cold window, at 8,192 it is a different router (−2,123 c/1k, −9 auth pts).
- **A second source of truth for learned state.** The outcome event is written once and is
  both the trace row and the WAL of the posterior; `fold(WAL) == live state` bit-exact, a
  snapshot at 50% replays the run's tail decisions with 0 mismatches, and re-bucketing the arm
  space is a re-fold of the same log (measured: re-fold 4.65 MAE vs 6.03 cold).

Magnitudes belong to `baseline-steady-v1` / `outage-recovery-v1` / `long-refused-v1` and the
committed catalog at n = 60,000. Orderings, mechanisms, and the bit-exact protocol checks are
the findings. CPython timings are projected to Go on ADR-0001's labelled 10/30/100× band.
