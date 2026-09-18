#14 evidence spike: safe policy rollout | n=60000 | policy seed 20260916 (incumbent) / 20260918 (candidate) | rollout seed 20260919 | lambda_to=45
worlds: baseline-steady-v1@sha256:b49193b7e715, outage-recovery-v1@sha256:1e0cd5cd764e, quiet-improvement-starved-v1@sha256:b58ae93262af

[S0] policy identity: content-addressed policies, assignment determinism

  The ticket asks for the policy identity schema: hash of the model parameters or
  semantic version? Both, with the hash as the identity and the version as a label
  (this repo content-addresses everything else the same way: scenarios, catalogs,
  constraint sets, the audit chain). policy_id = sha256 over the canonical tuple
  {algorithm, arm schema + band edges, prior artifact digest, policy seed, score
  and protocol config, catalog hash, constraint-set hash}. Any component change --
  a refreshed prior artifact, a lambda_to retune, a sampler change, a seed
  rotation -- is a different policy and rolls out as one; identical content is
  the SAME policy no matter how many times it is deployed.

policy                                                     policy_id (sha256, first 19 hex)
---------------------------------------------------------  --------------------------------
incumbent (seed 20260916, lam 45)                          sha256:f644a7054e35...          
candidate (seed 20260918, lam 45) -- seed rotation only    sha256:f9163503d02b...          
candidate (seed 20260918, lam 10) -- mispriced lambda_to   sha256:56d840d9b350...          
candidate (prior foxtrot x1.25) -- miscalibrated artifact  sha256:be2b3e1eebf5...          
candidate (draw_alg=normal) -- rejected sampler            sha256:6be97074a4aa...          

  identity components of the incumbent: algorithm=ts-beta-adr0006,
  arm_schema=bin6xreg5xscaxmandatexband6, prior=sha256:f774460c170f...,
  policy_seed=20260916, lam_to=45.0, eta=0.05,
  n_min=1000, te_mode=beta, draw_alg=exact,
  catalog=sha256:9139b0932cf1d0c9, constraint_set=sha256:32691c932a4481f8

  checks:
  (1) same content rebuilds to the same identity:
      PASS (a fresh instance of the incumbent
      hashes identically -- identity is content, not deploy time, not instance)
  (2) every changed component rebuilds to a different identity:
      PASS
      (5 distinct policies from 5 distinct tuples)
  (3) assignment is a pure function of (rollout seed, seq): share 2,500bp
      measured over 60,000 seqs = 25.08%
      (deterministic, sticky, no ambient state -- a re-decided transaction lands
      on the same side, and OPE re-derives the split offline)


[S1] the risk, priced: what a cutover actually costs

  The ticket's premise -- a new policy starts with a cold posterior -- is mostly
  FALSE in this architecture, and the exceptions are the point. The fold over the
  WAL is policy-independent learned state (outcome ops are per-arm world facts);
  a candidate boots from the incumbent's newest snapshot (R47/R97/R102) and is
  warm at boot. The residual risk is a MIGRATION THAT LOSES STATE. Priced here on
  a no-op deploy (same algorithm, same artifact, seed rotation only), steady world.

deploy                              margin c/1k (full)  first 10k post-cut  rest post-cut  auth %  MAE pts
----------------------------------  ------------------  ------------------  -------------  ------  -------
never deploy (incumbent)            27443.0             27728.6             27388.3        70.8    4.25   
warm 100% at seq 0 (seed rotation)  27331.9             27724.9             27289.6        70.8    4.23   
COLD 100% at seq 10k (store lost)   27135.3             27404.5             27019.1        70.9    4.48   

  readings:
  - A warm no-op cutover is free: 27331.9 vs
    27443.0 c/1k full-run -- indistinguishable. This is the H0
    the whole ticket stands on: warm start is not an approximation, it is the
    same fold, and the seed rotation only re-addresses the draws.
  - The cold cutover (the migration that loses the store) pays a
    324.0 c/1k cold-start tax in its first 10k transactions ON A NO-OP
    DEPLOY -- same algorithm, same artifact, nothing wrong except missing state.
    It reproduces ADR-0006's Jeffreys cold window (informative vs Jeffreys
    first-10k: +1,539 c/1k there) in the rollout frame, and MAE degrades with it
    (4.48 vs 4.25 pts).
  - The unit is TRANSACTIONS: the first 10k post-cut is 1.2 days at the committed
    scenario's pace (8,640 txns/day) and 2 seconds at ADR-0001's 5,000 dps fleet
    budget. The wall clock is the deployment's arrival rate; the tax is paid in
    decisions.


