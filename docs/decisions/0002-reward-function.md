# ADR-0002: Reward function — two-part objective, end-to-end Bernoulli label, priced ambiguity

- **Status**: Accepted
- **Date**: 2026-09-11
- **Resolves**: [#4 Reward function specification](https://github.com/Sehaan-1/switchback/issues/4)
- **Depends on**: [ADR-0001](0001-engine-language-go.md) (Go core; no Python in `Decide()`)
- **Feeds**: #7 (posterior update protocol), #9 (censoring scope), #11 (3DS estimation), #5 (floor margin + attempt caps as constraints), #15 (OPE needs eligibility logged), #17 (baselines, oracle, frontier), #3 (bandit-vs-table, answered with numbers)
- **Evidence**: [`spikes/0004-reward-function/`](../../spikes/0004-reward-function/RESULTS.md) — 60,000-transaction paired simulation on two synthetic fleets, `python3 spikes/0004-reward-function/fleet.py 60000`

---

## Decision

**1. The objective is a per-attempt scalar in minor currency units of the transaction currency:**

```
R(i, r) = 1[authorized] · win_i(r)
          − 1[declined | abandoned] · fee(r)
          − 1[timeout] · (fee(r) + λ_to)

win_i(r) = amount_i · (take_bps − cost_bps(r)) / 10⁴  +  (take_fixed − fixed_fee(r))
fee(r)   = attempt_fee(r)                    # charged on submission, declines are not free
λ_to     = 45 (config, cents)                # price of an unresolved attempt: reconciliation
                                               # + late-auth exposure, see term table (#10)
```

**2. The bandit's posterior models exactly one quantity: P(authorized | arm), end-to-end.** No money in the label. Margin is *computed from fee schedules at decision time*, never learned — it is not uncertain. This keeps Beta–Bernoulli conjugacy (so #7 gets its cheap update, #8 its drift statistic, and #13 its 16 bytes per arm) and it is the reason the reward can be stated as one expression instead of a model.

**3. The decision score is the reward under a sampled parameter, and a retry is an EV test, not a policy of "retry on soft decline":**

```
score_i(r) = θ̂_i(r) · win_i(r) − (1 − θ̂_i(r)) · fee(r) − π̂_i(r) · λ_to
retry on r' after a soft decline  ⟺  θ̂_i(r') · win_i(r') − (1 − θ̂_i(r')) · fee(r') > 0
```

where θ̂ is a Thompson draw from the arm's Beta posterior and π̂ is a draw from a **separate** per-arm timeout rate. `λ_to` and the retry test are the only places latency and retry cost enter. There is **no per-millisecond latency term** and **no 3DS multiplier**.

**4. Outcome taxonomy is closed and is a first-class part of the reward contract** — the label is an enum, not a boolean, and *unknown is not decline*:

| Outcome | Meaning | Label for P(auth) | In reported reward |
| --- | --- | --- | --- |
| `authorized` | issuer approved | success | +win |
| `declined_soft` | retryable elsewhere (`do_not_honor`, `retry_limit_*`) | failure | −fee |
| `declined_hard` | never retry this card (`insufficient_funds`, `stolen_card`, …) | failure | −fee |
| `abandoned` | 3DS challenge never completed | failure | −fee |
| `timeout` | deadline passed, result **unknown** | **excluded from updates** | −fee − λ_to |

**5. `floor_margin` is a hard constraint, evaluated before sampling, on platform margin** — `take_bps − cost_bps(r) ≥ floor_margin_bps`. Not a soft penalty: a penalty makes the floor negotiable per-transaction, and the audit trail (#5.6) cannot answer "did we respect the contract" if the answer is "we paid a penalty to break it". **Open product question, flagged not buried**: the spec calls it *merchant* floor_margin while a merchant's own margin is not a function of which processor we choose (their price is `take_bps`, fixed by contract). We implement the platform-margin reading, which is the only one that varies by route, and #5 should confirm with product.

**6. Chargeback, fraud and dispute losses are out of the online reward.** A label must be computable inside the update window. Interchange-adjustment and chargeback notifications arrive 30–120 days after authorization; if they gate the posterior, either the posterior waits or the update is attributed to the wrong state of the world. They enter as (a) offline analysis over the trace log, (b) constraints if a merchant requires them, (c) a future reopen trigger (§Reopen 3).

## Term table (the ticket's requested format: observed / estimated / constraint vs objective)

| Term | Status | Owner | Note |
| --- | --- | --- | --- |
| `amount`, `currency`, `sca_required`, `deadline_ms`, `floor_margin_bps` | **observed** at decision time (request) | #12 | inputs, not estimates |
| `cost_bps`, `fixed_fee`, `attempt_fee`, `take_bps` | **observed** (fee schedules, config) | #5 | deterministic lookup ⇒ *never* in the posterior |
| `θ = P(authorized \| arm)` | **estimated** (Beta posterior) | #7 | the only learned quantity in the objective |
| `π = P(timeout \| processor)` | **estimated**, separate count | #7/#13 | feeds the price, not the auth rate |
| `P(challenge)`, `P(abandon \| challenge)` | **estimated offline**, used for *cold arms' priors and OPE reweighting only* | #11 | deliberately **not** in the score — see below |
| `λ_to` | **config**, unit = cents | #7 | one knob, human-interpretable price |
| `deadline_ms`, currency support, residency/SCA mandate, attempt caps, `floor_margin` | **constraints** (filter the action space before sampling) | #5 | not penalties |
| `authorized` for the *attempted* arm | **observed** after the fact (the counterfactual arms are not) | #9 | the censoring problem, scoped: it is *only* `timeout` |

Why propensities are recorded over the *legal* set: filtering before the bandit means an arm that was never eligible is never counted as a failure. Post-bandit vetoing would teach the model that a regulatorily-forbidden route "declined", which is the fast way to a router that keeps proposing it.

## Why 3DS is not a multiplier (consideration 3)

The ticket proposes `E[reward] = P(auth) × (1 − P(challenge)·P(dropout|challenge)) × margin`. In this system that **double counts**: the label already contains the dropout, because an abandoned challenge is observed as a non-authorization. The engine sees "the customer never came back" as a lost sale — which is exactly what it is.

Priced, not asserted (`correlated-3ds-v1`, 60k transactions, deltas vs the chosen design):

| variant | margin ¢/1k | auth pts | MAE of θ̂ vs truth |
| --- | --- | --- | --- |
| chosen: end-to-end label | — | — | 2.25 pts |
| + 3DS multiplier on top of θ̂ | +64.8 correlated, **−11.5 decorrelated** | +0.06 / −0.01 | 2.25 (unchanged — it is a scoring change) |
| abandonment censored out of updates | +22.9 / +10.5 | +0.02 / −0.00 | **5.05** (2.2× worse) |
| timeout counted as decline | **+719.9 / +718.4** | +1.25 / +1.22 | **8.79**, 57.0 pts on the punished arm |
| **three-way: π̂ priced at λ_to=45** | **+311.6 / +337.2** | **+1.95 / +1.78** | **1.92 — better than the chosen design** |
| smooth per-ms latency price (static table) | +398.5 / +378.8 | +0.88 / +0.85 | 3.04, and **cannot see the drift event** |
| hard gate on learned timeout rate | **−1077 / −1244** | −1.69 / −1.89 | **12.89** |

Three things the experiment taught that prose would have missed:

1. **The multiplier's harm flips sign with the fleet.** It is a monotone-ish distortion, so it mostly preserves ranking — until a processor is 3DS-strong but auth-weak, and then it misranks. A term whose effect depends on which synthetic fleet you happened to simulate cannot be tuned or reviewed. Rejected.
2. **Censoring abandonment is free on margin and expensive on the estimate.** Dropping abandonments to "be safe about attribution" (#11.4's instinct) costs 2.2× posterior error and buys nothing, because it silently changes the estimand from P(capture) to P(approve | reached). If anyone wants the conditional rate, it is a *second* posterior, not a modification of the first.
3. **A hard eligibility gate on a learned statistic starves the arms it excludes.** H is the worst variant here for both margin and estimate error, because exclusion stops data collection, which freezes the estimate that drives exclusion. Exogenous constraints (currency, residency) don't have this property — they depend on the context, not on traffic. **Any learned-state-dependent filter therefore needs forced exploration (#7.6) and must log eligibility, not just the chosen action, or #15's IPS is biased.** That is a constraint on #5's design, discovered here.

The `timeout` row is the uncomfortable one: counting ambiguous timeouts as failures is the most *profitable* ablation on this fleet (+720 ¢/1k, consistently across both fleets) and the second-worst estimator. A benchmark that reports margin or regret alone selects for it. The correct move is not to ignore the ambiguity — `λ_to` captures 43% of that gain with *lower* error than the unbiased-but-blind design — it is to price the event instead of laundering it into the success label.

## Latency: constraint, with a price on the residual (consideration 2)

`deadline_ms` is a constraint (ADR-0001 R4: every processor call carries a derived deadline). The residual — attempts still in flight at the deadline — is priced once, as `λ_to`, and no further. Answers to the ticket's question "should a 3s authorization be worse than a 200ms one even if both succeed?": **yes, but it is already worse** — the deadline is what makes it worse, and a *successful* slow authorization is a business cost that belongs to the merchant-facing SLO, not to the arm's reward. Adding a per-ms term to the reward (variant G) buys margin only when you hand it the true latency table, and it cannot revise that table when a processor degrades; in the post-drift window of this run, learned `π̂` (25492.9 ¢/1k) edges the static penalty (25477.1). In production nobody gets handed the table, so the adaptive version wins on the axis that matters and the smooth penalty is rejected.

`λ_to` is config with a unit (cents), versioned through ADR-0001's artifact contract, so raising the price of ambiguity is a reviewable diff and not a code change.

## Retry cost (consideration 4)

The reward must penalize attempts, not just reward eventual success. Mechanically: `fee(r)` appears in the loss term of every non-authorized outcome *and* in the retry gate above. So an arm that "eventually succeeds on the second try" has to clear the price of the first submission — which is why `A_never retry` (−1802 ¢/1k, −5.26 auth pts) and `retry unpriced` (+2.11 auth pts, −76.8 ¢/1k) are both wrong in opposite directions, and the fee term is the knob between them. Note what the fee term is *not*: it is not load-bearing for *ranking* between processors at these price levels (±0.3%); it is load-bearing for the *retry decision* and for the honesty of reported margin. Small-ticket economics come from `fixed_fee`/`attempt_fee` being absolute, which is also why the objective is in minor units and not basis points — in bps, a declined $20 ticket looks free and the policy learns to burn micro-tickets.

Scheme-level retry limits (Visa's excessive-retry rules, #5.1) are **constraints** with a per-card-per-day counter, not reward terms: a penalty makes a scheme violation something you pay for.

## Time horizon (consideration 6)

Per-transaction, myopic. A "relationship health" term — routing volume to one acquirer degrades future rates — makes the reward depend on the *policy's own past actions*, which breaks the i.i.d. assumption the Beta update and the regret bound rest on, and would make #15's off-policy estimates incoherent without a full MDP. Volume caps, concentration limits and rate-step schedules give 80% of the effect as constraints, are auditable, and cost nothing in theory. If real money turns out to be left on the table (see Reopen 4), the honest fix is a separate ticket on contextual/batch routing, not a bolted-on decay term in this reward.

## What this binds on later tickets

- **#7**: arms are `(context bucket × processor)`, reward is Bernoulli-by-construction so Beta stays conjugate; the update is `α += 1` / `β += 1` / *nothing* for `timeout`, plus `to += 1`. `λ_to` is the one scoring constant. Forced exploration budget must cover arms excluded by learned-state gates, not just new arms.
- **#9**: the censoring scope is now exact — one class (`timeout`) is censored, by design, and the abandoned class is *not* censored. Everything else #9 worries about is counterfactual arms, which is IPS territory (#15).
- **#11**: owns estimating `P(challenge)` and `P(abandon | challenge)` — for cold-start priors and for OPE reweighting only. Required signals to log: 3DS flow version, exemption used, challenge presented (bool), challenge completed (bool), liability shift (bool), `mandate` flag. Mandate transactions get their own arm bucket: SCA rules and dropout behave differently there and averaging them is how you hide a regulatory problem.
- **#5**: `floor_margin`, currency/regulatory caps and attempt caps all land in the *pre-sampling* filter, and the filter's output (the eligible set) must be logged per decision for #15 and the audit trail.
- **#15**: propensities must be recorded over the eligible set with the eligibility set itself; IPS weights are meaningless without it. Also: a `timeout` outcome has no usable label, so OPE must treat those rows as censored (which is one place the naive design and the right design agree).
- **#17**: four definitions to adopt verbatim —
  1. *cost-only baseline* = cheapest processor subject to `p̂_stale ≥ q`, and report the **whole swept frontier** (the frontier is stepwise; a single interpolated number hides that our policy sits *off* its left edge: cheaper **and** +1.37 pts auth at matched cost on this fleet).
  2. *equal effective cost* = matched on **cost per authorized** transaction, never per request — per-request is gameable by declining more, since declines still cost submission fees.
  3. *oracle* = a **policy** with true rates through the identical execution loop, plus a **clairvoyant** bound (best realized single-attempt outcome). Both, with the reason: a rate-only oracle is *beatable* — the chosen design scores −0.21% against it, because the oracle cannot see the deadline collision. "Negative regret" in a report is a sign the oracle is mis-specified, and we should say so up front rather than clip it.
  4. every reported number carries the **scenario id**, and estimate quality (MAE of θ̂ against simulated truth) ships next to margin, because D above wins on margin while being the wrong estimator.
  5. baselines must be given the same *information*, not just the same algorithm: our static table here had no latency term, which flatters the bandit. #17 should include a "static table + latency rule" baseline.
- **#3**, answered by the same run so it cannot be hand-waved: with the chosen reward (three-way, λ_to=45) the bandit beats a stale static table by +120 ¢/1k full-run and **+311 ¢/1k post-drift**; with the naive reward (no ambiguity price) it *loses* to the static table by −192 ¢/1k full-run. The bandit is not free and does not automatically win; it pays its exploration tax until something changes, and the reward function decides whether it earns it back. #3 should adopt that framing — "bandit, conditional on pricing what a table cannot learn" — rather than the usual advocacy.

## Consequences

**Positive.** One learned quantity, so the state store is 16 bytes per arm and the update is two increments (#13 gets its cheap answer from here); reward is computable at T+1 from data the engine already has; every term is either observed or has a named estimator and owner; `λ_to` is a single interpretable price instead of a latency-shaped term in the objective; the 3DS/liability-shift interaction is learned rather than assumed, so it holds when an issuer changes behaviour without us re-deriving a formula; the taxonomy doubles as the audit trail's vocabulary.

**Accepted costs.**
1. The reward is not dollar-weighted, so a $10,000 authorization and a $10 one update the same posterior. We buy scale back through `win_i(r)` in the score, which is where the money actually is — but the arm's *rate* is then an average over amount bands within the bucket, and #7's amount-band definition carries the whole burden of that approximation.
2. `abandoned` counted as a failure makes the processor partly responsible for merchant-funnel drop-off. That is *intended* (it is a lost sale and processors do compete on frictionless rates), but it means a processor paired with a badly-designed checkout looks worse than it is; #11's decomposition is the remedy, and it is offline.
3. `timeout` excluded from updates means an outage that causes timeouts is not punished in the *rate* — only via `λ_to` and the gate-free fact that π̂ rises. Under-prices catastrophic behaviour; mitigated by #8's drift detector and by alerting on π̂, not only on auth rate.
4. `λ_to=45` is calibrated on a synthetic fleet. On real data it is a business parameter (what one unresolved authorization costs in ops time and risk), and it should be set by whoever owns the chargeback/reconciliation budget, not by us.
5. Two posteriors per arm instead of one (auth + timeout) doubles nothing important, but it does put a second statistic in #13's schema and #12's interface.

**Design rules (continuing ADR-0001's numbering):**
- R9 — the posterior estimates P(authorized) and nothing else. No reward scaling, no margin in the label.
- R10 — outcome taxonomy is a closed enum at the ingest boundary; `timeout` never maps to a decline, and an absent outcome never maps to anything.
- R11 — every reward term must be computable within the update window (T+1). Lagged labels are offline signals.
- R12 — latency enters as eligibility + `π̂·λ_to`. No per-ms term in the objective.
- R13 — `floor_margin` and scheme retry limits are constraints evaluated before sampling; nothing that gates real money is a penalty.
- R14 — the trace log records the *eligible set*, the sampled θ̂ and π̂, and the chosen action, in that order of importance. A decision that cannot be replayed from the log cannot be benchmarked (#17) or defended (#5.6).

## Reopen triggers

1. Real fleets show `P(abandon | challenge)` dominating the variance of `win` (say, challenge-abandonment loss > 30% of expected margin on SCA traffic) — then the joint decomposition moves into the score and the multiplier is back on the table with the double-count handled by conditioning the posterior on "reached the issuer".
2. Chargeback/fraud data becomes available at T+1 (some networks and fraud vendors do) — revisit R11; the objective should then include expected fraud loss, and #7's conjugacy question reopens (Beta is the wrong family for a [0,1]-continuous reward).
3. Merchant/product confirms `floor_margin` means *merchant* margin — then it is not a routing constraint at all and #5 must re-derive it as a fee-ceiling rule.
4. Measured cost of volume concentration (rate step changes, acquirer throttling) exceeds 1% of margin on production traces — opens a contextual/sequential-policy ticket; do not bolt a decay term onto this reward.
5. `λ_to` sweep on real traffic shows the optimum far from "a few attempt fees" (i.e. the knob is doing the work of a latency model) — then latency becomes a first-class arm statistic and this ADR's §"Latency" is revised.
6. Any benchmark harness that cannot report MAE of θ̂ alongside margin — the D result above is only visible with both; without it we would have shipped D.

## Alternatives considered

- **Continuous reward = realized margin, normalized to [0,1]**, i.e. maximize dollars directly in the posterior (the literal reading of `authorization rate × margin`). Rejected: it breaks Beta conjugacy, forces a different likelihood (#7 becomes a variance-estimation problem), and makes the drift statistic in #8 depend on the amount mix rather than on behaviour. Our design gets the same optimization (the score multiplies by `win`) without importing dollars into the posterior.
- **Dollar-weighted Bernoulli** (`reward = win / max_win` as a success probability proxy): rejected, it is the same breakage with a sparser justification.
- **Two arms per processor, one for the 3DS flow and one for the non-3DS flow.** Genuinely attractive — it is the cleanest way to make frictionless rate an *arm* property and would let the router choose the flow, not just the processor. Deferred to #7 (arm granularity is where it belongs) because it multiplies the sparse-arm problem that #7.1 is already worried about, and requires the exemption logic in #5 to land first.
- **Constraint-layer veto after sampling** (with the reward unchanged): rejected above — contaminates the label with regulatory non-events.
- **Timeouts counted as declines (D)**: rejected on evidence, not principle — it is the best-performing variant on margin, which is precisely why the record has to say why we are not taking it.

## Appendix

```bash
python3 spikes/0004-reward-function/fleet.py 60000      # ~95s, deterministic, stdlib only
```

Limitations of the evidence, stated plainly: one synthetic fleet with invented rates, 24 arms, a fixed 2-attempt cap, single-currency margins (no FX), and a dropout model constant across brands except a size and corporate bump. The *rankings* of the ablations reproduce across both fleets and are robust to a scenario change as crude as permuting the frictionless rates; the *magnitudes* (¢/1k, ±pts) are properties of `synthetic-fleet-v1` and must not be quoted as expected production impact. #6's harness is where those numbers get earned, and #17's committed benchmarks are where they become claims.
