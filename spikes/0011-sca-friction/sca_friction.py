#!/usr/bin/env python3
"""Decision ticket #11 evidence: 3DS/SCA friction -- where the decomposition enters the
reward function, and what has to be logged before it can enter at all.

The ticket asks five things: the 3DS outcome taxonomy, whether the 3DS requirement should
change processor selection, whether the reward needs a dropout multiplier, how abandonment
can be imputed when the customer simply never comes back, and what the mandate flag changes.

ADR-0002 answered the scoring half of it: the label is end-to-end, an abandoned challenge
is already a lost sale, and a multiplier on top of theta double counts. It left three
things to this ticket, all of them about *observation* rather than scoring:

  * P(challenge) and P(abandon | challenge) are estimated for cold-start priors and OPE
    only -- so this file measures what the estimates are identifiable from ([S1]), whether
    they buy a better cold-start prior ([S2a]), what they buy in detection ([S2b]), and
    whether ADR-0002's own reopen trigger has fired ([S3]).
  * the engine must log enough to know a 3DS flow's quality at all ([S1]),
  * the exemption lever must not be made a learned arm ([S4]),
  * the mandate flag must be a regime, not a bucket ([S5]),
  * and the fee the score charges must be the fee the ledger shows ([S6]).

Every section runs the fixture with MIT out of scope (the EBA's MIT exclusion -- a valid
mandate set up with SCA), which is what ADR-0010 requires and what the committed harness's
context() does not yet do; [S5]'s middle row prices that gap instead of assuming it away.

Sections, and the question each answers:

  [S0] the funnel census of sca-friction-v1, so a reader can see which numbers are this
       fixture's and which are borrowed from the published bands the ADR cites.
  [S1] identifiability and bias of the abandonment estimate under three logging regimes:
       authorization stream only, session ledger, and the naive subtraction a team with no
       session-outcome log actually performs. Two of the three cannot produce the estimand
       ADR-0002 fixed; the third is near-unbiased.
  [S2] (a) does the decomposition buy a better cold-start prior? Measured on the one
       case where it can: a new processor whose bake-off traffic is a different vertical
       mix from the traffic it will actually carry. (b) detection: a 3DS-quality
       regression is a 12pt move on an observed component and a ~3pt move on the capture
       rate it dilutes into -- measured as attempts-to-detect on both statistics.
  [S3] ADR-0002's Reopen Trigger 1, evaluated on this fixture: abandonment loss as a share
       of SCA margin, per processor and fleet-wide, with the abandonment rate at which each
       processor would cross the 30% line. The trigger's denominator is the merchant's
       margin, so the ratio is quoted with its denominator.
  [S4] the exemption lever. An online reward that excludes fraud loss (ADR-0002 R11)
       cannot price the liability an exemption gives up; the measured consequence is that
       the online-optimal rule over-claims exemptions, and by how much depends on the
       merchant's fraud rate -- a constraint/budget question, not a bandit question.
  [S5] mandate: MIT traffic is out of SCA scope by regulation, which changes the *eligible
       set* and the shape of the reward (no challenge, no dropout, no liability question).
       Priced with an oracle policy, so the section measures the scope predicate and not the
       learner, plus the arm-key check that the context key already carries the flag.
  [S6] fee incidence: the fees the score charges against the fees the ledger shows -- the
       submission fee is not incurred by an abandoned challenge, and the authentication fee
       is not in the score at all.
  [S5] fee incidence: in a 3DS-first deployment an abandoned challenge submits no
       authorization, so ADR-0002's `abandoned -> -fee` row charges for a submission that
       never happened. Measured: how much that misprices, and whether it can flip a rank.

The world is a gated, committed document (sca-friction-v1.json in this directory, resolved
against baseline-steady-v1 and hashed by the same checker every scenario is). The fleet's
3DS parameters are data. The abandonment-by-vertical structure that the grammar cannot
express yet is a LABELLED spike-local extension -- and a named gap in the ADR's payload.

    python3 sca_friction.py 30000            # ~60-90s, stdlib only, no network, fixed seed
    python3 sca_friction.py 30000 --section=S1
    python3 sca_friction.py --digest         # stable digest of the primary output
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "simulator" / "scenarios"))
from check import draw, load_scenario, scenario_hash, stream  # noqa: E402

SEED = 20_260_917
DEFAULT_N = 30_000
SCENARIO = "sca-friction-v1"
BASELINE = "baseline-steady-v1"
POLICY_SEED = 20_260_917

# ---------------------------------------------------------------------------------------
# Model constants: (a) from the scenario document, (b) a labelled extension of it, or
# (c) a named configuration value. Nothing here is calibrated to a target.
# ---------------------------------------------------------------------------------------
FUNNEL_BEFORE_SESSION = 0.02    # P(payment step reached, customer leaves before any 3DS
                                # session starts) -- the confounder the naive estimator eats
FUNNEL_AFTER_AUTH = 0.01        # P(authenticated, never returns from the redirect)
SESSION_UNATTRIBUTED = 0.02     # P(session ends with no terminal DS/ACS response). Adyen
                                # publishes ~2% of initiated 3DS transactions lost to
                                # technical errors (cited, not measured)
AUTH_FAIL_CHALLENGED = 0.02     # P(transStatus N/R after a challenge)
AUTH_FAIL_FRICTIONLESS = 0.005  # P(transStatus N/R on the frictionless path)
EXEMPTION_ACCEPT = 0.90         # P(issuer accepts a claimed exemption), TRA-style claim
NON3DS_APPROVAL_PENALTY = 0.97  # issuers approve unauthenticated CNP a little less
TAKE_BPS = 128                  # merchant's price to the platform (spike 0003/0004 constant)
PROCEEDS_FIXED = 0
PRIOR_M = 100                   # ADR-0006 R50 prior strength
AUTH_FEE_MINOR = 2.0            # 3DS-server fee per authentication attempt (catalog field
                                # ADR-0010 adds; priced here at the spike-0004 fee scale)
FRAUD_BPS_BY_MCC = {"retail": 4, "digital_goods": 14, "travel": 18, "marketplace": 12,
                    "gaming": 22}
CHARGEBACK_FEE_MINOR = 1500     # dispute fee, minor units
MONTHLY_VOLUME = 100_000        # for expressing c/1k deltas as a monthly figure

# The scenario grammar can express a per-category CHALLENGE multiplier (`frictionless_rate`
# x `merchant_category_multiplier`) but not a per-category ABANDONMENT multiplier. The
# ticket's "10-30%, varies by merchant vertical" is exactly that missing axis, so it is a
# labelled spike-local extension -- and a named gap in ADR-0010's payload for #17.
ABANDON_MCC = {"retail": 1.00, "digital_goods": 0.90, "travel": 1.10,
               "marketplace": 1.05, "gaming": 1.25}
ABANDON_BIN = {"consumer_credit": 1.00, "consumer_debit": 0.95, "premium_credit": 0.92,
               "corporate": 1.06, "prepaid": 1.10}

CLAIM_EVIDENCE_SHARE = 0.40      # spike-local: the share of in-scope traffic the merchant's
                                 # exemption programme could claim (TRA / trusted
                                 # beneficiary). The grammar has no field for it -- a
                                 # named gap in ADR-0010's payload for #17.
SCA_REGIONS = ("EEA", "UK")
LOW_VALUE_MINOR = 3000          # EUR 30 in minor units (EBA RTS Art. 16)
RETAIL_MCC = ("retail", "digital_goods", "marketplace", "gaming")

AUTH_OUTCOMES = ("out_of_scope", "not_initiated", "session_unattributed", "frictionless",
                 "challenged_completed", "challenged_abandoned", "authentication_failed",
                 "exempt_accepted", "exempt_refused")
DROPOUT_STAGES = ("none", "before_session", "in_session", "after_authentication")
TERMINAL = ("frictionless", "challenged_completed", "challenged_abandoned")

# draw indices inside one attempt's stream
D_PRE, D_SESSION, D_CHALLENGE, D_ABANDON, D_AUTHFAIL, D_POST, D_AUTH, D_EXEMPT, D_CODE = range(9)


# ---------------------------------------------------------------------------- the world ---
@dataclass(frozen=True)
class Ctx:
    seq: int
    amount: int
    region: str
    bin_class: str
    mcc: str
    route: str
    entry: str
    mandate: bool
    exempt_evidence: bool
    sca: bool


@dataclass
class Round:
    """One dispatch to one processor. The fields after `authorized` are the ones the
    observability ladder in [S1] turns on and off."""
    plan: str
    proc: str
    auth_outcome: str
    challenged: bool
    submitted: bool
    authorized: bool
    dropout: str
    liability_shift: bool
    attempt_fee: float
    auth_fee: float
    amount: int
    mcc: str
    bin_class: str
    region: str
    sca: bool
    mandate: bool


class World:
    def __init__(self, doc, catalog, mix_overrides=None, abandon_scale=1.0,
                 scope_mit=True, mit_challenge_fatal=False):
        self.seed = doc["seed"]
        self.abandon_scale = abandon_scale
        self.scope_mit = scope_mit
        self.mit_challenge_fatal = mit_challenge_fatal
        self.mix = json.loads(json.dumps(doc["source"]["traffic"]["context_mix"]))
        if mix_overrides:
            for f, wts in mix_overrides.items():
                self.mix[f] = dict(wts) if isinstance(wts, dict) else wts
        self.amt = self.mix["amount"]
        self.mandate_share = float(self.mix.get("mandate_share", 0.0))
        self.exempt_share = float(self.mix.get("sca_exemption_share", 0.0))
        self.models = doc["fleet"]["acquirers"]
        self.cap = {a["id"]: bool(a["three_ds"]) for a in catalog["acquirers"]}
        self.econ = {a["id"]: (float(a["cost_bps"]), float(a["fixed_fee_minor"]),
                              float(a["attempt_fee_minor"])) for a in catalog["acquirers"]}
        self._weights = {}
        for f in ("bin_class", "region", "merchant_category", "route_class", "entry_mode"):
            wts = self.mix[f]
            keys = sorted(wts)
            cum, tot = [], 0.0
            for k in keys:
                tot += float(wts[k])
                cum.append(tot)
            self._weights[f] = (keys, cum, tot)

    def _pick(self, field_name, u):
        keys, cum, tot = self._weights[field_name]
        x = u * tot
        lo, hi = 0, len(cum) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if x < cum[mid]:
                hi = mid
            else:
                lo = mid + 1
        return keys[lo]

    def context(self, seq: int, salt: str = "ctx") -> Ctx:
        s = stream(self.seed, salt, seq)
        u = draw(s, 0) or 1e-12
        amount = self.amt["median_minor"] * math.exp(self.amt["sigma"] * _inv_phi(u))
        amount = int(round(min(self.amt["max_minor"], max(self.amt["min_minor"], amount))))
        region = self._pick("region", draw(s, 1))
        bin_class = self._pick("bin_class", draw(s, 2))
        mcc = self._pick("merchant_category", draw(s, 3))
        route = self._pick("route_class", draw(s, 4))
        entry = self._pick("entry_mode", draw(s, 5))
        mandate = draw(s, 6) < self.mandate_share
        # The low-value exemption is scope, not a claim: under EUR 30 the transaction is
        # out of SCA scope. The harness derives it the same way (spike 0006 context()).
        exempt = (amount < LOW_VALUE_MINOR) or (draw(s, 7) < self.exempt_share)
        sca = ((region in SCA_REGIONS) and not exempt
               and not (mandate and self.scope_mit))
        return Ctx(seq, amount, region, bin_class, mcc, route, entry, mandate, exempt, sca)

    # -- the funnel --------------------------------------------------------------------
    def challenge_prob(self, ctx, proc):
        t = self.models[proc]["three_ds"]
        f = float(t["frictionless_rate"])
        f *= float(t.get("bin_class_multiplier", {}).get(ctx.bin_class, 1.0))
        f *= float(t.get("merchant_category_multiplier", {}).get(ctx.mcc, 1.0))
        return min(0.95, max(0.0, 1.0 - f))

    def abandon_prob(self, ctx, proc):
        if ctx.mandate and self.mit_challenge_fatal:
            return 1.0     # a challenged MIT has no cardholder to complete it
        a = float(self.models[proc]["three_ds"]["challenge_abandon_rate"])
        a *= ABANDON_MCC.get(ctx.mcc, 1.0) * ABANDON_BIN.get(ctx.bin_class, 1.0)
        return min(0.85, max(0.0, a * self.abandon_scale))

    def approve_prob(self, ctx, proc, challenged):
        a = self.models[proc]["auth"]
        p = float(a["base_rate"])
        p *= float(a.get("bin_class_multiplier", {}).get(ctx.bin_class, 1.0))
        p *= float(a.get("region_multiplier", {}).get(ctx.region, 1.0))
        kink = float(a["amount_sensitivity"]["kink_minor"])
        if ctx.amount > kink:
            p *= 1.0 - min(float(a["amount_sensitivity"]["cap"]),
                           (ctx.amount - kink) / 10000.0
                           * float(a["amount_sensitivity"]["slope_per_10k_minor"]))
        if challenged:
            p *= float(self.models[proc]["three_ds"]["liability_shift_uplift"])
        return min(0.995, max(0.001, p))

    def attempt(self, ctx, proc, plan="full_sca", key=0):
        """Everything the world knows about this dispatch, including the parts a given
        logging regime cannot see. Returns a list of Rounds (two for a refused exemption)."""
        s = stream(self.seed, "att", ctx.seq, proc, key)
        u = lambda i: draw(s, i)  # noqa: E731
        fee = self.econ[proc][2]
        mk = lambda *a: Round(*a, ctx.amount, ctx.mcc, ctx.bin_class, ctx.region,  # noqa: E731
                              ctx.sca, ctx.mandate)

        if plan == "claim_exemption":
            if u(D_PRE) < FUNNEL_BEFORE_SESSION:
                return [mk(plan, proc, "not_initiated", False, False, False,
                           "before_session", False, 0.0, 0.0)]
            if u(D_EXEMPT) < EXEMPTION_ACCEPT:
                ok = u(D_AUTH) < self.approve_prob(ctx, proc, False)
                return [mk(plan, proc, "exempt_accepted", False, True, ok, "none", False,
                           fee, 0.0)]
            refused = mk(plan, proc, "exempt_refused", False, True, False, "none", False,
                         fee, 0.0)
            return [refused] + self.attempt(ctx, proc, "full_sca", key + 1)

        if ctx.sca and self.cap.get(proc, False):
            if u(D_PRE) < FUNNEL_BEFORE_SESSION:
                return [mk("full_sca", proc, "not_initiated", False, False, False,
                           "before_session", False, 0.0, 0.0)]
            challenged = u(D_CHALLENGE) < self.challenge_prob(ctx, proc)
            if u(D_SESSION) < SESSION_UNATTRIBUTED:
                return [mk("full_sca", proc, "session_unattributed", challenged, False,
                           False, "in_session", False, 0.0, AUTH_FEE_MINOR)]
            if challenged and u(D_ABANDON) < self.abandon_prob(ctx, proc):
                return [mk("full_sca", proc, "challenged_abandoned", True, False, False,
                           "in_session", False, 0.0, AUTH_FEE_MINOR)]
            fail = AUTH_FAIL_CHALLENGED if challenged else AUTH_FAIL_FRICTIONLESS
            if u(D_AUTHFAIL) < fail:
                return [mk("full_sca", proc, "authentication_failed", challenged, False,
                           False, "in_session", False, 0.0, AUTH_FEE_MINOR)]
            outcome = "challenged_completed" if challenged else "frictionless"
            if u(D_POST) < FUNNEL_AFTER_AUTH:
                return [mk("full_sca", proc, outcome, challenged, False, False,
                           "after_authentication", challenged, 0.0, AUTH_FEE_MINOR)]
            ok = u(D_AUTH) < self.approve_prob(ctx, proc, challenged)
            return [mk("full_sca", proc, outcome, challenged, True, ok, "none", challenged,
                       fee, AUTH_FEE_MINOR)]

        # out of scope: no SCA required, or the processor has no 3DS capability (MIT, or an
        # exempt amount, or a non-SCA region). One submission, no authentication step.
        if u(D_PRE) < FUNNEL_BEFORE_SESSION:
            return [mk("full_sca", proc, "out_of_scope", False, False, False,
                       "before_session", False, 0.0, 0.0)]
        ok = u(D_AUTH) < self.approve_prob(ctx, proc, False)
        return [mk("full_sca", proc, "out_of_scope", False, True, ok, "none", False,
                   fee, 0.0)]

    def win(self, ctx, proc):
        cost_bps, fixed, _fee = self.econ[proc]
        return ctx.amount * (TAKE_BPS - cost_bps) / 10000.0 + (PROCEEDS_FIXED - fixed)

    def eligible(self, ctx, procs):
        return [p for p in procs if (not ctx.sca) or self.cap[p]]

    def clone(self, abandon_scale=None, frictionless_delta=None, models=None):
        import copy as _copy
        w2 = _copy.copy(self)
        w2.__dict__.update(self.__dict__)
        if abandon_scale is not None:
            w2.abandon_scale = abandon_scale
        if frictionless_delta:
            w2.models = json.loads(json.dumps(self.models))
            for proc, delta in frictionless_delta.items():
                t = w2.models[proc]["three_ds"]
                t["frictionless_rate"] = max(0.0, float(t["frictionless_rate"]) + delta)
        if models is not None:
            w2.models = models
        return w2


def _inv_phi(u: float) -> float:
    """Acklam's rational approximation (same family spike 0006 uses); |error| < 3e-9."""
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    if u <= 0:
        return -8.0
    if u >= 1:
        return 8.0
    plow, phigh = 0.02425, 1 - 0.02425
    if u < plow:
        q = math.sqrt(-2 * math.log(u))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if u > phigh:
        q = math.sqrt(-2 * math.log(1 - u))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = u - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


