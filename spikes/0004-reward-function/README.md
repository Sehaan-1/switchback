# Spike: does the reward function need a 3DS term, a latency term, a retry term?

`fleet.py` runs a paired, deterministic simulation of two synthetic acquirer fleets and
ablates each candidate reward term against the design ADR-0002 picks. Read the module
docstring first: it states which variants exist, why the estimator-vs-scoring distinction
matters, and why the second fleet exists at all.

```bash
python3 fleet.py 60000     # ~95s, stdlib only, no network, fixed seed
```

Three things this is designed to catch, because they are the ones that survive review:

- **Double counting.** If the label already contains an effect (an abandoned challenge is a
  lost sale), the same effect as a multiplier on top is invisible in aggregate and wrong in
  the tail. The `+3DS multiplier` variant exists to show its sign flips between fleets.
- **Estimand drift.** Censoring "unattributable" rows doesn't make an estimate conservative,
  it changes what is estimated. Measured as |p̂ − p_true|, which margin alone cannot see.
- **Reward-only benchmarks selecting bad designs.** Counting ambiguous timeouts as declines
  is the most profitable ablation here and a poor estimator. So the MAE column ships next to
  the margin column, and #17 inherits that pairing.

Magnitudes belong to `synthetic-fleet-v1`. Orderings and sign flips are the findings.
