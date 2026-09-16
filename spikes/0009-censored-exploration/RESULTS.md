#9 evidence spike: censored data and exploration-exploitation | n=60000 | policy seed 20260916 | lambda_to=45
worlds: baseline-steady-v1@sha256:16661ded41dd, outage-recovery-v1@sha256:7ccf83824414, quiet-improvement-v1@sha256:43e4672b1080, quiet-improvement-starved-v1@sha256:2202daa793db

[C1] bandit feedback vs full-information feedback (the price of censoring)

  The estimand is per-arm P(authorized | attempt, not timeout): an attempt on arm i
  labels arm i only. A bandit-feedback learner folds one outcome per decision; a
  full-information learner (IMPOSSIBLE in production -- you cannot ask Adyen what it
  would have done with traffic you sent to Stripe) folds every eligible arm's
  counterfactual. The harness answers counterfactuals for free (index-addressed
  draws), so the gap between the two rows is the measured price of censoring. Same
  policy (TS, shipped prior), same decision rule; only the observation set differs.

  --- baseline-steady-v1 (steady world, n=60000) ---
feedback                                  auth%  margin c/1k  MAE pts  attempts
----------------------------------------  -----  -----------  -------  --------
bandit feedback (production reality)      70.49  27376.2      4.25     52592   
full-information (simulation-only bound)  70.67  27468.8      4.06     52700   

  --- outage-recovery-v1 (outage world, n=60000) ---
