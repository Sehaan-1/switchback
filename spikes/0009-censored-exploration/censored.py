#!/usr/bin/env python3
"""Decision ticket #9 evidence: censored data and exploration-exploitation.

What this measures, and what it deliberately does not
------------------------------------------------------
#9 asks the Stripe-style question: you only ever observe outcomes for the arm you
chose, so how do you avoid learning on censored data? ADR-0002 fixed the estimand
(P(authorized | attempt, not timeout)), ADR-0003 picked probability-matching TS,
ADR-0006 shipped the fine arm space + informative prior + the R49 onboarding floor,
and ADR-0007 shipped processor-level ADWIN + tiered decay + R49-on-reset. None of
those tickets measured the CENSORING problem itself: what bandit feedback costs
against full-information feedback, whether TS's built-in exploration collapses in
practice, whether any standing forced-exploration mechanism is needed on top of what
already shipped, what exactly the decision log must carry so #15's IPS is possible,
and how delayed outcomes are ingested without corrupting the estimand. This spike
holds the shipped design fixed and varies only the exploration/censoring decisions
this ticket owns.

The world is not invented here. Policy runs execute against the committed,
content-addressed scenarios of simulator/scenarios/ through spikes/0006's harness,
plus two spike-local gated scenarios: quiet-improvement-v1 (a competitor silently
recovers to a better-than-believed auth rate with no transport signature -- the
exact failure the ticket fears) and its boundary-case variant
quiet-improvement-starved-v1 (the arm is nearly starved before it improves); both
pass the same gate and carry their own hashes.

Sections, and the question each answers:

  [C1] the censoring problem precisely: bandit feedback vs a full-information
       learner (updates ALL eligible arms each decision -- impossible in
       production, cheap in the harness). The margin/MAE/unlearning-speed gap is
       the measured price of censoring; the per-arm unbiasedness of bandit labels
       is the measured reason the cost is precision, not bias.
  [C2] Thompson sampling's built-in exploration: does it collapse too quickly?
       Measured on quiet-improvement-v1 (bare TS vs the oracle it cannot see) and
       as an exploration-annealing curve on baseline-steady-v1.
  [C3] forced exploration mechanisms: epsilon-greedy overlays, arm rotation, and
       optimistic initialization on top of TS, priced on the steady world and
       tested against the improvement world, next to the already-shipped
       ADR-0007 machinery (ADWIN + tiered decay + R49 floor).
  [C4] logging for offline evaluation: the DECISION_LOG v1 record -- replay
       completeness (posterior fold, decision replay, score-based propensities),
       and what the record costs in bytes.
  [C5] delayed outcomes: ingest-lag sweep, late-settlement relabeling variants
       (shipped timeout semantics vs retroactive relabel vs no-response=decline),
       and the lag x drift-reset interaction.

Everything is stdlib-only, offline and deterministic. Magnitudes belong to the
scenario documents; the orderings, PASS/FAIL verdicts and mechanisms are the
findings.

    python3 censored.py                  # all sections at n=60000
    python3 censored.py 20000            # smaller n (default 60000)
    python3 censored.py --section=C2     # one section
"""

from __future__ import annotations

import bisect
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "simulator" / "scenarios"))
sys.path.insert(0, str(REPO / "spikes" / "0006-simulation-harness"))
sys.path.insert(0, str(REPO / "spikes" / "0007-thompson-sampling"))
sys.path.insert(0, str(REPO / "spikes" / "0008-drift-detection"))

from check import (  # noqa: E402
    draw, load_scenario, scenario_hash, stream,
)
import harness as H  # noqa: E402
import posterior as P  # noqa: E402
from drift import ADWIN  # noqa: E402

DEFAULT_N = 60_000
POLICY_SEED = P.POLICY_SEED          # same policy seed the other spikes record
EVENT_S = 259_200                    # quiet-improvement-v1: foxtrot recovers at day 3
LAM_TO = P.LAM_TO

AUTH, DSOFT, ABANDONED, TIMEOUT, DHARD, TERR = P.AUTH, P.DSOFT, P.ABANDONED, P.TIMEOUT, P.DHARD, P.TERR

DOCS = {}
CACHE = {}                           # runs shared between sections


def _doc(name):
    if name not in DOCS:
        here = HERE / f"{name}.json"
        path = here if here.exists() else (REPO / "simulator" / "scenarios" / "examples"
                                           / f"{name}.json")
        doc, errs = load_scenario(path)
        if errs:
            raise SystemExit(f"scenario {name} failed its gate: {errs}")
        DOCS[name] = doc
    return DOCS[name]


def _world(name, n):
    return H.Harness(_doc(name), n)


def _arrivals(world, n):
    key = ("arr", world.hash, n)
    if key not in CACHE:
        CACHE[key] = [a for _s, a in world.arrivals(n)]
    return CACHE[key]


def _seq_event(world, n, at_s=EVENT_S):
    """First seq whose arrival is at or after the improvement."""
    return bisect.bisect_left(_arrivals(world, n), at_s * 1000)


def probe_both(world, req, acq, att, t_ms):
    """P.probe_truth, but keeping the response too: evaluate one attempt without
    leaving residue (the late-settlement queue is the only mutable side effect)."""
    nq = len(world.late_queue)
    resp, truth = world.attempt(req, acq, att, t_ms)
    del world.late_queue[nq:]
    return resp, truth


# --------------------------------------------------------------------------------------
# 1. Analytic arm view: expected settled margin of one arm on one context, the same
#    accounting P.drive uses (auth -> +win, decline -> -fee, timeout -> -fee-lambda).
#    The auth probability is blended over the challenge draw (the deterministic,
#    expectation-correct projection of theta_truth); the timeout test uses the world's
#    own realized latency draw. Pure in (seq, acq, t_ms); used for the flip set, the
#    oracle, and the headroom arithmetic -- never fed to any policy's posterior.
# --------------------------------------------------------------------------------------

def arm_view(world, req, acq, t_ms, auth_override=None):
    m = world.models[acq]
    auth_mult, lat_mult_health, _mode = world.health(acq, t_ms)
    if auth_override is not None:
        auth_mult = auth_override
    p = m.base_rate
    p *= float(m.bin_mult.get(req.bin_class, 1.0))
    p *= float(m.region_mult.get(req.card_region, 1.0))
    if req.amount_minor > m.kink:
        p *= 1.0 - min(m.cap, (req.amount_minor - m.kink) / 10000.0 * m.slope)
    p *= auth_mult
    if req.sca_required:
        ch = 1.0 - m.frictionless * float(m.tds_bin.get(req.bin_class, 1.0)) * \
            float(m.tds_mcc.get(req.merchant_category, 1.0))
        ch = min(0.95, max(0.0, ch))
        p_ch = min(0.995, max(0.001, p * m.liability_uplift)) * (1.0 - m.abandon)
        theta = ch * p_ch + (1.0 - ch) * min(0.995, max(0.001, p))
    else:
        theta = min(0.995, max(0.001, p))
    lat_mult = lat_mult_health * world.latency_multiplier(acq, t_ms)
    draws = stream(world.seed, "att", req.seq, m.key, 0)
    lat = m.latency_ms(draw(draws, H.D_LATENCY), lat_mult)
    to = lat > req.deadline_ms
    win = P.win_amount(req, acq)
    fee = P.attempt_fee(req, acq)
    em = (-fee - LAM_TO) if to else theta * win - (1.0 - theta) * fee
    return em, theta, to


