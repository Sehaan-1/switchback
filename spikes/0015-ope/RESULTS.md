#15 evidence spike: off-policy evaluation | n=60,000 | policy seed 20260916 | ope seed 20260920 | lambda_to=45.0
world: baseline-steady-v1@sha256:b49193b7e715 | window: 60,000 decisions ~= 7.0 days at scenario pace (60k fills the scenario's 604,800 s)

[O1] the DECISION_LOG v1 dependency, exercised end to end

  The logging run: shipped TS (informative prior m=100, eta=0.05, n_min=1000,
  floor_mode=onboard, draw_alg=exact) over baseline-steady-v1, n=60,000 decisions.
  Realized: 27376.2 c/1k margin, auth rate 70.49%,
  head shares: charlie 44.7%, bravo 17.4%, foxtrot 8.0%, echo 6.6%, alpha 2.8%
  Decisions with an eligible arm that has win+fee < 0 (the sign trap, see below):
  24.8% -- the constraint filter passes spread-positive but
  fixed-fee-underwater arms at small amounts, exactly the corners a formula must not
  divide through incorrectly.

  (1) decision replay from the logged record + key-addressed draws: 1301/1301 bit-exact -> PASS  (C4's gate, re-run on this spike's run; without it nothing below means anything)
  (2) propensity estimators vs score-based MC-100k reference, 24 logged decision states, error in pts:
method                                       chosen-arm mean |dp|  any-arm max |dp|  ms/state
-------------------------------------------  --------------------  ----------------  --------
theta-only plug-in (C4, biased)              61.20                 100.00            0.1
score plug-in, sign-naive (the C4 tag, R61)  14.04                 100.00            134.0
score plug-in, sign-fixed                    5.71                  75.37             134.4
MC R=64 (C4's reference method)              1.78                  11.86             3.7
quadrature 24x4 (shipped here)               0.09                  0.71              43.8
quadrature 48x16 (convergence check)         0.04                  0.20              412.0

  The sign trap in one sentence: on 24.8% of decisions the
  eligible set contains an arm with win+fee < 0; dividing the score inequality by
  (win+fee) silently REVERSES it, and the C4 plug-in as shipped loses most of its
  error budget there (5→3 rows above). The theta-only plug-in remains broken on this
  margin-skewed fleet no matter the sign handling. The quadrature is exact to the
  reference's own noise and -- unlike the plug-in -- has no single-draw noise because
  it integrates over the arm's own posterior too. It is also DETERMINISTIC: no stream
  key exists that changes the answer.

  (3) the mixture-weight identity: w = pi_t/pi_0 needs pi_0(target|x) only on
      rows whose head IS the target (every other row carries exactly 1-rho):
target   rho   rows recomputed  share of window  recompute wall (s)  rows/s (this box)  ESS/N
-------  ----  ---------------  ---------------  ------------------  -----------------  -----
charlie  0.20  26,821           44.7%            342.1               175                0.823
foxtrot  0.20  4,785            8.0%             40.7                1,475              0.051

  -> the propensity-recompute cost of a shift query is proportional to the TARGET'S
     logged head share, not the window -- the dashboard's 7-day question pays the
     quadrature on the shifted arm's rows only.

  [section O1: 495s]

[O2] the counterfactual query interface: shift(target=B, share=rho, window)

  The panel's question, pinned as a query object (this is the interface the ticket
  asked for; [O5] attaches the validation gate and [O6] the caller):

    query     = {kind: "shift", target: <processor id>, share: rho in (0,1],
                 window: [t0, t1), tz: UTC}
    estimand  = V(pi_t) = E[ per-decision net margin ] and authorized rate, reported
                per 1k decisions, with delta vs the logging policy and a CI
    pi_t      = the LOGGED policy's recorded trajectory, overlaid: per decision an
                independent rho-coin (key-addressed) forces `target` to chain head
                when the constraint filter makes it eligible; the logged chain's
                remainder (minus target) is the fallback tail, depth <= 2 as shipped
    answer    = {value_c_1k, auth_rate, delta_c_1k, ci95, support: {...}, verdict}

  Two semantic boundaries are measured, not debated -- both are properties of THIS
  estimand, and both bound what the panel may claim:

  (0) what the shift's target policy IS, spelled out as samplers -- the rho-coin
  overlay that the weights mirror (the panel's v1 question), and the cleaner
  clipped-in-place variant it coincides with in the support-rich regime:

    coin-shift (rho): perform one key-addressed coin flip. u >= rho: run the
      LOGGED policy's replayed trajectory unmodified. u < rho: the constraint-
      eligible set is RE-EVALUATED and the decision is taken on it with the
      target pinned to chain head BY CLIPPING -- no new randomness: the
      policy's own score draws rank; wherever they already put target at head,
      the decision is IDENTICAL to the logged one; where they didn't, the
      decision becomes target with the policy's own best-helper tail. The
      sampled reward field this sampler walks over the logged active set:
        r_pi0(x,h) w.p. 1-rho        (the logged trajectory's own reward)
          + rho * 1[score-argmax == target] * r_pi0(x,h)    (clipped duplicate)
          + rho * 1[argmax != target] * r_(target, next-best)(x)
      head-IPS prices exactly the first two terms DOWN TO THE CENT: the head-
      level reweight of the logged rows touches precisely the rows the coin or
      the clip keeps at target-head, and the active-set table in [O2](1) is
      them counted out loud. The real approximation, third term: the decision
      boundary CONTINUES to rank tails by the same private score field -- the
      shift inherits pi_0's noise as its own, over ALL eligible contexts, one
      draw per context -- so what the head-level weight charges for a coerced
      context (one realization of "target with its policy-chosen best helper")
      is one draw from the same distribution the overlay itself makes. The
      exhaustive weight missing-mass table in [O5] prices how much of the
      counterfactual active set the log never visits; [O4]'s tail-conditioning
      probe limits the actual per-<head context> difference.

  (1) the confounding, in the open (target=foxtrot, paired world probes):
population mean                                                           c/decision
------------------------------------------------------------------------  ----------
E_logged[r | x, head=foxtrot]  (the mass the rho-weights stand in for)    92.14
E[r_forced(x, foxtrot) | ALL eligible x]  (what the shift actually buys)  22.28
gap = context-selection confounding the 1/pi_0 weights exist to remove    -69.86

      lettered so the support gate can be read on one line (foxtrot):
        a. unroutable decisions (empty chain; the shift cannot take them): counted
           in [O3]'s support table (20.6% of the window).
        b. admissible probability mass for the clipped duplicate: the sum over
           head==target rows of 1/pi0 is printed there too (32% of the eligible
           population at this window -- the counterfactual mass the log DOES
           provide under in-window re-aliasing).
        c. the palate the log refuses to price: contexts where target wins only
           with tiny pi0 -- sampled through the world only via the kicker
           rejection probe in [O1](2) and the E[w_bar] world rows in [O5].


  (2) the one approximation that IS in the weights: chain-tail conditioning. On
      536 eligible contexts (2464 skipped: target never heads in 100
      draw tries -- these carry the smallest pi_0 anyway), tail drawn argmax-
      consistent (as the log's target-head rows have) vs free (as a forced decision
      has), paired on the same world:
          E[r_forced-tail - r_argmax-tail] = +0.039 c/dec
      => at share rho this leaks rho*+0.039 c/dec into the value estimate
         (+7.9 c/1k at rho=0.20), inside [O5]'s proposed bound
         -- the head-level formulation ships with this measured bound on its sleeve.

  (3) re-learning divergence: the shift overlay composed with the logged
      trajectory is what a log can identify. A fleet that had LIVED the shift
      would also have LEARNED from the shifted traffic -- priced here:
target policy variant                                     7d value c/1k
--------------------------------------------------------  -------------
shift overlay on the logged trajectory (OPE's estimand)   25624.7
shift overlay on a posterior re-learning under the shift  25614.9
logged policy, same window (baseline)                     27376.2

      divergence = -9.8 c/1k over the 7-day window. The panel answers
      the overlay question; the re-learning composite is unknowable from the log alone
      and belongs to the rollout machinery (ADR-0013's staged canary measures it live).

  [section O2: 24s]

[O3] IPS variance at payment volumes; clipping

  Estimator sampling error vs PAIRED ground truth (the world is index-addressed, so
  the overlay truth replays through the same attempt draws -- C4/[M3] counterfactual
  stability). Grid: window N x share rho x target. Estimators: IPS, SNIPS, clipped
  variants. Errors vs the replayed truth of sec O2's protocol, in c/1k.

  support, counted over this window (the numbers a gate compares against -- not
  symbols): 12,346 unroutable of 60,000 (20.6% -- every one
  a decision the shift cannot take); foxtrot: constraint-eligible on 41,579
  rows (69.3%), chain-head on 4,785 (8.0%);
  across the three query classes the support is not an abstract opacity budget,
  it is exactly these three counts. The delta row has head count 0 with
  eligibility in the tens of thousands: the honest answer to a zero-support
  query over a 7-day window is 'we have never headed delta -- ask through the
  ADR-0013 canary', not any of the numbers below.

  paired truth for the logged policy this window: 27,376.2 c/1k vs the log's own 27,376.2 -> PASS (the replay prices the logging policy exactly; only the overlay distinguishes the rows below)
  target = foxtrot: medium support (8.0% head share, the Adyen-analog)
query x estimator           truth c/1k  estimate c/1k  err c/1k  ESS/N
--------------------------  ----------  -------------  --------  -----
foxt rho=0.05 IPS           26950.5     26506.8        -443.6    0.516
foxt rho=0.05 SNIPS         26950.5     27406.7        456.2     0.516
foxt rho=0.05 IPS clip10    26950.5     26723.8        -226.7    0.516
foxt rho=0.05 SNIPS clip10  26950.5     27740.5        790.0     0.516

foxt rho=0.20 IPS           25624.7     23898.6        -1726.0   0.051
foxt rho=0.20 SNIPS         25624.7     27511.8        1887.2    0.051
foxt rho=0.20 IPS clip10    25624.7     24672.1        -952.6    0.051
foxt rho=0.20 SNIPS clip10  25624.7     29014.8        3390.1    0.051

foxt rho=0.50 IPS           23009.7     18682.2        -4327.4   0.005
foxt rho=0.50 SNIPS         23009.7     27814.7        4805.0    0.005
foxt rho=0.50 IPS clip10    23009.7     20280.5        -2729.2   0.005
foxt rho=0.50 SNIPS clip10  23009.7     32682.0        9672.3    0.005


  target = charlie: rich support (45% head share)
query x estimator           truth c/1k  estimate c/1k  err c/1k  ESS/N
--------------------------  ----------  -------------  --------  -----
char rho=0.05 IPS           27378.1     27394.6        16.5      0.987
char rho=0.05 SNIPS         27378.1     27506.2        128.1     0.987
char rho=0.05 IPS clip10    27378.1     27392.9        14.8      0.987
char rho=0.05 SNIPS clip10  27378.1     27512.5        134.4     0.987

char rho=0.20 IPS           27385.2     27449.7        64.5      0.823
char rho=0.20 SNIPS         27385.2     27902.4        517.2     0.823
char rho=0.20 IPS clip10    27385.2     27435.4        50.2      0.823
char rho=0.20 SNIPS clip10  27385.2     27942.9        557.7     0.823

char rho=0.50 IPS           27397.3     27560.0        162.7     0.414
char rho=0.50 SNIPS         27397.3     28725.0        1327.7    0.414
char rho=0.50 IPS clip10    27397.3     27515.7        118.3     0.414
char rho=0.50 SNIPS clip10  27397.3     28849.6        1452.2    0.414


  target = delta: no support (0% head share -- the refused query)
query x estimator           truth c/1k  estimate c/1k  err c/1k  ESS/N
--------------------------  ----------  -------------  --------  -----
delt rho=0.05 IPS           26567.1     26460.9        -106.2    0.999
delt rho=0.05 SNIPS         26567.1     27336.5        769.3     0.999
delt rho=0.05 IPS clip10    26567.1     26460.9        -106.2    0.999
delt rho=0.05 SNIPS clip10  26567.1     27336.5        769.3     0.999

delt rho=0.20 IPS           24041.7     23715.0        -326.7    0.988
delt rho=0.20 SNIPS         24041.7     27199.6        3157.9    0.988
delt rho=0.20 IPS clip10    24041.7     23715.0        -326.7    0.988
delt rho=0.20 SNIPS clip10  24041.7     27199.6        3157.9    0.988

delt rho=0.50 IPS           19129.1     18223.0        -906.1    0.889
delt rho=0.50 SNIPS         19129.1     26809.8        7680.6    0.889
delt rho=0.50 IPS clip10    19129.1     18223.0        -906.1    0.889
delt rho=0.50 SNIPS clip10  19129.1     26809.8        7680.6    0.889


  Reading: on ONE world, plain IPS < SNIPS on every support-bearing row -- the
  realized weight mean sits below 1 and the division amplifies -- and clipping
  earns its keep exactly where the support thins. But one world is not evidence:
  [O5] re-prices every estimator on four re-seeded worlds, where the unclipped
  rows meet their catastrophe (a pi0 ~ 1e-8 head row: +9.1M c/1k of error) and
  only the clipped rows keep money units. The no-support row is not a variance
  problem at all: there is nothing to reweight, and the protocol's answer is
  refusal ([O5]), not a confident number. Charlie's rich support shows the other
  edge the gate must protect: errors there are all sub-600 c/1k at 60k.

  [section O3: 14s]

[O4] doubly robust or not
  DM calibration on support: E[r_hat(x, logged head)] = 34,146.9 c/1k vs realized-per-routed 34,468.8 -> CALIBRATED (on-support is the easy half of DM's job; the shift's forced mass is the other)
  target=foxtrot; DM = the engine's own end-of-window posteriors composed into ADR-0002's two-part margin + the logged per-processor fallback contribution:
estimator              estimate c/1k  err c/1k
---------------------  -------------  --------
rho=0.05 truth=26,950
  IPS                  26506.8        -443.6
  SNIPS                27406.7        456.2
  SNIPS clip10         27740.5        790.0
  DM                   26981.8        31.3
  DR                   26743.8        -206.7
  SNDR                 26735.7        -214.7
  SNDR clip10          27194.8        244.3
rho=0.20 truth=25,625
  IPS                  23898.6        -1726.0
  SNIPS                27511.8        1887.2
  SNIPS clip10         29014.8        3390.1
  DM                   26620.1        995.4
  DR                   24901.2        -723.5
  SNDR                 24641.3        -983.4
  SNDR clip10          26764.1        1139.4
rho=0.50 truth=23,010
  IPS                  18682.2        -4327.4
  SNIPS                27814.7        4805.0
  SNIPS clip10         32682.0        9672.3
  DM                   25896.6        2887.0
  DR                   21216.0        -1793.7
  SNDR                 18927.9        -4081.8
  SNDR clip10          25766.0        2756.3

  and the case DR is FOR -- propensities you cannot trust. Using the logged plug-in
  tag (R61's provenance field, sign-naive as shipped) as the weight, rho=0.20:
estimator on the tag              estimate c/1k  err c/1k
--------------------------------  -------------  --------
IPS on tag                        24184.9        -1439.8
SNIPS on tag                      28539.8        2915.1
DR on tag                         26541.9        917.2
cue: exact quadrature rows above

  The architecture never occupies that case: the propensity is not ESTIMATED from
  the log, it is RECOMPUTED from the logged posterior (R47/R61), so the classical
  motivation for DR -- insurance against a misspecified propensity model -- does
  not exist here. What remains is variance: whether the DM's control-variate earns
  its keep. (Section time so far includes the decision-QMC for DR's mixture term;
  that cost is DR-specific and lands on every query, unlike the mixture identity.)

  [section O4: 433s]

[O5] validation protocol + the proposed error bound

  The protocol (this is the benchmark the spec asks for):
    V1. split nothing: the SAME content-addressed world serves both policies;
    V2. run the logging policy once (the DECISION_LOG it emits is the input);
    V3. replay the target overlay through the frozen trajectory -- the replayed
        ground truth (this spike's truth_shift; production equivalence: the
        ADR-0013 canary at 1-5% IS a support-generating truth pass);
    V4. report |estimate - truth| in c/1k and relative to |delta vs baseline|;
    V5. the protocol's own noise floor is measured by re-seeding the WORLD and
        running matched (log, truth) pairs -- the spread of the replayed DELTA is
        the finest error the protocol can testify about (below).


  your own log is the canary (V2 read literally): the 7-day DECISION_LOG is
  already a ground-truth instrument -- the same window served under a different
  policy would need no other world than the persona the log already paid for:
    * 20.6% of this
      scenario's window is empty-chain (rhythm, not missingness -- counted live),
    * the support table's (b) row is the hybrid identity: the logged trajectory
      buys 32% of the counterfactual target population at rho=0.20;
    * the high-k solved states of [O1](2) tell you the residual is a countable
      set of thin-pi0 decisions, not a uniform fog.

  matched protocol replicates, shift(foxtrot, 0.2) on re-seeded worlds:
replicate           log policy c/1k  overlay truth c/1k  delta c/1k  E[w]
------------------  ---------------  ------------------  ----------  -------
world seed +0       27376.2          25624.7             -1751.6     0.869
world seed +10,000  26819.6          25056.5             -1763.0     463.120
world seed +20,000  27505.4          25837.6             -1667.8     46.635
world seed +30,000  27515.4          25778.9             -1736.5     0.865
    -> replayed delta mean -1,729.7, sd 42.7 c/1k across worlds: the
       protocol's noise floor for THIS query -- the truth is STABLE, so
       E[w_bar] replicated across worlds takes both signs: on two worlds
       the high 1/pi0 tail never fired (<1), on two one absurd-pi0 head
       row detonates it (>>1): the missing mass is real in both signs and
       the two counterfactual limbs below count it directly.
  estimator error vs replayed truth, per world (c/1k):
estimator     | err on world +0    +10k    +20k    +30k  mean        RMS
------------  -----------------------------------------  ----------  ---------
IPS            -1726.0  +9100181.5  +244503.6    -773.3  +2335546.4  4551732.9
IPS clip10      -952.6    -836.4   -1008.0    -997.2       -948.6       951.0
SNIPS          +1887.2   -5352.7  -20040.7   +3123.6      -5095.7     10530.9
SNIPS clip10   +3390.1   +3480.1   +3310.7   +3397.1      +3394.5      3395.0
DM              +995.4   +1559.7    +938.1   +1723.6      +1304.2      1348.6
DR              -723.5  +1265383.4  -394546.7   +1099.9  +217803.3   662733.7
SNDR clip10    +1139.4   +1215.7    +662.5   +1365.3      +1095.7      1126.9
  weight accounting, canonical world, foxtrot, measured (not interpolated):
support quantity                                             value
-----------------------------------------------------------  ---------------------------------------------------------------------
window share with an empty chain (unroutable)                12,346 of 60,000 = 20.6%
observed head rows of the eligible population                4,785 of 41,579 = 11.5%
in-window aliasing mass: sum 1/pi0 over those rows, as a     13,400 = 32.2% of the eligible population
thinnest observed head row: pi0 =                            2.20e-04  (a ~1-in-4,548 context-visit pick)
replicate worlds (table above) show the volatile other end:  a world whose log contains a pi0 ~ 1e-8 head row reads E[w_bar] = 463
       -> two worlds' E[w_bar] < 1 (tail never fired), two >> 1 (tail row
       already logged). Both regimes are the SAME missing-mass mechanism at
       opposite signs -- at this window the 1/pi0 machinery lives or dies by
       a handful of logged tail rows, which is exactly why the estimator
       ranking below (unclipped exploding at +9.1M c/1k, clipped at ~1k)
       is the point of the section.

  clip sweep (canonical world, c/1k err vs replayed truth):
query            cap       IPS err  SNIPS err
---------------  --------  -------  ---------
foxtrot rho=0.2  clip=1    -1651.8  +2916.1
foxtrot rho=0.2  clip=3    -1177.0  +3241.9
foxtrot rho=0.2  clip=10   -952.6   +3390.1
foxtrot rho=0.2  clip=30   -863.9   +3405.0
foxtrot rho=0.2  clip=100  -942.6   +3237.9
charlie rho=0.2  clip=1    -236.7   +405.0
charlie rho=0.2  clip=3    +46.2    +569.8
charlie rho=0.2  clip=10   +50.2    +557.7
charlie rho=0.2  clip=30   +56.9    +545.9
charlie rho=0.2  clip=100  +63.8    +519.8

  estimator noise, IPS clip10 (headline), shift(foxtrot, 0.20), N=60,000 (400
  decision bootstraps): estimate 24,672.1 c/1k, 95% CI [24,185.3, 25,131.7],
  bootstrap sd 237.5 c/1k; replayed truth 25,624.7 c/1k lies OUTSIDE the
  CI -- the CI is a SAMPLING statement about the clipped-IPS functional; it cannot
  see the missing-mass term, which the support diagnostics (E[w], ESS, the anchor
  table per world) exist to price. The panel therefore ships estimate + CI +
  support verdict together, never a bare point.

  achieved errors across the dashboard's query families (headline estimator):
query             truth c/1k  IPS-clip10 c/1k  err c/1k  rel err
----------------  ----------  ---------------  --------  ------------------------
foxtrot rho=0.05  26,950.5    26,723.8         -226.7    53.2% of |delta|
foxtrot rho=0.20  25,624.7    24,672.1         -952.6    54.4% of |delta|
foxtrot rho=0.50  23,009.7    20,280.5         -2729.2   62.5% of |delta|
charlie rho=0.05  27,378.1    27,392.9         +14.8     n/a (|delta| < 100 c/1k)
charlie rho=0.20  27,385.2    27,435.4         +50.2     n/a (|delta| < 100 c/1k)
charlie rho=0.50  27,397.3    27,515.7         +118.3    n/a (|delta| < 100 c/1k)
delta rho=0.05    -           -                -         REFUSED (support gate)
delta rho=0.20    -           -                -         REFUSED (support gate)
delta rho=0.50    -           -                -         REFUSED (support gate)

  proposed bound for the benchmark metric (chosen from the tables above, see ADR):
  a shift query PASSES the replay protocol when, at N >= 30k decisions in the window:
    support gate: >= 1,000 logged head==target rows (else REFUSED, before any
                  estimator runs), and
    accuracy: |estimate - truth| <= max(1500 c/1k, 75% of |truth - baseline|),
    and the panel ALWAYS surfaces the support diagnostics (E[w_bar], ESS/N, head
    rows) next to the point estimate -- the tables above say why: at this fleet's
    scale, IPS-clip10 holds -949 +/- ~50 c/1k across four worlds on an 8%-share
    target at rho=0.20 (RMS ~951), and unclipped rows are one absurd-pi0 row away
    from +9.1M c/1k of fantasy. The bound is bigger than ADR-0002's ~700 c/1k
    ablation prizes: the panel's question is intrinsically a small-signal one,
    and the honest answer carries its uncertainty rather than hiding it.

  [section O5: 274s]

[O6] read path and packaging

  read path, measured on this run's real records (60,000 rows, stdlib sqlite3):
    write the decision rows      : 0.2 s, 12.0 MB (199 B/row)
    window scan + margin sum     : 24 ms (2,531,569 rows/s)
    exact recompute throughput   : 809 decisions/s (quadrature 24x4, 1,573 rows)
  the store numbers sit on ADR-0012's measurements ([W4-W6]); what #15 adds is that
  the BOTTLENECK IS THE RECOMPUTE, by ~4 orders of magnitude, and the mixture
  identity ([O1](3)) is what keeps it proportional to the target's head share.

  fleet-scale arithmetic for the panel's 7-day question (a labelled MODEL on the
  measured rates, not a run): 7d at 5,000 dps = 3.02e9 decisions; window scan at
  the measured rows/s or ADR-0012's columnar tier [W5] is minutes; recompute at
  rho=0.20 to an arm at 8.0% head share = 2.2e8 rows x 1/809/s = single-core
  days; the engine-side answers are (i) R94: read sealed daily partitions from a
  replica, embarrassingly parallel by (shard, day), and (ii) the AMORTIZED form the
  panel actually wants -- recompute propensities incrementally for the small set of
  arms in the active query vocabulary as partitions seal, so a pane refresh is a
  SUM, not a recompute day.

  [section O6: 2s]