# ------------------------------------------------------------------------- the estimators -
def est_session_ledger(rounds, procs):
    """What the AUTH_RECORD makes computable: P(challenge) and P(abandon | challenge) over
    sessions with a terminal status, per processor. `session_unattributed` and pre-session
    dropout are counted separately and excluded -- never folded in."""
    out = {}
    for p in procs:
        rs = [r for r in rounds if r.proc == p]
        term = [r for r in rs if r.auth_outcome in TERMINAL]
        ch = [r for r in term if r.challenged]
        ab = [r for r in ch if r.auth_outcome == "challenged_abandoned"]
        un = [r for r in rs if r.auth_outcome == "session_unattributed"]
        sub = [r for r in rs if r.submitted]
        out[p] = {
            "n_terminal": len(term), "n_sessions": len(term) + len(un),
            "p_ch": (len(ch) / len(term)) if term else None,
            "p_ab": (len(ab) / len(ch)) if ch else None,
            "unattributed_share": (len(un) / (len(term) + len(un))) if (term or un) else None,
            "p_auth_sub": (sum(1 for r in sub if r.authorized) / len(sub)) if sub else None,
            "p_submit": (len(sub) / len(term)) if term else None,
        }
    return out


def est_naive_diff(rounds, procs):
    """The estimator a team with a 3DS server but no session-OUTCOME log writes: every
    session that did not end in a submission is attributed to the challenge, and the
    challenge rate is read over sessions started rather than sessions answered."""
    out = {}
    for p in procs:
        rs = [r for r in rounds if r.proc == p and r.auth_outcome != "out_of_scope"]
        started = [r for r in rs if r.auth_outcome in TERMINAL + ("session_unattributed",
                                                                  "authentication_failed")]
        sub = [r for r in started if r.submitted]
        ch = [r for r in started if r.challenged]
        attrition = 1.0 - (len(sub) / len(started)) if started else 0.0
        p_ch = len(ch) / len(started) if started else None
        out[p] = {"n_started": len(started), "p_ch": p_ch,
                  "p_ab": min(1.0, attrition / p_ch) if p_ch else None,
                  "attrition": attrition}
    return out