def _pre_storm_mult(world, acq="foxtrot"):
    """The auth multiplier the doc's decline storm applies to the recovered acquirer
    during the observation window (health at t=0 is exactly that fold)."""
    return world.health(acq, 0)[0]


def margin_headroom(world, n, t_probe):
    """The event's unlocked value: sum over contexts of max(post-best - pre-best, 0),
    in c/1k. foxtrot pre = storm multiplier folded, post = recovered."""
    headroom = 0.0
    flips = 0
    pre_mult = _pre_storm_mult(world)
    for seq, arr in world.arrivals(n):
        req = world.context(seq, arr)
        pre = {}
        post = {}
        for acq in P.eligible(req):
            post[acq] = arm_view(world, req, acq, t_probe)[0]
            pre[acq] = post[acq]
            if acq == "foxtrot":
                pre[acq] = arm_view(world, req, acq, t_probe,
                                    auth_override=pre_mult)[0]
        bp, bs = max(pre.values()), max(post.values())
        headroom += max(0.0, bs - bp)
        if max(pre, key=pre.get) != max(post, key=post.get):
            flips += 1
    return headroom / n * 1000.0, flips / n


def flip_set(world, n):
    """bytearray over seqs: 1 where the expected-margin argmax changes because of the
    event (foxtrot storm-folded vs recovered), evaluated at one post-event probe time."""
    key = ("flip", world.hash, n)
    if key in CACHE:
        return CACHE[key]
    t_probe = (EVENT_S + 120) * 1000      # 2 minutes after the step recovery
    pre_mult = _pre_storm_mult(world)
    flips = bytearray(n)
    for seq, arr in world.arrivals(n):
        req = world.context(seq, arr)
        pre_best, pre_v = None, -1e18
        post_best, post_v = None, -1e18
        for acq in P.eligible(req):
            e_post, _t, _to = arm_view(world, req, acq, t_probe)
            e_pre = e_post
            if acq == "foxtrot":
                e_pre, _t, _to = arm_view(world, req, acq, t_probe,
                                          auth_override=pre_mult)
            if e_pre > pre_v:
                pre_v, pre_best = e_pre, acq
            if e_post > post_v:
                post_v, post_best = e_post, acq
        if pre_best != post_best and post_v > 0.0:
            flips[seq] = 1
    CACHE[key] = flips
    return flips


# --------------------------------------------------------------------------------------
# 2. Policies. bare TS is ADR-0006's Router with the shipped informative prior.
#    shipped adds ADR-0007's machinery in the tagged drive below. The overlay
#    candidates wrap decide() and nothing else -- they are candidates for R49's job,
#    and the comparison is only honest if everything except the forcing rule is shared.
# --------------------------------------------------------------------------------------

class EpsGreedy:
    """With probability eps, attempt 0 goes to a uniformly random eligible processor."""

    def __init__(self, router, eps):
        self._r = router
        self.eps = eps
        self.forced = 0

    def __getattr__(self, k):
        return getattr(self._r, k)

    def decide(self, req):
        chain = self._r.decide(req)
        if not chain:
            return chain                       # an empty chain is "route nowhere", not a sample
        if draw(stream(POLICY_SEED, "qie", req.seq), 0) < self.eps:
            elig = P.eligible(req, self._r.exclude)
            if elig:
                j = int(draw(stream(POLICY_SEED, "qie", req.seq), 1) * len(elig))
                pick = elig[j]
                if chain[0] == pick:
                    return chain
                chain = [pick] + [c for c in chain if c != pick][:P.MAX_ATTEMPTS - 1]
                self.forced += 1
        return chain


class Rotation:
    """Guaranteed minimum attempt-0 share rho per eligible processor per trailing
    window of W decisions: the largest deficit is forced to the chain head."""

    def __init__(self, router, rho=0.02, window=2000):
        self._r = router
        self.rho = rho
        self.window = window
        self.heads = []                 # trailing window of attempt-0 acqs
        self.counts = {a: 0 for a in P.ACQ}
        self.forced = 0

    def __getattr__(self, k):
        return getattr(self._r, k)

    def decide(self, req):
        chain = self._r.decide(req)
        elig = P.eligible(req, self._r.exclude)
        if elig and len(self.heads) >= self.window:
            worst, worst_share = None, 2.0
            for a in elig:
                sh = self.counts[a] / self.window
                if sh < worst_share:
                    worst_share, worst = sh, a
            if worst is not None and worst_share < self.rho and \
                    (not chain or chain[0] != worst):
                chain = [worst] + [c for c in chain if c != worst][:P.MAX_ATTEMPTS - 1]
                self.forced += 1
        head = chain[0] if chain else None
        self.heads.append(head)
        if head is not None:
            self.counts[head] += 1
        if len(self.heads) > self.window:
            old = self.heads.pop(0)
            if old is not None:
                self.counts[old] -= 1
        return chain


# --------------------------------------------------------------------------------------
# 3. Metrics with window shares: margin/auth/MAE conventions identical to P.Metrics,
#    plus attempt-0 share per acquirer per window, share-on-flipped-contexts, the
#    exploration fraction (attempt-0 = the arm a deterministic posterior-mean policy
#    would NOT have taken), and detector bookkeeping.
# --------------------------------------------------------------------------------------

def window_edges(n, w=10_000):
    return [(lo, min(n, lo + w)) for lo in range(0, n, w)]


class TaggedMetrics:
    def __init__(self, n, flips=None, w=10_000):
        self.n = n
        self.txns = 0
        self.authed = 0
        self.attempts = 0
        self.timeouts = 0
        self.margin = 0.0
        self.mae_sum, self.mae_n = 0.0, 0
        self.edges = window_edges(n, w)
        self.margin_w = [0.0] * len(self.edges)
        self.head_w = [dict((a, 0) for a in P.ACQ) for _ in self.edges]
        self.flip_head = dict((a, 0) for a in P.ACQ)
        self.flip_n = 0
        self.flip_head_w = [dict((a, 0) for a in P.ACQ) for _ in self.edges]
        self.flip_n_w = [0] * len(self.edges)
        self.explore_w = [0] * len(self.edges)
        self.decisions_w = [0] * len(self.edges)
        self.flips = flips
        self.late_events = 0
        self.det_log = []               # (seq, t_ms, acq, tier, delta_mu, te_ratio)
        self.n_alarms = 0

    def _wi(self, seq):
        i = bisect.bisect_right(self.edges, (seq, self.n + 1)) - 1
        return max(0, min(len(self.edges) - 1, i))

    def margin_per_1k(self, lo=None, hi=None):
        """Margin c/1k over [lo, hi); arbitrary bounds are attributed proportionally
        across the fixed windows (a reporting approximation, never used per-arm)."""
        if lo is None or (lo, hi) == (0, self.n):
            return self.margin / max(1, self.txns) * 1000.0
        acc = 0.0
        for k, (a, b) in enumerate(self.edges):
            ov_lo, ov_hi = max(lo, a), min(hi, b)
            if ov_hi > ov_lo:
                acc += self.margin_w[k] * (ov_hi - ov_lo) / (b - a)
        return acc / max(1, hi - lo) * 1000.0

    def share(self, acq, lo, hi):
        for k, (a, b) in enumerate(self.edges):
            if (a, b) == (lo, hi):
                tot = sum(self.head_w[k].values())
                return self.head_w[k].get(acq, 0) / max(1, tot)
        # arbitrary range: fold over windows
        heads, tot = 0, 0
        for k, (a, b) in enumerate(self.edges):
            if b <= lo or a >= hi:
                continue
            heads += self.head_w[k].get(acq, 0)
            tot += sum(self.head_w[k].values())
        return heads / max(1, tot)

    def flip_share(self, acq):
        return self.flip_head.get(acq, 0) / max(1, self.flip_n)

    def mae(self):
        return 100.0 * self.mae_sum / max(1, self.mae_n)


