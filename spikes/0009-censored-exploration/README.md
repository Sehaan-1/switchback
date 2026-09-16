# Spike: censored data and exploration-exploitation

`censored.py` is the empirical evidence for #9: the Stripe-style design question of
how to avoid learning on censored data. You only ever observe outcomes for the arm
you chose; the counterfactual ("what would the competitor have done with this
transaction?") is structurally absent. The ticket asks for the exploration strategy,
the forced-exploration mechanism (if any), the propensity logging schema, and the
delayed-outcome handling protocol.

ADR-0002 fixed the estimand, ADR-0003 picked probability-matching Thompson sampling,
ADR-0006 shipped the fine arm space with an informative prior and the R49 onboarding
floor, and ADR-0007 shipped processor-level ADWIN with tiered decay. This spike holds
that design fixed and measures only what #9 owns: the price of bandit feedback
against a full-information bound, whether TS's built-in exploration suffices,
whether any *standing* forced-exploration mechanism earns its tax, the exact decision
record #15's IPS needs, and how delayed outcomes are ingested without corrupting the
estimand.

It executes against the committed, content-addressed scenarios (`baseline-steady-v1`,
`outage-recovery-v1`) plus two spike-local gated scenarios: `quiet-improvement-v1`
(a competitor silently recovers from a believed 0.806 to a true 0.92 with no transport
signature — the exact failure mode the ticket fears) and its boundary variant
`quiet-improvement-starved-v1` (the prior artifact has been refreshed *during* the
storm, so the arm is starved to a few percent of traffic *before* it improves).

```bash
python3 censored.py 60000                 # ~13 min, regenerates the committed RESULTS.md
python3 censored.py 20000 --section=C5    # ~30s smoke of section C5
```

Findings:

- **[C1] Bandit labels are unbiased; censoring buys precision loss, not bias.** A
  full-information learner (impossible in production, cheap in the harness) folding
  every eligible arm's counterfactual beats the production learner by only ~+90 c/1k
  and ~−0.2 MAE pts on this fleet. The ticket's survivorship bias is a *variance*
  statement: a starved arm's posterior drifts wide, not wrong — the signal TS reads.
- **[C2] Built-in exploration anneals exactly as designed — and that is the trap.**
  On the steady world explorative picks fall 4.3% → 1.8% per 10k window. On the
  quiet-improvement world bare TS feeds the written-off arm at its pre-event share
  forever (10.5% → 10.5%) while the shipped detector machinery (adapts after
  ~6.2k txns of post-event feed) lifts its share 1.3–1.6× and flip-context share from
  17.6% to ~25% against an oracle 49%. Detection works with *zero standing budget*;
  re-learning is the taxed part.
- **[C3] No standing forced-exploration mechanism earns its keep.** ε-greedy 0.01
  pays −79 c/1k of standing tax for ~nothing gained on the improvement world;
  ε-greedy 0.05 −467 and arm rotation −519. Optimistic priors are inert (−29) — a
  prior is not a mechanism. On the improvement world at this horizon bare TS is
  net-best; the honest statement is that the shipped machinery is event insurance
  whose premium is the post-reset floor cost, and the balance degrades as the arm
  gets starved — see [C2]'s boundary case, where the floor's re-feed is structural.
- **[C4] The decision log must pin a score-based propensity, not a θ-only one.**
  DECISION_LOG v1 replays decisions bit-exactly from logged posteriors +
  key-addressed draws, and WAL prefix-fold reproduces the decision-time posterior
  — but the θ-plug-in propensity is catastrophically biased on a margin-skewed fleet
  (~84 pts mean chosen-arm error vs a score-based MC reference); the score-based
  plug-in is the same cost class (~14 pts worst state) and the log tags it; #15
  recomputes exact propensities by WAL replay. 87.4% of decisions have a nontrivial
  eligible set — eligibility is part of the propensity.
- **[C5] Delayed outcomes: update on arrival, never rewrite the label.** Ingest lag
  up to 256 txns (~45 min at fleet pace) is free; 8,192 txns costs ~−115 c/1k. A
  late-settling timeout never changes the auth label: margins between the three
  protocols are path-noise indecisive (−46/−13 c/1k against shipped), but
  retro-relabel moves the *estimand* to P(eventually authorized) — the rate of a
  router without a deadline — and no-response=decline corrupts it visibly
  (+0.43 MAE pts here; −1,004 c/1k on ADR-0002's timeout-heavy trace). Exclusion
  stands; the boundary is the timeout share, not the protocol.

Magnitudes belong to the committed scenarios at n = 60,000 (`policy seed 20260916`).
Orderings, mechanisms, and the estimand arguments are the normative claims.