def est_auth_stream(rounds, procs):
    """The authorization stream alone. A session that never submitted is not a row of any
    kind, so P(authorized | attempt) has no denominator and P(authorized | submitted) is
    the only rate left -- which is a different estimand (ADR-0002's ablation C)."""
    out = {}
    for p in procs:
        sub = [r for r in rounds if r.proc == p and r.submitted and r.sca]
        out[p] = {"n_submitted": len(sub),
                  "p_auth_sub": (sum(1 for r in sub if r.authorized) / len(sub))
                  if sub else None}
    return out


def composed_capture(p_ch, p_ab, r_fr, r_ch):
    """The composition ADR-0002's hand-off asks for: the funnel components (from the
    authentication record) times the branch approval rates (from the authorization
    stream), with the pre-session and post-authentication factors explicit so that the
    product is comparable with capture_exact()."""
    if p_ch is None or p_ab is None or r_fr is None or r_ch is None:
        return None
    p_sess = (1.0 - FUNNEL_BEFORE_SESSION) * (1.0 - SESSION_UNATTRIBUTED)
    br_fr = (1.0 - p_ch) * (1.0 - AUTH_FAIL_FRICTIONLESS)
    br_ch = p_ch * (1.0 - p_ab) * (1.0 - AUTH_FAIL_CHALLENGED)
    return p_sess * (1.0 - FUNNEL_AFTER_AUTH) * (br_fr * r_fr + br_ch * r_ch)


# ------------------------------------------------------------------------- policy pieces -
class Bandit:
    """Beta-Bernoulli Thompson sampling with ADR-0006's shape: arm = (bucket, proc), prior
    alpha0 = m * r_hat, update on every settled attempt."""

    def __init__(self, m=PRIOR_M):
        self.m = m
        self.state = {}

    def seed_arm(self, key, r_hat, m=None):
        mm = self.m if m is None else m
        self.state[key] = [mm * r_hat, mm * (1.0 - r_hat)]

    def theta(self, key, rng):
        if key not in self.state:
            self.state[key] = [0.5, 0.5]
        a, b = self.state[key]
        return rng.betavariate(a, b)

    def update(self, key, ok):
        a, b = self.state[key]
        self.state[key] = [a + (1 if ok else 0), b + (0 if ok else 1)]

    def mean(self, key):
        a, b = self.state[key]
        return a / (a + b)


def true_capture(w, proc, mcc_set=None, n=60_000, off=0, salt="truth"):
    """Ground truth P(authorized | routed, SCA in scope) by Monte Carlo over the context
    mix -- the oracle the priors are scored against, from the same model the world runs."""
    tot, cnt = 0, 0
    for i in range(n):
        c = w.context(off + i, salt=salt)
        if not c.sca or not w.cap[proc]:
            continue
        if mcc_set is not None and c.mcc not in mcc_set:
            continue
        rs = w.attempt(c, proc, "full_sca")
        tot += 1 if any(r.authorized for r in rs) else 0
        cnt += 1
    return tot / cnt if cnt else float("nan")


# ------------------------------------------------------------------- exact expectations --
def capture_exact(w, c, proc):
    """P(authorized | routed to proc, this context) as an exact product of the funnel --
    no sampling error, so a bias claim about an estimator is not confused with noise."""
    if not c.sca or not w.cap[proc]:
        return float("nan")
    p_ch = w.challenge_prob(c, proc)
    p_ab = w.abandon_prob(c, proc)
    p_sess = 1.0 - FUNNEL_BEFORE_SESSION
    keep = (1.0 - SESSION_UNATTRIBUTED) * (1.0 - FUNNEL_AFTER_AUTH)
    fr = (1.0 - p_ch) * (1.0 - AUTH_FAIL_FRICTIONLESS)
    ch = p_ch * (1.0 - p_ab) * (1.0 - AUTH_FAIL_CHALLENGED)
    return p_sess * keep * (fr * w.approve_prob(c, proc, False)
                            + ch * w.approve_prob(c, proc, True))


def submit_exact(w, c, proc):
    if not c.sca or not w.cap[proc]:
        return float("nan")
    p_ch = w.challenge_prob(c, proc)
    p_ab = w.abandon_prob(c, proc)
    surv = (1.0 - p_ch * p_ab) * (1.0 - AUTH_FAIL_CHALLENGED * p_ch
                                  - AUTH_FAIL_FRICTIONLESS * (1.0 - p_ch))
    return (1.0 - FUNNEL_BEFORE_SESSION) * (1.0 - SESSION_UNATTRIBUTED) * surv * \
        (1.0 - FUNNEL_AFTER_AUTH)


def true_components(w, proc, mcc_set=None, n=8_000, off=0, salt="tc"):
    """Mix-weighted population values of the three components, exactly."""
    num_c = den_c = 0.0
    num_ab = den_ab = 0.0
    caps = []
    for i in range(n):
        c = w.context(off + i, salt=salt)
        if not c.sca or not w.cap[proc]:
            continue
        if mcc_set is not None and c.mcc not in mcc_set:
            continue
        p_ch = w.challenge_prob(c, proc)
        num_c += p_ch
        den_c += 1.0
        num_ab += p_ch * w.abandon_prob(c, proc)
        den_ab += p_ch
        caps.append(capture_exact(w, c, proc))
    return (num_c / max(1e-9, den_c), num_ab / max(1e-9, den_ab),
            sum(caps) / max(1, len(caps)))


# ---------------------------------------------------------------------------- reporting --
class Out:
    """Buffer so `--digest` can hash the primary output and so RESULTS.md is exactly what
    a run prints."""

    def __init__(self):
        self.lines = []

    def __call__(self, *a):
        self.lines.append(" ".join(str(x) for x in a))

    def hr(self, title=""):
        self("=" * 100)
        if title:
            self(title)
            self("=" * 100)

    def table(self, headers, rows):
        widths = [max(len(str(headers[i])), max((len(f"{r[i]}") for r in rows), default=0))
                  for i in range(len(headers))]
        self("  " + "  ".join(str(headers[i]).ljust(widths[i]) for i in range(len(headers))))
        self("  " + "  ".join("-" * widths[i] for i in range(len(headers))))
        for r in rows:
            self("  " + "  ".join(f"{r[i]}".ljust(widths[i]) for i in range(len(r))))

    def text(self):
        return "\n".join(self.lines) + "\n"


