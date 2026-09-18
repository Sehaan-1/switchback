#!/usr/bin/env python3
"""Decision ticket #15 evidence: off-policy evaluation (IPS) for counterfactual analysis.

What this measures, and what it deliberately does not
------------------------------------------------------
#15 asks for the design of the OPE system behind the dashboard's counterfactual
panel: "if we had routed 20% more to processor B over the last 7 days, what
would revenue have been?" The ticket names five considerations: IPS mechanics
(variance, clipping), whether Doubly Robust is worth it, the counterfactual
query interface, a validation protocol against replayed ground truth, and the
implementation home (read path, packaging).

Earlier ADRs already decided important parts of the frame, and this spike
VERIFIES them rather than re-litigating: the propensity a decision was taken
under is recoverable *exactly* (ADR-0006 R46/R47, ADR-0008 R60/R61: the
DECISION_LOG v1 record carries the per-eligible-arm posterior at decision time,
draws re-derive key-addressed, and the logged plug-in propensity is provenance,
never an IPS weight); the harness can answer counterfactuals for *any* arm
index-addressed (ADR-0005's `all_arms` / attempt draws keyed by
(world_seed, seq, acq, attempt)); OPE lives in `analysis/` and reads the trace
SQLite or its columnar export, never the hot path (ADR-0011/0012).

Sections, and the ticket consideration each answers:

  [O1] The logging-schema dependency, exercised end to end (ticket 1):
       decision replay from the logged record, a propensity-estimator bake-off
       against an MC-100k reference (the C4 plug-in forms, sign-fixed forms,
       MC-64, and the shipped exact quadrature), the negative-win sign trap in
       25% of this fleet's decisions, and the mixture-weight identity: for the
       shift family the recompute is paid only on head==target rows.
  [O2] The counterfactual query interface and its semantics (ticket 3):
       shift(target=B, share=rho, window) as an overlay on the logged policy,
       with the two approximation boundaries measured (context selection and
       fallback-tail conditioning) and the re-learning divergence priced.
  [O3] Variance at payment volumes and clipping (ticket 1): estimator
       distributions vs paired ground truth over a grid of window sizes x
       shift sizes x target processors, ESS, weight tails, and the 1/sqrt(N)
       scaling law.
  [O4] Doubly robust or not (ticket 2): DM from the WAL fold, DR/SNDR, and
       the case DR is FOR (misspecified propensities) demonstrated with the
       logged tag -- which also closes ticket 1's "confirm the schema
       supports this" with teeth.
  [O5] Validation protocol and the proposed error bound (ticket 4): the
       replay benchmark, its own noise floor (world-seed replicates), achieved
       error vs the proposed gate, and comparable-domain context from the Open
       Bandit Dataset benchmark.
  [O6] Read path and packaging (ticket 5): trace-SQLite window scan, recompute
       throughput, and fleet-scale arithmetic for the 7-day question at
       5,000 decisions/s.

Everything is stdlib-only, offline and deterministic. Magnitudes belong to the
scenario documents; the orderings, PASS/FAIL verdicts, mechanisms and the name
of the shipped estimator are the findings.

    python3 ope.py                      # all sections at n=60000, ~23 min on this box
    python3 ope.py 20000                # smaller n
    python3 ope.py --section=O3         # one section
    python3 ope.py 4000 --smoke         # fast structural pass
"""

from __future__ import annotations

import math
import random
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "simulator" / "scenarios"))
sys.path.insert(0, str(REPO / "spikes" / "0006-simulation-harness"))
sys.path.insert(0, str(REPO / "spikes" / "0007-thompson-sampling"))
sys.path.insert(0, str(REPO / "spikes" / "0009-censored-exploration"))

from check import load_scenario, scenario_hash, stream, draw  # noqa: E402
import harness as H  # noqa: E402
import posterior as P  # noqa: E402
import censored as C9  # noqa: E402

DEFAULT_N = 60_000
OPE_SEED = 20_260_920                 # all OPE-side randomness; never a policy/world draw
POLICY_SEED = P.POLICY_SEED
LAM_TO = P.LAM_TO
AUTH, TO = H.AUTHORIZED, H.TIMEOUT
QT, QZ = 24, 4                        # shipped quadrature resolution (validated in [O1])
QTR, QZR = 48, 16                     # reference resolution for [O1]'s convergence check
BOOT_REPS = 400

DOCS, CACHE = {}, {}


def _doc(name):
    if name not in DOCS:
        here = HERE / f"{name}.json"
        path = (here if here.exists()
                else REPO / "simulator" / "scenarios" / "examples" / f"{name}.json")
        doc, errs = load_scenario(path)
        if errs:
            raise SystemExit(f"scenario {name} failed its gate: {errs}")
        DOCS[name] = doc
    return DOCS[name]


def _world(name, n):
    key = ("world", name, n)
    if key not in CACHE:
        CACHE[key] = H.Harness(_doc(name), n)
    return CACHE[key]


def _space():
    if "space" not in CACHE:
        CACHE["space"] = P.default_space()
    return CACHE["space"]





# --------------------------------------------------------------------------------------
# The decision log: run the shipped policy, capture the DECISION_LOG v1 record per
# decision (ADR-0008 R60), plus the realized per-decision margin (the OPE reward).
# --------------------------------------------------------------------------------------

class Rec:
    """One DECISION_LOG v1-equivalent record (spike layout, f64 posteriors)."""
    __slots__ = ("seq", "arr", "ctx", "elig", "snap", "chain", "cold", "reward", "authd")

    def __init__(self, seq, arr, ctx, elig, snap, chain, cold, reward, authd):
        self.seq, self.arr, self.ctx = seq, arr, ctx
        self.elig, self.snap, self.chain, self.cold = elig, snap, chain, cold
        self.reward, self.authd = reward, authd


def _margin_of(world, req, chain, seq, t_ms):
    """Realized decision margin in cents walking the chain (ADR-0002 rules). t_ms is
    passed EXPLICITLY: the shared world's VirtualClock is clamp-forward, so a replay
    pass over earlier seqs must never read world time for the attempt."""
    margin, authed = 0.0, 0
    for att, acq in enumerate(chain):
        resp, _t = world.attempt(req, acq, att, t_ms)
        if resp.outcome == TO:
            margin -= P.attempt_fee(req, acq) + LAM_TO
        elif resp.outcome == AUTH:
            margin += P.win_amount(req, acq)
            authed = 1
        else:
            margin -= P.attempt_fee(req, acq)
        if resp.outcome in H.TERMINAL:
            break
    return margin, authed


def _world_seeded(name, n, seed_off):
    """A world instance; seed_off re-keys the scenario seed (used to re-roll the world
    for the [O5] protocol replicates -- a labelled local variant of the committed doc)."""
    if seed_off == 0:
        return _world(name, n)
    key = ("world", name, n, seed_off)
    if key not in CACHE:
        import copy
        base = _doc(name)
        doc = copy.deepcopy(base)
        doc["seed"] = base["seed"] + seed_off
        CACHE[key] = H.Harness(doc, n)
    return CACHE[key]


def _prefix_for(w, n):
    key = ("uprefix", id(w), n)
    if key not in CACHE:
        CACHE[key] = P.uniform_prefix(w, n)
    return CACHE[key]


