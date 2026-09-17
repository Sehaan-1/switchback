#11 evidence spike: 3DS/SCA friction and the reward function | n=30,000 | policy seed 20260917 | fixture seed 20260915
world: sca-friction-v1@sha256:8089760b6cd8aadbf9e (extends baseline-steady-v1) -- six acquirers, capability and economics pinned by catalog_hash; echo has no 3DS capability, so it is illegal for SCA traffic
MIT is out of SCA scope (a mandate set up with SCA) in every section; [S5] prices the predicate the committed harness uses today. Magnitudes are properties of this fixture; model constants live in the module docstring.

====================================================================================================
[S0] the funnel census of sca-friction-v1 (round-robin policy, 30,000 transactions)
====================================================================================================
  proc     sessions  challenged  abandoned|ch  auth|subm  unattrib  submitted
  -------  --------  ----------  ------------  ---------  --------  ---------
  alpha    1,702     11.2%       18.5%         90.4%      1.1%      93.4%    
  bravo    1,752     10.3%       13.5%         88.7%      1.8%      92.2%    
  charlie  1,741     14.1%       20.9%         85.3%      2.1%      92.0%    
  delta    1,732     39.0%       28.6%         92.6%      2.1%      83.8%    
  foxtrot  1,733     28.6%       33.2%         79.9%      2.0%      85.7%    

  The fixture's fleet-mean challenge rate is set by the scenario document's
  frictionless rates (0.62 to 0.93) and its per-category multipliers; the
  published band the ADR cites is 15-20% challenged in SCA markets, and the
  abandonment band is 10-30% (Stripe/Adyen, cited in the ADR as an external
  model, not as a measurement of this fixture). The fixture deliberately sits
  at the band's edge on both axes: challenge rates span 10-39% and abandonment
  13-33% across the five 3DS-capable processors. Read `auth|subm` carefully: it
  is conditional on surviving the funnel, so it flatters every arm, and it
  flatters the high-friction arms most -- delta's 92.6% sits 14 points above
  its 78.6% end-to-end capture, foxtrot's 79.9% sits 12 points above its
  67.6% ([S1b] prices exactly that overstatement).

