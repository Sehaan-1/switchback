# Spike: what happens when a processor is up but slow

One file, `protocol.py`, driving the ADR-0005
[`harness`](../0006-simulation-harness/harness.py) with a working reference
implementation of the protocol
[ADR-0009](../../docs/decisions/0009-idempotency-double-charge.md) picks: a
per-transaction **lease** that blocks any dispatch while an attempt is
unconfirmed, an **idempotent resend** as the status probe, and a
**confirmed-terminal** classification. It is measured against two bounds: the
**naive** fallback (immediate retry to the next chain arm on a sync timeout —
which is what the ADR-0005 reference driver does today) and the
**clairvoyant** (instant truth; the upper bound on sale recovery for any
protocol that does not double-charge).

```bash
python3 protocol.py            # ~25 s, stdlib only, no network, fixed seed
python3 protocol.py --digest   # protocol-run digests, for cross-process checks
```

`protocol.py` also accepts `run_policy(..., trace_seq=N)` and
`debug=True` for auditing individual transactions — the way [M2] of
`spikes/0006` poisons the clock, these are the means by which the invariants
were found to be true rather than assumed.

## Why Python, again

Same reason as [`spikes/0001-latency-budget/`](../0001-latency-budget/): the
questions #10 asks are not "how fast is Go" but "does the design have the
properties we claim". The double-charge invariant, the lease, and the
classification table are structural properties a language cannot improve on;
the CPython per-transaction cost is reported in [S4] as a labelled band, not a
claim.

## What the model adds to the ADR-0005 world

The world is the ADR-0005 harness, untouched. The spike adds three things on
top, all stated constants with their bands:

- **The record.** For an attempt the engine gave up on (sync timeout), the
  processor either keeps a queryable record of the outcome or it does not.
  `record = (issuer declined) or (u_late < late_settlement.share)` and
  `settlement = (would_approve) and (u_late < share)`, at the fixture's exact
  `late_ms` — the spike re-derives the fixture's settlement channel from the
  same index-addressed draws, and [S1]'s steady-world count (23.9% of
  dispatched timeouts settle late) is the cross-check against ADR-0005 [M5]'s
  published 24.4%.
- **The contract.** `M(P) = 10 × p95`, clamped to [3 s, 15 s]: the point after
  which the router stops trusting that an answer is coming. A confirmable
  processor has finalised its record by M; a record that finalises past M (the
  fixture's GPD tail) is a charge without a sale for reconciliation — the
  cws tail in [S2](b) and [S3].
- **The probing client.** A re-call with the same (txn, attempt) is a status
  query, not a new authorization: the stored final answer once it has
  finalised, else in-flight. In Go this is the same `Authorize` call (same
  request, same `IdempotencyKey`) returning `ErrInFlight` on the error channel
  — no new method, no new field (ADR-0005 R27 holds).

The world's full answer for an attempt is computed **once** and memoized; a
probe re-reads the record, never recomputes it. A probe that recomputed
anything differently from `world.attempt` would be a world disagreeing with
itself.

## What each section is for

| Section | The question | The kind of answer |
| --- | --- | --- |
| `S1` | Does the naive fallback double-charge, and does the protocol hold its invariant? | double-charge counts, charges-without-sale, I1 overlap windows, saved-sale rates — naive / protocol / clairvoyant, on the steady world and the committed `idempotency-stress-v1` |
| `S2` | What does waiting for the truth cost? | the protocol's deficit against the clairvoyant (the lost-record mass); probe-interval sweep (a cost knob); resolution-window sweep (a safety knob — cws appears below M) |
| `S3` | Is the classification protocol what it claims? | the ambiguity mix (revealed auth / soft / hard / gave_up / gave_up-settles), lease-hold medians by class, and the executable I1/I2/I3 audit |
| `S4` | What does the executor cost? | µs/transaction, CPython band |

Three things this is designed to catch, because they are the ones that survive
review:

- **A silent-pass invariant.** The I1 overlap can *not* be measured by a
  post-hoc interval scan: the naive's first attempt is never confirmed, so it
  never appears in any interval, and the scan reports zero on the very policy
  that violates the invariant. The audit therefore counts at dispatch time
  (a dispatch while a prior attempt is unconfirmed). If you move this
  measurement, the naive row will pass your invariant and the benchmark will
  be about nothing.
- **A world that disagrees with itself.** The probe's timing, the settlement
  times, and the decline codes are re-derived from the harness's own
  index-addressed draws. Any drift between the probe model and
  `world.attempt` is a benchmark measuring a different world than the one the
  scenario hash pins. The [S1] steady-world cross-check against [M5] is the
  tripwire.
- **A benchmark that cannot fail.** A world without `late_settlement` (or with
  a probe contract the client does not honour) can *never* double-charge, so
  "dbl = 0" on it proves nothing. The stress scenario commits the exposure —
  the workhorse in a 12-minute `failure_mode: timeout` outage with its
  late-settlement share doubled — and [S1] shows the naive failing it at
  5.0% of all transactions before the protocol is allowed to claim the
  invariant.

## Not in scope here

`RESULTS.md` is generated output — regenerate it, do not edit it. The Go
attempts-manager, the lease row in the state store's WAL, and the `ErrInFlight`
sentinel are #12's and #13's; the benchmark gate that asserts dbl = 0 on every
committed run is #17's. The routing *policy* (a deterministic static cost
table with the ADR-0002 EV retry test at a flat prior) is a stand-in: the
protocol is policy-agnostic, and the lease blocks the same-processor retry a
learning policy would sometimes choose while unconfirmed, either way.