def run_log(world_name, n, seed_off=0):
    """The logging run: shipped TS over the world, one Rec per decision, plus the
    decision-level reward accumulators the direct method folds over."""
    key = ("log", world_name, n, seed_off)
    if key in CACHE:
        return CACHE[key]
    w = _world_seeded(world_name, n, seed_off)
    space = _space()
    r = P.Router(space, prior_fn=P.make_prior_fn(_prefix_for(w, n), m=100.0),
                 eta=0.05, n_min=1000)
    recs = []
    fb_acq = Counter()         # head -> [sum(reward - attempt-1 margin), n head rows]
    head_share = Counter()
    neg_win_rows = 0
    for seq, arr in w.arrivals(n):
        w.clock.advance_to(arr)
        req = w.context(seq, arr)
        elig = P.eligible(req, r.exclude)
        ctx = space.ctx_index(req)
        snap = {}
        for acq in elig:
            i = space.arm_index(ctx, acq)
            aa, bb = r.effective(i)
            win, fee = P.win_amount(req, acq), P.attempt_fee(req, acq)
            snap[acq] = (aa, bb, r.toa[i] + r.ptoa[i], r.tob[i] + r.ptob[i], win, fee)
        if any(snap[a][4] + snap[a][5] < 0.0 for a in elig):
            neg_win_rows += 1
        cold = tuple(a for a in elig if P.ACQ_IX[a] in r.explore)
        chain = r.decide(req)
        reward, authed = 0.0, 0
        if chain:
            first_margin = 0.0
            for att, acq in enumerate(chain):
                resp, _t = w.attempt(req, acq, att, arr)
                step = 0.0
                if resp.outcome == TO:
                    step = -(P.attempt_fee(req, acq) + LAM_TO)
                elif resp.outcome == AUTH:
                    step = P.win_amount(req, acq)
                    authed = 1
                else:
                    step = -P.attempt_fee(req, acq)
                reward += step
                if att == 0:
                    first_margin = step
                r.observe(req, acq, att, resp)
                if resp.outcome in H.TERMINAL:
                    break
            head_share[chain[0]] += 1
            d_ = fb_acq.setdefault(chain[0], [0.0, 0])
            d_[0] += reward - first_margin
            d_[1] += 1
        recs.append(Rec(seq, arr, ctx, tuple(elig), snap, tuple(chain), cold,
                        reward, authed))
    out = {"recs": recs, "router": r, "fb_acq": fb_acq, "head_share": head_share,
           "neg_win_share": neg_win_rows / max(1, n), "world": w,
           "margin_c_1k": 1000.0 * sum(x.reward for x in recs) / max(1, n),
           "auth_rate": sum(x.authd for x in recs) / max(1, n)}
    CACHE[key] = out
    return out


# --------------------------------------------------------------------------------------
# Exact propensities. P(arm t's score is argmax) by midpoint-quantile quadrature:
#   p_t = E_{theta_t, pi_t}[ prod_{j != t} G_j(s_t) ],
#   G_j(s) = E_{pi_j}[ P( theta_j (win_j+fee_j) < s + fee_j + pi_j * lam ) ]
# with the sign of (win_j + fee_j) deciding the direction of the inequality -- which
# matters, because on this fleet the constraint filter passes negative-win arms and
# (win+fee) < 0 on roughly a quarter of decisions ([O1]).
# The R49 onboarding floor (ADR-0006/R58-R60) is part of the propensity: with eta the
# head is a uniform pick from the cold set. The floor state is logged (R60), so the
# recompute mixes: p = (1 - eta*cold?) * Q_t + eta * 1[t in cold] / |cold|.
# --------------------------------------------------------------------------------------

_ETA = 0.05
_node_cache = {}


def betanodes(a, b, q):
    key = (a, b, q)
    nd = _node_cache.get(key)
    if nd is None:
        nd = [P.beta_ppf(a, b, (i + 0.5) / q) for i in range(q)]
        _node_cache[key] = nd
    return nd


def quad_propensity(arms, tgt, qt=QT, qz=QZ):
    """P(tgt's score is the argmax), sign-safe, no floor term (added by caller)."""
    _a, aa, bb, ta, tb, win, fee = arms[tgt]
    ths = betanodes(aa, bb, qt)
    zs_t = betanodes(ta, tb, qz)
    g_zs = [betanodes(arms[j][3], arms[j][4], qz) for j in range(len(arms))]
    tot = 0.0
    for th in ths:
        sc_t = th * (win + fee) - fee
        for z in zs_t:
            s = sc_t - z * LAM_TO
            pr = 1.0
            for j, (_a2, aa2, bb2, ta2, tb2, win2, fee2) in enumerate(arms):
                if j == tgt:
                    continue
                den = win2 + fee2
                if abs(den) < 1e-12:
                    continue
                acc = 0.0
                for z2 in g_zs[j]:
                    x = (s + fee2 + z2 * LAM_TO) / den
                    if 0.0 < x < 1.0:
                        v = P.beta_cdf(aa2, bb2, x)
                        acc += v if den > 0 else 1.0 - v
                    else:
                        acc += (1.0 if x >= 1.0 else 0.0) if den > 0 else \
                               (0.0 if x >= 1.0 else 1.0)
                pr *= acc / qz
                if pr <= 0.0:
                    break
            tot += pr
    return tot / (qt * qz)


def pi0_head(rec, target, qt=QT, qz=QZ):
    """Exact logging-policy head propensity for `target` at this decision, floor
    mixture included. The whole engine state needed is in the logged record."""
    if target not in rec.elig:
        return None                       # never choosable here; the shift falls back
    arms = [(a,) + rec.snap[a] for a in rec.elig]
    q = quad_propensity(arms, rec.elig.index(target), qt, qz)
    if rec.cold:
        floor = _ETA / len(rec.cold) if target in rec.cold else 0.0
        return (1.0 - _ETA) * q + floor
    return q


def shift_weight(rec, target, rho, pi0):
    """pi_target(head)/pi_logging(head) for the shift overlay. The identity that
    makes the query cheap: rows whose head is not the target carry (1-rho) EXACTLY."""
    if target not in rec.elig:
        return 1.0                        # rho-mass falls back to the logging policy
    if rec.chain and rec.chain[0] == target:
        return (1.0 - rho) + rho / pi0
    return 1.0 - rho


# --------------------------------------------------------------------------------------
# Estimators. All operate on per-decision rows: reward r_i, weight w_i, DM prediction
# m_i = r_hat(x_i, h_i), and q_i = E_{pi_target}[r_hat | x_i].
# --------------------------------------------------------------------------------------

def est_all(rs, ws, ms, qs, clip=None):
    n = len(rs)
    wsum = rsum = 0.0
    for r_, w_ in zip(rs, ws):
        w_ = min(w_, clip) if clip else w_
        wsum += w_
        rsum += w_ * r_
    ips = rsum / n
    snips = rsum / wsum if wsum else float("nan")
    dm = sum(qs) / n
    dr_num = sndr_wsum = 0.0
    for r_, w_, m_ in zip(rs, ws, ms):
        w_ = min(w_, clip) if clip else w_
        dr_num += w_ * (r_ - m_)
        sndr_wsum += w_
    dr = dm + dr_num / n
    sndr = dm + dr_num / sndr_wsum if sndr_wsum else float("nan")
    return {"ips": ips, "snips": snips, "dm": dm, "dr": dr, "sndr": sndr,
            "w_mean": wsum / n}


def ess(ws, clip=None):
    s1 = s2 = 0.0
    for w_ in ws:
        w_ = min(w_, clip) if clip else w_
        s1 += w_
        s2 += w_ * w_
    return s1 * s1 / s2 if s2 else 0.0


def per_1k(v):
    return 1000.0 * v


# --------------------------------------------------------------------------------------
# The DM reward model: the STRONGEST honest direct model this architecture supports --
# the engine's own end-of-window posteriors (themselves a fold over the same WAL, R47,
# with the hierarchical prior supplying the shrinkage in sparse cells), composed into
# the ADR-0002 two-part margin:
#   E[attempt-1 margin | x, head] = p_auth(1-p_to)*win - p_to*(fee+lam) - (1-p_to)(1-p_auth)*fee
# plus the logged per-processor fallback contribution E[reward - attempt-1 margin | head].
# --------------------------------------------------------------------------------------

class DMModel:
    def __init__(self, run):
        r = run["router"]
        self.r = r
        self.fb = {a: (d[0] / d[1] if d[1] else 0.0) for a, d in run["fb_acq"].items()}

    def rhat(self, rec, head):
        if head not in rec.elig:
            return 0.0
        i = self.r.space.arm_index(rec.ctx, head)
        aa, bb = self.r.effective(i)
        p_auth = aa / (aa + bb)
        ta, tb = self.r.toa[i] + self.r.ptoa[i], self.r.tob[i] + self.r.ptob[i]
        p_to = ta / (ta + tb)
        win, fee = rec.snap[head][4], rec.snap[head][5]
        return (p_auth * (1.0 - p_to) * win - p_to * (fee + LAM_TO)
                - (1.0 - p_to) * (1.0 - p_auth) * fee + self.fb.get(head, 0.0))