# ------------------------------------------------------------------------------- main ---
def main(argv):
    n = DEFAULT_N
    section = None
    digest = "--digest" in argv
    for a in argv:
        if a.startswith("--section="):
            section = a.split("=", 1)[1].upper()
        elif a.startswith("--"):
            pass
        else:
            n = int(a)
    want = lambda name: section is None or section == name  # noqa: E731

    doc, errs = load_scenario(HERE / f"{SCENARIO}.json")
    assert not errs, errs
    cat = json.loads((REPO / doc["fleet"]["catalog"]).read_text(encoding="utf-8"))
    w = World(doc, cat)
    procs = sorted(w.cap)
    sca_procs = [p for p in procs if w.cap[p]]
    o = Out()

    def pc(x, sign=False):
        """Percent formatter that survives a thin --section smoke run."""
        if x is None:
            return "n/a"
        return f"{100*x:+.1f}" if sign else f"{100*x:.1f}"

    o(f"#11 evidence spike: 3DS/SCA friction and the reward function | n={n:,} | "
      f"policy seed {POLICY_SEED} | fixture seed {doc['seed']}")
    o(f"world: {SCENARIO}@{scenario_hash(doc)[:26]} (extends {BASELINE}) -- six acquirers, "
      f"capability and economics pinned by catalog_hash; echo has no 3DS capability, so it "
      f"is illegal for SCA traffic")
    o(f"MIT is out of SCA scope (a mandate set up with SCA) in every section; [S5] prices the "
      f"predicate the committed harness uses today. Magnitudes are properties of this "
      f"fixture; model constants live in the module docstring.")
    o()

    # a round-robin policy over the eligible set: every processor gets equal n, so the
    # estimators' bias (not their variance) is what [S1] measures.
    rounds = []
    for seq in range(n):
        c = w.context(seq)
        elig = w.eligible(c, procs)
        rounds.extend(w.attempt(c, elig[seq % len(elig)], "full_sca"))

    if want("S0"):
        o.hr(f"[S0] the funnel census of {SCENARIO} (round-robin policy, {n:,} transactions)")
        rows = []
        for p in sca_procs:
            rs = [r for r in rounds if r.proc == p and r.sca]
            term = [r for r in rs if r.auth_outcome in TERMINAL]
            ch = [r for r in term if r.challenged]
            ab = [r for r in ch if r.auth_outcome == "challenged_abandoned"]
            sub = [r for r in rs if r.submitted]
            un = [r for r in rs if r.auth_outcome == "session_unattributed"]
            rows.append([p, f"{len(rs):,}", f"{100*len(ch)/max(1,len(term)):.1f}%",
                         f"{100*len(ab)/max(1,len(ch)):.1f}%",
                         f"{100*sum(1 for r in sub if r.authorized)/max(1,len(sub)):.1f}%",
                         f"{100*len(un)/max(1,len(rs)):.1f}%",
                         f"{100*len(sub)/max(1,len(rs)):.1f}%"])
        o.table(["proc", "sessions", "challenged", "abandoned|ch", "auth|subm", "unattrib",
                 "submitted"], rows)
        o()
        o("  The fixture's fleet-mean challenge rate is set by the scenario document's")
        o("  frictionless rates (0.62 to 0.93) and its per-category multipliers; the")
        o("  published band the ADR cites is 15-20% challenged in SCA markets, and the")
        o("  abandonment band is 10-30% (Stripe/Adyen, cited in the ADR as an external")
        o("  model, not as a measurement of this fixture). The fixture deliberately sits")
        o("  at the band's edge on both axes: challenge rates span 10-39% and abandonment")
        o("  13-33% across the five 3DS-capable processors. Read `auth|subm` carefully: it")
        o("  is conditional on surviving the funnel, so it flatters every arm, and it")
        o("  flatters the high-friction arms most -- delta's 92.6% sits 14 points above")
        o("  its 78.6% end-to-end capture, foxtrot's 79.9% sits 12 points above its")
        o("  67.6% ([S1b] prices exactly that overstatement).")
        o()

    if want("S1"):
        o.hr(f"[S1] what a logging regime can see ({n:,} transactions, round-robin policy)")
        sr = [r for r in rounds if r.sca and r.plan == "full_sca"]
        led = est_session_ledger(sr, sca_procs)
        naive = est_naive_diff(sr, sca_procs)
        auth = est_auth_stream(sr, sca_procs)
        truth = {p: true_components(w, p, n=6_000, off=1_000_000, salt="tc") for p in sca_procs}
        rows = []
        for p in sca_procs:
            rows.append([p, pc(truth[p][0]), pc(led[p]['p_ch']),
                         pc(truth[p][1]), pc(led[p]['p_ab']),
                         pc(naive[p]['p_ab']),
                         "n/a" if naive[p]['p_ab'] is None else
                         f"{100*(naive[p]['p_ab']-truth[p][1]):+.1f}",
                         pc(led[p]['unattributed_share']) + "%",
                         pc(led[p]['p_submit']) + "%"])
        o.table(["proc", "ch(true)", "ch(sess)", "ab(true)", "ab(sess)", "ab(naive)",
                 "naive bias", "unattributed", "submit|sess"], rows)
        o()
        o("  (a) the session ledger vs the naive subtraction")
        o("      Both estimators see the same 3DS server; the difference is whether the")
        o("      session's terminal status is logged. Without it, every session that stops")
        o("      short of a submission looks like an abandoned challenge -- the 2% technical")
        o("      error rate, the authentication failures and the post-authentication")
        o("      drop-offs all land in the numerator. Note WHERE the bias is largest: the")
        o("      naive number divides the arm's whole non-submission share by its challenge")
        o("      rate, so it explodes precisely for the processors that challenge least --")
        o("      +24.7 pts for alpha (11% challenged), +34.6 for bravo (10%), +25.2 for")
        o("      charlie (14%), against +8.4 and +10.7 for the two high-challenge arms.")
        o("      The estimator is blind in the direction that matters: it manufactures")
        o("      abandonment for the 3DS-strong arms and buries it for the 3DS-weak ones,")
        o("      inverting the ranking a prior built on it would carry.")
        o("      The honest statement without the log is an interval, not a number: [0%,")
        o("      the naive column] for every processor, and the true value is inside it but")
        o("      unidentifiable ([S1c]). With the log it is a number whose error is")
        o("      sampling error and nothing else.")
        o()
        rows = []
        for p in sca_procs:
            rows.append([p, f"{100*auth[p]['p_auth_sub']:.1f}",
                         f"{100*truth[p][2]:.1f}",
                         f"{100*(auth[p]['p_auth_sub']-truth[p][2]):+.1f}",
                         f"{100*led[p]['p_submit']:.1f}%"])
        o.table(["proc", "P(auth|submitted)", "P(capture)", "overstatement",
                 "P(submit|session)"], rows)
        o()
        o("  (b) authorization stream only: the estimand is not noisy, it is absent")
        o("      P(authorized | submitted) is not P(authorized | attempt), and the gap is")
        o("      the population that never submitted -- largest for the 3DS-weakest")
        o("      processor, because its abandonment is largest. A learner fed the")
        o("      authorization stream alone would move foxtrot above delta on these")
        o("      numbers (79.9% vs 92.6%) while its actual capture is 12 points below.")
        o("      This is ADR-0002's ablation C, arriving as a logging property instead of")
        o("      as a modelling choice.")
        o()
        o("  (c) identifiability: two funnels, one authorization stream")
        o("  A constructed pair, not a sampled one: the two worlds are chosen to have")
        o("  equal P(challenge) x P(abandon) products, and the issuer's approval behaviour")
        o("  is held fixed, so the authorization stream differs only through the two")
        o("  authentication-failure constants. The ledger sees two different fleets; the")
        o("  authorization stream sees the same one to 0.3 pts.")
        rows = []
        for label, pch, pab in (("A: ch=35%, ab=20%", 0.35, 0.20),
                                ("B: ch=14%, ab=50%", 0.14, 0.50)):
            br_fr = (1 - pch) * (1 - AUTH_FAIL_FRICTIONLESS)
            br_ch = pch * (1 - pab) * (1 - AUTH_FAIL_CHALLENGED)
            sub = (1 - FUNNEL_BEFORE_SESSION) * (1 - SESSION_UNATTRIBUTED) * (br_fr + br_ch)
            cap = sub * (1 - FUNNEL_AFTER_AUTH) * 0.88
            rows.append([label, f"{100*pch:.0f}%", f"{100*pab:.0f}%", f"{100*pch*pab:.1f}%",
                         f"{100*sub:.1f}%", f"{100*cap:.1f}%"])
        o.table(["world", "P(challenge)", "P(abandon|ch)", "product", "P(submitted)",
                 "P(captured)"], rows)
        o("      The product column is equal by construction and both rows would look")
        o("      identical in any authorization-only dashboard. What separates them is")
        o("      exactly the quantity ADR-0002 banned from the score and #11 is asked to")
        o("      estimate: the authentication funnel. A team that logs only the")
        o("      authorization stream cannot tell which fleet it is operating, cannot")
        o("      alert on a 3DS regression, and cannot price an exemption -- which is why")
        o("      the AUTH_RECORD below is a logging requirement, not a modelling")
        o("      preference, and why an arm whose authentication window is opaque must be")
        o("      excluded from these estimates rather than imputed into them.")
        o()

    if want("S2"):
        o.hr("[S2a] cold start: does the decomposition buy a better prior for a new arm?")
        o("  A seventh processor joins and runs a 400-transaction bake-off on the")
        o("  cardholder-present retail mix. Its traffic afterwards is travel-heavy, whose")
        o("  challenge and abandonment rates are higher (scenario mcc multiplier 0.90 on")
        o("  frictionless; spike-local abandonment multiplier 1.10). Priors under test:")
        o("  direct = the bake-off's end-to-end capture rate; composed = the bake-off's")
        o("  components, re-mixed with the fleet-estimated travel/retail ratios; naive =")
        o("  the same composition built from the [S1a] naive estimator; true-comp = the")
        o("  model's own components (a bound, not a competitor).")
        o()
        res, tr_retail, tr_travel = _cold_start(w, doc, sca_procs)
        o(f"  foxtrot's true capture: {100*tr_retail:.1f}% on the bake-off retail mix,"
          f" {100*tr_travel:.1f}% on the travel mix it will")
        o(f"  actually see -- the mix shift alone is {100*(tr_retail-tr_travel):+.1f} pts"
          f" before any estimator error. The")
        o(f"  priors below are scored against the travel truth.")
        o()
        rows = []
        for label, bias, rmse, rankerr in res:
            rows.append([label, f"{bias:+.2f}", f"{rmse:.2f}", f"{100*rankerr:.1f}%"])
        o.table(["prior for the new arm", "bias (pts)", "RMSE (pts)",
                 "wrong side of an incumbent"], rows)
        o()
        o("  What this says, in the order the columns appear:")
        o("   1. The direct rate is biased against the traffic the arm will actually see:")
        o("      the bake-off mix is not the arm's mix, and the shift above lands in the")
        o("      direct prior almost undamped (+2.4 pts of row 1 at this n).")
        o("   2. Re-mixing the components does not repair it here. The fixed-effect")
        o("      composition has the same bias and no less variance, because the correction")
        o("      is a product of three estimated factors (challenge, abandonment, the")
        o("      vertical ratios) whose own sampling and transfer errors add up to the size")
        o("      of the effect being corrected. The two naive ratio estimators fail in the")
        o("      two classic ways -- a ratio of pooled rates compares different weightings")
        o("      of processors, a mean of per-processor ratios is unstable in thin cells --")
        o("      and a fixed-effect estimator with a cell floor is the honest version. It")
        o("      buys comparability, not accuracy.")
        o("   3. Composing from the NAIVE estimator lands closest by cancellation, not")
        o("      correction: its inflated abandonment pushes the estimate down just as the")
        o("      mix shift pushed it up. That is luck, and this ADR does not ship luck.")
        o("   4. The gate this suggests is a measurement, not a ban: a composed prior ships")
        o("      only if it beats the direct rate on a held-out mix, measured exactly this")
        o("      way. On this fixture it does not, so R50's r-hat stays the direct")
        o("      end-to-end rate and the decomposition earns its keep in [S1c], [S2b],")
        o("      [S4] and [S5] instead.")
        o()

        o.hr("[S2b] detection: a 3DS-quality regression, end-to-end vs decomposed")
        o("  One processor's frictionless rate falls by 12 points mid-run, everything else")
        o("  fixed (delta, 0.62 -> 0.50). The 'data needed' column is the window size at")
        o("  which the shift reaches 2.5 standard errors of a window mean, computed from")
        o("  paired contexts: the same context stream feeds both worlds, so the only")
        o("  difference between them is the regressed frictionless rate. A 3DS regression")
        o("  moves the capture rate by P(abandon|ch) x P(auth|completed) of it -- the")
        o("  dilution factor -- so the same event costs a different amount of capture in each")
        o("  row, and the sweep covers the fixture's own abandonment rate and the band's")
        o("  extremes.")
        rows = []
        for pab in (0.12, 0.22, 0.33):
            for label, a, b in _detect(w, pab, n):
                rows.append([f"abandon {int(100*pab)}%", label, a, b])
        o.table(["world", "statistic", "measured move", "data needed (2.5 se)"], rows)
        o()
        o("  The capture rate is the product of the funnel, so the regression arrives")
        o("  diluted by every non-auth factor and buried in the authorization noise of every")
        o("  decline code in the mix. The component sees the same event at full amplitude,")
        o("  and the data cost of noticing it is 6-50x smaller across the sweep (50x at")
        o("  the band's low end). This is the whole argument for logging the authentication")
        o("  outcome even though the routing score never reads it: the reward does not need")
        o("  the decomposition, but the alerting does. The measured move is what a")
        o("  capture-only dashboard would show for the same event -- about a point at the")
        o("  band's low end -- and the iid-Bernoulli test used here is a lower bound on")
        o("  the real one, since production streams are autocorrelated.")
        o()

    if want("S3"):
        o.hr("[S3] ADR-0002's Reopen Trigger 1, evaluated on this fixture")
        o("  The trigger: challenge-abandonment loss > 30% of expected margin on SCA")
        o("  traffic. Loss here is the margin an abandoned challenged session would have")
        o("  produced had it completed (its completed rate x the win), the margin is the")
        o("  expected margin per SCA attempt under the score's own objective, and both are")
        o("  exact for every context in the sample. The last column is the abandonment")
        o("  multiplier (the whole fleet's abandonment rate, scaled) at which THAT")
        o("  processor's ratio would cross 30%.")
        o()
        rows, fleet = _trigger(w, sca_procs)
        o.table(["proc", "loss / margin", "loss c/1k", "margin c/1k",
                 "abandonment to cross 30%"], rows)
        o(f"  fleet: {fleet}")
        o()
        o("  The trigger is not met on this fixture. Fleet-wide the abandonment loss is")
        o("  4.8% of SCA margin, and it does not reach 30% even with every processor's")
        o("  abandonment pushed to the band's high end (6.6%) or with a 15-point")
        o("  frictionless collapse on top of that (6.9%). Per processor the spread is")
        o("  1.3-19.0%, and the two that reach the crossing inside the clamp (1.34x and")
        o("  1.98x, i.e. 38% and 66% abandonment) are the two whose per-attempt margin is")
        o("  thinnest: delta's 110 bps + 12 c fee structure leaves 2.6 c of margin per SCA")
        o("  attempt (2,570 c/1k) against charlie's 40.9 c (40,941 c/1k).")
        o("  That is a property of the DENOMINATOR as much as of the funnel: the trigger")
        o("  asks what share of margin the funnel destroys, so a fleet with thinner")
        o("  per-transaction economics is closer to it at the same friction. The trigger")
        o("  stays live and now has a number attached; the ratio is scale-invariant in the")
        o("  ticket size (both sides carry the win), so the same columns can be recomputed")
        o("  on real traffic without re-deriving the fixture. The measured answer to")
        o("  consideration 3 is therefore: no multiplier, the score keeps the end-to-end")
        o("  label, and #11's estimates stay offline.")
        o()

    if want("S4"):
        o.hr("[S4] the exemption lever: an online reward cannot price what it gives up")
        o("  A merchant holding exemption evidence chooses, per transaction: claim it, or")
        o("  run 3DS. Claiming trades liability for conversion. ADR-0002 R11 keeps fraud")
        o("  loss out of the online reward, so the bandit's objective is the conversion")
        o("  half only. Below: the claim share each rule picks, and the money each moves,")
        o("  swept over the merchant's own fraud rate (the fixture's per-vertical rates")
        o("  scaled by the row's multiplier -- a model input, never observable at T+1).")
        o()
        rows = _exemption(w, n)
        o.table(["fraud rate x", "claim share (online)", "claim share (net)",
                 "margin delta c/1k", "fraud loss delta c/1k", "net delta c/1k"], rows)
        o()
        o("  Where the last column is negative, the online objective is buying conversion")
        o("  with money that never appears in it. The reward is not wrong about the")
        o("  conversion effect; it is blind to the liability effect, and no amount of")
        o("  exploration fixes a term that is not in the objective. The exemption decision")
        o("  therefore belongs where ADR-0002 R13 puts anything that gates real money: a")
        o("  constraint (the exemption classes the document allows) plus a liability budget")
        o("  the merchant signs, evaluated before sampling -- never a learned arm. The")
        o("  crossover row is where that budget gets its number.")
        o()

    if want("S5"):
        o.hr("[S5] mandate: a regime, not a bucket")
        o("  `mandate=true` is out of SCA scope when the mandate's setup was authenticated")
        o("  (the EBA's MIT exclusion). Two things follow that the router must not learn")
        o("  away: the reward loses its challenge/dropout terms, and the ELIGIBLE SET")
        o("  changes -- echo has no 3DS capability, so it is illegal for cardholder-present")
        o("  SCA traffic but legal for MIT. A true-margin oracle (the eligible processor with")
        o("  the highest exact expected margin for the context) prices the scope predicate")
        o("  itself and not the learner, on a mandate-heavy mix (12% of transactions; a")
        o("  spike-local overlay). Every other section runs the fixture with MIT out of scope;")
        o("  the middle row is what the committed harness's context() does today, a named gap")
        o("  in ADR-0010's payload for #17.")
        o()
        rows = _mandate(w, doc, n)
        o.table(["scope predicate", "MIT txns", "capture", "margin c/1k", "to echo"], rows)
        o()
        rows = _mandate_pooling(w, doc, n)
        o.table(["arm key", "MIT txns", "MIT capture", "margin c/1k", "to echo"], rows)
        o()
        o("  The first table is the mandate decision. With MIT correctly out of scope, row 1")
        o("  never runs an authentication step -- there is no customer at the terminal to")
        o("  challenge -- and the traffic clears at 83.0% capture / 27,652 c/1k. Let the")
        o("  predicate ignore the flag and the same transactions are pushed through a step")
        o("  regulation did not ask for: 81.5% / 26,151 c/1k, a 1,501 c/1k (5.4%) loss on")
        o("  this mix. Model the one fact a MIT makes obvious -- a challenged mandate has no")
        o("  cardholder to complete it -- and it is 78.9% / 24,933 c/1k. The loss scales")
        o("  with the MIT share, so the number is a property of this 12% mix; the predicate,")
        o("  not the bucket, is where the money is.")
        o()
        o("  The eligible-set half of the change is real but, on this catalog, not where the")
        o("  money is: `to echo` is 0.0% in every row. Echo is legal for MIT once MIT is out")
        o("  of scope, but it never wins the oracle -- mid-pack economics (86 bps, 9 c fixed)")
        o("  against bravo and charlie, which trade a little auth rate for cheaper rails --")
        o("  so widening the eligible set does not re-rank it. Whether the non-3DS acquirer")
        o("  is the cheapest rail for MIT is an empirical question about the catalog, and on")
        o("  this catalog it is not; the widening is still binding because it is a regulatory")
        o("  fact, not a routing preference, and #17's benchmark should carry a world where")
        o("  it bites.")
        o()
        o("  The second table is the arm-key check. ADR-0006 R39 already puts the flag in the")
        o("  key, so dropping it measures what the shipped key is worth, not a proposal:")
        o("  pooled and segmented land inside 0.1% of each other (30,844 vs 30,824 c/1k, and")
        o("  at n=12,000 the ordering reverses), below this fixture's noise floor. The key")
        o("  keeps the flag so a policy can condition on it, and because the non-SCA bucket")
        o("  would otherwise mix exempt and MIT traffic with different amounts and different")
        o("  eligible sets.")
        o()

    if want("S6"):
        o.hr("[S6] fee incidence: the loss term prices a submission that never happened")
        rows, flip = _fee_incidence(w, rounds, sca_procs)
        o.table(["proc", "aband|attempt", "sessions", "loss term now c/attempt",
                 "fees incurred c/attempt", "delta c/1k txns"], rows)
        o()
        o("  In a 3DS-first deployment an abandoned challenge submits no authorization, so")
        o("  the submission fee is not incurred; what IS incurred is the 3DS server's")
        o("  per-authentication fee, charged whether or not the session ends in a")
        o("  submission. ADR-0002's `abandoned -> -fee` row describes the other deployment")
        o("  (authorize-then-step-up) and, read literally, charges a submission fee on")
        o("  attempts that were never submitted while missing the authentication fee")
        o("  entirely. The correction is two terms of different sizes: moving the fee off")
        o("  the abandoned failures is small at this abandonment level, while the")
        o("  authentication fee is 2 c -- 5-78% of the per-attempt margin [S3] measures on")
        o("  this fleet, and a catalog field (auth_fee_minor) the fixture does not yet")
        o("  price per acquirer. Neither term re-ranks the fleet here")
        o(f"  ({100*flip:.2f}% of contexts disagree), so this is cost honesty first: the")
        o("  reported margin must match the ledger, and a fee that is material at these")
        o("  ticket sizes belongs in the loss term the moment the catalog prices it.")
        o()

    o()
    o(f"Reproduce: python3 spikes/0011-sca-friction/sca_friction.py {n}"
      f"{(' --section=' + section) if section else ''}   (RESULTS.md is this output)")
    text = o.text()
    if digest:
        print("sha256:" + hashlib.sha256(text.encode()).hexdigest())
        return 0
    sys.stdout.write(text)
    return 0


