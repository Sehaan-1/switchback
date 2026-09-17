#10 evidence spike: idempotency and double-charge prevention | static-table policy THETA0=0.85, cap=2, M=10xp95 clamped [3s,15s], probe 500ms/100ms
worlds: baseline-steady-v1@16661ded41dd, idempotency-stress-v1@14273698a329 | windows {'oneoff_cnp': 30, 'recurring_mit': 600, 'card_on_file': 600, 'installment': 600}s

====================================================================================================
[S1] the exposure: does the naive fallback double-charge, and does the protocol hold
====================================================================================================
  The invariant: at most one UNCONFIRMED authorization per transaction at any time (I1)
  below is the count of overlapping in-flight (processor, key) pairs -- the
  double-charge WINDOW. dbl = transactions with two authorizing pairs (the
  realized double charge); cws = settlements on a non-authorized terminal
  (charges without a sale, i.e. the reconciliation/void load).

  policy       = naive (immediate fallback, no lease) | protocol (ADR-0009) |
                 clairvoyant (instant truth; the no-double-charge upper bound)

  baseline-steady-v1@16661ded41dd  n=20,000
               saved    late  sync answer at the deadline gave_up     dbl   cws     I1   att margin c/1k
  naive       saved  87.61%  late  4.11%  sync 83.5/10.0/ 6.5 auth/decl/proc  gave_up  0.00%  dbl    163  cws   122  I1    819  att 1.095  margin  24631.4
  protocol    saved  85.98%  late  2.48%  sync 83.5/10.0/ 6.5 auth/decl/proc  gave_up  3.40%  dbl      0  cws     0  I1      0  att 1.059  margin  24177.3
  clairvoyant saved  88.04%  late  3.25%  sync 83.5/10.0/ 6.5 auth/decl/proc  gave_up  0.00%  dbl      0  cws     0  I1      0  att 1.083  margin  25057.3

  idempotency-stress-v1@14273698a329  n=60,000
               saved    late  sync answer at the deadline gave_up     dbl   cws     I1   att margin c/1k
  naive       saved  81.70%  late 11.07%  sync 70.6/ 9.3/20.1 auth/decl/proc  gave_up  0.00%  dbl   2999  cws  2545  I1   7500  att 1.181  margin  22077.3
  protocol    saved  81.62%  late 10.99%  sync 70.6/ 9.3/20.1 auth/decl/proc  gave_up  6.87%  dbl      0  cws     5  I1      0  att 1.069  margin  22882.2
  clairvoyant saved  85.38%  late  5.71%  sync 70.6/ 9.3/20.1 auth/decl/proc  gave_up  0.00%  dbl      0  cws     0  I1      0  att 1.114  margin  24044.1

  Reading: the naive row on the stress world is the ticket's scenario in a
  scenario -- 'routed to the workhorse, the workhorse is slow, retry the
  second processor before the first answers'. Every dbl in that row is a
  customer charged TWICE (the sale closed on the second arm, the money
  settled on the first); every cws is a customer charged for a payment we
  told them failed. The protocol row must show dbl = 0 and I1 = 0; the
  gap between protocol and clairvoyant is the information-theoretic price
  of confirmation (it cannot see the lost record until M, the contractual
  bound, and the chain never proceeds from an unconfirmed attempt).

====================================================================================================
[S2] the price of confirmation: what waiting for the truth costs
====================================================================================================
  On idempotency-stress-v1 (the charlie event [360s,1080s) is a fifth of the
  run). saved_pct is the sale-recovery rate; the protocol's deficit against
  the clairvoyant is exactly the transactions whose first attempt lost its
  record (the probe cannot recover what was never kept) -- a bound on the
  processor's record reliability, not on the router's parameters.

  headline at n=60,000 (from [S1]):
  clairvoyant saved  85.38%  late  5.71%  sync 70.6/ 9.3/20.1 auth/decl/proc  gave_up  0.00%  dbl      0  cws     0  I1      0  att 1.114  margin  24044.1
  protocol    saved  81.62%  late 10.99%  sync 70.6/ 9.3/20.1 auth/decl/proc  gave_up  6.87%  dbl      0  cws     5  I1      0  att 1.069  margin  22882.2
  naive       saved  81.70%  late 11.07%  sync 70.6/ 9.3/20.1 auth/decl/proc  gave_up  0.00%  dbl   2999  cws  2545  I1   7500  att 1.181  margin  22077.3

  protocol vs clairvoyant: -3.76 auth pts (the lost-record mass the
  probe cannot recover); the protocol eliminates 2999 double
  charges and 2545 charges-without-sale that the naive leaves,
  for -0.08 auth pts of sale rate.

  (a) probe interval (window at the 30s default): a cost knob, not an
  outcome knob -- the record exists or it does not, the probe only finds it
  faster. Probes are status queries: free, and outside the attempt cap.
   interval   saved  gave_up   probes  margin c/1k
        250   81.24     7.03    77834      22946.5
        500   81.24     7.03    43291      22946.5
       1000   81.24     7.03    25112      22946.5
       2000   81.24     7.03    15964      22946.5

  (b) the customer-present resolution window (interval at the 500ms default):
  the safety-vs-sales knob. Above every contractual M (<= 15s in this fleet)
  it does nothing; below M it retires ambiguities earlier -- and a record
  that finalises after the window closes but still settles is a charge
  without a sale (cws) the protocol can no longer catch in time.
   window   saved  gave_up   cws  margin c/1k
       5s   81.22     7.05     6      22940.8
      30s   81.24     7.03     3      22946.5
     120s   81.24     7.03     3      22946.5

  The expensive number is the clairvoyant gap above: it is the lost-record
  mass, and the lever on it is the processor contract (record reliability +
  M), not the router's knobs. The 5s row prices merchants whose checkout
  budget is tighter than the fleet's M: every such millisecond traded for
  speed buys cws, which is why R67's default sits above M.