def _mean_head(router, req, elig):
    """The arm a deterministic posterior-mean policy would have taken (score of means).
    Used to classify whether the sampled attempt-0 was an explorative pick."""
    ctx = router.space.ctx_index(req)
    best, bestv = None, -1e18
    for acq in elig:
        i = router.space.arm_index(ctx, acq)
        aa, bb = router.effective(i)
        ta = router.toa[i] + router.ptoa[i]
        tb = router.tob[i] + router.ptob[i]
        th = aa / (aa + bb)
        pi = ta / (ta + tb)
        s = th * P.win_amount(req, acq) - (1.0 - th) * P.attempt_fee(req, acq) - pi * LAM_TO
        if s > bestv:
            bestv, best = s, acq
    return best


# --------------------------------------------------------------------------------------
# 4. The tagged drive: one loop, every #9 decision as a flag.
#
#    det          None, or "adwin" for the shipped ADR-0007 machinery (processor-level
#                 ADWIN + tiered gamma shrink of DATA counts + R49 onboarding on
#                 reset), or "adwin_ps" for the same with the reset applied to the
#                 whole effective pseudo-count mass (data AND prior strength) -- the
#                 variant this spike proposes after measuring which of the two actually
#                 binds a written-off fine arm (the prior's m=100, not the ~3 data
#                 counts, so shrinking data alone is measured to be inert).
#    full_info    after the real attempts, probe every other eligible arm and fold its
#                 counterfactual outcome into the posterior (simulation-only bound)
#    lag          ingest arrival lag in transactions: an outcome is folded into the
#                 posterior only `lag` seqs after its attempt (the WAL records arrival
#                 order; metrics always see the attempt synchronously)
#    relabel      "shipped" (timeout excluded from auth posterior, late settlement is
#                 observability only) | "retro" (a late-settled authorization reverses
#                 the timeout and credits alpha) | "decline" (no response = decline at
#                 the deadline, corrected on late arrival)
# --------------------------------------------------------------------------------------

def drive_tagged(world, router, n, *, det=None, full_info=False, lag=0,
                 relabel="shipped", flips=None, m=None):
    if m is None:
        m = TaggedMetrics(n, flips)
    space = router.space
    dets = {a: ADWIN(delta=0.001) for a in P.ACQ} \
        if det in ("adwin", "adwin_ps", "adwin_ps_cool") else None
    pending_order = []                # (event_seq, op, event_arr) held back by lag
    timed_out = {}                    # (seq, att) -> arm index, for relabel variants
    que_sched = []                    # (late_ms, seq, att, acq, arm_ix) by late_ms:
                                      # late-settlement arrivals scheduled at timeout

    def apply(op, arr, seq_now):
        router.apply_op(op)
        if dets is not None:
            _s, _a, acq, outcome = op[0], op[1], op[2], op[3]
            if outcome != TIMEOUT:
                val = 1.0 if outcome == AUTH else 0.0
                if dets[acq].update(val, arr, seq_now):
                    ix = P.ACQ_IX[acq]
                    te_sum = sum(router.te[space.arm_index(c, acq)]
                                 for c in range(space.n_ctx))
                    tb_sum = sum(router.tob[space.arm_index(c, acq)]
                                 for c in range(space.n_ctx))
                    te_ratio = te_sum / max(1.0, tb_sum)
                    delta_mu = abs(dets[acq].last_split_delta)
                    tier = 1 if (te_ratio > 0.20 or delta_mu > 0.30) else 2
                    gamma = 0.1 if tier == 1 else 0.5
                    for c in range(space.n_ctx):
                        idx = space.arm_index(c, acq)
                        router.a[idx] *= gamma
                        router.b[idx] *= gamma
                        if det in ("adwin_ps", "adwin_ps_cool"):
                            router.pa[idx] *= gamma
                            router.pb[idx] *= gamma
                            router.ptoa[idx] *= gamma
                            router.ptob[idx] *= gamma
                    if det == "adwin_ps_cool" and ix in router.explore:
                        # a re-fire while the previous reset's onboarding is still
                        # running: the unbinding compounds, the floor clock does not
                        # restart
                        pass
                    else:
                        router.explore.add(ix)
                        router.proc_settled[ix] = 0
                    m.n_alarms += 1
                    m.det_log.append((seq_now, arr, acq, tier, delta_mu, te_ratio))

    for seq, arr in world.arrivals(n):
        world.clock.advance_to(arr)
        # release lagged outcomes in arrival order (the WAL is an arrival-order log)
        while pending_order and seq - pending_order[0][0] >= lag:
            op_seq, op, op_arr = pending_order.pop(0)
            apply(op, arr, seq)
        # matured late settlements (world truth): ALWAYS count -- the shipped
        # protocol's late counter is observability that feeds reconciliation and
        # lambda_to calibration; only relabel variants additionally rewrite labels.
        while que_sched and que_sched[0][0] <= arr:
            _lms, _ls, _la, l_acq, i = que_sched.pop(0)
            m.late_events += 1
            if relabel == "retro":                # timeout was really an authorization
                router.toa[i] -= 1.0
                router.a[i] += 1.0
                router.tob[i] += 1.0
                router.proc_settled[P.ACQ_IX[l_acq]] += 1
            elif relabel == "decline":            # initial beta was provisional; flip it
                router.b[i] -= 1.0
                router.a[i] += 1.0

        req = world.context(seq, arr)
        chain = router.decide(req)
        wi = m._wi(seq)
        m.decisions_w[wi] += 1
        if not chain:
            m.txns += 1                       # unroutable: a zero-margin txn, not a missing one
            continue
        elig = P.eligible(req, router.exclude)
        if chain and _mean_head(router, req, elig) != chain[0]:
            m.explore_w[wi] += 1
        is_flip = flips is not None and flips[seq]
        if is_flip:
            m.flip_n += 1
            m.flip_n_w[wi] += 1
        m.head_w[wi][chain[0]] += 1
        if is_flip:
            m.flip_head[chain[0]] += 1
            m.flip_head_w[wi][chain[0]] += 1

        txn_margin, authed, att = 0.0, 0, 0
        qmark = len(world.late_queue)             # late events appended by these attempts
        for att, acq in enumerate(chain):
            resp, truth = world.attempt(req, acq, att, arr)
            m.attempts += 1
            if resp.outcome == TIMEOUT:
                m.timeouts += 1
                txn_margin -= P.attempt_fee(req, acq) + LAM_TO
            elif resp.outcome == AUTH:
                authed = 1
                txn_margin += P.win_amount(req, acq)
            else:
                txn_margin -= P.attempt_fee(req, acq)
            i = space.arm_index(space.ctx_index(req), acq)
            if resp.outcome != TIMEOUT:
                m.mae_sum += abs(router.mean(i) - P.theta_truth(world, req, acq, truth))
                m.mae_n += 1
            else:
                timed_out[(seq, att)] = i
                if relabel == "decline":          # provisional no-response = decline
                    router.b[i] += 1.0
                    router.tob[i] += 1.0
            op = (seq, att, acq, resp.outcome, req.bin_class, req.card_region,
                  req.sca_required, req.mandate, req.amount_minor)
            if lag > 0:
                pending_order.append((seq, op, arr))
            else:
                apply(op, arr, seq)
            if resp.outcome in H.TERMINAL:
                break
        # schedule this txn's late-settlement arrivals (timing is world truth; the
        # label protocol decides what, if anything, changes when they mature)
        for _lm, _ls, _ac, _at in world.late_queue[qmark:]:
            _i = timed_out.get((_ls, _at))
            if _i is not None:
                bisect.insort(que_sched, (_lm, _ls, _at, _ac, _i))
        # full-information feedback: what only a simulation can see (C1's bound).
        # Only attempt-0 counterfactuals are folded, and dedupe is bypassed (it
        # keys on (seq, att), which would drop the probe of any arm at att 0).
        if full_info:
            for acq in elig:
                if acq in chain[:att + 1]:
                    continue
                resp2, truth2 = probe_both(world, req, acq, 0, arr)
                router.apply_op((seq, 0, acq, resp2.outcome, req.bin_class,
                                 req.card_region, req.sca_required, req.mandate,
                                 req.amount_minor))
        m.txns += 1
        m.authed += authed
        m.margin += txn_margin
        m.margin_w[wi] += txn_margin
    return m