====================================================================================================
[S1] what a logging regime can see (30,000 transactions, round-robin policy)
====================================================================================================
  proc     ch(true)  ch(sess)  ab(true)  ab(sess)  ab(naive)  naive bias  unattributed  submit|sess
  -------  --------  --------  --------  --------  ---------  ----------  ------------  -----------
  alpha    11.3      11.2      17.0      18.5      41.7       +24.7       1.1%          96.6%      
  bravo    9.6       10.3      12.9      13.5      47.5       +34.6       1.9%          97.4%      
  charlie  14.4      14.1      19.8      20.9      45.0       +25.2       2.2%          96.2%      
  delta    38.7      39.0      28.5      28.6      36.9       +8.4        2.1%          88.2%      
  foxtrot  30.9      28.6      33.8      33.2      44.5       +10.7       2.0%          89.7%      

  (a) the session ledger vs the naive subtraction
      Both estimators see the same 3DS server; the difference is whether the
      session's terminal status is logged. Without it, every session that stops
      short of a submission looks like an abandoned challenge -- the 2% technical
      error rate, the authentication failures and the post-authentication
      drop-offs all land in the numerator. Note WHERE the bias is largest: the
      naive number divides the arm's whole non-submission share by its challenge
      rate, so it explodes precisely for the processors that challenge least --
      +24.7 pts for alpha (11% challenged), +34.6 for bravo (10%), +25.2 for
      charlie (14%), against +8.4 and +10.7 for the two high-challenge arms.
      The estimator is blind in the direction that matters: it manufactures
      abandonment for the 3DS-strong arms and buries it for the 3DS-weak ones,
      inverting the ranking a prior built on it would carry.
      The honest statement without the log is an interval, not a number: [0%,
      the naive column] for every processor, and the true value is inside it but
      unidentifiable ([S1c]). With the log it is a number whose error is
      sampling error and nothing else.

  proc     P(auth|submitted)  P(capture)  overstatement  P(submit|session)
  -------  -----------------  ----------  -------------  -----------------
  alpha    90.4               83.4        +7.0           96.6%            
  bravo    88.7               84.0        +4.7           97.4%            
  charlie  85.3               79.0        +6.3           96.2%            
  delta    92.6               78.6        +13.9          88.2%            
  foxtrot  79.9               67.6        +12.3          89.7%            

  (b) authorization stream only: the estimand is not noisy, it is absent
      P(authorized | submitted) is not P(authorized | attempt), and the gap is
      the population that never submitted -- largest for the 3DS-weakest
      processor, because its abandonment is largest. A learner fed the
      authorization stream alone would move foxtrot above delta on these
      numbers (79.9% vs 92.6%) while its actual capture is 12 points below.
      This is ADR-0002's ablation C, arriving as a logging property instead of
      as a modelling choice.

  (c) identifiability: two funnels, one authorization stream
  A constructed pair, not a sampled one: the two worlds are chosen to have
  equal P(challenge) x P(abandon) products, and the issuer's approval behaviour
  is held fixed, so the authorization stream differs only through the two
  authentication-failure constants. The ledger sees two different fleets; the
  authorization stream sees the same one to 0.3 pts.
  world              P(challenge)  P(abandon|ch)  product  P(submitted)  P(captured)
  -----------------  ------------  -------------  -------  ------------  -----------
  A: ch=35%, ab=20%  35%           20%            7.0%     88.5%         77.1%      
  B: ch=14%, ab=50%  14%           50%            7.0%     88.8%         77.3%      
      The product column is equal by construction and both rows would look
      identical in any authorization-only dashboard. What separates them is
      exactly the quantity ADR-0002 banned from the score and #11 is asked to
      estimate: the authentication funnel. A team that logs only the
      authorization stream cannot tell which fleet it is operating, cannot
      alert on a 3DS regression, and cannot price an exemption -- which is why
      the AUTH_RECORD below is a logging requirement, not a modelling
      preference, and why an arm whose authentication window is opaque must be
      excluded from these estimates rather than imputed into them.

====================================================================================================
[S2a] cold start: does the decomposition buy a better prior for a new arm?
====================================================================================================
  A seventh processor joins and runs a 400-transaction bake-off on the
  cardholder-present retail mix. Its traffic afterwards is travel-heavy, whose
  challenge and abandonment rates are higher (scenario mcc multiplier 0.90 on
  frictionless; spike-local abandonment multiplier 1.10). Priors under test:
  direct = the bake-off's end-to-end capture rate; composed = the bake-off's
  components, re-mixed with the fleet-estimated travel/retail ratios; naive =
  the same composition built from the [S1a] naive estimator; true-comp = the
  model's own components (a bound, not a competitor).

  foxtrot's true capture: 68.2% on the bake-off retail mix, 65.5% on the travel mix it will
  actually see -- the mix shift alone is +2.7 pts before any estimator error. The
  priors below are scored against the travel truth.

  prior for the new arm                                     bias (pts)  RMSE (pts)  wrong side of an incumbent
  --------------------------------------------------------  ----------  ----------  --------------------------
  direct (bake-off e2e rate, retail mix)                    +2.40       3.54        0.0%                      
  direct (bake-off e2e rate, target mix - not available)    -0.21       2.62        0.0%                      
  composed (fixed-effect vertical ratios, warm cells only)  +2.44       3.42        0.0%                      
  composed (ratio of pooled rates - the weighting trap)     +1.35       2.83        0.0%                      
  composed from the naive subtraction                       -0.10       2.43        0.0%                      
  true components (oracle bound)                            +0.00       0.00        0.0%                      

  What this says, in the order the columns appear:
   1. The direct rate is biased against the traffic the arm will actually see:
      the bake-off mix is not the arm's mix, and the shift above lands in the
      direct prior almost undamped (+2.4 pts of row 1 at this n).
   2. Re-mixing the components does not repair it here. The fixed-effect
      composition has the same bias and no less variance, because the correction
      is a product of three estimated factors (challenge, abandonment, the
      vertical ratios) whose own sampling and transfer errors add up to the size
      of the effect being corrected. The two naive ratio estimators fail in the
      two classic ways -- a ratio of pooled rates compares different weightings
      of processors, a mean of per-processor ratios is unstable in thin cells --
      and a fixed-effect estimator with a cell floor is the honest version. It
      buys comparability, not accuracy.
   3. Composing from the NAIVE estimator lands closest by cancellation, not
      correction: its inflated abandonment pushes the estimate down just as the
      mix shift pushed it up. That is luck, and this ADR does not ship luck.
   4. The gate this suggests is a measurement, not a ban: a composed prior ships
      only if it beats the direct rate on a held-out mix, measured exactly this
      way. On this fixture it does not, so R50's r-hat stays the direct
      end-to-end rate and the decomposition earns its keep in [S1c], [S2b],
      [S4] and [S5] instead.

