# Spike: 3DS/SCA friction — what the decomposition is for, and what has to be logged first

`sca_friction.py` is the empirical evidence for #11: the Stripe-style design question of how
3DS/SCA outcomes enter the reward and the routing decision. ADR-0002 already answered the
*scoring* half of it — the label is end-to-end, an abandoned challenge is a lost sale, and a
multiplier on top of θ double counts. What #11 owns is the *observation* half: whether
`P(challenge)` and `P(abandon | challenge)` are identifiable from what a router actually logs,
what they buy when they are, whether ADR-0002's own reopen trigger has fired, why the
exemption lever cannot be a learned arm, and what the mandate flag changes about scope.

It executes against a spike-local gated world, [`sca-friction-v1.json`](sca-friction-v1.json)
(`sca-friction-v1@sha256:8089760b6cd8aadbf9e`, an overlay on `baseline-steady-v1`,
extending it so the two axes the ticket is about are deliberately decorrelated: delta is the
fleet's auth-strongest processor with a high-friction 3DS flow, charlie is auth-weak with a
frictionless one, and echo has no 3DS capability at all). The `three_ds` block is the ADR-0005
schema's; the per-vertical abandonment multipliers are spike-local constants, labelled as
model inputs in the module docstring.

```bash
python3 sca_friction.py 30000                 # ~14 s, stdlib only, deterministic
python3 sca_friction.py 30000 --section=S5    # one section
python3 sca_friction.py 30000 --digest        # sha256 of the primary output text
python3 sca_friction.py --digest              # n=30,000 default
```

`RESULTS.md` is generated output — regenerate it, do not edit it. The digest of the output is
stable across runs at a fixed n.

## What each section is for

| Section | The question | The kind of answer |
| --- | --- | --- |
| `S0` | What does this fixture's fleet actually look like? | the funnel census, so the reader can separate fixture numbers from the published bands the ADR cites |
| `S1` | Which logging regimes can estimate `P(challenge)` / `P(abandon \| challenge)` at all? | per-arm estimator comparison — session ledger vs naive subtraction vs authorization stream — plus a constructed two-world pair that no authorization-only dashboard can separate |
| `S2a` | Does the decomposition buy a better cold-start prior? | bias/RMSE of prior variants for a new arm whose bake-off mix is not its production mix |
| `S2b` | What does it buy in detection? | the data cost of noticing a 12-pt frictionless regression on the component vs end-to-end |
| `S3` | Has ADR-0002's Reopen Trigger 1 fired? | abandonment loss as a share of expected margin, per processor and fleet-wide, with the crossing point for each |
| `S4` | Can the exemption claim be a learned arm? | the claim share each rule picks and the money each moves, swept over the merchant's fraud rate |
| `S5` | What does `mandate=true` change? | the scope predicate priced by an oracle (MIT out of scope vs in scope, and a fatal-challenge variant), plus the arm-key check |
| `S6` | Does the reward charge the fee the ledger shows? | shipped loss term vs incurred fees per attempt and the rank flip count |

## Headlines

- **`S1` — the estimates have a precondition: a session-outcome log.** With a 3DS server but no
  session terminal status, the naive subtraction invents abandonment for exactly the arms that
  challenge least (+24.7 / +34.6 / +25.2 pts for alpha / bravo / charlie at n=30,000, against
  +8.4 / +10.7 for the two high-challenge arms) and inverts the ranking a prior built on it
  would carry. The authorization stream alone is worse than noisy: it is a different estimand
  (foxtrot's 79.9% conditional beats delta's 92.6% on paper while its capture is 12 points
  lower). The constructed pair makes the point dead: two worlds with the same
  `P(challenge) × P(abandon | challenge)` product (7.0%) clear 88.5% / 88.8% of sessions and
  are indistinguishable in an authorization-only dashboard.
- **`S2a` — on this fixture the decomposition does not buy a better prior, and the ADR says so.**
  The direct bake-off rate carries +2.40 pts of bias into a new arm's travel mix; the honest
  fixed-effect composition carries +2.44, the pooled-ratio version +1.35, and the naive
  composition wins (−0.10) only by cancelling two errors against each other. R50's `r̂` stays
  the direct rate; a composed prior ships only if a committed benchmark shows it beating direct
  on a held-out mix.
- **`S2b` — it buys detection, by 6–50×.** A 12-pt frictionless collapse moves the challenge
  rate by ~11.2 pts (detected in ~115 attempts) and the end-to-end capture rate by −1.20 /
  −2.70 / −4.13 pts (detected in 6,089 / 1,460 / 708). The reward never reads the component;
  the alerting does.
- **`S3` — Reopen Trigger 1 is not met, and now has a number.** Fleet-wide the abandonment loss
  is 4.8% of SCA margin (6.6% with every processor at the band's high end, 6.9% with a 15-pt
  frictionless collapse on top); per processor it spans 1.3–19.0%, and the two that reach the
  crossing inside the fixture's clamp do so at 1.34× (≈38% abandonment, delta) and 1.98×
  (≈66%, foxtrot) — the two with the thinnest per-attempt margin, because the trigger's
  denominator is margin, not volume.
- **`S4` — the exemption claim cannot be a learned arm.** The online objective claims 100% of
  exemptions in every fraud-rate row; the net objective declines claims as the fraud rate
  rises, and the net delta is negative from 0.5× (−1,917 c/1k) to 4× (−37,340). Fraud loss is
  out of the online reward (ADR-0002 R11), so no amount of exploration prices this — it is a
  constraint plus a signed liability budget.
- **`S5` — mandate is a scope predicate, not a bucket.** With MIT correctly out of SCA scope
  the traffic clears 83.0% capture / 27,652 c/1k; ignoring the flag costs 1,501 c/1k (5.4%),
  and 2,719 c/1k under the fatal-challenge model. The eligible-set widening (echo becomes
  legal for MIT) is real but never wins this catalog's oracle (`to echo` 0.0% in every row),
  while the arm-key split itself is inside the fixture's noise (±0.1%).
- **`S6` — the loss term prices a submission that never happened.** An abandoned challenge
  submits nothing, so ADR-0002's `abandoned → −fee` row charges the wrong fee; what *is*
  incurred is the 3DS server's per-session authentication fee (2 c here), which the score does
  not charge at all. The correction moves the loss term by +683 to +1,643 c/1k of attempts and
  re-ranks nothing (0.00% of contexts flip) — cost honesty first, and material wherever
  attempt fees are larger than 2 c.

## Not in scope here

The rules are ADR-0010's; this file only measures. The harness gap the spike works around —
`spikes/0006`'s `context()` derives `sca` from region and exemption only, with no mandate
carve-out — is a payload item for #17/#12, not a change made here. The spike's
`auth_fee_minor` constant stands in for a catalog field the acquirer catalog does not carry
yet (#5's). And "echo is never the cheapest MIT rail" is a property of this catalog: the
eligibility rule is regulatory and binding either way, but a fleet where the non-3DS acquirer
is cheapest needs its own run before anyone quotes [S5]'s eligible-set half as a gain.