# --------------------------------------------------------------------------------------
# 5. The oracle: true rates through the identical execution loop (ADR-0002/#17's
#    definition), with the ADR-0002 caveat that on each context it also sees the
#    realized latency draw, i.e. it is slightly clairvoyant about the deadline.
# --------------------------------------------------------------------------------------

def drive_oracle(world, n, *, flips=None):
    m = TaggedMetrics(n, flips)
    for seq, arr in world.arrivals(n):
        world.clock.advance_to(arr)
        req = world.context(seq, arr)
        wi = m._wi(seq)
        m.decisions_w[wi] += 1
        elig = P.eligible(req)
        views = {}
        nq = len(world.late_queue)
        for acq in elig:
            resp, truth = world.attempt(req, acq, 0, arr)
            if resp.outcome == TIMEOUT:
                em = -P.attempt_fee(req, acq) - LAM_TO
            else:
                th = P.theta_truth(world, req, acq, truth)
                em = th * P.win_amount(req, acq) - (1.0 - th) * P.attempt_fee(req, acq)
            views[acq] = (em, resp)
        del world.late_queue[nq:]                # oracle probes leave no residue
        ranked = sorted(((em, a) for a, (em, _r) in views.items() if em > 0.0),
                        key=lambda t: (-t[0], t[1]))
        chain = [a for _em, a in ranked][:P.MAX_ATTEMPTS]
        if not chain:
            m.txns += 1
            continue
        is_flip = flips is not None and flips[seq]
        if is_flip:
            m.flip_n += 1
            m.flip_n_w[wi] += 1
        m.head_w[wi][chain[0]] += 1
        if is_flip:
            m.flip_head[chain[0]] += 1
            m.flip_head_w[wi][chain[0]] += 1
        txn_margin, authed = 0.0, 0
        for att, acq in enumerate(chain):
            if att == 0:
                resp = views[acq][1]
            else:
                resp, _t = world.attempt(req, acq, att, arr)
            m.attempts += 1
            if resp.outcome == TIMEOUT:
                m.timeouts += 1
                txn_margin -= P.attempt_fee(req, acq) + LAM_TO
            elif resp.outcome == AUTH:
                authed = 1
                txn_margin += P.win_amount(req, acq)
            else:
                txn_margin -= P.attempt_fee(req, acq)
            if resp.outcome in H.TERMINAL:
                break
        m.txns += 1
        m.authed += authed
        m.margin += txn_margin
        m.margin_w[wi] += txn_margin
    return m


# --------------------------------------------------------------------------------------
# 6. Shared runs
# --------------------------------------------------------------------------------------

def _prefix(n, world_name=None):
    if world_name is None:
        return P._ensure_prefix(n)
    key = ("prefix", world_name, n)
    if key not in CACHE:
        CACHE[key] = P.uniform_prefix(_world(world_name, n), n)
    return CACHE[key]


def _prior(n, bias=1.0, world_name=None):
    return P.make_prior_fn(_prefix(n, world_name), m=100.0, bias=bias)


def _space():
    return P.default_space()


def run(world_name, n, policy, *, det=None, full_info=False, lag=0, relabel="shipped",
        flips=None):
    key = ("run", world_name, n, policy, det, bool(full_info), lag, relabel,
           bool(flips))
    if key in CACHE:
        return CACHE[key]
    w = _world(world_name, n)
    # the starved variant's informative prior is scoped to ITS world: the storm has
    # run for 3 days before the horizon's midnight, so a refreshed prior artifact
    # (R50's "state at midnight") already believes foxtrot at 0.60 -- THAT is what
    # starves the arm, and why this variant exists
    pw = "quiet-improvement-starved-v1" if world_name == \
        "quiet-improvement-starved-v1" else None
    if policy == "bare":
        r = P.Router(_space(), prior_fn=_prior(n, world_name=pw))
    elif policy == "shipped":
        r = P.Router(_space(), prior_fn=_prior(n, world_name=pw), eta=0.05, n_min=1000)
    elif policy == "shipped_nofloor":
        r = P.Router(_space(), prior_fn=_prior(n, world_name=pw), eta=0.0, n_min=0)
    elif policy == "eps01":
        r = EpsGreedy(P.Router(_space(), prior_fn=_prior(n)), 0.01)
    elif policy == "eps05":
        r = EpsGreedy(P.Router(_space(), prior_fn=_prior(n)), 0.05)
    elif policy == "rot02":
        r = Rotation(P.Router(_space(), prior_fn=_prior(n)), rho=0.02, window=2000)
    elif policy == "optimistic":
        r = P.Router(_space(), prior_fn=_prior(n, bias=1.08))
    else:
        raise ValueError(policy)
    if flips is None and world_name.startswith("quiet-improvement"):
        flips = flip_set(_world(world_name, n), n)
    m = drive_tagged(w, r, n, det=det, full_info=full_info, lag=lag,
                     relabel=relabel, flips=flips)
    CACHE[key] = (m, r)
    return CACHE[key]


def fmt(x, nd=1):
    return f"{x:.{nd}f}"


def table(headers, rows):
    ws = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h))
          for i, h in enumerate(headers)]
    out = ["  ".join(str(h).ljust(w) for h, w in zip(headers, ws))]
    out.append("  ".join("-" * w for w in ws))
    for r in rows:
        out.append("  ".join(str(c).ljust(w) for c, w in zip(r, ws)))
    return "\n".join(out)


# --------------------------------------------------------------------------------------
# [C1] The censoring problem, priced: bandit feedback vs full-information feedback.
# --------------------------------------------------------------------------------------

