# Spike: why a bandit, and why Beta-Bernoulli Thompson sampling

`bandit.py` answers #3's two questions with one paired simulation: (1) why a multi-armed
bandit instead of a static weight table, and (2) within bandits, why Thompson sampling
over UCB, epsilon-greedy, and EXP3. Read the module docstring first: it states which
policies exist and which question each one answers.

```bash
python3 bandit.py 40000    # ~10s, stdlib only, no network, fixed seed
```

## What this is designed to catch

- **The variance a table cannot hold.** [V1] quantifies the true auth-rate spread across
  BIN class, region/currency, and time-of-day, plus the two acquirer events (an outage at
  the margin workhorse, an improvement at the budget processor). A static table is a point
  estimate; the world is this wide.
- **The adaptation gap.** [F1] measures the full-run and post-drift margin of every policy
  against Thompson sampling. The static tables (including the latency-aware one ADR-0002
  required) and the clock-based schemes (A/B rebalance) pay no exploration tax and then
  miss the outage; the bandit pays a small tax and earns it back.
- **The re-discovery gap.** [F3] shows that *every* online learner is slow to re-discover
  a challenger that improved, because its posterior is confident from pre-event data.
  This is the measured reason the architecture is "TS + drift detection" (#8), not bare
  TS — and the reason "deterministic argmax for production" is rejected: it has no
  exploration and no uncertainty to trigger a reset.

## Not in scope here

The reward is held fixed to ADR-0002's chosen form (two-part score, timeout excluded from
the auth posterior and priced at `lambda_to = 45`); the 3DS channel is held out because it
multiplies the same number every algorithm would see. Both belong to #4/#11, not to the
algorithm-family question. `RESULTS.md` is generated output — regenerate it, do not edit it.
Magnitudes belong to `synthetic-fleet-v1`; orderings and gaps are the findings.
