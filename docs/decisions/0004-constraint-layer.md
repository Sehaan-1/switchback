# ADR-0004: ConstraintSet — a closed-vocabulary document, filtered before sampling

Resolves [#5](https://github.com/Sehaan-1/switchback/issues/5). Evidence:
[`spikes/0005-constraints/`](../../spikes/0005-constraints/RESULTS.md) — a compiler and evaluator for the
actual schema, loaded through the repository's own gate (not a parallel copy of it). Reproduce
with
`python3 spikes/0005-constraints/policy.py 100000` (~100 s, stdlib only, fixed seed).

## Decision

- **A ConstraintSet is a versioned, hashed JSON document** whose rule vocabulary is *closed*:
  six families, fifteen rule heads, each with typed `params`, an optional closed `predicate`,
  and an **enforcement point declared by the rule head in the schema**
  (`constraints/schema/constraint-set.schema.json`, v1.0.0, JSON Schema draft 2020-12). A
  merchant configures by selecting heads, parameters and predicates. There is no expression
  language and no merchant-supplied code path.
- **Enforcement is a filter before sampling**, at six declared points — `candidate` and
  `transaction` (the only two that can refuse an arm), then `plan`, `ordering`, `lease` and
  `presentation` (which cannot). The bandit only ever sees a legal arm set (ADR-0003 R17), and
  the submit-time guard re-runs the *same* evaluator over the whole set instead of asking each
  arm about itself.
- **Five outcomes, named distinctly, because "nothing is legal" is not one thing**: `routed`,
  `waived` (legal only after a bounded, recorded relaxation), `deferred` (a velocity or
  reattempt rule handed the attempt to the scheduler) and `prohibited` (a hard stop or a
  cumulative ceiling did exactly what it exists to do) are all the layer working; only
  `unroutable` — an empty candidate set that nothing may waive — is a defect, and it escalates
  rather than declining (ADR-0002 R10).
- **Precedence is explicit and narrow**: a platform-fixed prefix `["regulatory","network"]`,
  then the merchant's own permutation of `["budget","econ","mandate"]`, then `preference` last,
  with ties inside a tier resolved by intersection. Precedence decides *which relaxable rule is
  waived when nothing is legal*; it never decides what is legal (measured: permuting it changed
  0 of 20,000 eligible sets). A waiver requires an operator-granted escape budget *and* the
  rule's own declared scope; regulatory and network rules cannot declare one at all.
- **Every decision emits one flat, hashable audit record** carrying the eligible set, a verdict
  for every rule that matched, the minimal conflict set when the eligible set is empty, which
  rule was waived at which rung, and both the document hash and the catalog hash.

## Context

Three facts about this project's shape change how the evidence should be read.

1. **This schema is the interface between a merchant's intent and the engine.** ADR-0001 puts
   the evaluator in Go on the hot path with no network hop and no allocation (R1, R3); ADR-0002
   R13 already required `floor_margin` and the scheme retry limits to be constraints evaluated
   *before* sampling rather than penalties; ADR-0003 R17 required the eligible set to be logged
   because TS propensities are computed over it. Those three rules make the constraint layer a
   dependency of the bandit, not a feature beside it.
2. **The numbers come from a fixture fleet with invented economics.** Six processors in
   `constraints/catalog/acquirer-catalog.example.json` and two merchant documents
   (`merchant.northwind.default`, 15 standing rules, the ordinary merchant;
   `merchant.aurora.strict`, 10 standing rules, the regulated one). Magnitudes belong to that
   fixture; the load-bearing findings are the direction of each error, the size of the census
   move a *document edit* buys, and the fact that the enforcement point is a schema property
   rather than compiler lore.
3. **"Nothing is legal" is the case that decides whether the layer is trusted.** A router that
   silently routes around a merchant's floor, or declines when it should escalate, loses the
   operator permanently. The census, the conflict witness and the audit record are therefore
   part of the decision, not commentary on it.

## 1. Constraint taxonomy: six families, fifteen rule heads (consideration 1)

| Family | Heads | What it expresses |
| --- | --- | --- |
| `regulatory` | `data_residency`, `sca_required` | which acquirer regions card data may be processed in; when SCA is required and which exemptions count |
| `network` | `reattempt_limit`, `hard_stop`, `retry_schedule` | windowed attempt ceilings, terminal decline categories (Visa Cat 1, MAC 03), issuer-prescribed waits |
| `budget` | `attempt_cap`, `chain_depth`, `reservation` | per-key velocity caps, how many acquirers one transaction may visit, in-flight leases |
| `mandate` | `require_3ds`, `must_process`, `exclude_processor`, `domestic_acquirer` | processor and route requirements, blacklist with expiry, domestic/regional acquiring |
| `econ` | `floor_margin`, `max_cost` | the merchant's margin floor on a named basis; the most a transaction may cost |
| `preference` | `rank` | ordering tiers and fallback — an ordering rule, never a filter |

All fifteen heads are exercised by the two committed documents: `merchant-default.json` uses 14
of 15 (it has no domestic mandate), `marketplace-strict.json` uses 10, and the union is all 15.
The heads, their parameters, their enforcement points and the worked examples are in
[`constraints/README.md`](../../constraints/README.md); the schema is the source of truth and
the checker is what makes it one.

**Deliberately not in the taxonomy**, because putting them there would make them
merchant-configurable:

- **Capability** — accepts this currency, is 3DS-capable, serves this market — is a fact about a
  processor, not merchant policy. The schema fixes `fail_mode.missing_capability` to the const
  `exclude`. Measured: capability is the single largest source of arm denials (427,210 denials
  across 100,000 decisions) and the one source that is not a conflict at all — an arm that
  cannot do the job was never a candidate.
- **Price** lives in the versioned catalog, not in the document. The merchant expresses
  thresholds only: `econ.floor_margin` (with a *required* `params.basis`, which resolves
  ADR-0002 §5's platform-versus-merchant margin ambiguity by making the document say which) and
  `econ.max_cost`.
- **Idempotency and double-charge protection** belong to
  [#10](https://github.com/Sehaan-1/switchback/issues/10). The layer exposes the primitive it
  needs — `budget.reservation`, an in-flight lease with a TTL — and this ADR fixes the
  *counting* semantics under it (§5); the settlement protocol is #10's decision, not a rule
  head here.
- **Regional coverage** (which markets an acquirer serves) is catalog data. The spike ships a
  counterexample to the alternative: a document that mandates an acquirer for a region where no
  acquirer has coverage manufactures a permanent `unroutable` class — a mandate with no
  capability behind it is not a constraint, it is a bug that looks like a policy.

## 2. Expression language: a closed vocabulary, not a DSL (consideration 2)

**Decision: JSON Schema with named rule heads, not an expression language (CEL, JsonLogic,
Rego, a hand-rolled DSL) and not a protobuf-only config.** A merchant never writes a formula;
they write `{"rule": "econ.floor_margin", "params": {"basis": "merchant_take",
"value_bps": 25}}` and, if they need to scope it, a predicate from the closed field set.

The rule grammar lives in `$defs/rule-<family>-<name>`, which has three consequences worth
naming:

1. **A rule head that does not exist cannot be referenced.** `network.retry_limit` is rejected
   at compile time by name (`examples/invalid/invalid-unknown-rule.json`), never silently
   ignored.
2. **`params` is where the specificity lives, and it is typed.** A counter ceiling that a
   uint8 slot cannot hold is refused (`invalid-counter-arithmetic.json`); a predicate field
   that is not in the context is refused (`invalid-unknown-field.json`); a predicate whose
   value type contradicts its field is refused with the field named
   (`invalid-type-mismatch.json`). A false-at-3am typo cannot reach production, because the
   document does not compile.
3. **The enforcement point is part of the head.** `properties.enforcement` is a `const` per
   rule head, checked by SV12, and the compiler reads it from the schema rather than from a
   table beside the evaluator. `invalid-scope-moved.json` is the fixture that proves a document
   cannot move a law or a scheme rule to a cheaper point of evaluation.

**Determinism is a property of the language, not a promise about reviewers.** Every rule is a
pure function of (document, request/route context, counter snapshot): no clock, no random, no
I/O, no reference to learned state. Property P2 replays every verdict and reason code
identically, which is what makes the audit record in §6 a replayable artifact rather than a
narrative. P1 adds the composition property: adding a constraint never *adds* an arm (0
violations / 5,000 decisions), so two documents cannot be combined into a weaker one.

**Configuration without writing code** looks like this, end to end: pick a head → set params →
optionally add a `when`/`after` predicate → pin `policy.catalog_hash` → run
`python3 constraints/check.py`. The 39 `$defs` and ~56 kB of schema are what a merchant's
config UI renders; the checker is what a CI job runs (nothing wires it yet — that lands with
#17's gates); and the census (§4) is what tells a merchant
their document is *satisfiable*, which no validator can.

**The escape hatch, bounded.** `constraints[]` carries single-transaction refinements — pin
this transaction to processor X, exclude Y for ten minutes — with a required `request_ref`, a
TTL capped at `PT1H`, and no ability to declare `relaxable` (SV10). These are operations
actions, not policy: they never become standing rules, and they are exactly the case the spike
first got wrong (evaluating them as global rules inflated denials). Pinned-request behaviour is
measured in `[F5]`, including what happens when the pin demands a processor the floor blocks
(`without the pin: still empty` — the pin is not an override).

**Counter-evidence, stated plainly.** A closed vocabulary cannot express a novel constraint,
and a merchant who needs one waits for a schema change. That is the accepted cost of the
guarantees above. The instrument that makes the cost visible is the census: `[F6]` found the
`domestic_acquirer` gap (§4) from a *measured* 0.00-arms case, not from a support ticket.

## 3. Enforcement point: filter before sampling, seal at submit (consideration 3)

**Decision: filter the arm set before the bandit samples, then re-assert at submit with the
same evaluator.** The six enforcement points, and what each may do:

| Enforcement point | Rules in the fixture documents | What it may do |
| --- | --- | --- |
| `candidate` | northwind 7, aurora 5 | remove arms from the candidate set (per decision, hot path) |
| `transaction` | northwind 3, aurora 3 | empty a set, defer or prohibit the whole attempt (hot path) |
| `plan` | northwind 1, aurora 1 | shape the fallback chain depth |
| `ordering` | northwind 2, aurora 1 | reorder the eligible arms; never remove one |
| `lease` | northwind 1 | account for attempts in flight but not settled |
| `presentation` | northwind 1 | gate the retry scheduler outside this decision |

Measured cost of the filter itself, CPython 3.11 on the fixture documents (`[M1]`, 100,000
decisions): northwind filter **35.00 µs/decision**, guard **6.32 µs/arm × 3.86 arms**; aurora
filter **30.92 µs**, guard **83.47 µs/arm × 0.26 arms**; per-decision total for aurora
**52.74 µs CPython**. Against ADR-0001's 20 µs p99 in-engine budget, that is **26.4%** of the
budget at a 10× compilation factor and **8.8%** at 30×. The filter is O(candidate arms), reads
only the request, the route and a counter snapshot, and allocates nothing but the pre-sized arm
slice — so it cannot reintroduce the allocation or the network hop ADR-0001 forbids. Real Go
numbers are #17's, in `go test -bench`; this is the pessimistic instrument, and it says the
filter is not what the budget is for.

**Why after-the-fact veto loses.** ADR-0003 R17 makes the propensity the posterior probability
that an arm is best *over the eligible set*, and the eligible set is logged. A veto pipeline
samples first and refuses second: the arm was still sampled, still consumed exploration, and —
if its outcome is recorded — contributes to a posterior for an action that was never available
in that context. IPS then reweights over an action set that never existed. The bandit must
sample from the set that is legal at that instant, which is also why the layer must not
"improve" the set after sampling: that would make the logged eligible set a lie.

**Why the guard must be the same code path.** `[PV]`-P3 is the counterexample: a submit-time
guard that asks *this arm* whether it satisfies a `must_process` mandate refuses the
transaction on **48.7%** of the 2,033 corporate-card decisions where the set-level filter
routes it (the mandate's own `if_unavailable: fall_back` is a property of the set, not of any
arm). Both guards run one evaluator over the whole set; a per-arm reimplementation diverges
exactly where money is. The two audit records in the appendix show what that buys: `[A2]`
  traces nine `pass` verdicts and one `partial` (the floor denied two arms and the transaction
  still routed on five), while `[A]` — the escalation — records five `deny` verdicts and names
  all twelve denied arms with their reasons. A per-arm guard could not have produced either
  list.

**The five-outcome census is the enforcement model's observable.** Over 100,000 decisions
(`[F1]`): northwind — routed **83.25%**, deferred **11.01%**, prohibited **5.74%**, *unroutable
0.00%*; aurora as shipped — routed **25.95%**, deferred **11.18%**, prohibited **5.54%**,
*unroutable 57.33%*. The 16.75% of northwind decisions that are not "routed" are **not** failures: 11.01% are the
3-per-day velocity cap handing the attempt to the scheduler and 5.74% are the scheme ceiling
and the Category-1 hard stop doing their jobs. Only the fifth bucket — `unroutable` — is a
defect, and it escalates rather than declining (ADR-0002 R10). `[F2b]` shows the
same census after three document edits — floor 40→30 bps, the domestic rule re-scoped, and a
fallback added — with unroutable at **0.00%** and routed at **82.72%**, mean arms 1.00 → 2.23,
and **no code change**.

## 4. Conflicts and precedence (consideration 4)

### The precedence vector

`precedence` is a required, total vector: `fixed_prefix` (always
`["regulatory","network"]`, declared rather than implicit so the audit record can echo it
back), then the merchant's permutation of the three tradeoff families, then `preference` last.
`tie_break` covers the case where two rules inside one tier disagree: there is no merchant
intent to consult at that point, so the answer is the strictest, i.e. the intersection.

**Precedence is not legality.** `[F6]` permutes the merchant's `order` across the whole traffic
stream: the eligible set is identical on **100.00%** of 20,000 decisions. Order is consulted
only when the merchant has asked for a bounded escape (§ below) and something must be chosen to
relax first. This is what makes it safe to expose the ordering to a merchant at all — a
merchant cannot use precedence to make an illegal route legal, only to say which of *their own*
rules they would rather bend.

Four rules of conflict resolution follow, and each is testable:

1. **Capability intersects first, unconditionally.** Currency, 3DS capability and market
   coverage are not policy and have no precedence position; an arm that cannot do the job is
   never in the set (§1).
2. **The platform prefix is fixed.** A document claiming a different `fixed_prefix` is rejected
   (SV8), and regulatory/network rules cannot declare `relaxable` at all (SV7) — so no
   configuration can put a law behind a merchant preference.
3. **Within the merchant tier, order is theirs; across families, intersection.** A rule is a
   filter, not a vote: two constraints that both match both apply.
4. **When the set is empty, the layer reports the minimal witness set**, not a wall of denials.
   That matters because a sequential filter's per-rule denial sets do not compose: an arm
   removed by rule A is never seen by rule B, so reading the denial map would attribute a
   one-rule fault to a four-rule conflict. The witness is computed by re-evaluating the
   pipeline with only the candidate subset active.

### The ticket's own example, measured

The ticket asks what happens when `must-domestic-acquirer` conflicts with the only acquirer
that supports the required currency. On the fixture fleet the literal reading of "domestic" is
the dominant failure: `[F2]` shows aurora's rule (`match: card_country`) leaving **57.331%** of
decisions unroutable, and the EEA volume that lands entirely on EEA acquirers is only
**41.27%** — because the fleet has four EEA acquirers in four countries, so "the acquirer must
be in the card's own country" cannot be satisfied for FR, IT, SE or PL cards. Widening the
reading to "same region" (`card_region`, or `card_country` with `allow_same_region`) takes
unroutable to **41.061%** and EEA-on-EEA volume to **87.84%**: 16.3 points of the failure were
the rule being read geographically, not the fleet's coverage.

The residual 41.061% is a *different* conflict — the merchant's 40 bps margin floor against a
processor that earns 33 — and `[F2b]` shows what the census buys: three document edits (floor
30 bps, the domestic rule re-scoped, `fall_back` added), no code change, unroutable **0.00%**,
routed **82.72%**, mean eligible arms 1.00 → 2.23. A reviewer who had been shown only the first
number would have concluded the fleet needed another acquirer.

The minimal witnesses are reported per signature (`[F1]`, aurora): `floor-margin-40 +
usd-goes-to-alpha` **36.932%**, `domestic-card-country` alone **15.273%**, `domestic-card-country
+ floor-margin-40` **4.168%**, `domestic-card-country + usd-goes-to-alpha` **0.958%**. The
single-rule witness in the second position is the one an operator can act on today; the pairs
are the ones that need a document revision.

### A schema change the census forced

`[F6]` compares three ways to express the same intent on **UK** cards (where "region" stops
meaning one thing):

| Form | Arms/decision (UK cards) |
| --- | --- |
| `match: card_country` (as shipped) | 0.00 |
| the same rule, waived entirely | 1.00 |
| `match: card_region` (re-scoped) | 0.00 |
| `params.acquirer_regions: ["EEA","UK"]` | 1.00 |

No acquirer is domiciled in GB, and "UK" is not an acquirer *region* in the catalog — so both
geographic readings leave the set empty, while waiving the rule drops the guarantee compliance
signed for. The merchant's actual intent is a **regulatory** region (the set of regions an
acquirer may legally serve), which the residency rule could already express as a region list.
The schema therefore grew `params.acquirer_regions` on `mandate.domestic_acquirer`:
**backwards-compatible** (the `match` form is untouched, and `anyOf` requires one of the two),
same catalog field, same shape as `regulatory.data_residency`, validated through the same SV4
path. The measured fix is the same rule head with one param replaced, which is the point: the
ladder did not grow a waiver the compliance team never signed, and the audit story is unchanged.

`[F6]` also measures waiving-versus-re-scoping on EEA traffic with residency and the floor
waived in every row, so the only variable is the domestic rule: **0.52** arms/decision as
shipped, **4.51** with the rule waived, **3.52** re-scoped to `card_region`. Waiving recovers
all 4.00 of the arms the rule was holding back and stops constraining anything; re-scoping
recovers 3.00 of them and keeps a checkable guarantee. A ladder that can only waive has no middle rung, and an audit log that records
"relaxed" without saying which of the two happened cannot tell them apart afterwards.

### Bounded relaxation, and what escalation is for

`relaxable` is a per-rule, signed, expiring permission with a declared scope
(`single_transaction` / `traffic_share` / `unbounded`), and it is unavailable on regulatory and
network rules by construction (SV7). Two independent promises bound it: the operator's global
escape budget (first come, first served) and the rule owner's declared share. `[F1b]` on
aurora:

| Global budget | Waived | Unroutable |
| --- | --- | --- |
| 0.00% | 0.000% | 57.331% |
| 0.10% | 0.075% | 57.277% |
| 1.00% | 0.740% | 56.821% |
| 2.00% | 1.462% | 56.353% |
| 30.00% | 26.742% | 32.227% |
| unbounded | 58.942% | 0.000% |

The interesting row is the last one. An unbounded budget does make the document satisfiable —
by waiving the two rules that carry its intent, on 58.9% of traffic. That is not a fix, it is a
policy change nobody reviewed, which is exactly why the default is a *bounded* budget whose
residual escalates with the minimal witness attached. `[F5]` walks one transaction up the
ladder rung by rung (0 → 1 → 2 → 3 arms as `usd-goes-to-alpha`, `domestic-card-country` and
`floor-margin-40` are waived in turn), records `['domestic-card-country',
'usd-goes-to-alpha']` as the minimal conflict for that context, and shows that without the ops
pin the set is still empty — the pin is not an override.

## 5. The retry budget: three ceilings, one lease, and a counter that fails safe (consideration 5)

The ticket flags retries as a real acquirer pain point, and the research says the pain is
precisely that **several different budgets are all called "the retry budget"**. They are not
interchangeable, and implementing one is not implementing the others.

| Budget | Owner | Fixture value | What it is for |
| --- | --- | --- | --- |
| cumulative reattempts in a window | scheme | 15 per card per 30 days (`network.reattempt_limit`) | staying inside Visa's excessive-reattempt rules; beyond it the fee is ~$0.10 domestic / ~$0.15 cross-border per reattempt |
| velocity (per day / per interval) | issuer | 3 per card per 24 h (`budget.attempt_cap`, `on_exceeded: queue`), plus a pace of 1/day and a 24 h minimum interval on the reattempt rule | not looking like fraud to the issuer of a soft-declining card |
| chain depth | merchant | 2 acquirers (northwind) / 3 (aurora, with an EV test) | how much of the customer's patience and the merchant's fee budget one transaction may spend |
| attempts in flight | platform | `budget.reservation` — key, TTL, `on_timeout` | not double-charging while an authorization is unresolved ([#10](https://github.com/Sehaan-1/switchback/issues/10)) |

Scheme constants the rules encode, with the sources in the spike's research notes: Visa
Category 1 declines never retry (and the fee applies to *any* reattempt), Categories 2 and 3 cap
at 15 per 30 days, Mastercard MAC 03 stops the chain outright, MAC 24–30 prescribe increasing
waits (1 hour to 10 days), and **if no decline category is present Visa's default is not to
retry**. That last default is the one a naive implementation gets backwards, so the schema
makes it explicit where the answer belongs: `params.missing_category`, which the fixture
documents set to `deny` (the enum's other value, `allow`, is there for merchants who would
rather pay for a bounded attempt than abandon the sale).

`[F4]` prices the arithmetic on one soft-declining card over 30 days — attempts, fee-bearing
attempts, fee, and the day the budget runs out:

| Recovery schedule | Attempts | Fee-bearing | Fee (USD) | Last attempt on day |
| --- | --- | --- | --- | --- |
| daily, no cap (naive) | 30 | 15 | 1.50 | 30 |
| daily + 15/30 d cap (ours) | 15 | 0 | 0.00 | 16 |
| every 2 days | 15 | 0 | 0.00 | 30 |
| weekly | 4 | 0 | 0.00 | 28 |
| exponential 1, 2, 4, 8, 16 d | 4 | 0 | 0.00 | 15 |

So "add exponential backoff" is a statement about *pacing*, not compliance: it spends 4 of the
free 15 attempts, while the cap-compliant daily schedule spends all 15 and still pays nothing
extra. The naive schedule is the only one that pays, and it pays because it exceeds the
*cumulative* ceiling on day 16 — which a per-day velocity cap alone would never have caught.
The ADR therefore requires the counters, not the pacing schedule, to be the compliance artifact.

**In flight.** An attempt that has been sent but not settled must hold its slot, or the layer
will happily send a second authorization for the same card and transaction. `[F4]`'s second
block shows the accounting: three attempts, none settled — two recorded provisionally plus two
reserved leases = 4 against a limit of 3, so the fourth attempt is **DEFERRED**, not declined;
at +120 s both leases have expired and the reservation drops to zero while the recorded count
stays at two, because only a late authorization releases the slot for real. At `limit - 1` the
layer is deciding on an upper bound, and erring toward deferral is the only safe direction: the
alternative is a double charge, which ADR-0002 priced at 45 cents of ambiguity cost per
unresolved attempt. The settlement protocol itself is #10's; the *counting* semantics above are
this ADR's.

**The counter must fail in the safe direction.** `[M3]` measures the ring counter against an
exact window in both directions: with exactly `slots` physical buckets it *under*-counts
(30,382 of 200,000 traffic-shaped slots; 90 of 120 on a daily-cadence stream) because it drops
an attempt that is still inside the window — the direction a scheme penalty is assessed on. One
guard bucket (`slots + 1` physical buckets) turns every error into an over-count (30,549 of
200,000, zero under-counts), which can refuse an attempt up to one bucket early and never
permit an extra one. That is not an optimization; the exactly-`slots` version is a bug with a
fee attached. Cost: 0.77 MB per 500,000 keys per 30-day window, and 23.5 MB for 500,000 live
keys at 31 physical uint8 buckets — fixed layout, bounded by the key count, where a
timestamps-in-a-list design needs ≥234 MB at the same key count *and* grows with attempt
volume. The compiler also refuses a configuration the counter cannot hold (a 400-attempt limit
in uint8 slots, `invalid-counter-arithmetic.json`, SV5).

## 6. Auditability: one record per decision, replayable without the engine (consideration 6)

Every decision emits one flat, JSON-serializable record (`[A]`, `[A2]`) with 13 top-level
fields, **2,328 canonical bytes** for a normal northwind decision and **1,940** for an
escalation, hashable as-is:

- `constraint_set {id, revision, hash}` and `catalog {version, hash}` — the exact pair of
  artifacts the verdict is valid for. A decision made under a catalog with a typo'd processor id
  is distinguishable from one made after the fix.
- `request` — the fields the rules actually read (currency, amount, card region/country, BIN
  class, mandate, SCA required/exemption).
- `eligible` — the eligible set, in order, which is what ADR-0003 R17 needs for propensities and
  #15 needs for IPS. A record without it cannot be reweighted.
- `pass_trace` — for **every** rule that matched: id, rule name, enforcement scope, verdict and
  how many arms it denied. Not only the denials: an audit that shows only refusals cannot prove
  the routes that *were* allowed were allowed for the right reason, and the `partial` verdicts
  in `[A2]` (the floor denied two arms and the transaction still routed on five) are the
  difference between a filter and a veto.
- `denied` — each arm and its reason code, sorted, so a dispute is answered with identifiers
  rather than prose.
- `conflict_set` — the minimal witness when the eligible set is empty; `fail_mode` — what the
  layer did about it (`escalate` in the fixture documents, never a decline).
- `precedence` — the compiled rule order, so the waiver order in force is part of the record.

Three properties make it an artifact rather than a log. **Deterministic** (P2): verdicts and
reason codes are identical on replay, because the language has no clock, no random and no I/O.
**Complete by construction**: the compiler enumerates the rules that matched, so a rule cannot
be added to a document without appearing in the trace. **Bounded in volume**: at 5,000
decisions/s the trace is ~7 MB/s, ~600 GB/day uncompressed, which is why ADR-0001 R1 keeps the
trace writer off `Decide()`'s path and why
[#13](https://github.com/Sehaan-1/switchback/issues/13) owns compression and the retention
period (400 days in these documents, with `audit.chain` available for hash-chaining).

One requirement is easy to miss and is written into R26: **a relaxation must be recorded with
which rule, at which rung, under which budget**, and a re-scope must not be recorded as a
waiver. The `[F6]` measurement above (0.52 / 4.51 / 3.52 arms per decision for the same rule) is
the reason — after the fact, "relaxed" without the rule id and the rung cannot distinguish a
merchant who gave up a guarantee from one who moved it.

## What this binds on later tickets

- **[#6](https://github.com/Sehaan-1/switchback/issues/6) (simulation harness).** The harness
  must emit contexts with the fields the predicates read (`bin_class`, `card_country`,
  `card_region`, `currency`, `amount_minor`, `bin_prefix`, `mandate`, `sca_required`,
  `entry_mode`, `channel`, `route_class`, `deadline_ms`) and must route every decision through
  the *same* compiled document it logs, or the census numbers here do not transfer. The fixture
  catalog's economic fields (`cost_bps`, `fixed_fee_minor`, `attempt_fee_minor`, `lat_p99_ms`,
  `three_ds`, `currencies`, `markets`) are the contract.
- **[#7](https://github.com/Sehaan-1/switchback/issues/7) (arm granularity).** The arm is
  `(processor, flow)` in this spike, and constraints are evaluated per arm, not per processor.
  `[F3]` is the input #7 has been waiting for: with `mode: always` the SCA rule cuts arms from
  4.73 to 3.52 and changes the eligible set on **53.17%** of decisions (18.84% under the
  exemption-evidence mode), and the residency rule changes it on **90.02%** of EEA cards —
  never emptying it, because five of the six acquirers are 3DS-capable and it costs the whole
  arm, both flows, only where a processor cannot do 3DS. So "must-3DS" is a set-sizing question
  for #7, not a flag on the request.
- **[#10](https://github.com/Sehaan-1/switchback/issues/10) (idempotency).** `budget.reservation`
  is the primitive; §5 fixes the counting semantics it must implement (provisional + reserved,
  TTL expiry, late authorization releases, err toward DEFERRED). A `reservation` rule that
  treats an unsettled attempt as settled is the double-charge bug.
- **[#13](https://github.com/Sehaan-1/switchback/issues/13) (state store).** 31 physical uint8
  buckets per counter key, fixed layout, ~23.5 MB per 500k keys per 30-day window, plus the
  trace at ~7 MB/s. Retention 400 days is in the documents; compression and the chain are #13's.
- **[#15](https://github.com/Sehaan-1/switchback/issues/15) (OPE).** The propensity is computed
  over the *logged* eligible set; every record carries it. The 0.00%-waiver default census and
  the 57.33% unroutable share under the shipped aurora document are the two regimes IPS must
  handle (dense vs empty support), and `[F1b]`'s budget sweep shows support is a knob the
  operator can move.
- **[#11](https://github.com/Sehaan-1/switchback/issues/11) (3DS friction).** `regulatory.sca_required`
  decides eligibility before sampling; what remains for #11 is the conversion cost of the arms
  that stay eligible, not the legality of the arm.
- **[#17](https://github.com/Sehaan-1/switchback/issues/17) (benchmarks).** Report the filter and
  guard cost separately from the bandit's sampling cost, at the arm counts #7 fixes, and publish
  the census alongside margin: a report that shows only margin cannot show that 11% of decisions
  were deferred by a velocity cap the merchant chose.

**Two schema changes are part of this decision, and both are shipped:**

1. `mandate.domestic_acquirer` gained `params.acquirer_regions` (a region list, validated by the
   same SV4 path as `regulatory.data_residency`). Backwards-compatible; motivated and measured
   in §4.
2. Every rule head declares its enforcement point (`properties.enforcement`, a `const` per head,
   checked by SV12), and the compiler reads it from the schema rather than from a lookup table.
   `examples/invalid/invalid-scope-moved.json` is the negative fixture.

## Consequences and design rules that follow

**Positive.** The merchant-facing surface is a reviewable JSON diff; the hot path has no
interpreter, no allocation beyond a pre-sized slice, and no network (ADR-0001 R1/R3); every
decision is replayable from its record; and the outcomes other than `routed` are
distinguished well enough that an operator can tell a defect from a policy. The 57.33% unroutable share for aurora
is a *feature of the measurement*, not of the design: it is a real document that cannot be
satisfied, and the census says so before the traffic does.

**Accepted costs, named.**

1. A closed vocabulary means a schema change (with a reviewer and a fixture) for a genuinely
   new constraint. Accepted; the alternative is an expression evaluator in the payment path.
2. Precedence is exposed to merchants but only for relaxation order, so the ordering can
   surprise a reader who expects it to be a legality rule. Mitigated by the permutation test
   (`[F6]`: 0 differences out of 20,000 decisions) and by the audit record echoing the
   compiled order.
3. The guard is a second evaluation of the same predicate set on the same inputs — pure
   redundancy for the happy path, and the only thing that catches a race between filtering and
   submission.
4. The fixture documents are deliberately infeasible in places; anyone quoting the 57.33%
   number as a production expectation is misreading a stress fixture.
5. An unbounded escape budget makes every document satisfiable and destroys its meaning
   (`[F1b]`). The default is bounded, and the residual escalates.

**Design rules (continuing ADR-0003's numbering):**

- **R19** — the constraint vocabulary is closed. A new rule head is a schema change plus a
  checker entry plus a positive and a negative fixture; a merchant never supplies an expression,
  a script, a regex or a formula. Inline `constraints[]` are request-scoped, TTL-bounded, and
  never relaxable (SV10).
- **R20** — constraints are enforced by filtering the arm set *before* sampling. The bandit
  samples only from the eligible set, and the eligible set is what is logged (extends R17).
- **R21** — the submit-time guard re-runs the same evaluator over the whole set. A per-arm guard
  is forbidden; P3 measures the 48.7% divergence it produces.
- **R22** — the census distinguishes routed / waived / deferred / prohibited / unroutable in
  code and in the record. Only `unroutable` escalates, escalation is never a decline (R10), and
  a counter's failure mode is chosen (queue vs deny), never defaulted.
- **R23** — relaxation is bounded and attributed: a waiver requires the operator's budget *and*
  the rule's declared scope, is recorded with the rule id and the rung, and regulatory/network
  rules are not relaxable at all (SV7).
- **R24** — counters are fixed-layout ring counters with the guard bucket (`slots + 1` physical
  buckets). An exactly-`slots` ring under-counts and is a bug with a scheme fee attached.
- **R25** — an attempt in flight holds its slot under a lease until settled; counters read
  provisionally recorded plus reserved attempts, and the layer errs toward DEFERRED.
- **R26** — every decision emits the record of §6: both hashes, the eligible set, the pass trace
  for every matched rule, the minimal conflict set when empty, and the waiver if any. A waiver
  that is not recorded is a bug, not a log gap.

## Reopen triggers

1. **A merchant needs a constraint the taxonomy cannot express** — three or more schema-change
   requests in a quarter, or one that cannot be expressed as a rule head at all. Then reopen the
   expression-language question, with those three cases as the specification.
2. **The filter stops being cheap.** If #17's Go benchmarks put filter + guard above 10% of the
   p99 in-engine budget at #7's arm count, the enforcement model is wrong (not the language) and
   the fix is arm-space or precomputation design, not skipping constraints.
3. **Unroutable escalations exceed 1% of decisions for a month** on documents that pass the
   checker. That means the census is not enough — the checker needs a satisfiability check at
   authoring time, and that becomes its own ticket.
4. **Permuting precedence ever changes an eligible set.** That would mean ordering leaked into
   legality; the design says it cannot, and the permutation test is the tripwire.
5. **A scheme changes the retry constants** (the 15/30-day ceiling, the MAC schedule, the
   fees). §5's numbers are then stale; re-run `[F4]` and re-derive the fixture params.
6. **A ring counter under-counts in production.** The guard bucket is measured to make
   over-counting the only error mode; observing an under-count means the arithmetic in SV5 or
   the engine's implementation diverges from this ADR.

## Alternatives considered

- **An expression language (CEL / JsonLogic / a DSL).** The steelman is real: a DSL expresses a
  constraint nobody anticipated, and every mature platform eventually grows one. Rejected here
  because this layer's job is to *refuse* payments in a regulated path: an expression evaluator
  is a new attack surface, a new determinism argument (does it have a clock? a regex engine with
  backtracking?), and a new review problem (what does a diff of two expressions mean?). The
  closed vocabulary keeps all three properties the ADR needs — compile-time rejection,
  replayability, reviewable diffs — and the escape hatch is a schema change with a fixture, not
  a formula in a config file. If trigger 1 fires, the answer is to grow heads, and only then to
  revisit this.
- **OPA/Rego as the policy engine.** Same objection plus a new runtime dependency and a
  general-purpose language in the request path; also its evaluation model (data documents,
  partial evaluation) does not map onto "filter this arm set with a counter snapshot" without
  more machinery than the rules themselves.
- **Protobuf/typed-config instead of JSON Schema.** Better for wire format and worse for the
  two things that matter here: a schema that also serves as the human-readable artifact a
  merchant reads, and a stdlib-only validator that runs anywhere. The compiled Go form is
  protobuf-adjacent anyway (fixed-layout structs, ADR-0001 R5); the schema is the authoring
  format.
- **Evaluate constraints after sampling and veto.** Rejected on correctness, not cost: the arm
  was sampled, so the propensity recorded over the "eligible" set is wrong for every vetoed
  decision, which breaks #15 and the audit claim that the logged set was the sampled set.
- **A per-arm guard at submit time.** Measured to diverge on 48.7% of the decisions it sees
  (P3). Rejected outright.
- **Unlimited or merchant-configured relaxation.** Makes every document satisfiable and destroys
  the meaning of the constraint (`[F1b]`: 58.942% of traffic waived to reach 0.000% unroutable).
  Rejected; relaxation is a signed, expiring, operator-granted exception with a share bound.
- **Timestamp lists for the counters.** Accurate by construction and rejected on memory: ≥234 MB
  per 500k keys at this window versus 23.5 MB fixed layout, and it grows with attempt volume
  rather than with the key count (ADR-0001 R5).
- **Capability as a rule.** Would let a merchant configure away a processor's inability to do a
  job, and turns "this arm cannot work" into a policy conflict in every census.
- **Region coverage as a mandate.** Measured as a dead end: a mandate for a region no acquirer
  covers converts a coverage gap into a permanent `unroutable` class, which is how the first
  draft of the fixture document turned 8.76% of a stream into escalations. Coverage is catalog
  data.

## Appendix: reproduction

```bash
python3 constraints/check.py                                # both documents accepted, six
                                                            # negative fixtures rejected
python3 spikes/0005-constraints/policy.py 100000            # ~100 s; RESULTS.md is this output
python3 spikes/0005-constraints/policy.py --section=F 20000 # conflict census only
```