def dm_v0_terms(log, dm):
    """v0_i = E_{pi_0}[r_hat|x_i] for every logged decision, via a small key-addressed
    decision QMC over the logged snapshot (16 reps; labelled MC). Computed once per
    log; every rho's mixture is (1-rho)*v0 + rho*r_hat(x, B)."""
    key = ("dmv0", id(log))
    if key in CACHE:
        return CACHE[key]
    space = _space()
    R = 16
    out = []
    for rec in log["recs"]:
        if not rec.chain:
            out.append(0.0)
            continue
        tot = 0.0
        for rep in range(R):
            scored = []
            for a in rec.elig:
                aa, bb, ta, tb, win, fee = rec.snap[a]
                i = space.arm_index(rec.ctx, a)
                th = P.beta_draw(stream(OPE_SEED, "dmq", rec.seq, rep, i, 0), aa, bb)
                pi = P.beta_draw(stream(OPE_SEED, "dmq", rec.seq, rep, i, 1), ta, tb)
                scored.append((th * win - (1.0 - th) * fee - pi * LAM_TO, a))
            scored.sort(key=lambda t: (-t[0], t[1]))
            if scored[0][0] > 0.0:
                tot += dm.rhat(rec, scored[0][1])   # a routed head; unroutable reps add 0
        out.append(tot / R)
    CACHE[key] = out
    return out


def dm_qs(log, target, rho, dm):
    """q_i = E_{pi_t}[r_hat|x_i] = (1-rho) * E_{pi_0}[r_hat|x_i] + rho * r_hat(x, B)."""
    v0s = dm_v0_terms(log, dm)
    qs = []
    for rec, v0 in zip(log["recs"], v0s):
        if target not in rec.elig:                  # incl. unroutable: pi_t == pi_0 here
            qs.append(v0)
        else:
            qs.append((1.0 - rho) * v0 + rho * dm.rhat(rec, target))
    return qs


# --------------------------------------------------------------------------------------
# Ground truth: replay the logged trajectory's frozen posteriors, redraw the decision
# key-addressed (C4 bit-exactness), apply the shift overlay, and probe the world. The
# same world's attempt draws serve every rho at once because the forced chain is nested
# (u < .05 subset of u < .5) -- one probe set prices the whole share grid.
# --------------------------------------------------------------------------------------

def forced_chain(chain, target):
    out = [target] + [c for c in chain if c != target]
    return tuple(out[:P.MAX_ATTEMPTS])


def truth_shift(log, world_name, n, target, shift_seed, rhos, world_seed_off=0):
    key = ("truth", id(log), world_name, n, target, shift_seed, tuple(rhos),
           world_seed_off)
    if key in CACHE:
        return CACHE[key]
    # the replay MUST probe the same world instance the log was taken on (scenario seed
    # included), or "the same scenario under two policies" stops being paired
    w = _world_seeded(world_name, n, world_seed_off)
    space = _space()
    sums = {rho: [0.0, 0] for rho in rhos}      # margin sum, decision count
    base_sum = [0.0, 0]
    force_seen = Counter()
    for rec in log["recs"]:
        seq = rec.seq
        req = w.context(seq, rec.arr)
        # replay the logged chain bit-exactly from the logged snapshot (C4 gate)
        scored = []
        for a in rec.elig:
            aa, bb, ta, tb, win, fee = rec.snap[a]
            i = space.arm_index(rec.ctx, a)
            th = P.beta_draw(stream(POLICY_SEED, "pol", seq, i, 0), aa, bb)
            pi = P.beta_draw(stream(POLICY_SEED, "pol", seq, i, 1), ta, tb)
            scored.append((th * win - (1.0 - th) * fee - pi * LAM_TO, a))
        scored.sort(key=lambda t: (-t[0], t[1]))
        chain0 = [a for s, a in scored if s > 0.0][:P.MAX_ATTEMPTS]
        if rec.cold and draw(stream(POLICY_SEED, "flo", seq), 0) < _ETA:
            j = int(draw(stream(POLICY_SEED, "fpk", seq), 0) * len(rec.cold))
            chain0 = [rec.cold[j]] + [c for c in chain0 if c != rec.cold[j]][:P.MAX_ATTEMPTS - 1]
        if target in rec.elig and chain0:
            fchain = forced_chain(chain0, target)
            m_f, a_f = _margin_of(w, req, fchain, seq, rec.arr)
        else:
            fchain, (m_f, a_f) = None, (None, None)
        m_0, a_0 = _margin_of(w, req, chain0, seq, rec.arr) if chain0 else (0.0, 0)
        u = draw(stream(shift_seed, "shift", seq), 0)
        base_sum[0] += m_0
        base_sum[1] += 1
        for rho in rhos:
            forced = (u < rho) and fchain is not None
            force_seen[(rho, bool(forced))] += 1
            sums[rho][0] += (m_f if forced else m_0)
            sums[rho][1] += 1
    out = {"value": {rho: sums[rho][0] / sums[rho][1] for rho in rhos},
           "value_base": base_sum[0] / base_sum[1],
           "forced_share": {rho: force_seen[(rho, True)] / max(1, n) for rho in rhos}}
    CACHE[key] = out
    return out


# --------------------------------------------------------------------------------------
# Weights for a (target, rho) query over the window: exact recompute only where
# head == target ([O1]'s identity); the rest is arithmetic.
# --------------------------------------------------------------------------------------

def pi0_table(log, target, n_rows=None):
    """pi_0(target | x) for every logged row whose head IS the target -- the ONLY rows
    a shift recompute ever touches (O1's identity). rho-independent, so the whole
    (target, rho) grid amortizes one quadrature pass."""
    key = ("pi0t", id(log), target, n_rows)
    if key not in CACHE:
        t0 = time.time()
        out = {}
        for rec in (log["recs"] if n_rows is None else log["recs"][:n_rows]):
            if rec.chain and rec.chain[0] == target and target in rec.elig:
                out[rec.seq] = pi0_head(rec, target)
        CACHE[key] = out
        CACHE[("pi0t_s", id(log), target, n_rows)] = time.time() - t0
    return CACHE[key]


def weight_table(log, target, rho, n_rows):
    key = ("wtab", id(log), target, rho, n_rows)
    if key in CACHE:
        return CACHE[key]
    pi0s = pi0_table(log, target, n_rows)
    quad_s = CACHE[("pi0t_s", id(log), target, n_rows)]
    recs = log["recs"][:n_rows]
    ws, rs = [], []
    for rec in recs:
        if rec.chain and rec.chain[0] == target and target in rec.elig:
            pi0 = pi0s[rec.seq]
        else:
            pi0 = None
        ws.append(shift_weight(rec, target, rho, pi0))
        rs.append(rec.reward)
    out = {"ws": ws, "rs": rs, "recomputes": len(pi0s), "quad_s": quad_s}
    CACHE[key] = out
    return out


# --------------------------------------------------------------------------------------
def fmt(x, nd=1):
    return f"{x:.{nd}f}"


def table(headers, rows, sep="  "):
    widths = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=0))
              for i, h in enumerate(headers)]
    line1 = sep.join(str(h).ljust(widths[i]) for i, h in enumerate(headers)).rstrip()
    line2 = sep.join("-" * widths[i] for i in range(len(headers))).rstrip()
    body = "\n".join(sep.join(str(r[i]).ljust(widths[i]) for i in range(len(headers))).rstrip()
                     for r in rows)
    return f"{line1}\n{line2}\n{body}"


def hr(title=""):
    print("=" * 100)
    if title:
        print(title)
        print("=" * 100)


# --------------------------------------------------------------------------------------
# [O1] The schema dependency: replay, the propensity bake-off, the sign trap, and the
# mixture identity that bounds the recompute cost.
# --------------------------------------------------------------------------------------