====================================================================================================
[S3] the classification audit: timeout vs terminal, executed
====================================================================================================
  Protocol, idempotency-stress-v1 at n=60,000. The merchant-visible answer
  at the 900ms deadline: authorized 70.6%  declined
  9.3%  processing 20.1%. The processing share is
  the SLO cost of the confirmation rule: an ambiguous timeout is NEVER
  reported as a decline (ADR-0002 R10 at the transaction level).

  the ambiguity mix: every dispatched attempt that passed its deadline,
  by how the protocol resolved it (the classification table in action):
    revealed_auth      5460  (47.5%)
    revealed_soft      1306  (11.4%)
    revealed_hard       605  ( 5.3%)
    gave_up            4120  (35.9%)
    gave_up_settles       5  (of the gave_up: a record that
        finalised past the probe budget yet still settled -- the cws tail)

  revealed_auth becomes a late authorization (the sale is kept, the money
  settles under it); revealed_soft drives the chain (confirmed no-auth,
  EV-gated); revealed_hard halts it (card-level verdict); gave_up retires
  the record-lost ambiguity at min(t0+M, window) -- the chain NEVER
  proceeds from it. The transport class (echo's refused outage [2100s,
  2700s)) never enters this table: it resolves synchronously, holding its
  lease for the error's own latency -- the fast certain class ADR-0005
  [M5] predicted: same routing consequence, none of the ambiguity.

  lease hold (dispatch -> confirmed resolution), median ms, by terminal class:
    authorized       n=  44539  median      330 ms
    authorized_late  n=   7769  median     2000 ms
    failed           n=   7655  median      386 ms
    gave_up          n=   4187  median     7000 ms

  Invariant audit (the executable form of the ADR's invariants):
    I1  overlapping unconfirmed authorizations   0   (must be 0)
    I2  a key presented to >1 processor          0   (keys are per (txn,
        attempt) and the lease blocks the second presentation; the probe
        reuses the SAME (acquirer, attempt) by construction -- the memo
        audit: every probe re-reads a record, never creates one
    I3  settlements on non-authorized terminals  5   (each is a
        void/reconciliation entry; at the 30s default window they arise
        only from the tail past M -- [S2](b)'s 5s row shows them appearing)

  All three pass on the stress world: 12-minute timeout outage on the
  highest-volume arm, late-settlement share doubled, plus the refused-
  connection contrast. The naive row of [S1] fails I1 by design: its
  overlap count is the size of the window the protocol closes.

====================================================================================================
[S4] the cost of the executor (CPython, labelled)
====================================================================================================
  protocol executor, idempotency-stress-v1, n=20,000: 0.88s wall -> 44.1 us/txn (21,348 attempts, 29,272 probes)
  CPython, 2 vCPU, no perf isolation: a band, not a claim. The Go target is
  ADR-0005 T3 (<= 2 us of harness per attempt) plus the engine's 20 us
  decision budget (ADR-0001); the lease is one map read per dispatch and the
  probe scheduler is one heap per transaction, so the native cost is the
  same order as an attempt, not a new budget. [S4] is the regression fixture
  for the executor's shape, not a Go benchmark.

====================================================================================================
findings
====================================================================================================
  total wall: 20.4s (2 vCPU, CPython, stdlib only)

  1. The naive fallback -- which is what the ADR-0005 reference driver does
     today -- double-charges on the stress world: 2999 transactions
     (5.00%) with two authorizing pairs, plus
     2545 charges without a sale, for 0.08 auth pts of
     sale rate that the protocol does not take. Even the ambient steady world
     is not clean under it: 163 double charges in 20,000 transactions.
     The protocol holds I1 and dbl = 0 by the lease; [S3] makes that an
     assertion a benchmark must pass, not a comment.
  2. The price of confirmation is the lost-record mass the probe cannot
     recover: 3.76 auth pts (protocol vs clairvoyant), against
     2999+2545 integrity events eliminated -- and the protocol still
     beats the naive on margin (22882 vs 22077 c/1k)
     because the probe is free and the naive burns attempts. Safety is not
     free; it is bounded, and the bound is a property of the processor's
     record reliability and M, not of the router's knobs ([S2]).
  3. A timeout is not a decline and not a transport error: it is the only
     outcome that may not authorize the next attempt. The classification
     table of ADR-0009 is what [S3] executes, and its audit rows are what
     #17's gate will assert on every committed benchmark.
