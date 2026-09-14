# ADR-0003: Routing algorithm — stochastic bandit, Beta-Bernoulli Thompson sampling, not a static table

- **Status**: Accepted
- **Date**: 2026-09-14
- **Resolves**: [#3 Why a bandit and not a static weight table?](https://github.com/Sehaan-1/switchback/issues/3)
- **Depends on**: [ADR-0001](0001-engine-language-go.md) (Go core; no Python in `Decide()`), [ADR-0002](0002-reward-function.md) (the reward the bandit optimizes, and its "bandit, conditional on pricing what a table cannot learn" framing)
- **Feeds**: #7 (posterior update protocol, arm granularity, forced exploration), #8 (drift detection is a hard dependency of this decision, not an add-on), #15 (propensities over the eligible set), #5 (constraints filter before sampling), #13 (16-byte arms + snapshot export), #17 (algorithm baselines + estimate-quality pairing), #12 (`Decide()` returns the sampled draws and propensity)
- **Evidence**: [`spikes/0003-bandit-vs-table/`](../../spikes/0003-bandit-vs-table/RESULTS.md) — 40,000-transaction coupled simulation, `python3 spikes/0003-bandit-vs-table/bandit.py 40000`, plus the #4 run in [`spikes/0004-reward-function/`](../../spikes/0004-reward-function/RESULTS.md)

---

## Decision

1. **Family: stochastic multi-armed bandit.** Routing is a per-decision choice among arms `(context bucket × processor)`, where a context bucket is a *categorical* key (BIN class, currency/region, SCA, mandate) — not a continuous feature vector. There is no continuous-feature model in v1.
2. **Variant: Beta-Bernoulli Thompson sampling**, Jeffreys prior `Beta(1/2, 1/2)` per arm, cold-start priors seeded from offline estimates (#11), under ADR-0002's reward (two-part score; `timeout` excluded from the auth posterior and priced at `λ_to`), and **coupled to a drift detector (#8)** that shrinks or resets a posterior on detected change. Thompson sampling is not shipped bare: #8 is what makes it re-discover a changed world.
3. **Why it dominates in this domain** (measured, 40k txns, `synthetic-fleet-v1`; gaps are margin ¢/1k vs TS): the static table loses **−1,711** full-run and **−4,383** post-drift; UCB loses **−1,101**; ε-greedy loses **−336 / −688** at ε = 0.05 / 0.10; EXP3 loses **−1,200**; A/B-with-rebalancing loses **−4,492 / −4,599**; the literal "60/30/10" table loses **−4,858**. TS is the only candidate that is (a) Bayesian — its exploration is proportional to *uncertainty of value*, so it anneals with no schedule to tune, and (b) probability-matching — the propensity of every arm is exactly its posterior probability of being best, which #15's off-policy evaluation and the audit trail get for free.
4. **The hybrid question is resolved as three separate answers.** *Freeze bandit weights into a static table for audit* — **adopted, as an export only**: a human-readable policy snapshot derived from posterior means, emitted on a configurable cadence; it is never read back as the control policy (measured freeze-lag cost is within noise, so the export is free and the *production-control* reading buys nothing). *Bandit with an explainability layer* — **adopted**: the explainability layer *is* the decision log (ADR-0002 R14) plus the snapshot export. *Bandit for exploration + deterministic argmax for production* — **rejected**: argmax has no uncertainty, so it can quote no propensity (breaking #15's OPE) and cannot trigger a drift reset (see F3).
5. **Audit/explainability tradeoff, stated honestly.** A table answers the *aggregate* question ("what is your routing policy") in one printable line, and we keep that line via the snapshot export. The bandit answers the *per-transaction* question ("why did my €500 go to Adyen") strictly better than a table ever could — a table's answer to one transaction is "because 30% of the time we send there", which is an unexplained lottery, whereas the bandit's answer is a recorded posterior and a recorded draw. What we give up is that the *live* policy is a distribution, not a fixed vector; we buy that back with the export, and accept that the export is stale by construction.

## Context

Two facts about the project's shape govern how the evidence reads:

- **The decision is per-transaction and myopic — already decided.** ADR-0002 §Time horizon established that there is no cross-transaction credit assignment in v1: the action's effect is a single authorization attempt, observed immediately, with relationship-health effects (volume concentration, rate steps) handled as *constraints*, not as future reward. That is precisely the boundary between a bandit and an MDP, and it is the reason "full RL" is not just too heavy but the wrong object. The bandit is not a simplification of RL here; RL would be a generalization of the bandit that this reward function (ADR-0002) deliberately does not need.
- **"Learning the table" and "being a bandit" are the same activity.** A static weight table is optimal only if the rates are stationary and known. They are neither (quantified in §1), so the table must be *estimated* — and estimating per-arm success rates from streamed outcomes, while deciding which arm to use next, is exactly the multi-armed bandit. A frozen table is not an alternative to the bandit; it is the bandit with the learning switched off. The only real question this ticket decides is *which learning rule*, and whether any of the freezing/determinism hybrids is worth its auditability.

## 1. The case for bandits: how much do the rates actually move? (consideration 1)

The ticket asks for a number, not a vibe. From the spike's [V1] table (closed-form true rates, `synthetic-fleet-v1`):

| dimension | measured spread in a processor's true auth rate |
| --- | --- |
| BIN class (consumer credit / debit / corporate / prepaid) | **9.2 – 21.8 pts** across the six processors |
| region / currency (SEPA, UK, US, LATAM, APAC) | **4.4 – 5.7 pts** |
| time-of-day (per-processor sinusoid) | **4.0 – 10.0 pts** peak-to-peak |
| acquirer event (the outage) | charlie **×0.86** auth and **×3** latency at t = 0.6 |
| acquirer event (the new issuer deal) | foxtrot **×1.15** auth at t = 0.8 |

And the *interaction* matters as much as the main effect: the best processor by auth rate differs per card class (consumer credit → delta, debit → bravo, corporate → charlie, prepaid → foxtrot in [V1]'s rank table), so a single global weight vector is wrong for whole classes of transactions. This is what the literal "60/30/10" table pays: **−4,858 ¢/1k** vs the bandit, and it is *not* fixable by choosing better fixed weights — the fixed weights are wrong somewhere for every non-degenerate choice, because the ranking is not constant across the context or across time.

The strongest steelman table is the one that *can* see the context: per-BIN-class argmax of a rate snapshot taken at t = 0 and never refreshed (`table_snapshot`). Against it, the bandit wins **+1,711 ¢/1k full-run and +4,383 ¢/1k post-drift**. The mechanism is visible in the columns: the snapshot's timeout rate is 15.3% (vs the bandit's 6.4%) because it keeps routing to charlie after the outage and charlie now exceeds the deadline — a table cannot see that the processor it points at became slow, and its staleness is not a one-off: its mean `|p̂ − p_true|` over the run is **9.18 pts vs 3.12** for the bandit. A stale table does not just lose the auth-rate change; it keeps paying for it transaction after transaction.

This is the same finding ADR-0002 already recorded from the #4 run, now confirmed from the algorithm side: with the chosen reward the bandit beats a stale static table by **+120 ¢/1k full-run and +311 ¢/1k post-drift** there, and loses by **−192 ¢/1k** with the naive reward. The bandit is not free and does not automatically win; it pays an exploration tax until something changes, and the reward function decides whether it earns it back. This ADR adopts that framing verbatim: **bandit, conditional on pricing what a table cannot learn.**

## 2. The case against: audit, debug, compliance (consideration 2)

The ticket's example is the right test: a merchant asking "why did you route my €500 transaction to Adyen?" deserves a clear answer. The steelman version of the case against the bandit is that a weight table *is* the answer — it says "60% of the time we route to Adyen" — while a bandit's posterior is opaque state.

The steelman does not survive contact with the actual question. "60% of the time we route to Adyen" is not an answer to "why *this* €500"; it is an admission that the table routes by lottery with fixed weights, which is *less* defensible than a recorded posterior. The bandit, with the decision log ADR-0002 already mandated (R14 — eligible set, sampled θ̂ and π̂, chosen action, in that order), answers the question with exactly the four things a compliance reviewer wants:

- **the belief**: Adyen's posterior `P(authorized) = 0.93` (234/252) at decision time;
- **the decision**: the sampled draw `θ̂ = 0.94`, scored against the fee schedule, beat Stripe's expected margin by €X;
- **the eligibility**: the constraint filter's output (why only these processors were legal);
- **the propensity**: the recorded probability the arm was chosen (needed for OPE, and the number a table does not have at all).

The costs the bandit genuinely imposes, named and accepted:

1. The live policy is a distribution, not a fixed vector, so a reviewer cannot read routing policy off a single line. **Mitigation:** the snapshot export (§3) is that line, emitted on a cadence to the audit store, with the posterior snapshots themselves append-only in #13's store so the *history* of beliefs is auditable, not just the current belief.
2. Debugging "why did it flip" requires replaying the decision log. **Mitigation:** R14 makes every decision replayable; a decision that cannot be replayed from the log cannot be benchmarked (#17) or defended (#5.6) — that was ADR-0002's rule, and this ADR makes the log the *only* supported explainability surface.
3. TS's draw is stochastic, so two identical-looking transactions can route differently. **Mitigation and acceptance:** that stochasticity is the exploration, it concentrates to near-determinism in steady state, and if a merchant segment requires *deterministic* routing, that is a per-segment constraint (#5) routing through the snapshot export — not a global switch to argmax.

## 3. Hybrid approaches (consideration 3)

The ticket lists three hybrids. Each gets a verdict, not a shrug:

- **"Bandit-selected weights periodically frozen into a static table for audit."** Adopted **as an export**. The spike's `freeze_k` policies quantify the *production-control* reading (alternate TS-explore for k transactions, then argmax of a frozen table for k): the margin cost vs TS is **+9.9 / +29.1 / +25.7 ¢/1k for k = 1k / 4k / 16k — within single-seed noise**, and the frozen table's staleness is barely above TS's (3.17–3.20 vs 3.12 pts). The finding is that freezing costs ~0 in margin, so the audit benefit of an export is free — but so is the corollary: making the frozen table the *control policy* buys nothing in margin, and it forfeits the propensity log for every exploit-phase transaction, which biases #15's IPS exactly when the world changed. Therefore: **export the table, never route through it.**
- **"Bandit with an explainability layer."** Adopted. The explainability layer is not a second component; it is R14's decision log plus the snapshot export. No separate subsystem to keep in parity with the engine.
- **"Bandit for exploration + deterministic policy for production."** Rejected on two counts, one measured and one structural. Measured (spike [F3]): after foxtrot improved at t = 0.8, the rate-only oracle routes **63.5%** of large consumer-credit tickets to it, but *every* online learner re-discovers it slowly — TS 4.8%, UCB 32.5%, greedy 7.1% — because their posteriors/bounds are confident from pre-event data. Deterministic argmax is the worst possible answer to this: it has no exploration to stumble on the change, and no uncertainty signal to notice that its belief has gone stale, so it cannot even trigger a re-estimation. Structural: argmax assigns propensity 1 to the chosen arm and 0 to the rest, so #15's importance weights are degenerate for every unchosen arm — off-policy evaluation of a deterministic policy over a non-trivial action space is not hard, it is undefined without a separate exploration mechanism (which would just be the bandit again). And the "production needs determinism" instinct is answered by steady state: TS's posterior concentrates, so in a settled environment TS *is* near-deterministic — without the cliff.

## 4. Comparison with alternatives (consideration 4)

- **Contextual bandits (LinUCB).** Rejected for v1, and the reason is structural, not a matter of taste: the features that predict auth here — BIN class, currency/region, SCA, mandate — are *categorical with small cardinality*. A linear model over one-hot encodings of those features is exactly the bucketed-arm model, with per-bucket intercepts; LinUCB's linear assumption adds nothing for categorical keys and costs the shared-covariance cold-start machinery (a real #7.1 concern that a structured Beta prior solves more cheaply). LinUCB earns its keep only when a *continuous or high-cardinality* feature set appears that generalizes *across* buckets — which is precisely Reopen trigger 2, and why ADR-0001's ONNX escape hatch exists.
- **Full RL.** Wrong object, not "too heavy." ADR-0002's reward is myopic and immediate; there is no state the router controls whose transition matters for future reward (volume/concentration are constraints), and the nonstationarity is handled by a drift detector, not a transition model. An MDP would import a credit-assignment problem the reward function was deliberately built not to have. (This is the strongest form of the case against, and it is disposed of in ADR-0002 §Time horizon, which this ADR inherits.)
- **Bayesian optimization.** Optimizes a *smooth, expensive, offline* objective over a *continuous* design space (hyperparameters, configs). Routing is the opposite shape: categorical arms, one cheap noisy sample per decision, a regret/adaptation objective online. BO's Gaussian-process smoothness prior over processors is not a defensible model of per-arm Bernoulli noise, and BO does not naturally express "keep adapting as the fleet degrades."
- **A/B testing with periodic rebalancing.** This is the strongest *measurable* competitor, so it gets the full run: `ab_k` alternates uniform exploration and argmax exploitation every k transactions. It loses **−4,492 / −4,599 ¢/1k** for k = 1k / 4k — the worst learner-family result after the literal table. The reason is exactly its virtue in a static world: uniform exploration re-measures *known-bad* arms every cycle at full cost, and the rebalance happens on a clock, so a change that lands just after a rebalance waits a full cycle. A/B is the right tool for a one-off comparison, the wrong tool for continuous routing.

## 5. Thompson sampling specifically (consideration 5)

The ticket asks why Beta-Bernoulli TS over UCB, ε-greedy, and EXP3, and what makes it "the honest, defensible version." Four properties of the payment domain do the work:

1. **The reward is Bernoulli by construction (ADR-0002), so Beta stays conjugate.** The posterior is two counts; the update is `α += 1` / `β += 1` (with `timeout` updating only the timeout count); the state store is 16 bytes per arm (#13 gets its cheap answer). Any non-conjugate competitor gives up the only reason the engine's hot path is ~nanoseconds per draw (ADR-0001 §1).
2. **TS is probability-matching, so propensities are free.** The propensity of each arm is its posterior probability of being best. #15 needs propensities over the eligible set for IPS; UCB, ε-greedy, and EXP3 each require a *separate* propensity computation, and the table/argmax policies have degenerate ones (0/1). TS is the only rule where the exploration policy *is* the logging policy, which is what makes the audit trail and the off-policy estimator the same artifact.
3. **TS's exploration is uncertainty-directed and self-annealing.** UCB's bonus is a *fixed* confidence interval that shrinks with pulls and lags a step change by construction — measured **−1,101 ¢/1k full-run, −1,689 post-drift**. ε-greedy's noise floor never anneals: it keeps sending 5–10% of traffic to known-bad arms forever — measured **−336 / −688**. TS samples each arm in proportion to how plausible it is that the arm is actually best, so exploration spends itself exactly where uncertainty remains, with no ε to schedule and no confidence constant to tune.
4. **TS gives a Bayesian answer to "why".** The merchant/compliance question is answered with a posterior and a recorded draw; UCB's answer is an index whose confidence-bound constant a reviewer must be told to trust, ε-greedy's is "5% of the time we roll dice", and EXP3's is worse — it maintains importance *weights*, not rates, so it can quote no per-arm rate or credible interval at all (and it loses **−1,200 ¢/1k** in the spike, paying the importance-weighting variance tax on signed, amount-scaled margins that must be squeezed into [0,1]). EXP3 exists for *adversarial* reward sequences; payments are stochastic with occasional jumps, and the adversarial machinery is pure cost there.

The one place bare TS is not enough is stated up front rather than hidden: **re-discovery after a change** (§3, [F3]). TS's exploration is proportional to *current* posterior uncertainty, and a confidently-bad arm that suddenly becomes good is not uncertain — so TS, like UCB and greedy, re-discovers it slowly (a 4.8% share where the oracle routes 63.5%). This is not a reason to pick a different sampler; it is the reason #8 is a *dependency of this decision*. The routing algorithm is **Thompson sampling plus drift detection**, and either half alone is worse than the table it replaces: the drift detector without a sampler cannot act on what it detects, and the sampler without the detector cannot re-open arms it has written off.

## What this binds on later tickets

- **#7 (posterior update protocol).** Arms are `(context bucket × processor)` with categorical bucket keys; update is two increments with `timeout` excluded from the auth posterior (ADR-0002). This ADR adds: (a) the prior is Jeffreys plus an offline cold-start prior (#11), (b) a forced-exploration budget must cover arms a learned-state gate excludes (ADR-0002 §"Why 3DS…" finding 3), and (c) a *tempered-TS* knob (clip the sampled draw's influence, or floor propensities) is the sanctioned way to reduce variance — it is not a switch to argmax.
- **#8 (drift detection).** This decision makes #8 load-bearing: the mechanism that resets/shrinks a posterior on detected change is what makes the bandit *re-discover* (§3, §5). #8 is not an optional add-on; if it cannot beat a weekly frozen table on real traces, Reopen trigger 1 fires.
- **#9 (censoring).** Unchanged by this ticket: the only censored class is `timeout` (ADR-0002); counterfactual arms are IPS territory (#15), not the online update's concern.
- **#15 (off-policy evaluation).** TS propensities = posterior probability each arm is best, over the *eligible* set, with the eligible set logged (ADR-0002). Deterministic policies (table, argmax, frozen export) have degenerate propensities and must be evaluated only on their exploit-phase windows — another reason they are not the control policy.
- **#13 (state store).** 16 bytes per arm + a timeout count (unchanged from ADR-0002), plus a new *policy snapshot* table for the audit export (schema-versioned, `generated_at`, `ttl`, same artifact contract as ADR-0001 §5).
- **#17 (benchmarks).** In addition to ADR-0002's five definitions, the algorithm baselines are now fixed: UCB, ε-greedy (two ε), EXP3, A/B-rebalance, and the two static tables (`table_global`, `table_snapshot`). Every reported number carries estimate quality (staleness MAE) next to margin, because the F3 finding — a reward-only report would have hidden the re-discovery gap — is only visible with both.
- **#12 (module layout).** `router.Decide` must return, alongside the action, the sampled θ̂ and π̂ and the propensity (the four R14 fields), as value types — no new interface surface beyond what ADR-0001/0002 already implied.

## Consequences and design rules that follow

**Positive.** One learning rule to implement, test, and explain; propensities and the audit trail are the same artifact; 16-byte arms and a two-increment update keep the hot path allocation-free; exploration anneals with no schedule; the merchant-facing answer to "why" is a recorded posterior, strictly better than a weight table's lottery; the aggregate "what is your policy" question is preserved via the snapshot export, so compliance keeps its one-line table.

**Accepted costs, named.**
1. The live policy is a distribution, not a fixed vector — the export is stale by construction. Accepted; the export is labeled a *derived artifact*, not the policy.
2. Two identical-looking transactions can route differently during exploration. Accepted; this is the exploration, it concentrates in steady state, and deterministic segments are a per-segment constraint, not a global mode.
3. Bare TS does not re-discover a changed world; the fix is #8, which is now on the critical path and cannot be descoped.
4. The spike's magnitudes are properties of `synthetic-fleet-v1`; the *orderings* (table < A/B < EXP3 < ε < UCB < TS ≈ freeze ≈ greedy) and the *mechanisms* (stale table keeps paying the outage; uniform re-measurement tax; confidence-bound lag; importance-weighting variance; discovery needs a reset) are the load-bearing findings, and they are the claims this ADR rests on.

**Design rules (continuing ADR-0002's numbering):**
- R15 — production routing is Thompson sampling. No deterministic argmax mode, no ε-schedule, no frozen-table control path. (Tempered TS, if ever needed, is a #7 knob, not an exit from this rule.)
- R16 — every decision logs the eligible set, the sampled θ̂ and π̂, the chosen action, and the propensity, in that order (extends R14). No decision may be logged without a propensity.
- R17 — a human-readable policy snapshot (frozen weight table derived from posterior means) is emitted on a configurable cadence as an audit artifact. It is schema-versioned and never read back as the routing policy.
- R18 — the arm space is bucketed on categorical keys only (BIN class, currency/region, SCA, mandate). No continuous-feature model in v1; introducing one reopens this ADR (trigger 2).

## Reopen triggers

Revisit this ADR if any of the following becomes true — each is falsifiable by a number:

1. **The drift detector cannot earn its keep.** On production traces, #8's re-optimization benefit fails to exceed the cost of a weekly frozen table (i.e., measured bandit+drift ≤ table refreshed weekly, sustained over two drift events). Then the hybrid flips: bandit offline to *learn* the table, frozen table in production — and this ADR's R15/R17 swap roles.
2. **A continuous or high-cardinality feature set appears** that measurably predicts auth *across* buckets (per-card issuer, amount-continuum, network-token presence, merchant-risk features) better than bucket keys alone — then LinUCB/contextual reopens, and #7's arm granularity is re-derived, with ADR-0001's ONNX path as the inference escape hatch.
3. **A regulated segment requires deterministic, pre-registered routing.** This is a per-segment constraint routing through the snapshot export, not a global switch — but if the *majority* of volume becomes deterministic-routing-mandated, the frozen-table export becomes the control policy for that majority and this ADR is effectively superseded for it.
4. **TS's exploration variance measurably hurts** — e.g., propensity-weighted analysis shows a large-ticket sampled onto a below-floor arm more often than the amount-band model (#7) can absorb. Then tempered TS / minimum-propensity floors are adopted (still TS family), and #7 owns the calibration.
5. **The reward turns adversarial** — a processor demonstrably gaming the estimator (e.g., rewarding-and-clawing to inflate its rate). That is the regime EXP3 exists for; it is out of scope until there is evidence, and then it is a *separate* ticket on adversarial routing, not a tweak here.

## Alternatives considered

- **Static weight table** (the ticket's own counter-proposal, in its strongest two forms). *Global 60/30/10*: −4,858 ¢/1k — wrong for whole BIN classes and blind to time. *Per-BIN-class snapshot, never refreshed*: −1,711 full-run / −4,383 post-drift, with a 15.3% timeout rate post-outage and 9.18-pt staleness. Both are, in the end, the bandit with learning disabled; the steelman is that they are trivially auditable, and that claim survives — which is why the *export* form is adopted while the *control* form is rejected.
- **Deterministic argmax of the posterior (the "deterministic production" hybrid).** Margin-competitive in the spike (+147 ¢/1k, within noise) — TS minus the exploration. Rejected not on margin but on structure: no uncertainty ⇒ no propensity ⇒ OPE undefined, and no signal to trigger re-estimation after drift. The closest thing to "deterministic in production" that is defensible is TS in steady state, where the posterior concentrates and sampling is near-deterministic without the cliff.
- **UCB.** Rejected on evidence (−1,101 / −1,689 post-drift): the confidence bound is a fixed interval that lags step changes and never anneals to zero, and UCB needs a separately-computed propensity for #15.
- **ε-greedy.** Rejected (−336 / −688): a permanent noise floor keeps money on known-bad arms forever, and its exploration is uniform, not value-directed.
- **EXP3.** Rejected (−1,200): built for adversarial reward sequences; on stochastic Bernoulli margins it pays the importance-weighting variance tax, and its state (importance weights) cannot answer "why this transaction" with a rate.
- **A/B testing with periodic rebalancing.** Rejected (−4,492 / −4,599): uniform re-measurement every cycle and a clock-based rebalance lag. The right tool for a one-off comparison; the wrong tool for continuous routing.
- **Contextual bandit (LinUCB) / full RL / Bayesian optimization.** Rejected in §4: categorical keys make LinUCB degenerate to the bucketed model; the myopic reward (ADR-0002) makes RL the wrong object; BO optimizes the opposite problem shape.

## Appendix: reproduction

```bash
python3 spikes/0003-bandit-vs-table/bandit.py 40000     # ~10s, deterministic, stdlib only
```

Deterministic (fixed seed, no network); RESULTS.md is the committed output of exactly this command. Coupled randomness: the scenario (context + per-processor outcome) is generated once, policy-independent, so policy gaps are not simulation noise. The 3DS channel is held out (decided by #4/#11) because it multiplies the same number every algorithm would see; the reward is held to ADR-0002's chosen form so the only thing that varies is the algorithm. Limitations stated plainly: one synthetic fleet, invented rates, arms bucketed by BIN class only (region enters the outcome model, not the arm key), and no drift detector in the loop — which is why the re-discovery finding in [F3] is a *dependency claim on #8*, not a simulation of #8.