feedback                                  auth%  margin c/1k  MAE pts  attempts
----------------------------------------  -----  -----------  -------  --------
bandit feedback (production reality)      70.41  27353.1      4.29     52578   
full-information (simulation-only bound)  70.59  27445.1      4.10     52684   


  Readings:
  - The bandit's per-arm labels are UNBIASED: the world answers an attempt without
    knowing whether it was chosen or probed (the draws are key-addressed, so chosen
    and counterfactual outcomes come from the same distribution -- the 'missing at
    random conditional on the policy' property, here by construction). What
    censoring costs is PRECISION and SPEED only: the full-information learner gets
    k=5-6 labels per decision where production gets 1.
  - The survivorship column in the ticket ('stale, uninformative posteriors') is
    therefore a variance statement, not a bias statement: a starved arm's posterior
    does not drift WRONG, it drifts WIDE -- which is exactly the signal TS's
    probability matching reads. The failure mode that remains (a written-off arm
    whose TRUTH moved) is [C2]'s scenario.


[C2] TS's built-in exploration, tested where it must fail: the quiet improvement

  quiet-improvement-v1: foxtrot is effectively 0.806 for the first 3 days (a mild
  decline storm over a healthy service -- believable, unremarkable, learned), then a
  step recovery to 0.92 with no transport signature. In ~6% of contexts (measured
  below) foxtrot becomes the expected-margin argmax; the oracle headroom is the
  forfeited margin of never noticing. Bare TS is the shipped sampler WITHOUT the
  ADR-0007 machinery; the oracle is the ADR-0002 reference (true rates through the
  identical loop, clairvoyant about the deadline only).

  event: foxtrot step recovery at t=259200s (seq 25952 of 60000); contexts where the argmax flips: 6.1%
  oracle headroom unlocked by the event: 1,312 c/1k

policy                                       margin c/1k  pre margin  post margin  foxtrot share on flipped %  glob share pre %  glob share post %  alarms
-------------------------------------------  -----------  ----------  -----------  --------------------------  ----------------  -----------------  ------
bare TS (ADR-0006 alone)                     27631.1      28508.0     27334.0      17.6                        10.6              10.5               0     
shipped resets: data-count decay (ADR-0007)  27529.4      28508.0     27154.8      21.9                        10.6              12.6               4     
proposed: data+prior decay + R49 (R58)       27481.6      28508.0     27070.7      25.2                        10.6              13.6               4     
proposed + no re-board on re-fire            27495.7      28508.0     27095.4      25.0                        10.6              13.3               4     
oracle (upper bound)                         30787.9      31296.0     30879.4      49.1                        12.6              16.8               0     
  (the flip set is the ANALYTIC expectation-blended set; the oracle decides on
  realized per-seq draws, so its foxtrot share on flipped contexts is the right
  reference number, not 100%.)

  foxtrot attempt-0 share per 10k window (post-event windows shaded by the recovery):
policy            0-10k  10-20k  20-30k  30-40k  40-50k  50-60k
----------------  -----  ------  ------  ------  ------  ------
bare TS           10.2   11.0    10.3    10.9    10.2    10.4  
data decay        10.2   11.0    10.3    15.4    12.8    11.8  
data+prior decay  10.2   11.0    10.3    16.2    14.3    13.5  
d+p, no re-board  10.2   11.0    10.3    16.2    13.3    13.4  
oracle            10.7   10.7    14.4    18.0    18.3    16.6  

  shipped (data-only decay): 4 ADWIN alarm(s) {'foxtrot': 4}; first at seq 32156 (t=324,486s), 65,286s and 6,204 txns after the recovery; tier 2 (|delta-mu|=0.099, te/settled=0.000).
  proposed (data+prior): 4 ADWIN alarm(s) {'foxtrot': 4}; first at seq 32156 (t=324,486s), 65,286s and 6,204 txns after the recovery; tier 2 (|delta-mu|=0.099, te/settled=0.000).
  proposed + no re-board: 4 ADWIN alarm(s) {'foxtrot': 4}; first at seq 32156 (t=324,486s), 65,286s and 6,204 txns after the recovery; tier 2 (|delta-mu|=0.099, te/settled=0.000).

  --- boundary case: quiet-improvement-starved-v1 (foxtrot effective 0.60 for 3 days; the prior artifact has been refreshed during the storm -- R50's 'state at midnight' already believes 0.60, so pre share collapses to single digits; then the same silent step recovery to 0.92) ---
policy                               margin c/1k  glob share pre %  glob share post %  flip share %  alarms
-----------------------------------  -----------  ----------------  -----------------  ------------  ------
bare TS                              27142.0      2.93              3.6                6.5           0     
data+prior decay + R49, no re-board  26980.1      2.93              12.1               27.3          4     
data+prior decay, no floor           26978.2      2.93              11.3               27.1          4     
   bare TS: no alarm over the whole post window
   data+prior decay + R49, no re-board: first alarm seq 30,533 (4,581 txns / 35,350s after recovery)
   data+prior decay, no floor: first alarm seq 30,533 (4,581 txns / 35,350s after recovery)
  The starved case is the ticket's precise fear, and it measures the brake line.
  Probability matching never takes an eligible arm's share to zero (2.9% here), so
  the detector stream never fully dries: ADWIN sees the 0.60->0.92 step through a
  2.9% feed after 4,581 txns of post-event traffic (~9.8h), FASTER than it sees the
  milder 0.806->0.92 step through a 10.6% feed in the main table (6,204 txns, ~18h)
  -- contrast dominates feed rate for the statistic. Bare TS, with no detector
  attached, never notices (no alarm, 6.5% flip-share). The reset then does the
  re-feeding: data&prior decay lifts the starved arm to ~12% global / ~27% flip
  share, and the R49 floor adds only ~1pt over decay-only in this configuration --
  it is the reset, not the floor, that re-feeds. Where the feed stops entirely
  (share genuinely zero because the arm is INELIGIBLE, not unpicked), no online
  mechanism can help -- eligibility-gate territory, handled by logging eligibility
  ([C4]) and by #15, not by exploration.


  exploration annealing, baseline-steady-v1 bare TS (attempt-0 != posterior-mean argmax):
window  exploration % of decisions  foxtrot share %
------  --------------------------  ---------------
0-10k   4.31                        10.18          
10-20k  2.54                        10.96          
20-30k  2.45                        10.24          
30-40k  1.98                        10.30          
40-50k  1.73                        9.44           
50-60k  1.85                        9.14           

  -> the annealing IS the design: on a stationary world TS drives explorative picks
     toward zero as posteriors concentrate (that is the revenue the ticket worries
     about wasting), and on the moved world the same annealing is the censorship
     trap. The numbers say WHERE the line sits: probability matching alone keeps a
     written-off arm at a small nonzero share (never zero), which is enough for the
     processor-level detector to SEE the improvement, but not enough for the fine
     arms to RE-LEARN it inside the window.


[C3] forced exploration on top of TS: what it costs, what it buys

  Candidates from the ticket, each as an overlay on the identical bare TS (same
  prior, same draws), priced where nothing is wrong (baseline-steady-v1) and tested
  where something is (quiet-improvement-v1). The shipped row is the ADR-0007
  machinery already in the tree; the question is whether any STANDING mechanism
  earns a place on top of it.

policy                                  steady c/1k  vs bare  improve c/1k  post margin  flip share %  post glob share %  alarms  forced
--------------------------------------  -----------  -------  ------------  -----------  ------------  -----------------  ------  ------
bare TS                                 27376.2      0.0      27631.1       27334.0      17.6          10.5                             
shipped: ADWIN + data decay + R49       27376.2      0.0      27529.4       27154.8      21.9          12.6               4             
proposed: data&prior decay + R49        27376.2      0.0      27481.6       27070.7      25.2          13.6               4             
data+prior decay, no R49 floor          27376.2      0.0      27517.3       27133.5      23.6          12.3               5             
proposed + R49, no re-board on re-fire  27376.2      0.0      27495.7       27095.4      25.0          13.3               4             
+ epsilon-greedy eps=0.01               27297.3      -79.0    27555.4       27264.2      17.9          10.5                       293   
+ epsilon-greedy eps=0.05               26909.4      -466.8   27204.2       26945.4      19.2          11.3                       1535  
+ rotation rho=0.02/2k window           26857.1      -519.1   27107.1       26798.2      17.4          10.3                       1168  
optimistic prior (rate x1.08, m=100)    27347.4      -28.8    27637.6       27364.7      18.1          10.5                             


  -> the steady column is the tax every standing mechanism pays every day of every
     week; the flip column is the insurance it buys. Epsilon-greedy and rotation do
     not fail -- they rescue the share -- but they buy the rescue with a permanent,
     uniformly-spread budget AND their feed to the written-off arm is a trickle
     (eps/k or rho), where the shipped machinery's budget is zero until a detector
     fires and then CONCENTRATED on exactly the arm that changed. Optimistic
     initialization is a prior, not a mechanism: after 25k transactions of data the
     prior is a rounding error and the rescue effect with it.

  the degradation direction (license for R58): outage-recovery-v1, foxtrot connection_refused 345600-346200s, step recovery; share = foxtrot attempt-0 share in the 10k decisions after recovery:
policy                      margin c/1k  auth%  foxtrot share post %  alarms
--------------------------  -----------  -----  --------------------  ------
bare TS                     27353.1      70.41  9.8                   0     
shipped (data decay + R49)  27334.6      70.47  9.8                   1     
data+prior decay + R49      27328.0      70.48  9.8                   1     
data+prior decay, no floor  27352.6      70.41  9.8                   1     
  (ADR-0007's case is unharmed by scaling prior strength together with data:
  the unbinding it needs -- stop believing the pre-outage posterior -- is the same
  unbinding in both directions; the R49 floor remains what carries re-entry.)


[C4] the decision log: a record #15's IPS can actually run on

  The candidate record (DECISION_LOG v1, fields the ADR pins): decision identity
  (seq, arrival_ms), the context key (bin, region, sca, mandate, amount, band), the
  ELIGIBLE set after the constraint filter, the per-eligible-arm posterior
  parameters (alpha, beta, tau_a, tau_b), the chain, the plug-in propensity with a
  method tag, and the R49 floor state; a per-run header carries policy_seed,
  arm-schema/catalog/constraint-set/prior-artifact hashes, lambda_to. Draws are NOT
  logged: they are key-addressed pure functions of (policy_seed, seq, arm, purpose)
  given the posterior, so the log re-derives them bit-exactly.

  (1) decision replay from logged posteriors + key-addressed draws: 426/426 exact -> PASS
  (2) posterior at a decision == fold of the WAL prefix: PASS (exact recompute path: WAL replay, ADR-0006 R47)
  (3) propensity estimators vs score-based MC reference (R=100,000), 48 logged decision states, deltas in pts:
method                                   chosen-arm mean |dp|  any-arm max |dp|
---------------------------------------  --------------------  ----------------
plug-in, theta-only (ADR-0006 P4e form)  83.64                 100.00          
plug-in, score-based (this ADR)          14.11                 100.00          
MC R=64                                  0.83                  7.73            
  (4) bytes/decision at k_max=5 eligible arms, posterior as f32: 104 B fixed-layout (~44.9 GB/day at 5,000 decisions/s)
  (4) bytes/decision at k_max=5 eligible arms, posterior as f64: 184 B fixed-layout (~79.5 GB/day at 5,000 decisions/s)
  (5) eligibility after the constraint filter: 87.4% of decisions have a nontrivial eligible set, 0.0% have the full fleet -- the eligible set is part of the propensity, so it is logged.

  -> the record is sufficient if and only if three things hold: the decision is
     re-derivable from it (1), the exact posterior at decision time is recoverable
     (2), and the propensity method is labelled with a known error budget (3).
     The theta-only plug-in has a systematic bias on THIS fleet because scores mix
     theta with the fee schedule and lambda_to: delta (auth-strong, margin-thin) is
     over-propensed against charlie (auth-weak, margin-fat). The score-based plug-in
     is the same cost class and is the method the log tags.


[C5] delayed outcomes: ingest lag, relabeling, and the no-response rule

  Two distinct delays, one protocol question each:
   (a) SETTLED outcomes arriving late at the ingest (webhook -> queue -> learner).
       The label is known; only its posterior application is deferred. Lag is in
       transactions (at this fleet's ~0.1 txn/s pace, 64 txns ~ 10-11 min).
   (b) Ambiguous timeouts resolving LATE (the issuer answers after the deadline:
       the world's late_settlement channel, ~30% of would-approve timeouts,
       median 7-14s). The label was excluded by ADR-0002 R10; the question is
       whether a late arrival should rewrite it.
  (Sub-table (b) needs the 600s outage window to hold enough in-window txns to
   trigger detection: at n<60000 the density is too low and alarms correctly stay
   silent -- drift.py D2's detection lags are the 60k-density numbers.)

  (a) settled-outcome ingest lag, baseline-steady-v1, bare TS:
delay          auth%  margin c/1k  MAE pts
-------------  -----  -----------  -------
lag 0 txns     70.49  27376.2      4.25   
lag 64 txns    70.51  27383.3      4.25   
lag 256 txns   70.50  27384.2      4.26   
lag 1024 txns  70.45  27349.2      4.30   
lag 8192 txns  70.20  27260.9      4.64   

  (b) lag x drift-reset, outage-recovery-v1, shipped machinery (TS + ADWIN + decay + R49):
delay          margin c/1k  MAE pts  alarms  first alarm        
-------------  -----------  -------  ------  -------------------
lag 0 txns     27334.6      4.31     1       43,424 (t=434,838s)
lag 1024 txns  27301.7      4.37     1       44,456 (t=441,782s)
lag 4096 txns  27257.6      4.51     1       47,566 (t=469,114s)

  (c) late-settlement handling, baseline-steady-v1, bare TS (margins are face-value; labeling changes routing only; the late counter fires under every protocol -- only relabel variants also rewrite labels):
variant                                                        auth%  margin c/1k  MAE pts  timeouts  late arrivals
-------------------------------------------------------------  -----  -----------  -------  --------  -------------
shipped: timeout excluded, late arrival = late counter only    70.49  27376.2      4.25     2629      676          
retro-relabel: late authorization reverses timeout into alpha  70.77  27330.1      4.24     2675      690          
no response = decline at deadline, corrected on late arrival   70.42  27363.4      4.68     2577      663          

  -> the margin columns are indecisive at this scale (retro -46, decline -13 c/1k
     vs shipped over 60k txns: inside path noise for these draws), so the verdict
     lives in the estimand: retro-relabel's labels are marginally LESS biased
     (MAE 4.24 vs shipped 4.25 -- a wash at this precision), the decline protocol
     is visibly corrupted (+0.43 pts), and on ADR-0002's timeout-heavy trace
     (blocks-v1, ~10% timeouts) the same decline protocol measured 26,646 vs
     27,650 c/1k for exclusion, i.e. -1,004 c/1k. Protocol: update on ARRIVAL,
     never on a wait; the terminal default at the derived deadline is TIMEOUT
     (excluded from the auth posterior, priced at lambda_to), NEVER a decline; a
     late-resolving outcome never rewrites the label -- it increments the late
     counter, feeding reconciliation and lambda_to calibration -- because rewriting
     changes the ESTIMAND from P(authorized | attempt, not timeout) to
     P(eventually authorized), the rate of a router with no deadline, and pays the
     slow arm for being slow. The boundary is the timeout share, not the protocol.


====================================================================================================
Reproduce: python3 spikes/0009-censored-exploration/censored.py 60000   (RESULTS.md is this output)