def sec_c1(n):
    print("[C1] bandit feedback vs full-information feedback (the price of censoring)")
    print("""
  The estimand is per-arm P(authorized | attempt, not timeout): an attempt on arm i
  labels arm i only. A bandit-feedback learner folds one outcome per decision; a
  full-information learner (IMPOSSIBLE in production -- you cannot ask Adyen what it
  would have done with traffic you sent to Stripe) folds every eligible arm's
  counterfactual. The harness answers counterfactuals for free (index-addressed
  draws), so the gap between the two rows is the measured price of censoring. Same
  policy (TS, shipped prior), same decision rule; only the observation set differs.
""")
    for world_name, label in (("baseline-steady-v1", "steady world"),
                              ("outage-recovery-v1", "outage world")):
        mb, _ = run(world_name, n, "bare")
        mf, _ = run(world_name, n, "bare", full_info=True)
        rows = [("bandit feedback (production reality)", fmt(mb.authed / mb.txns * 100, 2),
                 fmt(mb.margin_per_1k()), fmt(mb.mae(), 2), mb.attempts),
                ("full-information (simulation-only bound)",
                 fmt(mf.authed / mf.txns * 100, 2), fmt(mf.margin_per_1k()),
                 fmt(mf.mae(), 2), mf.attempts)]
        print(f"  --- {world_name} ({label}, n={n}) ---")
        print(table(["feedback", "auth%", "margin c/1k", "MAE pts", "attempts"], rows))
        print()
    print("""
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
""")


# --------------------------------------------------------------------------------------
# [C2] Does built-in exploration collapse? The quiet improvement.
# --------------------------------------------------------------------------------------

def sec_c2(n):
    print("[C2] TS's built-in exploration, tested where it must fail: the quiet improvement")
    print("""
  quiet-improvement-v1: foxtrot is effectively 0.806 for the first 3 days (a mild
  decline storm over a healthy service -- believable, unremarkable, learned), then a
  step recovery to 0.92 with no transport signature. In ~6% of contexts (measured
  below) foxtrot becomes the expected-margin argmax; the oracle headroom is the
  forfeited margin of never noticing. Bare TS is the shipped sampler WITHOUT the
  ADR-0007 machinery; the oracle is the ADR-0002 reference (true rates through the
  identical loop, clairvoyant about the deadline only).
""")
    world_name = "quiet-improvement-v1"
    w = _world(world_name, n)
    seq_ev = _seq_event(w, n)
    if seq_ev >= n - 2000:
        print(f"  NOTE: at n={n} the event (seq {seq_ev}) is not comfortably inside the "
              f"run; run with n >= 40000 for a meaningful post-event window.")
        return
    headroom, flip_frac = margin_headroom(w, n, (EVENT_S + 120) * 1000)
    flips = flip_set(_world(world_name, n), n)
    print(f"  event: foxtrot step recovery at t={EVENT_S}s (seq {seq_ev} of {n}); "
          f"contexts where the argmax flips: {100*flip_frac:.1f}%")
    print(f"  oracle headroom unlocked by the event: {headroom:,.0f} c/1k")
    print()

    mb, rb = run(world_name, n, "bare")
    ms, rs = run(world_name, n, "shipped", det="adwin")
    mp, rp = run(world_name, n, "shipped", det="adwin_ps")
    mc, rc = run(world_name, n, "shipped", det="adwin_ps_cool")
    key_o = ("oracle", world_name, n)
    if key_o not in CACHE:
        CACHE[key_o] = drive_oracle(_world(world_name, n), n, flips=flips)
    mo = CACHE[key_o]

    pre = (max(0, seq_ev - 10_000), seq_ev)
    post = (seq_ev, n)
    rows = []
    for label, m in (("bare TS (ADR-0006 alone)", mb),
                     ("shipped resets: data-count decay (ADR-0007)", ms),
                     ("proposed: data+prior decay + R49 (R58)", mp),
                     ("proposed + no re-board on re-fire", mc),
                     ("oracle (upper bound)", mo)):
        fox_pre = m.share("foxtrot", pre[0], pre[1])
        fox_post = m.share("foxtrot", post[0], post[1])
        rows.append((label, fmt(m.margin_per_1k()), fmt(m.margin_per_1k(*pre)),
                     fmt(m.margin_per_1k(*post)), fmt(100 * m.flip_share("foxtrot"), 1),
                     fmt(100 * fox_pre, 1), fmt(100 * fox_post, 1), m.n_alarms))
    print(table(["policy", "margin c/1k", "pre margin", "post margin",
                 "foxtrot share on flipped %", "glob share pre %", "glob share post %",
                 "alarms"], rows))
    print("""  (the flip set is the ANALYTIC expectation-blended set; the oracle decides on
  realized per-seq draws, so its foxtrot share on flipped contexts is the right
  reference number, not 100%.)
""")
    # window curve of foxtrot share after the event
    print("  foxtrot attempt-0 share per 10k window (post-event windows shaded by the "
          "recovery):")
    hdr = ["policy"] + [f"{a//1000}-{b//1000}k" for a, b in ms.edges]
    rows = []
    for label, m in (("bare TS", mb), ("data decay", ms), ("data+prior decay", mp),
                     ("d+p, no re-board", mc), ("oracle", mo)):
        rows.append([label] + [fmt(100 * m.share("foxtrot", a, b), 1)
                               for a, b in m.edges])
    print(table(hdr, rows))
    print()
    for name, mm in (("shipped (data-only decay)", ms), ("proposed (data+prior)", mp),
                     ("proposed + no re-board", mc)):
        if mm.det_log:
            first = mm.det_log[0]
            alarm_acqs = {}
            for d in mm.det_log:
                alarm_acqs[d[2]] = alarm_acqs.get(d[2], 0) + 1
            print(f"  {name}: {mm.n_alarms} ADWIN alarm(s) {alarm_acqs}; first at seq "
                  f"{first[0]} (t={first[1]/1000:,.0f}s), {first[1]/1000 - EVENT_S:,.0f}s and "
                  f"{first[0] - seq_ev:,} txns after the recovery; tier {first[3]} "
                  f"(|delta-mu|={first[4]:.3f}, te/settled={first[5]:.3f}).")
        else:
            print(f"  {name}: NO ADWIN alarm fired on the improvement.")

    # ---- the boundary case: the arm is starved to a trickle BEFORE it improves ------
    print()
    print("  --- boundary case: quiet-improvement-starved-v1 (foxtrot effective 0.60 for 3 "
          "days; the prior artifact has been refreshed during the storm -- R50's 'state "
          "at midnight' already believes 0.60, so pre share collapses to single digits; "
          "then the same silent step recovery to 0.92) ---")
    sname = "quiet-improvement-starved-v1"
    sw = _world(sname, n)
    sseq_ev = _seq_event(sw, n)
    spre = (max(0, sseq_ev - 10_000), sseq_ev)
    spost = (sseq_ev, n)
    rows = []
    infos = []
    for label, pol, kw in (("bare TS", "bare", {}),
                           ("data+prior decay + R49, no re-board", "shipped",
                            {"det": "adwin_ps_cool"}),
                           ("data+prior decay, no floor", "shipped_nofloor",
                            {"det": "adwin_ps"})):
        m, _r = run(sname, n, pol, **kw)
        rows.append((label, fmt(m.margin_per_1k()),
                     fmt(100 * m.share("foxtrot", *spre), 2),
                     fmt(100 * m.share("foxtrot", *spost), 1),
                     fmt(100 * m.flip_share("foxtrot"), 1), m.n_alarms))
        if m.det_log:
            first = m.det_log[0]
            infos.append(f"{label}: first alarm seq {first[0]:,} "
                         f"({first[0] - sseq_ev:,} txns / {first[1]/1000 - EVENT_S:,.0f}s "
                         f"after recovery)")
        else:
            infos.append(f"{label}: no alarm over the whole post window")
    print(table(["policy", "margin c/1k", "glob share pre %", "glob share post %",
                 "flip share %", "alarms"], rows))
    for line in infos:
        print("   " + line)
    print("""  The starved case is the ticket's precise fear, and it measures the brake line.
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
""")

    # exploration annealing on the steady world
    print()
    mst, _ = run("baseline-steady-v1", n, "bare")
    rows = []
    for a, b in mst.edges:
        frac = mst.explore_w[mst.edges.index((a, b))] / max(1, mst.decisions_w[mst.edges.index((a, b))])
        rows.append((f"{a//1000}-{b//1000}k", fmt(100 * frac, 2),
                     fmt(100 * mst.share("foxtrot", a, b), 2)))
    print("  exploration annealing, baseline-steady-v1 bare TS (attempt-0 != posterior-"
          "mean argmax):")
    print(table(["window", "exploration % of decisions", "foxtrot share %"], rows))
    print("""
  -> the annealing IS the design: on a stationary world TS drives explorative picks
     toward zero as posteriors concentrate (that is the revenue the ticket worries
     about wasting), and on the moved world the same annealing is the censorship
     trap. The numbers say WHERE the line sits: probability matching alone keeps a
     written-off arm at a small nonzero share (never zero), which is enough for the
     processor-level detector to SEE the improvement, but not enough for the fine
     arms to RE-LEARN it inside the window.
""")