[S2] shadow mode: what it can clear, how long it must run

  Shadow = a candidate folding the SHARED WAL (it sees every settled outcome the
  incumbent's attempts produce -- per-arm world facts, MAR conditional on the
  incumbent, so the fold stays unbiased; ADR-0008 C1) and logging the decisions it
  does NOT execute. Zero revenue risk by construction: nothing it says is executed,
  no lease is minted, no dispatch happens (ADR-0009's dbl=0/I1=0 gate reads the
  executed side only). The question the ticket asks -- how long must shadow run --
  has two answers, and the difference between them IS the finding: a warm-started
  shadow is at its plateau from the first window; a shadow that could not inherit
  state must wait for the fold to warm it.

shadow variant                              attempt-0 agreement %  rows   note                              
------------------------------------------  ---------------------  -----  ----------------------------------
warm shadow (snapshot boot, seed rotation)  75.9                   3,846  (at plateau from the first window)

  cold-shadow agreement per 10k window (the warm-up curve):
window   attempt-0 agreement %
-------  ---------------------
0k-10k   59.9                 
10k-20k  70.6                 
20k-30k  74.5                 
30k-40k  72.7                 
40k-50k  73.6                 
50k-60k  72.8                 

  (cold shadow final: 70.7%
  over 4,616 rows; MAE of its own fold at end of run:
  4.25 pts -- the fold warms it, exactly as ADR-0006's cold-start
  table predicts.)

  what shadow CAN clear, and what it cannot:
  - CAN: decision-rule validity at scale (chains non-empty, constraint verdicts
    sane, propensities logged, replay bit-exactness -- C4's checks unchanged,
    since a shadow decision is the same pure function of fold + key-addressed
    draws), and the deployment plumbing end to end.
  - CANNOT: executed margin. The shadow's chain is never dispatched; its 'margin'
    is a counterfactual only OPE can estimate (#15), with plug-in-grade error
    (ADR-0008 C4) unless recomputed exactly. Zero revenue risk buys zero revenue
    EVIDENCE -- that is the canary's job.
  - CAN: warm up the candidate's fold for free (it folds the shared WAL), which
    is why shadow duration is NOT a learning wait when warm-started.

  shadow record cost (labelled model, ADR-0012 byte discipline): a shadow row
  carries (seq u64, policy_id u64-trunc, chain u8[k_max], arrival_ms u48, plug-in
  propensity f32) = 30 B/decision -> 13.0 GB/day at 5,000 dps, +29% on the
  104 B DECISION_LOG v1 row, only while a rollout is in shadow. The shadow fold
  itself is 135 KiB of arrays (ADR-0012 [W8]a) plus one extra Decide() on the
  sampled path.

  verdict: shadow >= 24h of fleet traffic AND >= 50k decisions AND replay PASS
  AND the ADR-0009 gates -- a validation window over traffic diversity, not a
  warm-up wait. A cold shadow (nothing to inherit) needs the fold first (measured
  above); and a cold shadow is itself the symptom of an R102 violation, not a
  stage to normalize.


[S3] the canary gate: null distribution, false aborts, regressions

  The gate: split traffic by a key-addressed draw (per transaction, i.i.d. across
  sides); in matched windows of 1,000 decisions per side compute
  D = margin_c1k(canary) - margin_c1k(control). Abort when D < -Y for Z=2
  consecutive windows, or D < collapse (-1.5Y) for one. Y is set from the NULL
  distribution measured here (no-op deploy, seed rotation only) as Y = 3 sigma.
  Both sides face the same world in the same window, so world events (outages,
  diurnal shape) difference out -- the property [S4] tests against a real outage.

null pooled (2 assignment seeds)  gate-windows  mean D  sigma   min      max   
--------------------------------  ------------  ------  ------  -------  ------
W = 1,000/side                    19            272.1   1754.4  -2641.1  3517.3
W = 2,000/side                    9             167.8   1399.3  -2384.5  2518.0
W = 4,000/side                    4             159.5   623.1   -156.8   1094.2

  Y is NOT a constant: it is 3 sigma measured on the live null at the gate window
  in use -- here Y = 1869 c/1k (routed) at W = 4,000 per side. The
  re-blockings bracket the sqrt law (1754.4 at W=1,000 on
  19 windows -> 623.1 at W=4,000 on only
  4 -- a 4-window sigma carries a ~35% error of its own, so the production
  gate re-measures the null continuously; at fleet pace a gate-window is
  3.2s, so the null tightens within minutes of traffic,
  while the committed scenario's clock (2h per gate-window at 8,640
  txns/day) is the starved one. 19 raw windows, 0 breaches of Y and of
  collapse (-1.5Y) on the pooled null; the raw-window breach count at
  3 sigma(W=1) is 0. The margin branch auto-aborts on TWO consecutive
  gate-windows below -Y or ONE below collapse; everything else is a promotion
  decision made on the stage's D trace (below). At 1-5% share the wait is the
  point: a 1% canary is a health check, not a statistics instrument.
  Coverage has its own null: the canary-vs-control arms ratio at matched
  decision counts is 0.897-0.923 across seeds -- TS's
  rich-get-richer arm dynamics make single-stage coverage NOISY, and the
  starvation signal ADR-0006 measured (-359 arms in 16,466, -2.2% at 20k) sits
  INSIDE that band at 25% exposure. Coverage is the slowest gate member: it
  accumulates across stages and is checked against the incumbent's own curve at
  each stage boundary, not per window.

regression deployed at 25%                     mean D c/1k (routed)  windows  unroutable delta pts  canary auth %  arms@matched counts  gate verdict                                                              
---------------------------------------------  --------------------  -------  --------------------  -------------  -------------------  --------------------------------------------------------------------------
charlie excluded (ConstraintSet regression)    -238.7                2        12.1                  61.3           145 vs 194           auto-ABORT at seq 21,762 (routability)                                    
draw_alg=normal (ADR-0006's rejected sampler)  53.8                  10       0.1                   70.8           277 vs 302           rides at 25% (coverage 0.92 in null band 0.90-0.92); accumulates at stages
prior artifact foxtrot x1.25                   -389.9                10       0.2                   70.6           270 vs 302           rides at 25% (coverage 0.89 in null band 0.90-0.92); accumulates at stages
lam_to 45 -> 10 (mispriced ambiguity)          -1305.4               10       -3.0                  73.4           270 vs 302           promotion BLOCKED (D trace, -1305 c/1k)                                   
lam_to 45 -> 0 (timeouts priced free)          -2451.3               8        -4.3                  74.7           259 vs 291           auto-ABORT at seq 48,018 (margin)                                         

  the same bad deploy, gated vs not (steady world, margins over seq 10,000..60,000):

    naive 100% cutover, no gate:  22495.8 c/1k  (-4892.5 vs never)
    gated 25% with auto-abort:    27062.9 c/1k  (-325.4 vs never)
    never deployed:               27388.3 c/1k

  readings:
  - The excluded-processor regression is caught in 11,762
    transactions at 25% share, and the auto-abort caps the damage near one
    window of exposure. At fleet pace (5,000 dps) a 1,000-decision window at 25%
    share is 0.8s; detection is seconds of wall clock, priced in DECISIONS --
    the wall clock is again the deployment's arrival rate.
  - The excluded-processor row is the one the ticket's fear is made of, and the
    margin gate alone would MISS it: excluding a processor that is the only
    eligible route for ~12% of contexts does not crash, it silently strands
    traffic (routability delta column), and the residual routing is only ~-750
    c/1k worse per ROUTED decision -- inside the margin gate's resolution. The
    gate is therefore a SET: routability (unroutable share delta) + margin
    windows + coverage (arms touched at matched decision counts) + replay PASS +
    dbl=0/I1=0. Two independent members catch what the others cannot.
  - The rejected normal sampler is the ADR-0006 lesson live, with the boundary
    measured: its margin D is NOT worse (starving exploration looks fine or
    better on a stationary world), the margin gate stays silent, and its
    coverage ratio sits INSIDE the null band at 25% exposure -- a single 25%
    stage cannot see a -2.2% starvation through TS's seed noise. A canary that
    wins margin while touching fewer arms is not a win; the coverage check is
    therefore cumulative across stages (and #16's panels), and the accepted
    residual risk is a starvation-deploy surviving the 25% stage. The lam rows
    are the same story on the margin axis: below-Y regressions do not abort,
    they fail to promote -- the cap holds, the deploy does not ship.
  - The miscalibrated prior rides through: at m=100 the fold's data counts
    dominate a wrong artifact within the cold window (ADR-0006's sweep), so a
    wrong prior artifact is bounded regret by construction. The warm-start
    design IS the prior-risk cap.
  - The lambda mispricing is the honest boundary of the gate: a regression worth
    less than Y per block is a monitored miss, not a caught one -- which is why Y
    is reported next to the null table instead of hidden in a config.


[S4] rollout vs the drift detector: share shifts, suppression, outages

  ADR-0007 handed #14 a payload: 'routing distribution shifts must not trigger
  false processor drift alarms; the canary manager must inform the drift detector
  or isolate canary streams.' ADR-0011 fixed the seam (between drift/'s alarm and
  the fold's DRIFT_RESET append); ADR-0012 fixed the reads (detector buckets +
  counter arena from Boot()). Never measured: whether a canary actually trips
  delta=1e-3 at the processor level, and what suppression costs when the alarm
  is TRUE. The shipped suppression design under test: after every share shift,
  a per-processor arming window of 256 settled observations; Tier-2 alarms inside
  it are HELD (ledger row, no decay) and re-checked at expiry against the
  trailing-256 mean; Tier-1 (transport fast-path, |dmu| > 0.30) NEVER suppressed.

steady-world rollout           alarms  held  refired  first alarm
-----------------------------  ------  ----  -------  -----------
no-op deploy, const 25%        0       0     0        -          
prior x1.25 deploy, const 25%  0       0     0        -          

  -> share shifts do NOT trip delta=1e-3 at the processor level on this fleet,
     even for a deploy that reroutes foxtrot share: the blended stream's rate
     barely moves when a few points of share change context mix. Suppression is
     insurance sized for bigger reshapes (a new ConstraintSet, a new arm space),
     not something every ramp step needs -- which is what makes a SHORT arming
     window affordable.

starved-improvement rollout         applied  held  refired  first applied  first refire  post-deploy c/1k
----------------------------------  -------  ----  -------  -------------  ------------  ----------------
protocol, suppression ON (shipped)  1        1     1        31,894         36,379        26592.8         
protocol, suppression OFF           2        0     0        31,894         -             26587.5         

  -> the improvement alarm is Tier-2 and lands INSIDE the arming window (the
     deploy and the silent recovery overlap): suppression delays the R58 reset
     that re-feeds foxtrot, and the delay is paid in margin. The re-check at
     expiry fires it (the shift is real), so the alarm is late, not lost -- but
     the arming window must stay SHORT (256 settled, not thousands).

  outage during rollout (outage-recovery-v1, no-op canary 25% from seq 10,000;
  foxtrot connection_refused at t=345,600s, gate armed at Y=1869):
    Tier-1 alarms applied during the rollout: 2
    (first at seq 34,590, t=345,949s) -- NEVER suppressed.
    paired D over the outage-window gate-windows: mean
    -68.1 c/1k over 4 windows,
    max |D| 2082.2 c/1k
    (abort bar Y = 1869.3 per gate-window) -- the world event DIFFERENCES
    OUT: both sides face the same outage, the deficit is a property of the
    POLICY, not the world.
    false aborts over the whole run: 0.

  the alternative -- isolate canary streams into their own detectors -- starves
  by construction: a 25% canary detector sees a quarter of the stream, so the
  same outage detection lag stretches ~4x in fleet decisions, and at 1-5% share
  it is ADR-0007's dilution failure again (a detector fed 1-2 attempts per
  outage window stays silent). One blended stream per processor, fed by every
  executed attempt, plus a short arming window, dominates it in this sweep.

[S5] the protocol end to end: shadow -> canary -> full on the improvement world

  quiet-improvement-starved-v1: foxtrot believed 0.60 (the artifact was refreshed
  mid-storm), truth recovers to 0.92 at day 3 with no transport signature. The
  team deploys at day 3.5: a new prior artifact refreshed from a fresh 2,000-
  transaction uniform probe (the ADR-0006 artifact path -- spent money, priced below)
  over the incumbent's snapshot fold. This is the rollout the ticket fears, in
  the direction it hopes for -- and the same machinery that caps a bad deploy
  bounds this good one.

  event: step recovery at t=259200s (seq 25,952); deploy at t=302400s (seq 31,031); argmax flips 11.3% of contexts; oracle headroom 2,365 c/1k.

  the protocol's own trace (suppression ON, gate armed at every canary stage, Y = 5,263):
    stage      seq range              share   windows   mean D
    control           0 ..    31,030     0%         -        (incumbent warmup)
    shadow       31,031 ..    33,030     0%         0        -
    canary       33,031 ..    34,030     1%         0        -
    canary       34,031 ..    36,030     5%         0        -
    canary       36,031 ..    44,030    25%         1        -5040.7
    canary       44,031 ..    52,030    50%         3        480.1
    canary       52,031 ..    59,999   100%         0        -

    raw windows total: 4; mean D -900.1 c/1k; min window -5040.7; auto-aborts: 0
    (the candidate is the BETTER policy here; the gate's job on this run
    is to NOT get in the way -- and the same calibration that aborts the
    bad deploys of [S3] rode this one through single-window noise.)

  the money table, margins over seq 31,031..60,000 (the deploy window) and full-run:
deployment                                   post-deploy c/1k  full-run c/1k  foxtrot share %  note             
-------------------------------------------  ----------------  -------------  ---------------  -----------------
protocol (shadow->1->5->25->50->100, gated)  26592.8           26988.5        12.5             shipped          
naive 100%, warm fold, STALE artifact        26663.7           27025.7        10.8             no refresh       
naive 100%, warm fold, refreshed artifact    26833.6           27110.1        15.3             no ramp, no gate 
naive 100%, COLD (store lost)                26600.2           26992.1        14.6             the ticket's fear
never deploy (bare TS, stale artifact)       26707.9           27052.0        3.1              status quo       
never deploy, ADR-0007/0008 machinery        26663.7           27022.3        10.8             in-run responder 
oracle (upper bound)                         30773.1           30254.5        17.7             clairvoyant      

  readings:
  - The warm deploys land within ~250 c/1k of one another over a 28,969-txn
    window whose per-window noise is ~1,754 c/1k [S3 null] -- on a GOOD
    deploy the protocol is roughly free: its premium against an instant
    100% cutover of the SAME candidate is 240.8 c/1k over the window (5.8 s
    at fleet pace), and the cold row shows the ticket's feared tax: 233.3
    c/1k against the same candidate warm.
  - The refreshed artifact moves the DIRECTION (foxtrot share 15.3% vs
    the stale 3.1% bare) but captures a fraction of the oracle headroom:
    a 2,000-probe artifact at m=100 is itself a noisy estimate, and some
    of what it displaces was fine. Artifact QUALITY is the prior-artifact path's (ADR-0006 §2); the
    protocol's job was to ship this one without a false abort, which it did.
  - The gate armed and rode through a scary window: the 25% stage's
    first window read -5040.7 c/1k against Y = 5,263 -- inside the bar, and
    the 2-consecutive rule means one noisy window never aborts alone.
    This is the calibration working as designed on a GOOD deploy.
  - 'Never deploy with machinery on' is the honest baseline the ticket
    does not name: the in-run responder (R58) re-feeds foxtrot without any
    deploy, at a short-window cost (the floor and decay tax, ADR-0008
    C2/C3's finding again). The protocol exists for the changes the
    machinery cannot make (artifacts, config, algorithms) and for the bad
    deploys [S3] prices.
  - The refreshed artifact's uniform probe costs 2,000 attempts outside
    the run window (ADR-0006's P2 construction) -- a known, one-off, priced
    spend; and per ADR-0008's warning, refreshing an artifact is itself a
    deploy-shaped event and belongs on this protocol's books.

====================================================================================================
Reproduce: python3 spikes/0014-safe-rollout/rollout.py 60000   (RESULTS.md is this output)