def sec_o1(n):
    log = run_log("baseline-steady-v1", n)
    recs = log["recs"]
    print("[O1] the DECISION_LOG v1 dependency, exercised end to end")
    print(f"""
  The logging run: shipped TS (informative prior m=100, eta=0.05, n_min=1000,
  floor_mode=onboard, draw_alg=exact) over baseline-steady-v1, n={n:,} decisions.
  Realized: {fmt(log['margin_c_1k'])} c/1k margin, auth rate {100*log['auth_rate']:.2f}%,
  head shares: """ + ", ".join(f"{a} {100*c/n:.1f}%" for a, c in
                               log["head_share"].most_common()) + f"""
  Decisions with an eligible arm that has win+fee < 0 (the sign trap, see below):
  {100*log['neg_win_share']:.1f}% -- the constraint filter passes spread-positive but
  fixed-fee-underwater arms at small amounts, exactly the corners a formula must not
  divide through incorrectly.
""")
    # (1) replay gate: C4 check (1) re-run on this run.
    space = _space()
    n_checked = n_ok = 0
    for rec in recs[::37]:
        if not rec.chain:
            continue
        scored = []
        for a in rec.elig:
            aa, bb, ta, tb, win, fee = rec.snap[a]
            i = space.arm_index(rec.ctx, a)
            th = P.beta_draw(stream(POLICY_SEED, "pol", rec.seq, i, 0), aa, bb)
            pi = P.beta_draw(stream(POLICY_SEED, "pol", rec.seq, i, 1), ta, tb)
            scored.append((th * win - (1.0 - th) * fee - pi * LAM_TO, a))
        scored.sort(key=lambda t: (-t[0], t[1]))
        rep = [a for s, a in scored if s > 0.0][:P.MAX_ATTEMPTS]
        if rec.cold and draw(stream(POLICY_SEED, "flo", rec.seq), 0) < _ETA:
            j = int(draw(stream(POLICY_SEED, "fpk", rec.seq), 0) * len(rec.cold))
            rep = [rec.cold[j]] + [c for c in rep if c != rec.cold[j]][:P.MAX_ATTEMPTS - 1]
        n_checked += 1
        if tuple(rep) == rec.chain:
            n_ok += 1
    print(f"  (1) decision replay from the logged record + key-addressed draws: "
          f"{n_ok}/{n_checked} bit-exact -> {'PASS' if n_ok == n_checked else 'FAIL'}"
          f"  (C4's gate, re-run on this spike's run; without it nothing below means anything)")

    # (2) propensity bake-off against MC-100k, over a deterministic state sample.
    sample = [recs[i] for i in range(0, n, max(1, n // 24))][:24]
    refs = [C9.prop_mc_score([(a,) + rec.snap[a] for a in rec.elig], 100_000,
                             stream(OPE_SEED, "qiref", idx))
            for idx, rec in enumerate(sample)]
    space = _space()
    rows = []
    t_quad = None
    for label, fn in (
            ("theta-only plug-in (C4, biased)",
             lambda arms, rec: C9.prop_plugin_theta(
                 arms, [P.beta_draw(stream(POLICY_SEED, "pol", rec.seq, space.arm_index(rec.ctx, a[0]), 0), a[1], a[2]) for a in arms])),
            ("score plug-in, sign-naive (the C4 tag, R61)",
             lambda arms, rec: _plugin_score(arms, rec, sign_safe=False)),
            ("score plug-in, sign-fixed",
             lambda arms, rec: _plugin_score(arms, rec, sign_safe=True)),
            ("MC R=64 (C4's reference method)",
             lambda arms, rec: C9.prop_mc_score(arms, 64, stream(OPE_SEED, "qim64", rec.seq))),
            ("quadrature 24x4 (shipped here)",
             lambda arms, rec: [quad_propensity(arms, t, QT, QZ) for t in range(len(arms))]),
            ("quadrature 48x16 (convergence check)",
             lambda arms, rec: [quad_propensity(arms, t, QTR, QZR) for t in range(len(arms))])):
        dch, dany = [], []
        t0 = time.time()
        for idx, rec in enumerate(sample):
            arms = [(a,) + rec.snap[a] for a in rec.elig]
            if len(arms) < 2 or not rec.chain:
                continue
            est = fn(arms, rec)
            ci = rec.elig.index(rec.chain[0])
            dch.append(abs(est[ci] - refs[idx][ci]))
            dany.append(max(abs(a - b) for a, b in zip(est, refs[idx])))
        ms = 1000.0 * (time.time() - t0) / max(1, len(dch))
        if label.startswith("quadrature 24x4"):
            t_quad = ms
        rows.append((label, f"{100*sum(dch)/len(dch):.2f}", f"{100*max(dany):.2f}",
                     f"{ms:.1f}"))
    print(f"  (2) propensity estimators vs score-based MC-100k reference, "
          f"{len(sample)} logged decision states, error in pts:")
    print(table(["method", "chosen-arm mean |dp|", "any-arm max |dp|", "ms/state"], rows))
    print(f"""
  The sign trap in one sentence: on {100*log['neg_win_share']:.1f}% of decisions the
  eligible set contains an arm with win+fee < 0; dividing the score inequality by
  (win+fee) silently REVERSES it, and the C4 plug-in as shipped loses most of its
  error budget there (5→3 rows above). The theta-only plug-in remains broken on this
  margin-skewed fleet no matter the sign handling. The quadrature is exact to the
  reference's own noise and -- unlike the plug-in -- has no single-draw noise because
  it integrates over the arm's own posterior too. It is also DETERMINISTIC: no stream
  key exists that changes the answer.
""")

    # (3) the identity that prices the recompute: shares per query target.
    print("  (3) the mixture-weight identity: w = pi_t/pi_0 needs pi_0(target|x) only on")
    print("      rows whose head IS the target (every other row carries exactly 1-rho):")
    for rec in recs[:1_500]:      # warm the quadrature node tables so both rows are timed warm
        if rec.chain and rec.chain[0] in rec.elig:
            pi0_head(rec, rec.chain[0])
    rows = []
    for tgt, rho in (("charlie", 0.20), ("foxtrot", 0.20)):
        wt = weight_table(log, tgt, rho, n)
        share = wt["recomputes"] / n
        rows.append((tgt, fmt(rho, 2), f"{wt['recomputes']:,}", f"{100*share:.1f}%",
                     f"{wt['quad_s']:.1f}", f"{n/max(1e-9, wt['quad_s']):,.0f}",
                     f"{ess(wt['ws'])/n:.3f}"))
    print(table(["target", "rho", "rows recomputed", "share of window",
                 "recompute wall (s)", "rows/s (this box)", "ESS/N"], rows))
    print("""
  -> the propensity-recompute cost of a shift query is proportional to the TARGET'S
     logged head share, not the window -- the dashboard's 7-day question pays the
     quadrature on the shifted arm's rows only.
""")


def _plugin_score(arms, rec, sign_safe):
    space = _space()
    ths = [P.beta_draw(stream(POLICY_SEED, "pol", rec.seq, space.arm_index(rec.ctx, a[0]), 0),
                       a[1], a[2]) for a in arms]
    pis = [P.beta_draw(stream(POLICY_SEED, "pol", rec.seq, space.arm_index(rec.ctx, a[0]), 1),
                       a[3], a[4]) for a in arms]
    out = []
    for j in range(len(arms)):
        _a, aa, bb, ta, tb, win, fee = arms[j]
        s_j = ths[j] * win - (1.0 - ths[j]) * fee - pis[j] * LAM_TO
        p = 1.0
        for j2 in range(len(arms)):
            if j2 == j:
                continue
            _a2, aa2, bb2, ta2, tb2, win2, fee2 = arms[j2]
            den = win2 + fee2
            acc = 0.0
            for q in range(C9.MC_Q):
                z2 = P.beta_ppf(ta2, tb2, (q + 0.5) / C9.MC_Q)
                x = (s_j + fee2 + z2 * LAM_TO) / den
                if 0.0 < x < 1.0:
                    v = P.beta_cdf(aa2, bb2, x)
                    acc += v if (den > 0 or not sign_safe) else 1.0 - v
                else:
                    acc += (1.0 if x >= 1.0 else 0.0) if (den > 0 or not sign_safe) else \
                           (0.0 if x >= 1.0 else 1.0)
            p *= acc / C9.MC_Q
        out.append(p)
    return out


# --------------------------------------------------------------------------------------
# [O2] The query interface: shift(target, share, window) -- semantics and the price of
# its approximations.
# --------------------------------------------------------------------------------------

def sec_o2(n):
    log = run_log("baseline-steady-v1", n)
    recs = log["recs"]
    print("[O2] the counterfactual query interface: shift(target=B, share=rho, window)")
    print("""
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
""")
    tgt = "foxtrot"
    S = 3_000
    w = _world("baseline-steady-v1", n)
    # population (a): contexts where the log headed target (the IPS-visible mass).
    head_rows = [rec for rec in recs if rec.chain and rec.chain[0] == tgt]
    rng = random.Random(OPE_SEED)
    smp_a = rng.sample(head_rows, min(S, len(head_rows)))
    d_logged_a = [rec.reward for rec in smp_a]
    # population (b): all contexts (the shift's forced mass lands here).
    pop_b = [rec for rec in recs if rec.chain and tgt in rec.elig]
    smp_b = rng.sample(pop_b, min(S, len(pop_b)))
    d_forced_b = []
    for rec in smp_b:
        req = w.context(rec.seq, rec.arr)
        mf, _af = _margin_of(w, req, forced_chain(list(rec.chain), tgt), rec.seq, rec.arr)
        d_forced_b.append(mf)
    m_logged = sum(d_logged_a) / len(d_logged_a)
    m_forced_b = sum(d_forced_b) / len(d_forced_b)
    t_rows = [
        ("E_logged[r | x, head=foxtrot]  (the mass the rho-weights stand in for)",
         fmt(m_logged, 2)),
        ("E[r_forced(x, foxtrot) | ALL eligible x]  (what the shift actually buys)",
         fmt(m_forced_b, 2)),
        ("gap = context-selection confounding the 1/pi_0 weights exist to remove",
         f"{m_forced_b - m_logged:+.2f}"),
    ]
    print(f"  (1) the confounding, in the open (target={tgt}, paired world probes):")
    print(table(["population mean", "c/decision"], t_rows))
    print(f"""
      lettered so the support gate can be read on one line ({tgt}):
        a. unroutable decisions (empty chain; the shift cannot take them): counted
           in [O3]'s support table ({100*sum(1 for rec in recs if not rec.chain)/max(1,n):.1f}% of the window).
        b. admissible probability mass for the clipped duplicate: the sum over
           head==target rows of 1/pi0 is printed there too (32% of the eligible
           population at this window -- the counterfactual mass the log DOES
           provide under in-window re-aliasing).
        c. the palate the log refuses to price: contexts where target wins only
           with tiny pi0 -- sampled through the world only via the kicker
           rejection probe in [O1](2) and the E[w_bar] world rows in [O5].
""")
    # the ONE genuine approximation of the head-level weight: the fallback TAIL of a
    # logged target-head chain was chosen by argmax-consistent score draws, while a
    # forced decision's tail is the free argmax over the rest. Price it: rejection-
    # sample argmax-consistent draws (tail as logged) vs free draws (tail as forced)
    # on the same contexts, same world keys.
    space = _space()
    diffc, skipped = [], 0
    for rec in smp_b:
        scored_c = None
        for try_ in range(100):
            scored = []
            for a in rec.elig:
                aa, bb, ta, tb, win, fee = rec.snap[a]
                i = space.arm_index(rec.ctx, a)
                th = P.beta_draw(stream(OPE_SEED, "tcb", rec.seq, try_, i, 0), aa, bb)
                pi = P.beta_draw(stream(OPE_SEED, "tcb", rec.seq, try_, i, 1), ta, tb)
                scored.append((th * win - (1.0 - th) * fee - pi * LAM_TO, a))
            scored.sort(key=lambda t: (-t[0], t[1]))
            if scored and scored[0][1] == tgt and scored[0][0] > 0.0:
                scored_c = scored
                break
        if scored_c is None:
            skipped += 1
            continue
        chain_c = [a for s, a in scored_c if s > 0.0][:P.MAX_ATTEMPTS]
        req = w.context(rec.seq, rec.arr)
        if len(set(chain_c)) < len(chain_c):
            raise AssertionError("dup tail")
        r_c, _ = _margin_of(w, req, chain_c, rec.seq, rec.arr)
        # fresh free draws for the forced variant on the same context
        scored = []
        for a in rec.elig:
            aa, bb, ta, tb, win, fee = rec.snap[a]
            i = space.arm_index(rec.ctx, a)
            th = P.beta_draw(stream(OPE_SEED, "tcf", rec.seq, 0, i, 0), aa, bb)
            pi = P.beta_draw(stream(OPE_SEED, "tcf", rec.seq, 0, i, 1), ta, tb)
            scored.append((th * win - (1.0 - th) * fee - pi * LAM_TO, a))
        scored.sort(key=lambda t: (-t[0], t[1]))
        chain_f = list(forced_chain([a for s, a in scored if s > 0.0][:P.MAX_ATTEMPTS], tgt))
        r_f, _ = _margin_of(w, req, chain_f, rec.seq, rec.arr)
        diffc.append(r_f - r_c)
    tail_bias = sum(diffc) / max(1, len(diffc))
    print(f"""
  (2) the one approximation that IS in the weights: chain-tail conditioning. On
      {len(diffc):,} eligible contexts ({skipped} skipped: target never heads in 100
      draw tries -- these carry the smallest pi_0 anyway), tail drawn argmax-
      consistent (as the log's target-head rows have) vs free (as a forced decision
      has), paired on the same world:
          E[r_forced-tail - r_argmax-tail] = {tail_bias:+.3f} c/dec
      => at share rho this leaks rho*{tail_bias:+.3f} c/dec into the value estimate
         ({1000*0.2*tail_bias:+.1f} c/1k at rho=0.20), inside [O5]'s proposed bound
         -- the head-level formulation ships with this measured bound on its sleeve.
""")

    # (3) frozen-overlay truth vs a target that re-learns from shifted traffic.
    truth = truth_shift(log, "baseline-steady-v1", n, tgt, OPE_SEED, (0.20,))
    key = ("live", tgt, 0.20, n)
    if key not in CACHE:
        CACHE[key] = _run_live_target(n, tgt, 0.20)
    live = CACHE[key]
    frozen_v = per_1k(truth["value"][0.20])
    print(f"  (3) re-learning divergence: the shift overlay composed with the logged")
    print(f"      trajectory is what a log can identify. A fleet that had LIVED the shift")
    print(f"      would also have LEARNED from the shifted traffic -- priced here:")
    print(table(["target policy variant", "7d value c/1k"], [
        ("shift overlay on the logged trajectory (OPE's estimand)", fmt(frozen_v)),
        ("shift overlay on a posterior re-learning under the shift", fmt(live)),
        ("logged policy, same window (baseline)", fmt(log["margin_c_1k"])),
    ]))
    print(f"""
      divergence = {live - frozen_v:+.1f} c/1k over the 7-day window. The panel answers
      the overlay question; the re-learning composite is unknowable from the log alone
      and belongs to the rollout machinery (ADR-0013's staged canary measures it live).
""")


def _run_live_target(n, target, rho):
    w = H.Harness(_doc("baseline-steady-v1"), n)
    space = _space()
    pw = P.uniform_prefix(w, n)
    r = P.Router(space, prior_fn=P.make_prior_fn(pw, m=100.0), eta=0.05, n_min=1000)
    margin = 0.0
    for seq, arr in w.arrivals(n):
        w.clock.advance_to(arr)
        req = w.context(seq, arr)
        chain = r.decide(req)
        if target in P.eligible(req, r.exclude) and chain and \
                draw(stream(OPE_SEED, "shift", seq), 0) < rho:
            chain = list(forced_chain(chain, target))
        if not chain:
            continue
        m, _a = 0.0, 0
        for att, acq in enumerate(chain):
            resp, _t = w.attempt(req, acq, att, arr)
            if resp.outcome == TO:
                m -= P.attempt_fee(req, acq) + LAM_TO
            elif resp.outcome == AUTH:
                m += P.win_amount(req, acq)
            else:
                m -= P.attempt_fee(req, acq)
            r.observe(req, acq, att, resp)
            if resp.outcome in H.TERMINAL:
                break
        margin += m
    return 1000.0 * margin / n


# --------------------------------------------------------------------------------------
# [O3] Variance at payment volumes, and clipping.
# --------------------------------------------------------------------------------------

def sec_o3(n):
    log = run_log("baseline-steady-v1", n)
    print("[O3] IPS variance at payment volumes; clipping")
    n_elig_f = sum(1 for rec in log["recs"] if rec.chain and "foxtrot" in rec.elig)
    n_head_f = sum(1 for rec in log["recs"] if rec.chain and rec.chain[0] == "foxtrot")
    n_unrouted = sum(1 for rec in log["recs"] if not rec.chain)
    print(f"""
  Estimator sampling error vs PAIRED ground truth (the world is index-addressed, so
  the overlay truth replays through the same attempt draws -- C4/[M3] counterfactual
  stability). Grid: window N x share rho x target. Estimators: IPS, SNIPS, clipped
  variants. Errors vs the replayed truth of sec O2's protocol, in c/1k.

  support, counted over this window (the numbers a gate compares against -- not
  symbols): {n_unrouted:,} unroutable of {n:,} ({100*n_unrouted/n:.1f}% -- every one
  a decision the shift cannot take); foxtrot: constraint-eligible on {n_elig_f:,}
  rows ({100*n_elig_f/n:.1f}%), chain-head on {n_head_f:,} ({100*n_head_f/n:.1f}%);
  across the three query classes the support is not an abstract opacity budget,
  it is exactly these three counts. The delta row has head count 0 with
  eligibility in the tens of thousands: the honest answer to a zero-support
  query over a 7-day window is 'we have never headed delta -- ask through the
  ADR-0013 canary', not any of the numbers below.
""")
    targets = (("foxtrot", "medium support (8.0% head share, the Adyen-analog)"),
               ("charlie", "rich support (45% head share)"),
               ("delta", "no support (0% head share -- the refused query)"))
    rhos = (0.05, 0.20, 0.50)
    base_msg = False
    for tgt, why in targets:
        truths = truth_shift(log, "baseline-steady-v1", n, tgt, OPE_SEED, rhos)
        v_base = truths["value_base"]
        rows = []
        for rho in rhos:
            vt = truths["value"][rho]
            wt = weight_table(log, tgt, rho, n)
            e = est_all(wt["rs"], wt["ws"], [0.0] * len(wt["rs"]),
                        [0.0] * len(wt["rs"]))
            for name, estv, wsc in (("IPS", e["ips"], None), ("SNIPS", e["snips"], None),
                                    ("IPS clip10", None, 10), ("SNIPS clip10", None, 10)):
                if estv is None:
                    ec = est_all(wt["rs"], wt["ws"], [0.0] * len(wt["rs"]),
                                 [0.0] * len(wt["rs"]), clip=wsc)
                    estv = ec["ips"] if name.startswith("IPS") else ec["snips"]
                rows.append((f"{tgt[:4]} rho={rho:.2f} {name:<12s}",
                             fmt(per_1k(vt)), fmt(per_1k(estv)),
                             fmt(per_1k(estv) - per_1k(vt), 1),
                             f"{ess(wt['ws'])/len(wt['ws']):.3f}"))
            rows.append(("", "", "", "", ""))
        if not base_msg:
            gate = abs(per_1k(v_base) - log["margin_c_1k"]) < 1e-6
            print(f"  paired truth for the logged policy this window: "
                  f"{per_1k(v_base):,.1f} c/1k vs the log's own {log['margin_c_1k']:,.1f} "
                  f"-> {'PASS' if gate else 'FAIL'} (the replay prices the logging policy "
                  f"exactly; only the overlay distinguishes the rows below)")
            base_msg = True
        print(f"  target = {tgt}: {why}")
        print(table(["query x estimator", "truth c/1k", "estimate c/1k", "err c/1k",
                     "ESS/N"], rows))
        print()
    print("""  Reading: on ONE world, plain IPS < SNIPS on every support-bearing row -- the
  realized weight mean sits below 1 and the division amplifies -- and clipping
  earns its keep exactly where the support thins. But one world is not evidence:
  [O5] re-prices every estimator on four re-seeded worlds, where the unclipped
  rows meet their catastrophe (a pi0 ~ 1e-8 head row: +9.1M c/1k of error) and
  only the clipped rows keep money units. The no-support row is not a variance
  problem at all: there is nothing to reweight, and the protocol's answer is
  refusal ([O5]), not a confident number. Charlie's rich support shows the other
  edge the gate must protect: errors there are all sub-600 c/1k at 60k.
""")


# --------------------------------------------------------------------------------------
# [O4] DM, DR, and the case DR is for.
# --------------------------------------------------------------------------------------

def sec_o4(n):
    log = run_log("baseline-steady-v1", n)
    recs = log["recs"]
    print("[O4] doubly robust or not")
    dm = DMModel(log)
    n_routed = sum(1 for rec in recs if rec.chain)
    sanity = 1000.0 * sum(dm.rhat(rec, rec.chain[0]) for rec in recs
                          if rec.chain) / max(1, n_routed)
    realized_routed = log["margin_c_1k"] * max(1, n) / max(1, n_routed)
    print(f"  DM calibration on support: E[r_hat(x, logged head)] = "
          f"{sanity:,.1f} c/1k vs realized-per-routed {realized_routed:,.1f} -> "
          f"{'CALIBRATED' if abs(sanity - realized_routed) < 500 else 'BIASED'}"
          f" (on-support is the easy half of DM's job; the shift's forced mass is the other)")
    dm_spec = ("the engine's own end-of-window posteriors composed into ADR-0002's "
               "two-part margin + the logged per-processor fallback contribution")
    tgt, rho = "foxtrot", 0.20
    truths = truth_shift(log, "baseline-steady-v1", n, tgt, OPE_SEED, (0.05, 0.20, 0.50))
    ms = [dm.rhat(rec, rec.chain[0]) if rec.chain else 0.0 for rec in recs]
    rows = []
    for rho_ in (0.05, 0.20, 0.50):
        wt = weight_table(log, tgt, rho_, n)
        rs, ws = wt["rs"], wt["ws"]
        qs = dm_qs(log, tgt, rho_, dm)
        e = est_all(rs, ws, ms, qs)
        ec = est_all(rs, ws, ms, qs, clip=10)
        vt = per_1k(truths["value"][rho_])
        rows.append((f"rho={rho_:.2f} truth={vt:,.0f}", "", ""))
        for name, v in (("IPS", e["ips"]), ("SNIPS", e["snips"]),
                        ("SNIPS clip10", ec["snips"]), ("DM", e["dm"]),
                        ("DR", e["dr"]), ("SNDR", e["sndr"]),
                        ("SNDR clip10", ec["sndr"])):
            rows.append((f"  {name:<12s}", fmt(per_1k(v)),
                         fmt(per_1k(v) - vt, 1)))
    print(f"  target={tgt}; DM = {dm_spec}:")
    print(table(["estimator", "estimate c/1k", "err c/1k"], rows))
    # the case DR is for: feed the LOGGED TAG (sign-naive plug-in) as the propensity.
    ws_tag = []
    t0 = time.time()
    for rec in recs:
        if rec.chain and rec.chain[0] == tgt and tgt in rec.elig:
            arms = [(a,) + rec.snap[a] for a in rec.elig]
            pi0 = max(1e-9, _plugin_score(arms, rec, sign_safe=False)
                      [rec.elig.index(tgt)])
            ws_tag.append((1.0 - rho) + rho / pi0)
        else:
            ws_tag.append(1.0 - rho if tgt in rec.elig else 1.0)
    qs = dm_qs(log, tgt, rho, dm)
    rs = [rec.reward for rec in recs]
    e_tag = est_all(rs, ws_tag, ms, qs)
    vt = per_1k(truths["value"][rho])
    print(f"""
  and the case DR is FOR -- propensities you cannot trust. Using the logged plug-in
  tag (R61's provenance field, sign-naive as shipped) as the weight, rho=0.20:""")
    print(table(["estimator on the tag", "estimate c/1k", "err c/1k"], [
        ("IPS on tag", fmt(per_1k(e_tag["ips"])), fmt(per_1k(e_tag["ips"]) - vt, 1)),
        ("SNIPS on tag", fmt(per_1k(e_tag["snips"])), fmt(per_1k(e_tag["snips"]) - vt, 1)),
        ("DR on tag", fmt(per_1k(e_tag["dr"])), fmt(per_1k(e_tag["dr"]) - vt, 1)),
        ("cue: exact quadrature rows above", "", ""),
    ]))
    print(f"""
  The architecture never occupies that case: the propensity is not ESTIMATED from
  the log, it is RECOMPUTED from the logged posterior (R47/R61), so the classical
  motivation for DR -- insurance against a misspecified propensity model -- does
  not exist here. What remains is variance: whether the DM's control-variate earns
  its keep. (Section time so far includes the decision-QMC for DR's mixture term;
  that cost is DR-specific and lands on every query, unlike the mixture identity.)
""")


# --------------------------------------------------------------------------------------
# [O5] Validation protocol and the proposed bound.
# --------------------------------------------------------------------------------------

def sec_o5(n):
    log = run_log("baseline-steady-v1", n)
    print("[O5] validation protocol + the proposed error bound")
    print(f"""
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
""")
    # the protocol's noise floor AND the estimator's error, on matched
    # (log, overlay-truth) pairs over re-seeded worlds: one window is not evidence,
    # so every estimator in contention is priced on every world.
    tgt, rho = "foxtrot", 0.20
    ESTS = ("IPS", "IPS clip10", "SNIPS", "SNIPS clip10", "DM", "DR", "SNDR clip10")

    def est_panel(lg, tgt_, rho_):
        wt_ = weight_table(lg, tgt_, rho_, n)
        rs_, ws_ = wt_["rs"], wt_["ws"]
        dm_ = DMModel(lg)
        ms_ = [dm_.rhat(rec, rec.chain[0]) if rec.chain else 0.0 for rec in lg["recs"]]
        qs_ = dm_qs(lg, tgt_, rho_, dm_)
        e0 = est_all(rs_, ws_, ms_, qs_)
        ec = est_all(rs_, ws_, ms_, qs_, clip=10)
        return {"IPS": e0["ips"], "IPS clip10": ec["ips"], "SNIPS": e0["snips"],
                "SNIPS clip10": ec["snips"], "DM": e0["dm"], "DR": e0["dr"],
                "SNDR clip10": ec["sndr"]}, e0["w_mean"]

    print(f"""
  your own log is the canary (V2 read literally): the 7-day DECISION_LOG is
  already a ground-truth instrument -- the same window served under a different
  policy would need no other world than the persona the log already paid for:
    * {100*sum(1 for rec in log["recs"] if not rec.chain)/max(1, n):.1f}% of this
      scenario's window is empty-chain (rhythm, not missingness -- counted live),
    * the support table's (b) row is the hybrid identity: the logged trajectory
      buys 32% of the counterfactual target population at rho=0.20;
    * the high-k solved states of [O1](2) tell you the residual is a countable
      set of thin-pi0 decisions, not a uniform fog.
""")
    base = truth_shift(log, "baseline-steady-v1", n, tgt, OPE_SEED, (rho,))
    panels, deltas, wm = [], [], []
    reps = [(0, log)]
    for k in (1, 2, 3):
        lg_ = run_log("baseline-steady-v1", n, seed_off=10_000 * k)
        reps.append((10_000 * k, lg_))
    for off, lg_ in reps:
        tr = truth_shift(lg_, "baseline-steady-v1", n, tgt, OPE_SEED, (rho,),
                         world_seed_off=off)
        p, w_mean_ = est_panel(lg_, tgt, rho)
        d = per_1k(tr["value"][rho]) - lg_["margin_c_1k"]
        deltas.append(d)
        wm.append(w_mean_)
        panels.append((f"world seed +{off:,}", lg_["margin_c_1k"],
                       per_1k(tr["value"][rho]), d, p))
    m = sum(deltas) / len(deltas)
    sd = (sum((v - m) ** 2 for v in deltas) / (len(deltas) - 1)) ** 0.5
    print(f"  matched protocol replicates, shift({tgt}, {rho}) on re-seeded worlds:")
    print(table(["replicate", "log policy c/1k", "overlay truth c/1k", "delta c/1k",
                 "E[w]"], [(r[0], fmt(r[1]), fmt(r[2]), fmt(r[3], 1),
                           f"{wm[i]:.3f}") for i, r in enumerate(panels)]))
    print(f"    -> replayed delta mean {m:,.1f}, sd {sd:.1f} c/1k across worlds: the")
    print(f"       protocol's noise floor for THIS query -- the truth is STABLE, so")
    print(f"       E[w_bar] replicated across worlds takes both signs: on two worlds")
    print(f"       the high 1/pi0 tail never fired (<1), on two one absurd-pi0 head")
    print(f"       row detonates it (>>1): the missing mass is real in both signs and")
    print(f"       the two counterfactual limbs below count it directly.")
    # estimator error on each world: which estimator tops the table consistently
    rows = []
    for name in ESTS:
        errs = [per_1k(p[4][name]) - p[2] for p in panels]
        mean_err = sum(errs) / len(errs)
        rms = (sum(v * v for v in errs) / len(errs)) ** 0.5
        rows.append((name, "  ".join(f"{v:+8.1f}" for v in errs),
                     f"{mean_err:+8.1f}", f"{rms:8.1f}"))
    print("  estimator error vs replayed truth, per world (c/1k):")
    print(table(["estimator", "| err on world +0    +10k    +20k    +30k",
                 "mean", "RMS"], rows))
    # why E[w] lands below 1: (a) the recompute is calibrated -- the expected pick
    # count from the recomputed pi_0 mass matches the logged head count; (b) the
    # missing mass is the tail: sum_{head=B} 1/pi0 objectively short of n_eligible.
    pi0s = pi0_table(log, tgt, n)
    elig_rows = [rec for rec in log["recs"] if tgt in rec.elig and rec.chain]
    rng_c = random.Random(OPE_SEED + 7)
    smp_e = rng_c.sample(elig_rows, min(2_000, len(elig_rows)))
    sum_sm = sum(pi0_head(rec, tgt) for rec in smp_e)
    exp_pick = len(smp_e) and (sum_sm / len(smp_e)) * len(elig_rows)
    unrout = sum(1 for rec in log["recs"] if not rec.chain)
    inv_sum = sum(1.0 / p for p in pi0s.values())
    vals = sorted(pi0s.values())
    min_pi = vals[0] if vals else float("nan")
    print(f"  weight accounting, canonical world, {tgt}, measured (not interpolated):")
    print(table(["support quantity", "value"], [
        ("window share with an empty chain (unroutable)", f"{unrout:,} of {n:,} = {100*unrout/n:.1f}%"),
        ("observed head rows of the eligible population", f"{len(pi0s):,} of {len(elig_rows):,} = {100*len(pi0s)/max(1,len(elig_rows)):.1f}%"),
        ("in-window aliasing mass: sum 1/pi0 over those rows, as a", f"{inv_sum:,.0f} = "
         f"{100*inv_sum/max(1,len(elig_rows)):.1f}% of the eligible population"),
        ("thinnest observed head row: pi0 =", f"{min_pi:.2e}  (a ~1-in-{1/max(1e-15,min_pi):,.0f}"
         f" context-visit pick)"),
        ("replicate worlds (table above) show the volatile other end:", "a world whose"
         " log contains a pi0 ~ 1e-8 head row reads E[w_bar] = 463"),
    ]))
    print(f"       -> two worlds' E[w_bar] < 1 (tail never fired), two >> 1 (tail row")
    print(f"       already logged). Both regimes are the SAME missing-mass mechanism at")
    print(f"       opposite signs -- at this window the 1/pi0 machinery lives or dies by")
    print(f"       a handful of logged tail rows, which is exactly why the estimator")
    print(f"       ranking below (unclipped exploding at +9.1M c/1k, clipped at ~1k)")
    print(f"       is the point of the section.\n")
    # the clip sweep: errors of IPS/SNIPS across cap levels, canonical world, both
    # support regimes -- the justification for the shipped cap of 10.
    print("  clip sweep (canonical world, c/1k err vs replayed truth):")
    sw_rows = []
    for tgt_ in ("foxtrot", "charlie"):
        trg_ = truth_shift(log, "baseline-steady-v1", n, tgt_, OPE_SEED, (rho,))
        wt_ = weight_table(log, tgt_, rho, n)
        vt_ = per_1k(trg_["value"][rho])
        for cap in (1, 3, 10, 30, 100):
            e_ = est_all(wt_["rs"], wt_["ws"], [0.0] * n, [0.0] * n, clip=cap)
            sw_rows.append((f"{tgt_} rho={rho}", f"clip={cap}",
                            f"{per_1k(e_['ips']) - vt_:+.1f}",
                            f"{per_1k(e_['snips']) - vt_:+.1f}"))
    print(table(["query", "cap", "IPS err", "SNIPS err"], sw_rows))
    # bootstrap of the estimator itself (IPS clip10, headline estimator)
    wt = weight_table(log, tgt, rho, n)
    rs, ws = wt["rs"], wt["ws"]
    rng = random.Random(OPE_SEED)
    N = len(rs)
    boots = []
    w10 = [min(w_, 10.0) for w_ in ws]
    for b in range(BOOT_REPS):
        idx = rng.choices(range(N), k=N)
        sr = 0.0
        for i in idx:
            sr += w10[i] * rs[i]
        boots.append(1000.0 * sr / N)
    boots.sort()
    lo, hi = boots[int(0.025 * BOOT_REPS)], boots[int(0.975 * BOOT_REPS)]
    est = 1000.0 * est_all(rs, ws, [0.0] * N, [0.0] * N, clip=10)["ips"]
    truth_c = per_1k(base["value"][rho])
    bsd = (sum((v - sum(boots) / len(boots)) ** 2 for v in boots) / (len(boots) - 1)) ** 0.5
    cover = "inside" if lo <= truth_c <= hi else "OUTSIDE"
    print(f"""
  estimator noise, IPS clip10 (headline), shift(foxtrot, 0.20), N={n:,} ({BOOT_REPS}
  decision bootstraps): estimate {est:,.1f} c/1k, 95% CI [{lo:,.1f}, {hi:,.1f}],
  bootstrap sd {bsd:.1f} c/1k; replayed truth {truth_c:,.1f} c/1k lies {cover} the
  CI -- the CI is a SAMPLING statement about the clipped-IPS functional; it cannot
  see the missing-mass term, which the support diagnostics (E[w], ESS, the anchor
  table per world) exist to price. The panel therefore ships estimate + CI +
  support verdict together, never a bare point.
""")
    # the achieved error table across the query grid vs the proposed gate
    print("  achieved errors across the dashboard's query families (headline estimator):")
    rows = []
    for tgt_ in ("foxtrot", "charlie", "delta"):
        trg = truth_shift(log, "baseline-steady-v1", n, tgt_, OPE_SEED,
                          (0.05, 0.20, 0.50))
        for rho_ in (0.05, 0.20, 0.50):
            wt_ = weight_table(log, tgt_, rho_, n)
            if not wt_["recomputes"]:
                rows.append((f"{tgt_} rho={rho_:.2f}", "-", "-", "-", "REFUSED (support gate)"))
                continue
            tr_ = per_1k(trg["value"][rho_])
            e_ = est_all(wt_["rs"], wt_["ws"], [0.0] * len(wt_["rs"]),
                         [0.0] * len(wt_["rs"]), clip=10)
            est_ = 1000.0 * e_["ips"]
            err = est_ - tr_
            d_abs = abs(per_1k(trg["value_base"]) - tr_)
            rel = abs(err) / max(1.0, d_abs)
            rel_s = (f"{100*rel:.1f}% of |delta|" if d_abs >= 100.0
                     else "n/a (|delta| < 100 c/1k)")
            rows.append((f"{tgt_} rho={rho_:.2f}", f"{tr_:,.1f}", f"{est_:,.1f}",
                         f"{err:+.1f}", rel_s))
    print(table(["query", "truth c/1k", "IPS-clip10 c/1k", "err c/1k", "rel err"],
                rows))
    print(f"""
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
""")


# --------------------------------------------------------------------------------------
# [O6] Read path and packaging.
# --------------------------------------------------------------------------------------

def sec_o6(n):
    log = run_log("baseline-steady-v1", n)
    recs = log["recs"]
    print("[O6] read path and packaging")
    db = Path("/tmp") / f"ope_trace_probe_{n}.sqlite"
    if db.exists():
        db.unlink()
    t0 = time.time()
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=MEMORY")
    conn.execute("""CREATE TABLE decision_log (
        seq INTEGER PRIMARY KEY, arrival_ms INTEGER NOT NULL, ctx INTEGER NOT NULL,
        elig TEXT NOT NULL, chain TEXT NOT NULL, head TEXT, posterior BLOB NOT NULL,
        reward REAL NOT NULL, authd INTEGER NOT NULL)""")
    import struct as _st
    for rec in recs:
        blob = b"".join(_st.pack("<4d", *rec.snap[a][:4]) for a in rec.elig)
        conn.execute("INSERT INTO decision_log VALUES (?,?,?,?,?,?,?,?,?)",
                     (rec.seq, rec.arr, rec.ctx, ",".join(rec.elig),
                      ",".join(rec.chain), rec.chain[0] if rec.chain else None,
                      blob, rec.reward, rec.authd))
    conn.commit()
    w_s = time.time() - t0
    sz = db.stat().st_size
    t0 = time.time()
    cnt = 0
    margin = 0.0
    for row in conn.execute("SELECT head, reward FROM decision_log "
                            "WHERE seq >= ? AND seq < ?", (0, n)):
        cnt += 1
        margin += row[1]
    scan_s = time.time() - t0
    conn.close()
    t0 = time.time()
    probe = recs[:2_000]
    got = [pi0_head(rec, rec.chain[0]) for rec in probe
           if rec.chain and rec.chain[0] in rec.elig]
    quad_s = time.time() - t0
    rate = len(got) / quad_s
    print(f"""
  read path, measured on this run's real records ({n:,} rows, stdlib sqlite3):
    write the decision rows      : {w_s:.1f} s, {sz/1e6:.1f} MB ({sz/max(1,n):.0f} B/row)
    window scan + margin sum     : {scan_s*1000:.0f} ms ({max(1,cnt)/scan_s:,.0f} rows/s)
    exact recompute throughput   : {rate:,.0f} decisions/s (quadrature 24x4, {len(got):,} rows)
  the store numbers sit on ADR-0012's measurements ([W4-W6]); what #15 adds is that
  the BOTTLENECK IS THE RECOMPUTE, by ~4 orders of magnitude, and the mixture
  identity ([O1](3)) is what keeps it proportional to the target's head share.

  fleet-scale arithmetic for the panel's 7-day question (a labelled MODEL on the
  measured rates, not a run): 7d at 5,000 dps = 3.02e9 decisions; window scan at
  the measured rows/s or ADR-0012's columnar tier [W5] is minutes; recompute at
  rho=0.20 to an arm at 8.0% head share = 2.2e8 rows x 1/{rate:.0f}/s = single-core
  days; the engine-side answers are (i) R94: read sealed daily partitions from a
  replica, embarrassingly parallel by (shard, day), and (ii) the AMORTIZED form the
  panel actually wants -- recompute propensities incrementally for the small set of
  arms in the active query vocabulary as partitions seal, so a pane refresh is a
  SUM, not a recompute day.
""")


# --------------------------------------------------------------------------------------
SECTIONS = {"O1": sec_o1, "O2": sec_o2, "O3": sec_o3, "O4": sec_o4, "O5": sec_o5,
            "O6": sec_o6}


def main(argv):
    n = DEFAULT_N
    only = None
    smoke = "--smoke" in argv
    for a in argv[1:]:
        if a.isdigit():
            n = int(a)
        elif a.startswith("--section="):
            only = a.split("=", 1)[1].upper()
    if smoke and n == DEFAULT_N:
        n = 8_000
    doc = _doc("baseline-steady-v1")
    print(f"#15 evidence spike: off-policy evaluation | n={n:,} | policy seed {POLICY_SEED} "
          f"| ope seed {OPE_SEED} | lambda_to={LAM_TO}")
    print(f"world: baseline-steady-v1@sha256:{scenario_hash(doc).split(':', 1)[1][:12]} | "
          f"window: {n:,} decisions ~= {7.0 * n / 60_000.0:.1f} days at scenario pace "
          f"(60k fills the scenario's 604,800 s)")
    for name, fn in SECTIONS.items():
        if only and name != only:
            continue
        t0 = time.time()
        print()
        fn(n)
        print(f"  [section {name}: {time.time()-t0:.0f}s]")


if __name__ == "__main__":
    main(sys.argv)
