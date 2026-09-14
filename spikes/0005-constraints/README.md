# Spike: does a compiled ConstraintSet actually decide anything, and can it explain itself?

`policy.py` is the evidence behind
[`docs/decisions/0004-constraint-layer.md`](../../docs/decisions/0004-constraint-layer.md).
It is a working compiler+evaluator for the artifact the ADR proposes — the schema in
[`constraints/schema/`](../../constraints/schema/), the fixture catalog and the two merchant
documents in [`constraints/examples/`](../../constraints/examples/) — loaded through the real
CI checker, not a parallel copy of it. Read the module docstring first: it lists the sections
and what question each one answers.

```bash
python3 policy.py 100000     # ~100s, stdlib only, no network, fixed seed
python3 policy.py --section=F 20000    # one section while iterating
```

The ticket asks for a schema, an enforcement model and precedence rules. Three of its six
considerations are questions about text, so the ADR answers those by reading the schema. The
other three are questions about behaviour, and this file exists to make them measurable.

## What this is designed to catch

- **"Nothing is legal" is not one thing.** `[F1]` censuses every decision into four outcomes:
  *routed*, *deferred* (a velocity/reattempt rule handed the attempt to the scheduler),
  *prohibited* (a scheme ceiling or hard stop did what it exists to do) and *unroutable* (the
  candidate set is empty and no rule may be waived). Only the last is a defect, and
  collapsing the four is how a review concludes the router is "broken 40% of the time" when
  it is behaving exactly as configured. `[F2b]` is the payoff: three document edits, no code
  change, and the unroutable share goes to zero.
- **A conflict has a minimal witness, not a wall of denials.** A sequential filter's per-rule
  denial sets do not compose — an arm removed by rule A is never seen by rule B — so
  `[F1]`/`[F5]` report the *minimal subset* of rules whose conjunction empties the set, found
  by re-evaluating the pipeline with only that subset active.
- **Precedence is for relaxation, not for legality.** `[F6]` permutes the merchant's declared
  order and the eligible set is identical on 100% of decisions: order decides *which* rule is
  waived when nothing is legal, never what is legal. That is what makes precedence safe to
  expose to a merchant.
- **A ring counter that is one bucket too small fails in the unsafe direction.** `[M3]`
  measures a ring against an exact window in both directions, on traffic-shaped and on
  daily-cadence streams: at exactly `slots` physical buckets it *under*-counts (30,382 of
  200,000), which is the direction scheme penalties are assessed on. One guard bucket flips
  the error to the over-count side and costs 0.77 MB per 500k keys.
- **The per-arm guard is not the filter.** `[PV]`-P3 is the counterexample the enforcement
  model has to answer: a guard asking "does *this* arm satisfy the mandate?" refuses 48.7% of
  the decisions the set-level filter routes. Both must run one evaluator.

## What is deliberately not measured here

Acquirer outcome behaviour is not simulated — the traffic generator has no networks and no
issuer. What is measured is the constraint layer's own cost, verdicts, conflicts, counters
and audit bytes. Processor economics and capability come from the committed fixture catalog
and are **invented**; magnitudes belong to `the fixture fleet`, and the findings are the
orderings, the direction of each error, and the fact that a document edit moves the census
more than any code path could. Real production constraint verdicts are #17's benchmarks.

`RESULTS.md` is generated output — regenerate it, do not edit it by hand.