====================================================================================================
[S2b] detection: a 3DS-quality regression, end-to-end vs decomposed
====================================================================================================
  One processor's frictionless rate falls by 12 points mid-run, everything else
  fixed (delta, 0.62 -> 0.50). The 'data needed' column is the window size at
  which the shift reaches 2.5 standard errors of a window mean, computed from
  paired contexts: the same context stream feeds both worlds, so the only
  difference between them is the regressed frictionless rate. A 3DS regression
  moves the capture rate by P(abandon|ch) x P(auth|completed) of it -- the
  dilution factor -- so the same event costs a different amount of capture in each
  row, and the sweep covers the fixture's own abandonment rate and the band's
  extremes.
  world        statistic                   measured move  data needed (2.5 se)
  -----------  --------------------------  -------------  --------------------
  abandon 12%  capture rate (end-to-end)   -1.20 pts      6,089               
  abandon 12%  challenge rate (component)  +11.15 pts     116                 
  abandon 22%  capture rate (end-to-end)   -2.70 pts      1,460               
  abandon 22%  challenge rate (component)  +11.20 pts     115                 
  abandon 33%  capture rate (end-to-end)   -4.13 pts      708                 
  abandon 33%  challenge rate (component)  +11.25 pts     115                 

  The capture rate is the product of the funnel, so the regression arrives
  diluted by every non-auth factor and buried in the authorization noise of every
  decline code in the mix. The component sees the same event at full amplitude,
  and the data cost of noticing it is 6-50x smaller across the sweep (50x at
  the band's low end). This is the whole argument for logging the authentication
  outcome even though the routing score never reads it: the reward does not need
  the decomposition, but the alerting does. The measured move is what a
  capture-only dashboard would show for the same event -- about a point at the
  band's low end -- and the iid-Bernoulli test used here is a lower bound on
  the real one, since production streams are autocorrelated.

====================================================================================================
[S3] ADR-0002's Reopen Trigger 1, evaluated on this fixture
====================================================================================================
  The trigger: challenge-abandonment loss > 30% of expected margin on SCA
  traffic. Loss here is the margin an abandoned challenged session would have
  produced had it completed (its completed rate x the win), the margin is the
  expected margin per SCA attempt under the score's own objective, and both are
  exact for every context in the sample. The last column is the abandonment
  multiplier (the whole fleet's abandonment rate, scaled) at which THAT
  processor's ratio would cross 30%.

  proc     loss / margin  loss c/1k  margin c/1k  abandonment to cross 30%
  -------  -------------  ---------  -----------  ------------------------
  alpha    2.1%           350        16,918       unreachable             
  bravo    1.3%           457        35,635       unreachable             
  charlie  3.0%           1,221      40,941       unreachable             
  delta    19.0%          487        2,570        1.34x (ab 38%)          
  foxtrot  12.8%          3,255      25,349       1.98x (ab 66%)          
  fleet: 4.8% at today's funnel, 6.6% with every processor at the band's 30% abandonment, 6.9% with a 15-pt frictionless collapse on top; the fleet does not cross 30% before the abandonment clamp

  The trigger is not met on this fixture. Fleet-wide the abandonment loss is
  4.8% of SCA margin, and it does not reach 30% even with every processor's
  abandonment pushed to the band's high end (6.6%) or with a 15-point
  frictionless collapse on top of that (6.9%). Per processor the spread is
  1.3-19.0%, and the two that reach the crossing inside the clamp (1.34x and
  1.98x, i.e. 38% and 66% abandonment) are the two whose per-attempt margin is
  thinnest: delta's 110 bps + 12 c fee structure leaves 2.6 c of margin per SCA
  attempt (2,570 c/1k) against charlie's 40.9 c (40,941 c/1k).
  That is a property of the DENOMINATOR as much as of the funnel: the trigger
  asks what share of margin the funnel destroys, so a fleet with thinner
  per-transaction economics is closer to it at the same friction. The trigger
  stays live and now has a number attached; the ratio is scale-invariant in the
  ticket size (both sides carry the win), so the same columns can be recomputed
  on real traffic without re-deriving the fixture. The measured answer to
  consideration 3 is therefore: no multiplier, the score keeps the end-to-end
  label, and #11's estimates stay offline.

====================================================================================================
[S4] the exemption lever: an online reward cannot price what it gives up
====================================================================================================
  A merchant holding exemption evidence chooses, per transaction: claim it, or
  run 3DS. Claiming trades liability for conversion. ADR-0002 R11 keeps fraud
  loss out of the online reward, so the bandit's objective is the conversion
  half only. Below: the claim share each rule picks, and the money each moves,
  swept over the merchant's own fraud rate (the fixture's per-vertical rates
  scaled by the row's multiplier -- a model input, never observable at T+1).

  fraud rate x  claim share (online)  claim share (net)  margin delta c/1k  fraud loss delta c/1k  net delta c/1k
  ------------  --------------------  -----------------  -----------------  ---------------------  --------------
  0x            100.0%                100.0%             +0.0               +0.0                   +0.0          
  0.5x          100.0%                34.2%              +2,653.4           +4,570.8               -1,917.4      
  1x            100.0%                25.5%              +3,151.1           +9,686.8               -6,535.7      
  2x            100.0%                0.0%               +3,851.5           +20,595.5              -16,744.0     
  4x            100.0%                0.0%               +3,851.5           +41,191.0              -37,339.5     

  Where the last column is negative, the online objective is buying conversion
  with money that never appears in it. The reward is not wrong about the
  conversion effect; it is blind to the liability effect, and no amount of
  exploration fixes a term that is not in the objective. The exemption decision
  therefore belongs where ADR-0002 R13 puts anything that gates real money: a
  constraint (the exemption classes the document allows) plus a liability budget
  the merchant signs, evaluated before sampling -- never a learned arm. The
  crossover row is where that budget gets its number.

====================================================================================================
[S5] mandate: a regime, not a bucket
====================================================================================================
  `mandate=true` is out of SCA scope when the mandate's setup was authenticated
  (the EBA's MIT exclusion). Two things follow that the router must not learn
  away: the reward loses its challenge/dropout terms, and the ELIGIBLE SET
  changes -- echo has no 3DS capability, so it is illegal for cardholder-present
  SCA traffic but legal for MIT. A true-margin oracle (the eligible processor with
  the highest exact expected margin for the context) prices the scope predicate
  itself and not the learner, on a mandate-heavy mix (12% of transactions; a
  spike-local overlay). Every other section runs the fixture with MIT out of scope;
  the middle row is what the committed harness's context() does today, a named gap
  in ADR-0010's payload for #17.

  scope predicate                               MIT txns  capture  margin c/1k  to echo
  --------------------------------------------  --------  -------  -----------  -------
  MIT out of scope (RTS Art. 12)                3,608     83.0%    27,652       0.0%   
  MIT in scope (predicate ignores mandate)      3,608     81.5%    26,151       0.0%   
  MIT in scope, challenged MIT cannot complete  3,608     78.9%    24,933       0.0%   

  arm key                       MIT txns  MIT capture  margin c/1k  to echo
  ----------------------------  --------  -----------  -----------  -------
  pooled (sca / not-sca)        3,592     82.8%        30,844       0.0%   
  segmented (sca / mit / else)  3,592     82.8%        30,824       0.1%   

  The first table is the mandate decision. With MIT correctly out of scope, row 1
  never runs an authentication step -- there is no customer at the terminal to
  challenge -- and the traffic clears at 83.0% capture / 27,652 c/1k. Let the
  predicate ignore the flag and the same transactions are pushed through a step
  regulation did not ask for: 81.5% / 26,151 c/1k, a 1,501 c/1k (5.4%) loss on
  this mix. Model the one fact a MIT makes obvious -- a challenged mandate has no
  cardholder to complete it -- and it is 78.9% / 24,933 c/1k. The loss scales
  with the MIT share, so the number is a property of this 12% mix; the predicate,
  not the bucket, is where the money is.

  The eligible-set half of the change is real but, on this catalog, not where the
  money is: `to echo` is 0.0% in every row. Echo is legal for MIT once MIT is out
  of scope, but it never wins the oracle -- mid-pack economics (86 bps, 9 c fixed)
  against bravo and charlie, which trade a little auth rate for cheaper rails --
  so widening the eligible set does not re-rank it. Whether the non-3DS acquirer
  is the cheapest rail for MIT is an empirical question about the catalog, and on
  this catalog it is not; the widening is still binding because it is a regulatory
  fact, not a routing preference, and #17's benchmark should carry a world where
  it bites.

  The second table is the arm-key check. ADR-0006 R39 already puts the flag in the
  key, so dropping it measures what the shipped key is worth, not a proposal:
  pooled and segmented land inside 0.1% of each other (30,844 vs 30,824 c/1k, and
  at n=12,000 the ordering reverses), below this fixture's noise floor. The key
  keeps the flag so a policy can condition on it, and because the non-SCA bucket
  would otherwise mix exempt and MIT traffic with different amounts and different
  eligible sets.

====================================================================================================
[S6] fee incidence: the loss term prices a submission that never happened
====================================================================================================
  proc     aband|attempt  sessions  loss term now c/attempt  fees incurred c/attempt  delta c/1k txns
  -------  -------------  --------  -----------------------  -----------------------  ---------------
  alpha    2.0%           98.1%     0.934                    2.497                    +1,562.87      
  bravo    1.3%           97.1%     0.910                    2.464                    +1,553.65      
  charlie  2.8%           98.2%     0.864                    2.507                    +1,642.73      
  delta    10.6%          98.0%     1.568                    2.397                    +829.10        
  foxtrot  9.1%           98.3%     2.836                    3.519                    +683.79        

  In a 3DS-first deployment an abandoned challenge submits no authorization, so
  the submission fee is not incurred; what IS incurred is the 3DS server's
  per-authentication fee, charged whether or not the session ends in a
  submission. ADR-0002's `abandoned -> -fee` row describes the other deployment
  (authorize-then-step-up) and, read literally, charges a submission fee on
  attempts that were never submitted while missing the authentication fee
  entirely. The correction is two terms of different sizes: moving the fee off
  the abandoned failures is small at this abandonment level, while the
  authentication fee is 2 c -- 5-78% of the per-attempt margin [S3] measures on
  this fleet, and a catalog field (auth_fee_minor) the fixture does not yet
  price per acquirer. Neither term re-ranks the fleet here
  (0.00% of contexts disagree), so this is cost honesty first: the
  reported margin must match the ledger, and a fee that is material at these
  ticket sizes belongs in the loss term the moment the catalog prices it.


Reproduce: python3 spikes/0011-sca-friction/sca_friction.py 30000   (RESULTS.md is this output)