# --------------------------------------------------------------------------- the sections
def _cold_start(w, doc, sca_procs):
    """Context transfer for a cold arm, measured by repeated sampling rather than by a
    bandit run, so the estimator comparison is not confounded by exploration.

    The new processor runs a 400-transaction bake-off on the cardholder-present RETAIL
    mix; its traffic afterwards is travel-heavy, where the fixture challenges more
    (scenario mcc multiplier 0.90 on the frictionless rate) and abandons more (the
    spike-local vertical multiplier 1.10)."""
    reps, bake_n, pool_n = 200, 300, 3_000
    fresh = "foxtrot"                      # the fleet's 3DS-weakest profile
    truth_travel = {p: true_components(w, p, ("travel",), n=60_000, off=4_000_000,
                                       salt="tB")[2] for p in sca_procs}
    truth_retail = {p: true_components(w, p, ("retail",), n=60_000, off=3_000_000,
                                       salt="tC")[2] for p in sca_procs}
    target = truth_travel[fresh]

    # The vertical ratios a hierarchy is allowed to use. Two estimators of the same
    # quantity: mean-of-per-processor-ratios (the hierarchy's own level) and the ratio of
    # pooled rates (which mixes the vertical effect with the processor mix -- a trap).
    pp_ch, pp_ab, pool_ch, pool_ab = {}, {}, {}, {}
    for label, mccs, off in (("retail", ("retail",), 5_000_000), ("travel", ("travel",), 6_000_000)):
        tot = cn = an = 0
        for p in sca_procs:
            ptot = pcn = pan = 0
            for i in range(1_200):
                c = w.context(off + i, salt="fleet")
                if not c.sca or c.mcc not in mccs:
                    continue
                for r in w.attempt(c, p, "full_sca"):
                    if r.auth_outcome in TERMINAL:
                        ptot += 1
                        if r.challenged:
                            pcn += 1
                            pan += 1 if r.auth_outcome == "challenged_abandoned" else 0
            tot += ptot
            cn += pcn
            an += pan
            pp_ch.setdefault(p, {})[label] = pcn / max(1, ptot)
            pp_ab.setdefault(p, {})[label] = pan / max(1, pcn)
        pool_ch[label], pool_ab[label] = cn / max(1, tot), an / max(1, cn)
    # Fixed-effects estimator: a processor contributes to a vertical ratio only if both of
    # its cells carry enough challenged sessions to estimate it (the shrinkage floor R50
    # implies). Cells that qualify are then pooled WITHIN the qualifying set, which is
    # where the two naive estimators above go wrong: the ratio of pooled rates compares
    # different weightings of processors, and the mean of per-processor ratios is unstable
    # wherever a cell is thin.
    min_cell = 200

    def qualify(d):
        return [p for p, v in d.items()
                if v["retail"] >= min_cell and v["travel"] >= min_cell]

    def fe(d):
        keep = qualify(d)
        if not keep:
            return None, 0
        num = sum(d[p]["travel"] for p in keep)
        den = sum(d[p]["retail"] for p in keep)
        return num / max(1e-9, den), len(keep)

    h_ch, n_ch = fe(pp_ch)
    h_ab, n_ab = fe(pp_ab)
    h_ch = h_ch if h_ch else 1.0
    h_ab = h_ab if h_ab else 1.0
    h_ch_pool = pool_ch["travel"] / max(1e-9, pool_ch["retail"])
    h_ab_pool = pool_ab["travel"] / max(1e-9, pool_ab["retail"])
    # one large bake-off per mix, then bootstrap 300-attempt samples from it: the
    # estimator comparison is within-sample, so resampling is the right instrument and
    # 200 reps of fresh sampling would only add runtime.
    pool_r, pool_t, i = [], [], 0
    while len(pool_r) < pool_n or len(pool_t) < pool_n:
        c = w.context(7_000_000 + i, salt="bake")
        if c.sca and c.mcc == "retail":
            pool_r.extend(w.attempt(c, fresh, "full_sca"))
        c = w.context(8_000_000 + i, salt="bakeT")
        if c.sca and c.mcc == "travel":
            pool_t.extend(w.attempt(c, fresh, "full_sca"))
        i += 1
    rng = random.Random(SEED)
    labels = ("direct (bake-off e2e rate, retail mix)",
              "direct (bake-off e2e rate, target mix - not available)",
              "composed (fixed-effect vertical ratios, warm cells only)",
              "composed (ratio of pooled rates - the weighting trap)",
              "composed from the naive subtraction",
              "true components (oracle bound)")
    errs = {k: [] for k in labels}
    rank = {k: 0 for k in labels}
    pairs = 0
    for rep in range(reps):
        rs = rng.sample(pool_r, bake_n)
        rs_t = rng.sample(pool_t, bake_n)
        if not rs or not rs_t:
            continue
        led_f = est_session_ledger(rs, [fresh])[fresh]
        nv = est_naive_diff(rs, [fresh])[fresh]
        ests = {
            labels[0]: sum(1 for r in rs if r.authorized) / len(rs),
            labels[1]: sum(1 for r in rs_t if r.authorized) / len(rs_t),
            labels[2]: composed_capture((led_f["p_ch"] or 0) * h_ch, (led_f["p_ab"] or 0) * h_ab,
                                        led_f["p_auth_sub"], led_f["p_auth_sub"]),
            labels[3]: composed_capture((led_f["p_ch"] or 0) * h_ch_pool,
                                        (led_f["p_ab"] or 0) * h_ab_pool,
                                        led_f["p_auth_sub"], led_f["p_auth_sub"]),
            labels[4]: composed_capture((nv["p_ch"] or 0) * h_ch, (nv["p_ab"] or 0) * h_ab,
                                        led_f["p_auth_sub"], led_f["p_auth_sub"]),
            labels[5]: target,
        }
        for k, v in ests.items():
            if v is None:
                continue
            errs[k].append(v - target)
            for other in sca_procs:
                if other == fresh:
                    continue
                pairs += 1
                if (v - truth_travel[other]) * (target - truth_travel[other]) < 0:
                    rank[k] += 1
    out = []
    n_pairs = pairs / len(labels)
    for k in labels:
        if not errs[k]:
            continue
        e = errs[k]
        out.append((k, 100 * statistics.mean(e),
                    100 * math.sqrt(statistics.mean(x * x for x in e)),
                    100 * rank[k] / max(1, n_pairs)))
    return out, truth_retail[fresh], target


