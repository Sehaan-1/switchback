# Spike: drift detection — ADWIN vs Two-Window KL

`drift.py` is the empirical evidence for #8: choosing the primary drift detection mechanism
(ADWIN vs two-window KL divergence), the reset strategy when drift occurs (full reset vs
partial decay vs the onboarding exploration floor), alarm management (false alarm rates,
distinguishing abrupt outages from gradual degradation), and the integration protocol with
the Thompson sampling bandit established in ADR-0003 and ADR-0006.

It executes against the committed, content-addressed scenarios through the ADR-0005 harness
(`baseline-steady-v1`, `outage-recovery-v1`, `black-friday-degraded-v1`), ensuring every claim
is cited against an immutable scenario hash rather than an invented test case.

```bash
python3 drift.py 60000                 # ~3 min, regenerates the committed RESULTS.md
python3 drift.py 1000 --section=D1     # ~1s smoke of section D1
```

Five core findings this spike proves:

- **ADWIN provides distribution-free false alarm bounds under stationarity.** At $\delta = 10^{-3}$,
  ADWIN achieves 0 false alarms across 60,000 stationary transactions on `baseline-steady-v1`
  while using $< 1.6$ KB per acquirer ($O(\log W)$ exponential histogram buckets). Two-Window
  KL has no theoretical bound: setting $\tau \le 0.05$ causes 55–412 false alarms from normal
  Bernoulli variance, while setting $\tau \ge 0.10$ makes it blind to gradual drift.
- **Two-Window KL fails on gradual degradation.** Under a 90-minute overload ramp (delta in
  `black-friday-degraded-v1`), Two-Window KL with $\tau = 0.10$ produces 0 detections because
  the reference window and recent window drift together, masking the 14% drop. ADWIN catches
  the degradation cleanly during the ramp with 0 false alarms on other arms.
- **The dilution finding is structural: detection must run at the processor level.** In the
  fine arm space (4,320 arms), an acquirer outage lasting 600 seconds routes only 10–15 attempts
  spread across 10+ distinct fine arms (maximum 1–2 attempts per arm). Running drift detectors
  per-arm starves the window, detecting nothing (0 alarms). Aggregating at the processor level
  concentrates all attempts into one stream, detecting the outage within 12 attempts.
- **Resets require partial decay plus the R49 onboarding floor.** A full reset to prior wipes out
  learned context knowledge and destabilizes estimation error. Partial decay ($\gamma = 0.2$)
  preserves relative arm ranking while multiplying posterior variance to encourage exploration.
  Coupled with ADR-0006's R49 onboarding floor ($\eta = 0.05, n_{min} = 1000$), the router actively
  probes the recovering acquirer, eliminating post-outage re-entry hysteresis.
- **Alarms must be tiered using ADR-0006's transport error counter.** Outages with transport
  errors (`connection_refused`) trigger immediate Tier 1 critical alerts and aggressive decay
  ($\gamma = 0.1$) via the `te` fast-path; gradual rate shifts without transport failures trigger
  Tier 2 warning alerts and moderate decay ($\gamma = 0.5$).

Magnitudes belong to the committed scenarios at $n = 60,000$. Orderings, mechanisms, and
mathematical bounds are the normative architectural claims.
