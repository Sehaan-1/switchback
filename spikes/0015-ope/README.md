# Spike: off-policy evaluation (IPS) for counterfactual analysis

`ope.py` is the empirical evidence for #15: the dashboard's counterfactual panel —
"what if we had routed 20% more to processor B over the last 7 days?" — needs an
off-policy evaluation (OPE) system, and the ticket asks five things: the IPS
mechanics and whether clipping is needed, whether a Doubly Robust estimator earns
its complexity, the counterfactual query interface, a validation protocol against
replayed ground truth with an acceptable error bound, and the implementation home.

The spike holds the shipped system fixed (ADR-0006's Thompson sampler with the
R49 onboarding floor, ADR-0008's DECISION_LOG v1, ADR-0005's index-addressed
world, ADR-0002's margin estimand) and measures what the ticket's questions come
down to on this fleet, at n = 60,000 decisions — which *is* the panel's 7-day
window at the committed scenario's pace.

- **[O1] The propensity a decision was taken under is recoverable exactly; the
  schema dependency closes.** Decision replay from DECISION_LOG v1 + key-addressed
  draws: 1301/1301 bit-exact (C4's gate, re-run, on this spike's run — 27,376.2
  c/1k, reproducing ADR-0008's steady-world number at the same seed). The
  propensity bake-off vs an MC-100k score-based reference on 24 logged states:
  theta-only plug-in 61.2 pts chosen-arm mean error (broken on this margin-skewed
  fleet), the R61 score plug-in tag 14.0 pts — and most of *that* is one sign
  bug: 24.8% of decisions carry an eligible arm with `win+fee<0`, which silently
  reverses the score inequality; sign-fixed 5.7 pts; MC-64 1.8 pts; the shipped
  midpoint-quantile **quadrature: 0.09 pts, deterministic, 50 ms/full state,
  ~7 ms/target arm**.
- **[O1/O2] The shift family has a closed-form weight, and its cost scales with
  the target's head share, not the window.** For `shift(B, ρ)`, w = 1−ρ on every
  row whose head is not B and (1−ρ)+ρ/π₀(B|x) on head-B rows only — the mixture
  identity: a 7-day query pays the quadrature on 8.0% of the window for foxtrot
  (43 s) or 44.7% for charlie (383 s), single core. The interface, the estimand
  (value + delta per 1k with a bootstrap CI + support verdict), the confounding
  the weights exist to remove (foxtrot: 92.1 c/dec on its logged head contexts
  vs 22.2 c/dec forced over all eligible contexts — a −69.9 c/dec gap), the
  one in-weight approximation (fallback-tail conditioning: +0.04 c/dec, priced
  by rejection-sampled paired probes), and the re-learning divergence the log
  cannot see (−9.8 c/1k over this window) are all measured, not asserted.
- **[O3/O5] At this fleet's scale, support is the scarce resource — and only
  clipped weights keep money units.** ESS/N ranges 0.005–0.99 across the
  (target × ρ) grid, but the sharper statement is the four-world replicate:
  E[w_bar] realizes 0.869 / 463.1 / 46.6 / 0.865 — on two worlds the tiny-π₀
  head mass never fires (the same mechanism that makes a zero-support query
  exact), on two worlds a single logged head row with π₀ ~ 1e-8 turns the
  unclipped IPS error into **+9,100,182 c/1k** — while clipped-IPS (cap 10) sits
  at −948.6±70 on all four (RMS 951.0). SNIPS's normalization rescues the
  exploding world and buys a worse typical case (+3,395 bias). Delta (zero
  head rows) is refused, not estimated.
- **[O4] Ship clipped IPS; DR is not worth it here.** The strong honest DM
  (engine posteriors → ADR-0002 margin + logged per-processor fallback term) is
  calibrated on support (−322 c/1k) but its fallback term inherits the
  context-selection gap the weights exist to remove: +1,304 c/1k on the headline
  query. Unclipped DR inherits the tail catastrophe (+1,265,383 on the exploding
  world); SNDR-clip10 (+1,096±283) is statistically indistinguishable from
  clipped IPS while paying a per-query decision-QMC. The tag-as-weight negative
  control prices the universe DR exists for — IPS −1,440 / SNIPS +2,915 /
  DR +917 c/1k off-truth — which is exactly why R107 forbids the tag as a
  weight. (Naive cell-mean DMs fail catastrophically: +8,979…+27,484 c/1k —
  the reason the structural DM exists.)
- **[O5] Validation = same-world replay, with its own floor measured.**
  The protocol: one content-addressed world, run the logging policy, replay the
  target overlay through the frozen trajectory, compare, re-seed and repeat.
  Replayed delta on the headline query: −1,729.7 c/1k, sd 42.7 across
  four 60k worlds — the truth is stable; the per-world table attributes error
  to the estimator. The recompute's signed calibration: expected picks 4,853
  vs 4,785 observed (1.014). The proposed gate: >= 1,000 logged head-target
  rows (decided before any estimator runs), then |err| <= max(1,500 c/1k,
  75% of |delta|) at N >= 30k — and for context, the published OBD benchmark
  band (IPW/DR 8–14% of policy value, DM ~2x, Saito et al., NeurIPS 2021 demo)
  vs the achieved 3.7% of value on the support-poor headline class here.
- **[O6] Read path and packaging.** Python in `analysis/` (ADR-0011), thin CLI,
  sealed partitions over a replica (ADR-0012 R94): window scan 30 ms
  (2.5M rows/s stdlib sqlite3), quadrature 809 decisions/s; the recompute is
  the bottleneck by ~4 orders of magnitude, so per-target π₀ tables are built
  at partition-seal time and a pane refresh is a sum, not a recompute day.

```bash
python3 ope.py 60000                 # ~23 min, regenerates the committed RESULTS.md
python3 ope.py 60000 --section=O3    # one section
python3 ope.py 4000 --smoke          # fast structural pass
```

Findings are per-section in RESULTS.md. Magnitudes belong to the committed
scenario (`baseline-steady-v1@sha256:b49193b7e715`, policy seed 20260916, OPE
seed 20260920, λ_to = 45¢, world replicates at +10k/+20k/+30k); the orderings,
mechanisms, the estimator choice, the query interface, the validation gate, and
the hard rule that the logged tag is never a weight are the normative claims.