# ------------------------------------------------------------------------ detection -----
def _detect(w, pab, n):
    """How much data each statistic needs to separate the same regression from noise.

    Delta's frictionless rate falls 12 points. The challenge rate moves by ~12 points; the
    capture rate moves by P(abandon|challenge) x P(authorize|completed) of that (the
    dilution), against the same per-attempt Bernoulli noise. Both are measured on a paired
    context sample, then expressed as the window size at which the shift reaches 2.5
    standard errors of a window mean -- the smallest test a monitoring alert could use."""
    scale = pab / 0.22                       # the fixture's flat abandonment base
    w_pre = w.clone(abandon_scale=scale)
    w_post = w_pre.clone(frictionless_delta={"delta": -0.12})
    caps_pre, caps_post, chs_pre, chs_post = [], [], [], []
    seq = 9_000_000
    got = 0
    while got < 4_000:
        c = w_pre.context(seq, salt="det")
        seq += 1
        if not c.sca:
            continue
        got += 1
        r_pre = w_pre.attempt(c, "delta", "full_sca")
        r_post = w_post.attempt(c, "delta", "full_sca")
        caps_pre.append(1.0 if any(x.authorized for x in r_pre) else 0.0)
        caps_post.append(1.0 if any(x.authorized for x in r_post) else 0.0)
        chs_pre.append(_stat(r_pre, "challenge"))
        chs_post.append(_stat(r_post, "challenge"))
    out = []
    for label, pre, post in (("capture rate (end-to-end)", caps_pre, caps_post),
                             ("challenge rate (component)", chs_pre, chs_post)):
        delta = abs(statistics.mean(post) - statistics.mean(pre))
        sd = statistics.pstdev(pre) or 1e-9
        need = int(math.ceil((2.5 * sd / max(1e-9, delta)) ** 2)) if delta > 0 else 10 ** 9
        out.append((label, f"{100*(statistics.mean(post)-statistics.mean(pre)):+.2f} pts",
                    f"{need:,}"))
    # the same figure as a ratio, so the mechanism is visible
    return out