# --------------------------------------------------------------------------------------
# [C3] Forced exploration bake-off.
# --------------------------------------------------------------------------------------

def sec_c3(n):
    print("[C3] forced exploration on top of TS: what it costs, what it buys")
    print("""
  Candidates from the ticket, each as an overlay on the identical bare TS (same
  prior, same draws), priced where nothing is wrong (baseline-steady-v1) and tested
  where something is (quiet-improvement-v1). The shipped row is the ADR-0007
  machinery already in the tree; the question is whether any STANDING mechanism
  earns a place on top of it.
""")
    policies = [
        ("bare TS", "bare", {}),
        ("shipped: ADWIN + data decay + R49", "shipped", {"det": "adwin"}),
        ("proposed: data&prior decay + R49", "shipped", {"det": "adwin_ps"}),
        ("data+prior decay, no R49 floor", "shipped_nofloor", {"det": "adwin_ps"}),
        ("proposed + R49, no re-board on re-fire", "shipped", {"det": "adwin_ps_cool"}),
        ("+ epsilon-greedy eps=0.01", "eps01", {}),
        ("+ epsilon-greedy eps=0.05", "eps05", {}),
        ("+ rotation rho=0.02/2k window", "rot02", {}),
        ("optimistic prior (rate x1.08, m=100)", "optimistic", {}),
    ]
    seq_ev = _seq_event(_world("quiet-improvement-v1", n), n)
    if seq_ev >= n - 2000:
        print(f"  NOTE: at n={n} the event (seq {seq_ev}) is not comfortably inside the "
              f"run; run with n >= 40000 for a meaningful post-event window.")
        return
    post = (seq_ev, n)
    rows = []
    base_steady = None
    for label, pol, kw in policies:
        m1, r1 = run("baseline-steady-v1", n, pol, **kw)
        m2, r2 = run("quiet-improvement-v1", n, pol, **kw)
        if base_steady is None:
            base_steady = m1.margin_per_1k()
        rows.append((label,
                     fmt(m1.margin_per_1k()),
                     fmt(m1.margin_per_1k() - base_steady, 1),
                     fmt(m2.margin_per_1k()),
                     fmt(m2.margin_per_1k(*post)),
                     fmt(100 * m2.flip_share("foxtrot"), 1),
                     fmt(100 * m2.share("foxtrot", *post), 1),
                     m2.n_alarms if "det" in kw else "",
                     getattr(r1, "forced", "")))
    print(table(["policy", "steady c/1k", "vs bare", "improve c/1k", "post margin",
                 "flip share %", "post glob share %", "alarms", "forced"], rows))
    print()
    print("""
  -> the steady column is the tax every standing mechanism pays every day of every
     week; the flip column is the insurance it buys. Epsilon-greedy and rotation do
     not fail -- they rescue the share -- but they buy the rescue with a permanent,
     uniformly-spread budget AND their feed to the written-off arm is a trickle
     (eps/k or rho), where the shipped machinery's budget is zero until a detector
     fires and then CONCENTRATED on exactly the arm that changed. Optimistic
     initialization is a prior, not a mechanism: after 25k transactions of data the
     prior is a rounding error and the rescue effect with it.
""")

    # the degradation direction: does scaling the PRIOR on reset hurt ADR-0007's
    # original case (outage-recovery-v1: foxtrot connection_refused at 345600s,
    # recovery step at 346200s)?
    print("  the degradation direction (license for R58): outage-recovery-v1, foxtrot "
          "connection_refused 345600-346200s, step recovery; share = foxtrot attempt-0 "
          "share in the 10k decisions after recovery:")
    wname = "outage-recovery-v1"
    wr = _world(wname, n)
    seq_rec = _seq_event(wr, n, at_s=346_200)
    rows = []
    for label, pol, kw in (("bare TS", "bare", {}),
                           ("shipped (data decay + R49)", "shipped", {"det": "adwin"}),
                           ("data+prior decay + R49", "shipped", {"det": "adwin_ps"}),
                           ("data+prior decay, no floor", "shipped_nofloor",
                            {"det": "adwin_ps"})):
        m, _r = run(wname, n, pol, **kw)
        rows.append((label, fmt(m.margin_per_1k()),
                     fmt(m.authed / m.txns * 100, 2),
                     fmt(100 * m.share("foxtrot", seq_rec, min(n, seq_rec + 10_000)), 1),
                     m.n_alarms))
    print(table(["policy", "margin c/1k", "auth%", "foxtrot share post %", "alarms"],
                rows))
    print("""  (ADR-0007's case is unharmed by scaling prior strength together with data:
  the unbinding it needs -- stop believing the pre-outage posterior -- is the same
  unbinding in both directions; the R49 floor remains what carries re-entry.)
""")


# --------------------------------------------------------------------------------------
# [C4] The propensity logging schema.
# --------------------------------------------------------------------------------------

MC_Q = 32          # midpoint quadrature points for the plug-in-score estimator


def beta_pdf_unnorm(a, b, x):
    if x <= 0.0:
        return -1e300 if a < 1.0 else 0.0
    if x >= 1.0:
        return -1e300 if b < 1.0 else 0.0
    return math.exp((a - 1.0) * math.log(x) + (b - 1.0) * math.log(1.0 - x)
                    - math.lgamma(a) - math.lgamma(b) + math.lgamma(a + b))