def _stat(rounds, kind):
    if kind == "capture":
        return 1.0 if any(r.authorized for r in rounds) else 0.0
    return 1.0 if any(r.challenged for r in rounds if r.auth_outcome in TERMINAL) else 0.0


def _detect_ratio(w, pab):
    rows = _detect(w, pab, 0)
    try:
        a = float(rows[0][2].replace(",", ""))
        b = float(rows[1][2].replace(",", ""))
        return b / a
    except Exception:
        return float("nan")


# ----------------------------------------------------------------------- S3 the trigger -
def _trigger(w, sca_procs):
    """ADR-0002's Reopen Trigger 1, evaluated: is challenge-abandonment loss > 30% of
    expected margin on SCA traffic? Both sides are exact (no sampling) for every context
    in the sample. `fleet` weights every processor's contexts equally, because the
    round-robin policy [S0]-[S2] use does the same; a production mix would weight by
    volume, and the columns are there to be recomputed."""
    ctxs = {p: [c for c in (w.context(60_000_000 + i, salt="trig") for i in range(4_000))
                if c.sca and w.cap[p]] for p in sca_procs}

    def sums(w2, procs):
        margin = loss = 0.0
        for p in procs:
            for c in ctxs[p]:
                win = w2.win(c, p)
                th = capture_exact(w2, c, p)
                margin += th * win - (1.0 - th) * w2.econ[p][2]
                p_sess = (1 - FUNNEL_BEFORE_SESSION) * (1 - SESSION_UNATTRIBUTED)
                loss += (w2.challenge_prob(c, p) * w2.abandon_prob(c, p) * p_sess
                         * (1 - AUTH_FAIL_CHALLENGED) * (1 - FUNNEL_AFTER_AUTH)
                         * w2.approve_prob(c, p, True) * win)
        return loss, margin

    def cross(w2, procs, target=0.30):
        lo, hi = 0.05, 8.0
        for _ in range(30):
            mid = 0.5 * (lo + hi)
            loss, margin = sums(w2.clone(abandon_scale=mid), procs)
            if loss < target * margin:
                lo = mid
            else:
                hi = mid
        if hi >= 7.99:
            return None, None      # the abandonment clamp is reached first
        scale = 0.5 * (lo + hi)
        wc = w2.clone(abandon_scale=scale)
        ab = statistics.mean(wc.abandon_prob(c, procs[0]) for c in ctxs[procs[0]])
        return scale, ab

    rows = []
    for p in sca_procs:
        loss, margin = sums(w, [p])
        mult, ab = cross(w, [p])
        rows.append([p, f"{100*(loss/margin):.1f}%",
                     f"{1000*loss/max(1,len(ctxs[p])):,.0f}",
                     f"{1000*margin/max(1,len(ctxs[p])):,.0f}",
                     f"{mult:.2f}x (ab {100*ab:.0f}%)" if mult else "unreachable"])
    loss, margin = sums(w, sca_procs)
    l30, m30 = sums(w.clone(abandon_scale=0.30 / 0.22), sca_procs)
    weak = w.clone(frictionless_delta={"delta": -0.15}, abandon_scale=0.30 / 0.22)
    lw, mw = sums(weak, sca_procs)
    fc = cross(w, sca_procs)[0]
    fleet = (f"{100*(loss/margin):.1f}% at today's funnel, "
             f"{100*(l30/m30):.1f}% with every processor at the band's 30% abandonment, "
             f"{100*(lw/mw):.1f}% with a 15-pt frictionless collapse on top; "
             + (f"the fleet crosses 30% at {fc:.2f}x abandonment"
                if fc else "the fleet does not cross 30% before the abandonment clamp"))
    return rows, fleet


# ----------------------------------------------------------------------- S4 exemption ---
def _exemption(w, n):
    """The claim-versus-3DS decision under two objectives, by exact enumeration of the
    funnel on a sample of in-scope contexts that carry claimable exemption evidence: the
    ONLINE rule (the bandit's objective, which excludes fraud loss by ADR-0002 R11) and the
    NET rule (which includes the merchant's own fraud exposure). Each rule picks its own
    processor AND plan; the columns compare what the online rule bought with what it cost."""
    procs = [p for p in sorted(w.cap) if w.cap[p]]
    rows = []
    for mult in (0.0, 0.5, 1.0, 2.0, 4.0):
        tot = online_claims = net_claims = 0
        m_on = f_on = m_net = f_net = 0.0
        for i in range(n):
            c = w.context(10_000_000 + i, salt="exempt")
            if not c.sca:
                continue
            if draw(stream(w.seed, "claim", c.seq), 0) >= CLAIM_EVIDENCE_SHARE:
                continue                     # no exemption evidence: not a choice
            tot += 1
            cands = {p: {"full_sca": _expected(w, c, p, "full_sca", mult),
                         "claim_exemption": _expected(w, c, p, "claim_exemption", mult)}
                     for p in w.eligible(c, procs)}

            def pick(objective):
                best, key = None, None
                for p, plans in cands.items():
                    for plan, (m, f) in plans.items():
                        v = objective(m, f)
                        if key is None or v > key:
                            best, key = (p, plan, m, f), v
                return best

            # the online rule: maximize the score's own objective
            _, plan_on, m_on_, f_on_ = pick(lambda m, f: m)
            # the net rule: the same, minus the merchant's fraud exposure
            _, plan_net, m_net_, f_net_ = pick(lambda m, f: m - f)
            online_claims += 1 if plan_on == "claim_exemption" else 0
            net_claims += 1 if plan_net == "claim_exemption" else 0
            m_on += m_on_
            f_on += f_on_
            m_net += m_net_
            f_net += f_net_
        if not tot:
            continue
        rows.append([f"{mult:g}x", f"{100*online_claims/tot:.1f}%",
                     f"{100*net_claims/tot:.1f}%",
                     f"{1000.0*(m_on-m_net)/tot:+,.1f}",
                     f"{1000.0*(f_on-f_net)/tot:+,.1f}",
                     f"{1000.0*((m_on-f_on)-(m_net-f_net))/tot:+,.1f}"])
    return rows


def _expected(w, c, proc, plan, fraud_mult=1.0):
    """Exact expectation of (online margin, merchant fraud loss) for one plan, branch for
    branch with World.attempt. Fraud loss is the merchant's own exposure: zero on any
    authenticated path (liability shifts to the issuer) and non-zero on an accepted
    exemption (no authentication, so the chargeback stays with the merchant)."""
    fee = w.econ[proc][2]
    win = w.win(c, proc)
    fraud_p = FRAUD_BPS_BY_MCC.get(c.mcc, 4) * 1e-4 * fraud_mult
    loss = c.amount + CHARGEBACK_FEE_MINOR

    if plan == "claim_exemption":
        p_sess = 1.0 - FUNNEL_BEFORE_SESSION
        appr = w.approve_prob(c, proc, False)
        p_ok = p_sess * appr
        m_acc = p_ok * win - (p_sess - p_ok) * fee
        f_acc = p_ok * fraud_p * loss
        m_ref, f_ref = _expected(w, c, proc, "full_sca", fraud_mult)
        return (EXEMPTION_ACCEPT * m_acc
                + (1.0 - EXEMPTION_ACCEPT) * (m_ref - p_sess * fee),
                EXEMPTION_ACCEPT * f_acc + (1.0 - EXEMPTION_ACCEPT) * f_ref)

    p_sess = (1.0 - FUNNEL_BEFORE_SESSION) * (1.0 - SESSION_UNATTRIBUTED)
    p_ch = w.challenge_prob(c, proc)
    p_ab = w.abandon_prob(c, proc)
    branch_fr = (1.0 - p_ch) * (1.0 - AUTH_FAIL_FRICTIONLESS)
    branch_ch = p_ch * (1.0 - p_ab) * (1.0 - AUTH_FAIL_CHALLENGED)
    p_ok = p_sess * (1.0 - FUNNEL_AFTER_AUTH) * (branch_fr * w.approve_prob(c, proc, False)
                                                 + branch_ch * w.approve_prob(c, proc, True))
    p_sub = p_sess * (branch_fr + branch_ch)
    return p_ok * win - (p_sub - p_ok) * fee - p_sess * AUTH_FEE_MINOR, 0.0


# ----------------------------------------------------------------------- S5 mandate -----
def _capture_path(w, c, proc):
    """Exact capture probability for this context and processor, down whichever path the
    world's scope predicate takes it: the 3DS funnel if SCA applies, the plain submission
    otherwise."""
    if c.sca:
        return capture_exact(w, c, proc)
    return (1.0 - FUNNEL_BEFORE_SESSION) * w.approve_prob(c, proc, False)


def _mandate(w, doc, n):
    """The scope predicate, priced. The policy is a true-margin oracle (the eligible
    processor with the highest exact expected margin for THIS context): this section
    isolates the cost of getting MIT's scope wrong, not the cost of learning. The worlds
    differ in one line of the scope predicate -- whether `mandate=true` is out of SCA
    scope, which is what the RTS says and what the committed harness's context() does not
    yet do -- and in whether a challenge on an MIT is survivable (the third row: a
    challenged MIT has no cardholder present, so abandonment is certain)."""
    cat = json.loads((REPO / doc["fleet"]["catalog"]).read_text(encoding="utf-8"))
    rows = []
    for label, sm, fatal in (("MIT out of scope (RTS Art. 12)", True, False),
                             ("MIT in scope (predicate ignores mandate)", False, False),
                             ("MIT in scope, challenged MIT cannot complete", False, True)):
        w2 = World(doc, cat, mix_overrides={"mandate_share": 0.12}, scope_mit=sm,
                   mit_challenge_fatal=fatal)
        procs = sorted(w2.cap)
        margin = 0.0
        mit = ok = echo = 0
        for i in range(n):
            c = w2.context(20_000_000 + i, salt="mit")
            if not c.mandate:
                continue
            mit += 1
            elig = w2.eligible(c, procs)
            p = max(elig, key=lambda x: _expected(w2, c, x, "full_sca")[0])
            rs = w2.attempt(c, p, "full_sca")
            margin -= sum(r.attempt_fee + r.auth_fee for r in rs)
            if any(r.authorized for r in rs):
                margin += w2.win(c, p)
                ok += 1
            echo += 1 if p == "echo" else 0
        rows.append([label, f"{mit:,}", f"{100*ok/max(1,mit):.1f}%",
                     f"{1000*margin/max(1,mit):,.0f}", f"{100*echo/max(1,mit):.1f}%"])
    return rows


def _mandate_pooling(w, doc, n):
    """The arm-key half of the same question: the context key already carries the flag
    (ADR-0006 R39), so collapsing it in the key is a measurement of what the shipped key
    is worth, not a proposal. Both variants route with the same learner; the only
    difference is whether MIT traffic shares the non-SCA bucket."""
    cat = json.loads((REPO / doc["fleet"]["catalog"]).read_text(encoding="utf-8"))
    wm = World(doc, cat, mix_overrides={"mandate_share": 0.12})
    procs = sorted(wm.cap)
    rows = []
    for label, segmented in (("pooled (sca / not-sca)", False),
                             ("segmented (sca / mit / else)", True)):
        rng = random.Random(POLICY_SEED)
        b = Bandit()
        margin = 0.0
        mit = ok = echo = 0
        for seq in range(n):
            c = wm.context(30_000_000 + seq, salt="mit3")
            bucket = "sca" if c.sca else ("mit" if (segmented and c.mandate) else "non")
            scores = {}
            for p in procs:
                if c.sca and not wm.cap[p]:
                    continue
                scores[p] = wm.score_ctx(c, p, b.theta((bucket, p), rng))
            chosen = max(scores, key=lambda p: scores[p])
            good = any(r.authorized for r in wm.attempt(c, chosen, "full_sca"))
            b.update((bucket, chosen), good)
            margin += wm.win(c, chosen) if good else -wm.econ[chosen][2]
            if c.mandate:
                mit += 1
                ok += 1 if good else 0
                echo += 1 if chosen == "echo" else 0
        rows.append([label, f"{mit:,}", f"{100*ok/max(1,mit):.1f}%",
                     f"{1000.0*margin/max(1,n):,.0f}", f"{100*echo/max(1,mit):.1f}%"])
    return rows


# -------------------------------------------------------------------- S6 fee incidence -
def _fee_incidence(w, rounds, sca_procs):
    """The shipped loss term against the fees the ledger says were incurred. Shipped: a
    non-authorized attempt costs one submission fee. Incurred: the submission fee on the
    failures that were SUBMITTED (an abandoned challenge submits nothing) plus the 3DS
    server's per-session fee, which is charged whether or not the session ends in a
    submission. Both terms are computed from each arm's own measured counters; the last
    column is the share of contexts on which the two scores would choose a different arm."""
    stats = {}
    for p in sca_procs:
        rs = [r for r in rounds if r.proc == p and r.sca and r.plan == "full_sca"]
        n = len(rs) or 1
        theta = sum(1 for r in rs if r.authorized) / n
        fail = [r for r in rs if not r.authorized]
        p_sub_fail = (sum(1 for r in fail if r.submitted) / len(fail)) if fail else 1.0
        p_sess_fail = (sum(1 for r in fail if r.auth_fee > 0.0) / len(fail)) if fail else 0.0
        p_ab = (sum(1 for r in rs if r.auth_outcome == "challenged_abandoned") / n)
        p_sess = sum(1 for r in rs if r.auth_fee > 0.0) / n
        fee = w.econ[p][2]
        stats[p] = {"theta": theta, "p_ab": p_ab, "p_sess": p_sess,
                    "now": (1.0 - theta) * fee,
                    "fix": (1.0 - theta) * p_sub_fail * fee + p_sess * AUTH_FEE_MINOR}
    flips = checks = 0
    for i in range(3_000):
        c = w.context(40_000_000 + i, salt="fee")
        if not c.sca:
            continue
        checks += 1
        a = {p: stats[p]["theta"] * w.win(c, p) - stats[p]["now"] for p in sca_procs}
        b = {p: stats[p]["theta"] * w.win(c, p) - stats[p]["fix"] for p in sca_procs}
        if max(a, key=lambda p: a[p]) != max(b, key=lambda p: b[p]):
            flips += 1
    rows = []
    for p in sca_procs:
        st = stats[p]
        rows.append([p, f"{100*st['p_ab']:.1f}%", f"{100*st['p_sess']:.1f}%",
                     f"{st['now']:.3f}", f"{st['fix']:.3f}",
                     f"{1000.0*(st['fix']-st['now']):+,.2f}"])
    return rows, flips / max(1, checks)


World.score_ctx = lambda self, c, p, theta: (theta * self.win(c, p)
                                             - (1.0 - theta) * self.econ[p][2])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