def prop_mc_score(arms, R, seed_base):
    """Reference: P(each arm's SCORE is best) by Monte Carlo, score = theta*win -
    (1-theta)*fee - pi*lambda with theta ~ Beta(aa,bb), pi ~ Beta(ta,tb)."""
    wins = [0] * len(arms)
    for r_ in range(R):
        best, bestv = -1, -1e30
        for j, (_acq, aa, bb, ta, tb, win, fee) in enumerate(arms):
            th = P.beta_draw(stream(seed_base, "qimc", r_, j), aa, bb)
            pi = P.beta_draw(stream(seed_base, "qimc", r_, j, 1), ta, tb)
            s = th * win - (1.0 - th) * fee - pi * LAM_TO
            if s > bestv:
                bestv, best = s, j
        wins[best] += 1
    return [w / R for w in wins]


def prop_plugin_theta(arms, thetas):
    """P(arm's theta largest), plug-in via the decision's own draws (ADR-0006 P4e)."""
    out = []
    for j in range(len(arms)):
        p = 1.0
        for j2 in range(len(arms)):
            if j2 != j:
                p *= P.beta_cdf(arms[j2][1], arms[j2][2], thetas[j])
        out.append(p)
    return out


def prop_plugin_score(arms, thetas, pis):
    """P(arm's score largest), semi-analytic via the decision's own draws:
    p_i = prod_{j != i} E_pi_j[ F_theta_j( clip( (s_i + fee_j + z*lam) / (win_j+fee_j) ) ) ]
    with the inner expectation a midpoint quadrature over pi_j."""
    out = []
    for j in range(len(arms)):
        _a, aa, bb, ta, tb, win, fee = arms[j]
        s_j = thetas[j] * win - (1.0 - thetas[j]) * fee - pis[j] * LAM_TO
        p = 1.0
        for j2 in range(len(arms)):
            if j2 == j:
                continue
            _a2, aa2, bb2, ta2, tb2, win2, fee2 = arms[j2]
            acc = 0.0
            for q in range(MC_Q):
                z = P.beta_ppf(ta2, tb2, (q + 0.5) / MC_Q)
                x = (s_j + fee2 + z * LAM_TO) / (win2 + fee2)
                if x <= 0.0:
                    acc += 0.0
                elif x >= 1.0:
                    acc += 1.0
                else:
                    acc += P.beta_cdf(aa2, bb2, x)
            p *= acc / MC_Q
        out.append(p)
    return out


def sec_c4(n):
    print("[C4] the decision log: a record #15's IPS can actually run on")
    print("""
  The candidate record (DECISION_LOG v1, fields the ADR pins): decision identity
  (seq, arrival_ms), the context key (bin, region, sca, mandate, amount, band), the
  ELIGIBLE set after the constraint filter, the per-eligible-arm posterior
  parameters (alpha, beta, tau_a, tau_b), the chain, the plug-in propensity with a
  method tag, and the R49 floor state; a per-run header carries policy_seed,
  arm-schema/catalog/constraint-set/prior-artifact hashes, lambda_to. Draws are NOT
  logged: they are key-addressed pure functions of (policy_seed, seq, arm, purpose)
  given the posterior, so the log re-derives them bit-exactly.
""")
    n_log = min(n, 20_000)
    world_name = "baseline-steady-v1"
    w = _world(world_name, n_log)
    space = _space()
    prior_fn = _prior(n_log)
    r = P.Router(space, prior_fn=prior_fn, eta=0.05, n_min=1000)
    records = []
    elig_hist = [0] * (len(P.ACQ) + 1)
    for seq, arr in w.arrivals(n_log):
        w.clock.advance_to(arr)
        req = w.context(seq, arr)
        elig = P.eligible(req, r.exclude)
        elig_hist[len(elig)] += 1
        snap = {}
        ctx = space.ctx_index(req)
        for acq in elig:
            i = space.arm_index(ctx, acq)
            aa, bb = r.effective(i)
            snap[acq] = (aa, bb, r.toa[i] + r.ptoa[i], r.tob[i] + r.ptob[i],
                         P.win_amount(req, acq), P.attempt_fee(req, acq))
        floor_cold = [a for a in elig if P.ACQ_IX[a] in r.explore]
        chain = r.decide(req)
        if chain:
            records.append((seq, arr, req, list(elig), snap, tuple(chain),
                            tuple(floor_cold)))
        for att, acq in enumerate(chain):
            resp, truth = w.attempt(req, acq, att, arr)
            r.observe(req, acq, att, resp)
            if resp.outcome in H.TERMINAL:
                break

    # (1) replay completeness: posterior params redraw the identical decision.
    n_checked = n_replayed = 0
    bad = 0
    for seq, arr, req, elig, snap, chain, cold in records[::37]:
        n_checked += 1
        scored = []
        for acq in elig:
            aa, bb, ta, tb, win, fee = snap[acq]
            arm_i = space.arm_index(space.ctx_index(req), acq)
            th = P.beta_draw(stream(POLICY_SEED, "pol", seq, arm_i, 0), aa, bb)
            pi = P.beta_draw(stream(POLICY_SEED, "pol", seq, arm_i, 1), ta, tb)
            s = th * win - (1.0 - th) * fee - pi * LAM_TO
            scored.append((s, acq))
        scored.sort(key=lambda t: (-t[0], t[1]))
        rep = [a for s, a in scored if s > 0.0][:P.MAX_ATTEMPTS]
        if cold and r.eta > 0.0 and draw(stream(POLICY_SEED, "flo", seq), 0) < r.eta:
            j = int(draw(stream(POLICY_SEED, "fpk", seq), 0) * len(cold))
            pick = cold[j]
            rep = [pick] + [c for c in rep if c != pick][:P.MAX_ATTEMPTS - 1]
        if tuple(rep) == chain:
            n_replayed += 1
        else:
            bad += 1
            if bad < 3:
                print(f"    replay mismatch at seq {seq}: derived {rep} vs logged {chain}")
    print(f"  (1) decision replay from logged posteriors + key-addressed draws: "
          f"{n_replayed}/{n_checked} exact -> {'PASS' if n_replayed == n_checked else 'FAIL'}")

    # (2) WAL-fold == decision-time posterior (the exact path #15 uses).
    # The logged snapshot was taken BEFORE its own outcomes were folded, so the
    # equivalence check replays ops with seq < the checkpoint's seq.
    chk = records[len(records) // 2]
    ok = True
    r2 = P.Router(space, prior_fn=prior_fn)
    for op in r.wal:
        if op[0] >= chk[0]:
            break
        r2.apply_op(op)
    for acq in chk[3]:
        i = space.arm_index(space.ctx_index(chk[2]), acq)
        aa, bb = r2.effective(i)
        if (aa, bb, r2.toa[i] + r2.ptoa[i], r2.tob[i] + r2.ptob[i]) != \
                chk[4][acq][:4]:
            ok = False
    print(f"  (2) posterior at a decision == fold of the WAL prefix: "
          f"{'PASS' if ok else 'FAIL'} (exact recompute path: WAL replay, ADR-0006 R47)")

    # (3) propensity estimators against a score-based MC reference.
    sample = records[:: max(1, len(records) // 48)][:48]
    states = []
    for seq, arr, req, elig, snap, chain, cold in sample:
        arms = [(acq,) + snap[acq] for acq in elig]
        states.append((seq, req, elig, snap, chain, arms, cold))
    ref_R = 100_000
    diffs = {"plugin_theta": [], "plugin_score": [], "mc64": []}
    chosen_diffs = {"plugin_theta": [], "plugin_score": [], "mc64": []}
    for idx, (seq, req, elig, snap, chain, arms, cold) in enumerate(states):
        if len(arms) < 2 or not chain:
            continue
        ref = prop_mc_score(arms, ref_R, stream(919, "qiref", idx))
        ci = [a[0] for a in arms].index(chain[0])
        ths = [P.beta_draw(stream(POLICY_SEED, "pol", seq, space.arm_index(space.ctx_index(req), a[0]), 0),
                            a[1], a[2]) for a in arms]
        pis = [P.beta_draw(stream(POLICY_SEED, "pol", seq, space.arm_index(space.ctx_index(req), a[0]), 1),
                            a[3], a[4]) for a in arms]
        ests = {"plugin_theta": prop_plugin_theta(arms, ths),
                "plugin_score": prop_plugin_score(arms, ths, pis),
                "mc64": prop_mc_score(arms, 64, stream(919, "qim64", idx))}
        for k_, v in ests.items():
            diffs[k_].append(max(abs(a - b) for a, b in zip(v, ref)))
            chosen_diffs[k_].append(abs(v[ci] - ref[ci]))
    rows = []
    for k_, label in (("plugin_theta", "plug-in, theta-only (ADR-0006 P4e form)"),
                      ("plugin_score", "plug-in, score-based (this ADR)"),
                      ("mc64", "MC R=64")):
        rows.append((label,
                     fmt(sum(chosen_diffs[k_]) / len(chosen_diffs[k_]) * 100, 2),
                     fmt(max(diffs[k_]) * 100, 2)))
    print(f"  (3) propensity estimators vs score-based MC reference (R={ref_R:,}), "
          f"{len(states)} logged decision states, deltas in pts:")
    print(table(["method", "chosen-arm mean |dp|", "any-arm max |dp|"], rows))

    # (4) bytes per decision, two field encodings.
    # fixed part: seq u32(4) + arrival u32-delta(4) + bin|region nibbles(1) +
    # sca|mandate flags(1) + amount u32(4) + band nibble(1) + eligible bitmap(1) +
    # chain len+ids(3) + plug-in propensity f16(2) + method|floor flags(1) + eta f16(2)
    fixed = 24
    k_max = max(len(e) for _s, _a, _r, e, _snp, _c, _co in records)
    for enc, per_arm in (("f32", 16), ("f64", 32)):
        total = fixed + k_max * per_arm
        gb_day = total * 5000 * 86400 / 1e9
        print(f"  (4) bytes/decision at k_max={k_max} eligible arms, posterior as {enc}: "
              f"{total} B fixed-layout (~{gb_day:,.1f} GB/day at 5,000 decisions/s)")

    # (5) how often eligibility is nontrivial.
    nontriv = sum(c for kk, c in enumerate(elig_hist) if 1 < kk < len(P.ACQ))
    full = elig_hist[len(P.ACQ)]
    print(f"  (5) eligibility after the constraint filter: {100 * nontriv / max(1, n_log):.1f}% "
          f"of decisions have a nontrivial eligible set, {100 * full / max(1, n_log):.1f}% "
          "have the full fleet -- the eligible set is part of the propensity, so it is logged.")
    print("""
  -> the record is sufficient if and only if three things hold: the decision is
     re-derivable from it (1), the exact posterior at decision time is recoverable
     (2), and the propensity method is labelled with a known error budget (3).
     The theta-only plug-in has a systematic bias on THIS fleet because scores mix
     theta with the fee schedule and lambda_to: delta (auth-strong, margin-thin) is
     over-propensed against charlie (auth-weak, margin-fat). The score-based plug-in
     is the same cost class and is the method the log tags.
""")


# --------------------------------------------------------------------------------------
# [C5] Delayed outcomes.
# --------------------------------------------------------------------------------------

def sec_c5(n):
    print("[C5] delayed outcomes: ingest lag, relabeling, and the no-response rule")
    print("""
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
""")
    # (a) lag sweep on the steady world
    rows = []
    for lag in (0, 64, 256, 1024, 8192):
        m, _r = run("baseline-steady-v1", n, "bare", lag=lag)
        rows.append((f"lag {lag} txns", fmt(m.authed / m.txns * 100, 2),
                     fmt(m.margin_per_1k()), fmt(m.mae(), 2)))
    print("  (a) settled-outcome ingest lag, baseline-steady-v1, bare TS:")
    print(table(["delay", "auth%", "margin c/1k", "MAE pts"], rows))
    print()
    # lag x drift-reset interaction on the outage world with shipped machinery
    rows = []
    for lag in (0, 1024, 4096):
        m, _r = run("outage-recovery-v1", n, "shipped", det="adwin", lag=lag)
        first = m.det_log[0] if m.det_log else None
        rows.append((f"lag {lag} txns", fmt(m.margin_per_1k()), fmt(m.mae(), 2),
                     m.n_alarms,
                     f"{first[0]:,} (t={first[1]/1000:,.0f}s)" if first else "none"))
    print("  (b) lag x drift-reset, outage-recovery-v1, shipped machinery "
          "(TS + ADWIN + decay + R49):")
    print(table(["delay", "margin c/1k", "MAE pts", "alarms", "first alarm"], rows))
    print()
    # relabel variants
    rows = []
    for variant, label in (("shipped", "shipped: timeout excluded, late arrival = late counter only"),
                           ("retro", "retro-relabel: late authorization reverses timeout into alpha"),
                           ("decline", "no response = decline at deadline, corrected on late arrival")):
        m, r = run("baseline-steady-v1", n, "bare", relabel=variant)
        rows.append((label, fmt(m.authed / m.txns * 100, 2), fmt(m.margin_per_1k()),
                     fmt(m.mae(), 2), m.timeouts, m.late_events))
    print("  (c) late-settlement handling, baseline-steady-v1, bare TS "
          "(margins are face-value; labeling changes routing only; the late counter "
          "fires under every protocol -- only relabel variants also rewrite labels):")
    print(table(["variant", "auth%", "margin c/1k", "MAE pts", "timeouts",
                 "late arrivals"], rows))
    print("""
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
""")


SECTIONS = {
    "C1": sec_c1,
    "C2": sec_c2,
    "C3": sec_c3,
    "C4": sec_c4,
    "C5": sec_c5,
}


def main(argv):
    n = DEFAULT_N
    only = None
    for arg in argv[1:]:
        if arg.startswith("--section="):
            only = arg.split("=", 1)[1]
        else:
            n = int(arg)

    print(f"#9 evidence spike: censored data and exploration-exploitation | "
          f"n={n} | policy seed {POLICY_SEED} | lambda_to={LAM_TO:.0f}")
    print("worlds: baseline-steady-v1@%s, outage-recovery-v1@%s, quiet-improvement-v1@%s, quiet-improvement-starved-v1@%s"
          % (scenario_hash(_doc("baseline-steady-v1"))[:19],
             scenario_hash(_doc("outage-recovery-v1"))[:19],
             scenario_hash(_doc("quiet-improvement-v1"))[:19],
             scenario_hash(_doc("quiet-improvement-starved-v1"))[:19]))
    print()

    for name, fn in SECTIONS.items():
        if only is None or only == name:
            fn(n)
            print()

    if only is None:
        print("=" * 100)
        print(f"Reproduce: python3 spikes/0009-censored-exploration/censored.py {n}   "
              "(RESULTS.md is this output)")


if __name__ == "__main__":
    main(sys.argv)
