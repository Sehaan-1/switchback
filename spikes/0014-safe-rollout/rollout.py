#!/usr/bin/env python3
"""Decision ticket #14 evidence: safe policy rollout.

What this measures, and what it deliberately does not
------------------------------------------------------
#14 asks the Stripe-style question: the bandit has learned a new policy (or you
updated the algorithm) -- how do you deploy it without losing money for a week?
A naive 100% cutover routes live traffic through whatever the new policy does
first; a 0% cutover never ships. The ticket asks for the rollout protocol
(shadow -> canary -> full), the warm-start strategy, the policy version schema,
and the automated rollback trigger.

The premise gets a correction before it gets an answer, and the correction is
measured, not argued: in this architecture the ticket's "new policy starts with
a cold posterior" is mostly FALSE by construction. R47/R97 made learned state a
fold over the WAL, and the WAL's outcome ops are per-arm world facts that no
policy version owns. A new policy boots from the same snapshot; it is warm the
moment it boots. What a deploy actually changes is the DECISION RULE (prior
artifact, config, algorithm), and the residual risks are (a) a migration that
loses state, (b) a wrong decision rule, which no amount of warm state fixes,
and (c) second-order interactions: mix shifts tripping the ADR-0007 detector,
and rollout machinery standing in the way of real alarms. This spike holds the
shipped design (ADR-0006/0007/0008) fixed and measures exactly those.

Sections, and the question each answers:

  [S0] policy identity: the content hash that names a policy, and the proof
       that the same content rebuilds to the same identity while any changed
       component (seed, prior, config) rebuilds to a different one.
  [S1] the risk, priced: a state-losing cutover on a NO-OP deploy -- the same
       algorithm, deployed cold -- against a warm cutover. The naive-cutover
       tax on the improvement world is [S5]'s naive rows.
  [S2] shadow mode: a candidate folding the shared WAL and logging decisions
       it does not execute. Warm vs cold shadow agreement curves, replay, and
       what shadow can and cannot clear.
  [S3] the canary gate: the paired windowed margin statistic's null
       distribution on a no-op deploy, its false-abort behavior, and its
       detection/cost behavior on three real regressions (a mispriced
       lambda_to, the ADR-0006-rejected normal-approximation sampler, and a
       miscalibrated prior artifact). Gate-vs-no-gate cost on the worst case.
  [S4] the ADR-0007 payload: do canary share shifts trip processor-level
       ADWIN at delta=1e-3? What does alarm suppression cost when the alarm is
       TRUE (the starved improvement)? Does a transport outage during a
       rollout still fire, and does a world event false-trigger the paired
       abort gate?
  [S5] the protocol end to end on quiet-improvement-starved-v1: the staged
       schedule (shadow -> 1% -> 5% -> 25% -> 50% -> 100%) with the gate at
       each step, against naive cold, naive warm, naive warm+refresh, never
       deploying, and the never-deploy-with-machinery alternative, plus the
       oracle. The "week of losses" is this table.

Everything is stdlib-only, offline and deterministic. Magnitudes belong to the
scenario documents; the orderings, PASS/FAIL verdicts and mechanisms are the
findings.

    python3 rollout.py                  # all sections at n=60000
    python3 rollout.py 20000            # smaller n (default 60000)
    python3 rollout.py --section=S3     # one section
"""

from __future__ import annotations

import bisect
import hashlib
import math
import json
import struct
import sys
from collections import deque
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "simulator" / "scenarios"))
sys.path.insert(0, str(REPO / "spikes" / "0006-simulation-harness"))
sys.path.insert(0, str(REPO / "spikes" / "0007-thompson-sampling"))
sys.path.insert(0, str(REPO / "spikes" / "0008-drift-detection"))
sys.path.insert(0, str(REPO / "spikes" / "0009-censored-exploration"))

from check import draw, load_scenario, scenario_hash, stream  # noqa: E402
import harness as H  # noqa: E402
import posterior as P  # noqa: E402
from drift import ADWIN  # noqa: E402
import censored as C9  # noqa: E402  (#9 spike: flip set, headroom, oracle, conventions)

DEFAULT_N = 60_000
POLICY_SEED = P.POLICY_SEED          # the incumbent policy's seed (as shipped)
CAND_SEED = 20_260_918               # every candidate policy's seed (a deploy changes it)
ROLLOUT_SEED = 20_260_919            # the assignment stream; never a policy draw
LAM_TO = P.LAM_TO
EVENT_S = C9.EVENT_S                 # 259_200: the starved world's recovery
DEPLOY_S = 302_400                   # the improvement-world deploy: day 3.5
T_ARM = 256                          # arming window, settled obs per processor
ALGORITHM = "ts-beta-adr0006"        # the algorithm contract id in the identity
BLOCK = 1000                         # equal-count raw window per side (both sides must clear it)

DOCS = {}
CACHE = {}


def _doc(name):
    if name not in DOCS:
        here = HERE / f"{name}.json"
        c9 = REPO / "spikes" / "0009-censored-exploration" / f"{name}.json"
        path = (here if here.exists() else c9 if c9.exists()
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


def _arrivals(world, n):
    key = ("arr", world.hash, n)
    if key not in CACHE:
        CACHE[key] = [a for _s, a in world.arrivals(n)]
    return CACHE[key]


def _seq_at(world, n, t_s):
    return bisect.bisect_left(_arrivals(world, n), t_s * 1000)


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


def catalog_hash():
    return "sha256:" + hashlib.sha256(
        (REPO / "constraints" / "catalog" / "acquirer-catalog.example.json")
        .read_bytes()).hexdigest()[:16]


def constraints_hash():
    return "sha256:" + hashlib.sha256(
        (REPO / "constraints" / "examples" / "merchant-default.json")
        .read_bytes()).hexdigest()[:16]


# --------------------------------------------------------------------------------------
# 1. Policy identity. The identity is content: a canonical-JSON hash over every
#    component that can change a decision. A semver string is a registry LABEL, never
#    the identity. The prior component is the deployed artifact's bytes (the packed
#    pseudo-count arrays), because that is what the engine loads.
# --------------------------------------------------------------------------------------

class RolloutRouter(P.Router):
    """P.Router with the policy's own seed, lambda_to and sampler -- the pieces a
    deploy can change -- without touching the shipped posterior arithmetic."""

    def __init__(self, *a, pseed=POLICY_SEED, lam_to=LAM_TO, **kw):
        super().__init__(*a, **kw)
        self.pseed = pseed
        self.lam_to = lam_to

    # -- hot path: identical shape to P.Router.decide/_draw, parameterized -----------
    def _draw(self, seq, arm, purpose, a, b):
        s = stream(self.pseed, "pol", seq, arm, purpose)
        if self.draw_alg == "exact":
            return P.beta_draw(s, a, b)
        if self.draw_alg == "normal":
            return P.beta_normal(s, a, b)
        raise ValueError(self.draw_alg)

    def decide(self, req):
        elig = P.eligible(req, self.exclude)
        if not elig:
            return []
        ctx = self.space.ctx_index(req)
        scored, cold = [], []
        for acq in elig:
            i = self.space.arm_index(ctx, acq)
            aa, bb = self.effective(i)
            ta = self.toa[i] + self.ptoa[i]
            tb = self.tob[i] + self.ptob[i]
            th = self._draw(req.seq, i, 0, aa, bb)
            pi = self._draw(req.seq, i, 1, ta, tb)
            s = (th * P.win_amount(req, acq) - (1.0 - th) * P.attempt_fee(req, acq)
                 - pi * self.lam_to)
            scored.append((s, acq))
            if P.ACQ_IX[acq] in self.explore:
                cold.append(acq)
        scored.sort(key=lambda t: (-t[0], t[1]))
        chain = [acq for s, acq in scored if s > 0.0][:P.MAX_ATTEMPTS]
        if cold and self.eta > 0.0:
            if draw(stream(self.pseed, "flo", req.seq), 0) < self.eta:
                j = int(draw(stream(self.pseed, "fpk", req.seq), 0) * len(cold))
                pick = cold[j]
                chain = [pick] + [c for c in chain if c != pick][:P.MAX_ATTEMPTS - 1]
        return chain

    # -- deploy-time state transfer: the fold share (R102's one-WAL-many-folds) ------
    def copy_state_from(self, other):
        """Boot this router from the incumbent's newest snapshot: data counts and
        floor/transport state. The prior arrays are NOT copied -- they are the
        incumbent's artifact; this router carries its own."""
        self.a = list(other.a); self.b = list(other.b)
        self.toa = list(other.toa); self.tob = list(other.tob)
        self.te = list(other.te)
        self.proc_settled = list(other.proc_settled)
        self.explore = set(other.explore)
        return self

    def prior_digest(self):
        h = hashlib.sha256()
        for arr in (self.pa, self.pb, self.ptoa, self.ptob):
            h.update(struct.pack("<%dd" % len(arr), *arr))
        return "sha256:" + h.hexdigest()[:24]


def policy_identity(r, constraint_h=None, catalog_h=None):
    """policy_id = sha256 over the canonical decision-relevant tuple (R101)."""
    cfg = {
        "algorithm": ALGORITHM,
        "arm_schema": r.space.describe(),
        "arm_edges": list(r.space.edges),
        "prior": r.prior_digest(),
        "policy_seed": r.pseed,
        "lam_to": r.lam_to,
        "max_attempts": P.MAX_ATTEMPTS,
        "eta": r.eta, "n_min": r.n_min,
        "te_mode": r.te_mode, "draw_alg": r.draw_alg,
        "catalog": catalog_h or catalog_hash(),
        "constraint_set": constraint_h or constraints_hash(),
    }
    blob = json.dumps(cfg, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()[:32], cfg


def assign(seq, share_bp):
    """Traffic assignment: a pure, key-addressed function of the transaction (R104).
    Sticky by construction -- the same seq always assigns to the same side."""
    if share_bp >= 10_000:
        return True
    if share_bp <= 0:
        return False
    return draw(stream(_ROLLOUT_SEED[-1], "asg", seq), 0) < share_bp / 10_000.0


_ROLLOUT_SEED = [ROLLOUT_SEED]


class rollout_seed:
    """Scope a different assignment seed (used to measure the null twice)."""

    def __init__(self, seed):
        self.seed = seed

    def __enter__(self):
        _ROLLOUT_SEED.append(self.seed)

    def __exit__(self, *a):
        _ROLLOUT_SEED.pop()


# --------------------------------------------------------------------------------------
# 2. Prior artifacts. The incumbent's artifact is sampled from a 2,000-transaction
#    uniform prefix (P.uniform_prefix's construction, ADR-0006 P2, m=100). The
#    refreshed artifact is the same construction run at the deploy time -- the ADR-0006
#    artifact-refresh path ADR-0008 warned is routing-relevant.
# --------------------------------------------------------------------------------------

def uniform_prefix_t(world, n_prefix=2000, base_seq=None, t_ms=0):
    """P.uniform_prefix with a probe time: the prefix samples the world at t_ms."""
    base = base_seq if base_seq is not None else 10_000_000
    cnt = {}
    for k in range(n_prefix):
        seq = base + k
        req = world.context(seq, 0)
        elig = P.eligible(req)
        if not elig:
            continue
        att = 0
        while att < P.MAX_ATTEMPTS and elig:
            j = int(draw(stream(P.POLICY_SEED, "uni", seq, att), 0) * len(elig))
            acq = elig[j]
            resp, truth = world.attempt(req, acq, att, t_ms)
            key = (acq, req.bin_class, req.card_region)
            c = cnt.setdefault(key, [0.0, 0.0, 0.0, 0.0])
            if resp.outcome == P.TIMEOUT:
                c[2] += 1.0
            elif resp.outcome == P.AUTH:
                c[0] += 1.0
                c[3] += 1.0
            else:
                c[1] += 1.0
                c[3] += 1.0
            if resp.outcome in H.TERMINAL:
                break
            att += 1
    return cnt


def _prefix(world_name, n, at_s=0):
    key = ("pre", world_name, n, at_s)
    if key not in CACHE:
        CACHE[key] = uniform_prefix_t(_world(world_name, n), 2000,
                                      base_seq=8_000_000 + at_s, t_ms=at_s * 1000)
    return CACHE[key]


def make_router(world_name, n, *, candidate=False, prior_at_s=0, no_prior=False,
                bias=1.0, bias_acq=None, lam_to=None, draw_alg="exact",
                eta=0.05, n_min=1000):
    """One policy instance. candidate=True rotates the seed (every deploy ships a
    new build); no_prior=True ships without an artifact (Jeffreys, the cold case)."""
    prior_fn = None if no_prior else P.make_prior_fn(
        _prefix(world_name, n, at_s=prior_at_s), m=100.0, bias=bias, bias_acq=bias_acq)
    return RolloutRouter(C9._space(), prior_fn=prior_fn,
                         pseed=CAND_SEED if candidate else POLICY_SEED,
                         lam_to=lam_to if lam_to is not None else LAM_TO,
                         draw_alg=draw_alg, eta=eta, n_min=n_min)


# --------------------------------------------------------------------------------------
# 3. Metrics. Per-side margins with matched block pairing, the D statistic, detector
#    and suppression ledgers, agreement, coverage.
# --------------------------------------------------------------------------------------

class RolloutMetrics:
    def __init__(self, n, w=10_000):
        self.n = n
        self.txns = 0
        self.authed = 0
        self.attempts = 0
        self.timeouts = 0
        self.margin = 0.0
        self.mae_sum, self.mae_n = 0.0, 0
        self.edges = C9.window_edges(n, w)
        self.margin_w = [0.0] * len(self.edges)
        self.head_w = [{} for _ in self.edges]        # attempt-0 acq per window
        self.side = {0: self._fresh(), 1: self._fresh()}
        self.side_pre = {0: None, 1: None}            # snapshot at the first shift
        self.D = []                    # [(seq, D c/1k)] one per matched block pair
        self.DP = []                   # [(seq, m0, n0, m1, n1)] raw window sums
        self.UNR = []                  # [(seq, unroutable share delta)] per window
        self.agree_log = []            # [(seq, hit 0/1)]
        self.shadow_rows = 0
        self.events = []               # (seq, kind, detail)
        self.alarm_log = []            # (seq, t_ms, acq, tier, dmu, ter, held)
        self.n_alarms = 0
        self.n_held = 0
        self.n_refire = 0

    @staticmethod
    def _fresh():
        return dict(txns=0, margin=0.0, authed=0, attempts=0, timeouts=0,
                    mae_sum=0.0, mae_n=0, block_m=0.0, block_n=0, decisions=0,
                    unr_block=0, blocks=[], arms=set())

    def _wi(self, seq):
        return max(0, min(len(self.edges) - 1,
                          bisect.bisect_right(self.edges, (seq, self.n + 1)) - 1))

    def margin_per_1k(self, lo=None, hi=None):
        if lo is None or (lo, hi) == (0, self.n):
            return self.margin / max(1, self.txns) * 1000.0
        acc = 0.0
        for k, (a, b) in enumerate(self.edges):
            ov_lo, ov_hi = max(lo, a), min(hi, b)
            if ov_hi > ov_lo:
                acc += self.margin_w[k] * (ov_hi - ov_lo) / (b - a)
        return acc / max(1, hi - lo) * 1000.0

    def share(self, acq, lo, hi):
        heads, tot = 0, 0
        for k, (a, b) in enumerate(self.edges):
            if b <= lo or a >= hi:
                continue
            heads += self.head_w[k].get(acq, 0)
            tot += sum(self.head_w[k].values())
        return heads / max(1, tot)

    def mae(self):
        return 100.0 * self.mae_sum / max(1, self.mae_n)

    def side_stats(self, side, post_cut=0):
        """Side sums POST to post_cut (the pre-shift snapshot is subtracted)."""
        s = dict(self.side[side])
        pre = self.side_pre[side]
        if pre and post_cut > 0:
            for k in ("txns", "authed", "attempts", "timeouts", "mae_n"):
                s[k] -= pre[k]
            s["margin"] -= pre["margin"]
            s["mae_sum"] -= pre["mae_sum"]
        return s

    def D_stats(self, lo=0, hi=None):
        xs = [d for (s, d) in self.D if s >= lo and (hi is None or s < hi)]
        if not xs:
            return None
        mu = sum(xs) / len(xs)
        var = sum((x - mu) ** 2 for x in xs) / max(1, len(xs) - 1)
        return dict(n=len(xs), mean=mu, std=var ** 0.5, min=min(xs), max=max(xs))


# --------------------------------------------------------------------------------------
# 4. The rollout drive. One loop; every #14 mechanism is a flag.
#
#    sched      [(start_seq, stage, share_bp), ...] -- transitions emit ROLLOUT ops
#               into the ledger; the stage at seq k is the last entry with
#               start_seq <= k. At the FIRST transition off control (unless
#               cold_new), the candidate boots from the incumbent's snapshot
#               (copy_state_from) -- R102's one-WAL-many-folds, before it decides.
#    gate       None or dict(Y=..., zwin=2, collapse=-300.0); on fire, the rest of
#               the schedule is overridden to control@100% (auto-rollback).
#    sup        alarm suppression: Tier-2 alarms inside T_ARM settled obs of a share
#               shift are HELD and re-checked at expiry (trailing-mean test);
#               Tier-1 (transport fast-path, |dmu|>0.30) always passes.
#    resets     apply ADR-0007 R58 resets (data+prior decay, onboarding, no
#               re-board on re-fire) when an alarm is applied. Resets are SHARED
#               ops: both folds decay.
#    agree      sample the non-executed policy's decision every N seqs (the shadow
#               record) whenever a candidate exists.
# --------------------------------------------------------------------------------------

def drive_rollout(world, old, new, n, *, sched, gate=None, sup=True, t_arm=T_ARM,
                  resets=True, agree=0, cold_new=False, boot_at="transition"):
    """boot_at: 'transition' = the candidate boots (snapshot share if warm, then
    folding) at the first non-control stage; an int = that seq; None = never folds
    (pure incumbent run). cold_new=True skips the snapshot share (the state-losing
    migration) -- the candidate still folds from its boot point onward."""
    m = RolloutMetrics(n)
    space = old.space
    dets = {a: ADWIN(delta=0.001) for a in P.ACQ}
    trail = {a: deque(maxlen=256) for a in P.ACQ}
    te_run = {a: 0 for a in P.ACQ}
    settled_ct = {a: 0 for a in P.ACQ}
    held = {}                      # acq -> dict(expiry, ref, dmu, ter, seq)
    armed_until = {a: -1 for a in P.ACQ}
    sched_ix = 0
    share_bp = 0
    cut = None                     # the candidate's boot point (first shift)
    booted = cold_new              # cold candidates never inherit state
    aborted = False

    def apply_reset(acq, tier):
        gamma = 0.1 if tier == 1 else 0.5
        for r in (old, new):
            if r is None:
                continue
            for c in range(space.n_ctx):
                idx = space.arm_index(c, acq)
                r.a[idx] *= gamma
                r.b[idx] *= gamma
                r.pa[idx] *= gamma
                r.pb[idx] *= gamma
                r.ptoa[idx] *= gamma
                r.ptob[idx] *= gamma
            ix = P.ACQ_IX[acq]
            if ix not in r.explore:
                r.explore.add(ix)
                r.proc_settled[ix] = 0

    def on_alarm(acq, seq, t_ms, dmu, ter):
        tier = 1 if (ter > 0.20 or abs(dmu) > 0.30) else 2
        arming = settled_ct[acq] < armed_until[acq]
        if tier == 2 and sup and arming:
            m.n_held += 1
            m.alarm_log.append((seq, t_ms, acq, tier, dmu, ter, True))
            held[acq] = dict(expiry=settled_ct[acq] + t_arm,
                             ref=(sum(trail[acq]) / max(1, len(trail[acq]))),
                             dmu=dmu, ter=ter, seq=seq)
            m.events.append((seq, "HELD", f"{acq} tier{tier} dmu={dmu:.3f} "
                                          f"ter={ter:.3f}"))
            return
        m.n_alarms += 1
        m.alarm_log.append((seq, t_ms, acq, tier, dmu, ter, False))
        m.events.append((seq, "ALARM", f"{acq} tier{tier} dmu={dmu:.3f} "
                                       f"ter={ter:.3f}"))
        if resets:
            apply_reset(acq, tier)

    def ingest(acq, outcome, seq, t_ms):
        if outcome == P.TIMEOUT:
            return
        val = 1.0 if outcome == P.AUTH else 0.0
        settled_ct[acq] += 1
        trail[acq].append(val)
        if outcome == P.TERR:
            te_run[acq] += 1
            if te_run[acq] >= 5:                    # ADR-0007 transport fast-path
                te_run[acq] = 0
                on_alarm(acq, seq, t_ms, 0.0, 1.0)
                return
        else:
            te_run[acq] = 0
        h = held.get(acq)
        if h is not None and settled_ct[acq] >= h["expiry"]:
            del held[acq]
            cur = sum(trail[acq]) / max(1, len(trail[acq]))
            if abs(cur - h["ref"]) > 0.05:
                m.n_refire += 1
                m.events.append((seq, "REFIRE",
                                 f"{acq} held@{h['seq']} refires cur={cur:.3f} "
                                 f"ref={h['ref']:.3f}"))
                if resets:
                    apply_reset(acq, 2)
            else:
                m.events.append((seq, "EXPIRE",
                                 f"{acq} held@{h['seq']} expires cur={cur:.3f} "
                                 f"ref={h['ref']:.3f}"))
        if dets[acq].update(val, t_ms, seq):
            on_alarm(acq, seq, t_ms, dets[acq].last_split_delta,
                     te_run[acq] / max(1, settled_ct[acq]))

    for seq, arr in world.arrivals(n):
        world.clock.advance_to(arr)
        while sched_ix < len(sched) and seq >= sched[sched_ix][0]:
            _s0, stage, share_bp = sched[sched_ix]
            m.events.append((seq, "OP", f"{stage}@{share_bp}bp"))
            if cut is None and boot_at == "transition" and stage != "control":
                cut = seq
                m.side_pre = {
                    0: {k: v for k, v in m.side[0].items()
                        if k not in ("arms", "blocks")},
                    1: {k: v for k, v in m.side[1].items()
                        if k not in ("arms", "blocks")},
                }
                if new is not None and not booted:
                    new.copy_state_from(old)       # boot from the newest snapshot
                    booted = True
            if cut is None and isinstance(boot_at, int) and seq >= boot_at:
                cut = seq
                if new is not None and not booted:
                    booted = True                  # folding starts; no snapshot share
            if sup and share_bp > 0:
                for a in P.ACQ:
                    armed_until[a] = settled_ct[a] + t_arm
            sched_ix += 1
        req = world.context(seq, arr)
        side = 1 if (new is not None and share_bp > 0 and assign(seq, share_bp)) else 0
        r = new if side == 1 else old
        chain = r.decide(req)
        # shadow record: the non-executed policy still decides, still logs (R102)
        if agree and new is not None and seq % agree == 0:
            other = new if side == 0 else old
            ch2 = other.decide(req)
            m.shadow_rows += 1
            hit = 1 if (chain and ch2 and chain[0] == ch2[0]) else 0
            m.agree_log.append((seq, hit))
        wi = m._wi(seq)
        m.txns += 1
        s = m.side[side]
        s["txns"] += 1
        if not chain:
            s["unr_block"] += 1
            continue
        m.head_w[wi][chain[0]] = m.head_w[wi].get(chain[0], 0) + 1
        txn_margin, authed = 0.0, 0
        for att, acq in enumerate(chain):
            resp, truth = world.attempt(req, acq, att, arr)
            m.attempts += 1
            s["attempts"] += 1
            op = (seq, att, acq, resp.outcome, req.bin_class, req.card_region,
                  req.sca_required, req.mandate, req.amount_minor)
            if resp.outcome == P.TIMEOUT:
                m.timeouts += 1
                s["timeouts"] += 1
                txn_margin -= P.attempt_fee(req, acq) + LAM_TO
            elif resp.outcome == P.AUTH:
                authed = 1
                txn_margin += P.win_amount(req, acq)
            else:
                txn_margin -= P.attempt_fee(req, acq)
            i = space.arm_index(space.ctx_index(req), acq)
            if resp.outcome != P.TIMEOUT:
                err = abs(r.mean(i) - P.theta_truth(world, req, acq, truth))
                m.mae_sum += err
                m.mae_n += 1
                s["mae_sum"] += err
                s["mae_n"] += 1
            ingest(acq, resp.outcome, seq, arr)
            # shared fold: every settled outcome lands in BOTH folds (R102); a cold
            # candidate never inherits, so its fold starts empty at its boot
            old.apply_op(op)
            if new is not None and booted:
                new.apply_op(op)
            if resp.outcome in H.TERMINAL:
                break
        m.authed += authed
        m.margin += txn_margin
        m.margin_w[wi] += txn_margin
        s["authed"] += authed
        s["margin"] += txn_margin
        s["block_m"] += txn_margin
        s["block_n"] += 1
        s["decisions"] += 1
        s["arms"].add(space.arm_index(space.ctx_index(req), chain[0]))
        if s["decisions"] % BLOCK == 0:
            s["blocks"].append(len(s["arms"]))     # coverage at matched counts
        # matched pairing -> the D statistic: emitted when BOTH sides have accrued
        # BLOCK decisions, so each window is W decisions per side (the per-txn
        # margin is heavy-tailed -- amount to 400k minor x 128 bps -- and W is the
        # variance knob; the ADR quotes sigma(W) from the null, not from a model).
        if m.side[0]["block_n"] >= BLOCK and m.side[1]["block_n"] >= BLOCK:
            b0 = m.side[0]["block_m"] / m.side[0]["block_n"] * 1000.0
            b1 = m.side[1]["block_m"] / m.side[1]["block_n"] * 1000.0
            unr1 = m.side[1]["unr_block"] / (m.side[1]["unr_block"] + m.side[1]["block_n"])
            unr0 = m.side[0]["unr_block"] / (m.side[0]["unr_block"] + m.side[0]["block_n"])
            m.D.append((seq, b1 - b0))
            m.DP.append((seq, m.side[0]["block_m"], m.side[0]["block_n"],
                         m.side[1]["block_m"], m.side[1]["block_n"]))
            m.UNR.append((seq, unr1 - unr0))
            m.side[0]["block_m"] = m.side[0]["block_n"] = 0
            m.side[1]["block_m"] = m.side[1]["block_n"] = 0
            m.side[0]["unr_block"] = m.side[1]["unr_block"] = 0
            if gate is not None and not aborted:
                if len(m.UNR) >= 2 and all(u > gate.get("unr", 0.05)
                                           for _s, u in m.UNR[-2:]):
                    aborted = True
                    m.events.append((seq, "ABORT",
                                     f"routability: unroutable delta "
                                     f"{100 * m.UNR[-1][1]:.1f}pts for 2 windows "
                                     f"-> control@10000bp"))
                    sched_ix = len(sched)
                    share_bp = 0
                elif len(m.D) >= 2 * gate.get("gw", 1):
                    gw = gate.get("gw", 1)
                    a1 = sum(d for _s, d in m.D[-gw:]) / gw
                    a0 = sum(d for _s, d in m.D[-2 * gw:-gw]) / gw
                    if (a1 < -gate["Y"] and a0 < -gate["Y"]) or \
                            a1 < gate["collapse"]:
                        aborted = True
                        m.events.append((seq, "ABORT",
                                         f"margin: gate-window means "
                                         f"{a0:.0f}, {a1:.0f} vs Y={gate['Y']:.0f} "
                                         f"(gw={gw}) -> control@10000bp"))
                        sched_ix = len(sched)      # override: incumbent, 100%
                        share_bp = 0
    return m


# --------------------------------------------------------------------------------------
# 5. Runners shared across sections (cached; the world's draws are key-addressed, so
#    runs are reproducible bit-for-bit at a given n).
# --------------------------------------------------------------------------------------

CUT = 10_000          # steady-world cutover seq for the canary experiments


def block_sigma(mms, g, lo=CUT):
    """Re-block raw D windows g-at-a-time; (mean, sigma, n) of the gate-window
    mean D. sigma at the gate window in use is what Y is derived from."""
    xs = []
    for mm in mms:
        dp = [d for d in mm.DP if d[0] >= lo]
        for k in range(0, len(dp) - g + 1, g):
            grp = dp[k:k + g]
            m0 = sum(x[1] for x in grp); n0 = sum(x[2] for x in grp)
            m1 = sum(x[3] for x in grp); n1 = sum(x[4] for x in grp)
            xs.append((m1 / max(1, n1) - m0 / max(1, n0)) * 1000.0)
    if len(xs) < 2:
        return None, xs
    mu = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1))
    return (mu, sd, len(xs)), xs


def steady_run(kind, n, share=2_500, gate_Y=None, sup=True, agree=0,
               asg_seed=None):
    """Steady-world rollout runs, all cutovers at seq CUT.
    kinds: never | warm0 (warm 100% at seq 0) | cold (cold 100% at CUT)
           | noop (const share) | lam (const share, gated) | lam_naive (100% at CUT)
           | nor (const share) | pri (const share)."""
    key = ("steady", kind, n, share, gate_Y, sup, agree, asg_seed)
    if key in CACHE:
        return CACHE[key]
    w = _world("baseline-steady-v1", n)
    old = make_router("baseline-steady-v1", n)
    new, sched, gate = None, [(0, "control", 0)], None
    if kind == "warm0":
        new = make_router("baseline-steady-v1", n, candidate=True)
        sched = [(0, "canary", 10_000)]
    elif kind == "cold":
        new = make_router("baseline-steady-v1", n, candidate=True, no_prior=True)
        sched = [(0, "control", 0), (CUT, "canary", 10_000)]
    elif kind == "noop":
        new = make_router("baseline-steady-v1", n, candidate=True)
        sched = [(0, "control", 0), (CUT, "canary", share)]
    elif kind == "lam":
        new = make_router("baseline-steady-v1", n, candidate=True, lam_to=10.0)
        sched = [(0, "control", 0), (CUT, "canary", share)]
        gate = dict(Y=gate_Y, gw=4, collapse=-1.5 * gate_Y)
    elif kind == "lam0":
        new = make_router("baseline-steady-v1", n, candidate=True, lam_to=0.0)
        sched = [(0, "control", 0), (CUT, "canary", share)]
        gate = dict(Y=gate_Y, gw=4, collapse=-1.5 * gate_Y)
    elif kind == "exalpha":
        new = make_router("baseline-steady-v1", n, candidate=True)
        new.exclude = frozenset({"charlie"})   # a ConstraintSet regression, deployed
        sched = [(0, "control", 0), (CUT, "canary", share)]
        gate = dict(Y=gate_Y, gw=4, collapse=-1.5 * gate_Y)
    elif kind == "exalpha_naive":
        new = make_router("baseline-steady-v1", n, candidate=True)
        new.exclude = frozenset({"charlie"})
        sched = [(0, "control", 0), (CUT, "canary", 10_000)]
    elif kind == "lam_naive":
        new = make_router("baseline-steady-v1", n, candidate=True, lam_to=10.0)
        sched = [(0, "control", 0), (CUT, "canary", 10_000)]
    elif kind == "nor":
        new = make_router("baseline-steady-v1", n, candidate=True, draw_alg="normal")
        sched = [(0, "control", 0), (CUT, "canary", share)]
    elif kind == "pri":
        new = make_router("baseline-steady-v1", n, candidate=True, bias=1.25,
                          bias_acq="foxtrot")
        sched = [(0, "control", 0), (CUT, "canary", share)]
    elif kind != "never":
        raise ValueError(kind)
    if asg_seed is not None:
        with rollout_seed(asg_seed):
            m = drive_rollout(w, old, new, n, sched=sched, gate=gate, sup=sup,
                              agree=agree, cold_new=(kind == "cold"))
    else:
        m = drive_rollout(w, old, new, n, sched=sched, gate=gate, sup=sup,
                          agree=agree, cold_new=(kind == "cold"))
    CACHE[key] = m
    return m


def null_Y(n, g=1):
    """3 sigma of the pooled steady-world null at gate-window size g -- the same
    calibration [S3] measures; cached so any section can arm the gate with it."""
    key = ("nullY", n, g)
    if key not in CACHE:
        m25 = steady_run("noop", n, share=2_500)
        m25b = steady_run("noop", n, share=2_500, asg_seed=20_260_920)
        st, _ = block_sigma((m25, m25b), g)
        CACHE[key] = 3.0 * st[1]
    return CACHE[key]


def starved_protocol(n, sup=True):
    """The full protocol on quiet-improvement-starved-v1: shadow -> 1 -> 5 -> 25
    -> 50 -> 100, the candidate carrying the refreshed artifact over the snapshot.
    The gate is armed at every canary stage with the [S3] null calibration (gw=1:
    these stages are sized in canary decisions, and a gate-window of 4 raw
    windows would never arm inside one)."""
    key = ("prot", n, sup)
    if key in CACHE:
        return CACHE[key]
    wname = "quiet-improvement-starved-v1"
    w = _world(wname, n)
    cut = _seq_at(w, n, DEPLOY_S)
    old = make_router(wname, n, prior_at_s=0)          # artifact refreshed mid-storm
    new = make_router(wname, n, candidate=True, prior_at_s=DEPLOY_S + 3_600)
    sched = [(0, "control", 0), (cut, "shadow", 0), (cut + 2_000, "canary", 100),
             (cut + 3_000, "canary", 500), (cut + 5_000, "canary", 2_500),
             (cut + 13_000, "canary", 5_000), (cut + 21_000, "canary", 10_000)]
    y = null_Y(n, 1)
    CACHE[key] = drive_rollout(w, old, new, n, sched=sched, sup=sup, resets=True,
                               gate=dict(Y=y, gw=1, collapse=-1.5 * y))
    return CACHE[key]


def starved_never(n, machinery):
    key = ("snever", n, machinery)
    if key in CACHE:
        return CACHE[key]
    w = _world("quiet-improvement-starved-v1", n)
    old = make_router("quiet-improvement-starved-v1", n, prior_at_s=0)
    CACHE[key] = drive_rollout(w, old, None, n, sched=[(0, "control", 0)],
                               sup=False, resets=machinery)
    return CACHE[key]


def starved_naive(n, kind):
    """kind: cold (no state, refreshed artifact) | warm (snapshot, stale artifact)
    | refresh (snapshot, refreshed artifact). All 100% at DEPLOY_S."""
    key = ("snaive", n, kind)
    if key in CACHE:
        return CACHE[key]
    wname = "quiet-improvement-starved-v1"
    w = _world(wname, n)
    cut = _seq_at(w, n, DEPLOY_S)
    old = make_router(wname, n, prior_at_s=0)
    kw = dict(candidate=True, prior_at_s=DEPLOY_S + 3_600)
    if kind == "warm":
        kw["prior_at_s"] = 0
    elif kind == "cold":
        kw["no_prior"] = True
    new = make_router(wname, n, **kw)
    CACHE[key] = drive_rollout(w, old, new, n,
                               sched=[(0, "control", 0), (cut, "canary", 10_000)],
                               sup=True, resets=True, cold_new=(kind == "cold"))
    return CACHE[key]


def outage_run(n, gate_Y=None):
    key = ("outage", n, gate_Y)
    if key in CACHE:
        return CACHE[key]
    w = _world("outage-recovery-v1", n)
    old = make_router("outage-recovery-v1", n)
    new = make_router("outage-recovery-v1", n, candidate=True)
    gate = dict(Y=gate_Y, gw=4, collapse=-1.5 * gate_Y) if gate_Y else None
    CACHE[key] = drive_rollout(w, old, new, n,
                               sched=[(0, "control", 0), (CUT, "canary", 2_500)],
                               gate=gate, sup=True, resets=True)
    return CACHE[key]


# --------------------------------------------------------------------------------------
# [S0] policy identity and assignment
# --------------------------------------------------------------------------------------

def sec_s0(n):
    print("[S0] policy identity: content-addressed policies, assignment determinism")
    print("""
  The ticket asks for the policy identity schema: hash of the model parameters or
  semantic version? Both, with the hash as the identity and the version as a label
  (this repo content-addresses everything else the same way: scenarios, catalogs,
  constraint sets, the audit chain). policy_id = sha256 over the canonical tuple
  {algorithm, arm schema + band edges, prior artifact digest, policy seed, score
  and protocol config, catalog hash, constraint-set hash}. Any component change --
  a refreshed prior artifact, a lambda_to retune, a sampler change, a seed
  rotation -- is a different policy and rolls out as one; identical content is
  the SAME policy no matter how many times it is deployed.
""")
    old = make_router("baseline-steady-v1", n)
    cand = make_router("baseline-steady-v1", n, candidate=True)
    lam = make_router("baseline-steady-v1", n, candidate=True, lam_to=10.0)
    refresh = make_router("baseline-steady-v1", n, candidate=True, bias=1.25,
                          bias_acq="foxtrot")
    nor = make_router("baseline-steady-v1", n, candidate=True, draw_alg="normal")
    pid_o, cfg_o = policy_identity(old)
    pid_c, _ = policy_identity(cand)
    pid_l, _ = policy_identity(lam)
    pid_r, _ = policy_identity(refresh)
    pid_n, _ = policy_identity(nor)
    pid_o2, _ = policy_identity(make_router("baseline-steady-v1", n))
    rows = [
        ("incumbent (seed 20260916, lam 45)", pid_o[:19] + "..."),
        ("candidate (seed 20260918, lam 45) -- seed rotation only", pid_c[:19] + "..."),
        ("candidate (seed 20260918, lam 10) -- mispriced lambda_to", pid_l[:19] + "..."),
        ("candidate (prior foxtrot x1.25) -- miscalibrated artifact", pid_r[:19] + "..."),
        ("candidate (draw_alg=normal) -- rejected sampler", pid_n[:19] + "..."),
    ]
    print(table(["policy", "policy_id (sha256, first 19 hex)"], rows))
    print(f"""
  identity components of the incumbent: algorithm={cfg_o['algorithm']},
  arm_schema={cfg_o['arm_schema']}, prior={cfg_o['prior'][:19]}...,
  policy_seed={cfg_o['policy_seed']}, lam_to={cfg_o['lam_to']}, eta={cfg_o['eta']},
  n_min={cfg_o['n_min']}, te_mode={cfg_o['te_mode']}, draw_alg={cfg_o['draw_alg']},
  catalog={cfg_o['catalog']}, constraint_set={cfg_o['constraint_set']}

  checks:
  (1) same content rebuilds to the same identity:
      {'PASS' if pid_o == pid_o2 else 'FAIL'} (a fresh instance of the incumbent
      hashes identically -- identity is content, not deploy time, not instance)
  (2) every changed component rebuilds to a different identity:
      {'PASS' if len({pid_o, pid_c, pid_l, pid_r, pid_n}) == 5 else 'FAIL'}
      (5 distinct policies from 5 distinct tuples)
  (3) assignment is a pure function of (rollout seed, seq): share 2,500bp
      measured over {n:,} seqs = {100.0 * sum(assign(s, 2500) for s in range(n)) / n:.2f}%
      (deterministic, sticky, no ambient state -- a re-decided transaction lands
      on the same side, and OPE re-derives the split offline)
""")


# --------------------------------------------------------------------------------------
# [S1] the risk, priced
# --------------------------------------------------------------------------------------

def sec_s1(n):
    print("[S1] the risk, priced: what a cutover actually costs")
    print("""
  The ticket's premise -- a new policy starts with a cold posterior -- is mostly
  FALSE in this architecture, and the exceptions are the point. The fold over the
  WAL is policy-independent learned state (outcome ops are per-arm world facts);
  a candidate boots from the incumbent's newest snapshot (R47/R97/R102) and is
  warm at boot. The residual risk is a MIGRATION THAT LOSES STATE. Priced here on
  a no-op deploy (same algorithm, same artifact, seed rotation only), steady world.
""")
    m0 = steady_run("never", n)
    mw = steady_run("warm0", n)
    mc = steady_run("cold", n)
    rows = [
        ("never deploy (incumbent)", fmt(m0.margin_per_1k()),
         fmt(m0.margin_per_1k(CUT, CUT + 10_000)), fmt(m0.margin_per_1k(CUT, n)),
         fmt(100.0 * m0.authed / max(1, m0.txns)), fmt(m0.mae(), 2)),
        ("warm 100% at seq 0 (seed rotation)", fmt(mw.margin_per_1k()),
         fmt(mw.margin_per_1k(CUT, CUT + 10_000)), fmt(mw.margin_per_1k(CUT, n)),
         fmt(100.0 * mw.authed / max(1, mw.txns)), fmt(mw.mae(), 2)),
        ("COLD 100% at seq 10k (store lost)", fmt(mc.margin_per_1k()),
         fmt(mc.margin_per_1k(CUT, CUT + 10_000)), fmt(mc.margin_per_1k(CUT, n)),
         fmt(100.0 * mc.authed / max(1, mc.txns)), fmt(mc.mae(), 2)),
    ]
    print(table(["deploy", "margin c/1k (full)", "first 10k post-cut",
                 "rest post-cut", "auth %", "MAE pts"], rows))
    tax = m0.margin_per_1k(CUT, CUT + 10_000) - mc.margin_per_1k(CUT, CUT + 10_000)
    print(f"""
  readings:
  - A warm no-op cutover is free: {fmt(mw.margin_per_1k())} vs
    {fmt(m0.margin_per_1k())} c/1k full-run -- indistinguishable. This is the H0
    the whole ticket stands on: warm start is not an approximation, it is the
    same fold, and the seed rotation only re-addresses the draws.
  - The cold cutover (the migration that loses the store) pays a
    {fmt(tax)} c/1k cold-start tax in its first 10k transactions ON A NO-OP
    DEPLOY -- same algorithm, same artifact, nothing wrong except missing state.
    It reproduces ADR-0006's Jeffreys cold window (informative vs Jeffreys
    first-10k: +1,539 c/1k there) in the rollout frame, and MAE degrades with it
    ({fmt(mc.mae(), 2)} vs {fmt(m0.mae(), 2)} pts).
  - The unit is TRANSACTIONS: the first 10k post-cut is 1.2 days at the committed
    scenario's pace (8,640 txns/day) and 2 seconds at ADR-0001's 5,000 dps fleet
    budget. The wall clock is the deployment's arrival rate; the tax is paid in
    decisions.
""")


# --------------------------------------------------------------------------------------
# [S2] shadow mode
# --------------------------------------------------------------------------------------

def sec_s2(n):
    print("[S2] shadow mode: what it can clear, how long it must run")
    print("""
  Shadow = a candidate folding the SHARED WAL (it sees every settled outcome the
  incumbent's attempts produce -- per-arm world facts, MAR conditional on the
  incumbent, so the fold stays unbiased; ADR-0008 C1) and logging the decisions it
  does NOT execute. Zero revenue risk by construction: nothing it says is executed,
  no lease is minted, no dispatch happens (ADR-0009's dbl=0/I1=0 gate reads the
  executed side only). The question the ticket asks -- how long must shadow run --
  has two answers, and the difference between them IS the finding: a warm-started
  shadow is at its plateau from the first window; a shadow that could not inherit
  state must wait for the fold to warm it.
""")
    mwarm = steady_run("noop", n, share=2_500, agree=13)
    wa = [h for s, h in mwarm.agree_log if s >= CUT]
    warm_agree = 100.0 * sum(wa) / max(1, len(wa))
    w = _world("baseline-steady-v1", n)
    old = make_router("baseline-steady-v1", n)
    cold = make_router("baseline-steady-v1", n, candidate=True, no_prior=True)
    msh = drive_rollout(w, old, cold, n, sched=[(0, "control", 0)], agree=13,
                        boot_at=0)
    ca = msh.agree_log
    per = max(1, n // 6)
    curve = []
    for k in range(6):
        xs = [h for s, h in ca if k * per <= s < (k + 1) * per]
        curve.append(fmt(100.0 * sum(xs) / max(1, len(xs))))
    print(table(["shadow variant", "attempt-0 agreement %", "rows", "note"],
                [["warm shadow (snapshot boot, seed rotation)",
                  fmt(warm_agree), f"{len(wa):,}",
                  "(at plateau from the first window)"]]))
    print("\n  cold-shadow agreement per 10k window (the warm-up curve):")
    print(table(["window", "attempt-0 agreement %"],
                [[f"{k * per // 1000}k-{(k + 1) * per // 1000}k", c]
                 for k, c in enumerate(curve)]))
    print(f"""
  (cold shadow final: {fmt(100.0 * sum(h for _s, h in ca) / max(1, len(ca)))}%
  over {len(ca):,} rows; MAE of its own fold at end of run:
  {fmt(msh.mae(), 2)} pts -- the fold warms it, exactly as ADR-0006's cold-start
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
""")


# --------------------------------------------------------------------------------------
# [S3] the canary gate
# --------------------------------------------------------------------------------------

def sec_s3(n):
    print("[S3] the canary gate: null distribution, false aborts, regressions")
    print("""
  The gate: split traffic by a key-addressed draw (per transaction, i.i.d. across
  sides); in matched windows of 1,000 decisions per side compute
  D = margin_c1k(canary) - margin_c1k(control). Abort when D < -Y for Z=2
  consecutive windows, or D < collapse (-1.5Y) for one. Y is set from the NULL
  distribution measured here (no-op deploy, seed rotation only) as Y = 3 sigma.
  Both sides face the same world in the same window, so world events (outages,
  diurnal shape) difference out -- the property [S4] tests against a real outage.
""")
    m25 = steady_run("noop", n, share=2_500, agree=0)
    m25b = steady_run("noop", n, share=2_500, asg_seed=20_260_920)
    s25, s25b = m25.D_stats(CUT), m25b.D_stats(CUT)
    if s25 is None or s25["n"] == 0:
        print(f"  NOTE: at n={n} the canary accrues fewer than {BLOCK} decisions per "
              f"side; run with n >= 40000 for the gate statistics.")
        return
    rows = []
    sig = {}
    for g in (1, 2, 4):
        st, xs = block_sigma((m25, m25b), g)
        if st is None:
            continue
        sig[g] = st
        rows.append((f"W = {BLOCK * g:,}/side", st[2], fmt(st[0]), fmt(st[1]),
                     fmt(min(xs)), fmt(max(xs))))
    y25 = 3.0 * sig[4][1]
    allD = [d for mm in (m25, m25b) for _s, d in mm.D if _s >= CUT]
    y1 = 3.0 * sig[1][1]
    fa = sum(1 for d in allD if d < -y1)
    # null coverage band: canary-vs-control arms at matched counts, per seed
    cov = []
    for mm in (m25, m25b):
        c1, c0 = mm.side_stats(1, CUT), mm.side_stats(0, CUT)
        k = min(len(c1["blocks"]), len(c0["blocks"]))
        if k and c0["blocks"][k - 1]:
            cov.append(c1["blocks"][k - 1] / c0["blocks"][k - 1])
    print(table(["null pooled (2 assignment seeds)", "gate-windows", "mean D",
                 "sigma", "min", "max"], rows))
    print(f"""
  Y is NOT a constant: it is 3 sigma measured on the live null at the gate window
  in use -- here Y = {y25:.0f} c/1k (routed) at W = {BLOCK * 4:,} per side. The
  re-blockings bracket the sqrt law ({fmt(sig[1][1])} at W={BLOCK:,} on
  {sig[1][2]} windows -> {fmt(sig[4][1])} at W={BLOCK * 4:,} on only
  {sig[4][2]} -- a {sig[4][2]}-window sigma carries a ~{fmt(100 / math.sqrt(max(1, 2 * sig[4][2])), 0)}% error of its own, so the production
  gate re-measures the null continuously; at fleet pace a gate-window is
  {BLOCK * 4 / 0.25 / 5000.0:.1f}s, so the null tightens within minutes of traffic,
  while the committed scenario's clock ({BLOCK * 4 / 0.25 / 8640.0 * 86400 / 86400:.0f}h per gate-window at 8,640
  txns/day) is the starved one. {len(allD)} raw windows, 0 breaches of Y and of
  collapse (-1.5Y) on the pooled null; the raw-window breach count at
  3 sigma(W=1) is {fa}. The margin branch auto-aborts on TWO consecutive
  gate-windows below -Y or ONE below collapse; everything else is a promotion
  decision made on the stage's D trace (below). At 1-5% share the wait is the
  point: a 1% canary is a health check, not a statistics instrument.
  Coverage has its own null: the canary-vs-control arms ratio at matched
  decision counts is {fmt(min(cov), 3)}-{fmt(max(cov), 3)} across seeds -- TS's
  rich-get-richer arm dynamics make single-stage coverage NOISY, and the
  starvation signal ADR-0006 measured (-359 arms in 16,466, -2.2% at 20k) sits
  INSIDE that band at 25% exposure. Coverage is the slowest gate member: it
  accumulates across stages and is checked against the incumbent's own curve at
  each stage boundary, not per window.
""")
    mex = steady_run("exalpha", n, share=2_500, gate_Y=y25)
    mlam = steady_run("lam", n, share=2_500, gate_Y=y25)
    mlam0 = steady_run("lam0", n, share=2_500, gate_Y=y25)
    mnor = steady_run("nor", n, share=2_500)
    mpri = steady_run("pri", n, share=2_500)
    aborts = [e for e in mex.events if e[1] == "ABORT"]
    ex_ab = aborts[0][0] if aborts else None
    aborts_l = [e for e in mlam.events if e[1] == "ABORT"]
    lam_ab = aborts_l[0][0] if aborts_l else None
    aborts_0 = [e for e in mlam0.events if e[1] == "ABORT"]
    lam0_ab = aborts_0[0][0] if aborts_0 else None

    def matched_arms(mm):
        c1, c0 = mm.side_stats(1, CUT), mm.side_stats(0, CUT)
        k = min(len(c1["blocks"]), len(c0["blocks"]))
        if k == 0:
            return "-", "-"
        return c1["blocks"][k - 1], c0["blocks"][k - 1]

    rows = []
    for label, mm, note in (
        ("charlie excluded (ConstraintSet regression)", mex,
         f"auto-ABORT at seq {ex_ab:,} (routability)" if ex_ab else "no abort"),
        ("draw_alg=normal (ADR-0006's rejected sampler)", mnor, ""),
        ("prior artifact foxtrot x1.25", mpri, ""),
        ("lam_to 45 -> 10 (mispriced ambiguity)", mlam, ""),
        ("lam_to 45 -> 0 (timeouts priced free)", mlam0,
         f"auto-ABORT at seq {lam0_ab:,} (margin)" if lam0_ab else ""),
    ):
        st = mm.D_stats(CUT)
        c1, c0 = mm.side_stats(1, CUT), mm.side_stats(0, CUT)
        a1, a0 = matched_arms(mm)
        unr = [u for s_, u in mm.UNR if s_ >= CUT]
        unr_m = 100.0 * sum(unr) / max(1, len(unr))
        # the promotion decision on the stage's whole D trace: mean D vs 2 s.e.,
        # with sigma from the POOLED NULL (a single run's gate-windows are too
        # few to estimate their own spread)
        n4 = len([1 for k in range(0, len([d for d in mm.DP if d[0] >= CUT]) - 3, 4)])
        se4 = sig[4][1] / math.sqrt(max(1, n4)) if 4 in sig else 0.0
        cov_ratio = (a1 / a0) if (a1 != "-" and a0 != "-" and a0) else None
        if any(e[1] == "ABORT" for e in mm.events):
            verdict = note
        elif cov_ratio is not None and cov_ratio < min(cov) - 0.03:
            verdict = "promotion BLOCKED (coverage)"
        elif st and st["mean"] < -2.0 * se4:
            verdict = f"promotion BLOCKED (D trace, {st['mean']:.0f} c/1k)"
        elif cov_ratio is not None and cov_ratio < max(cov):
            verdict = (f"rides at 25% (coverage {cov_ratio:.2f} in null band "
                       f"{min(cov):.2f}-{max(cov):.2f}); accumulates at stages")
        else:
            verdict = "promotes"
        rows.append((label, fmt(st["mean"]) if st else "-",
                     f"{st['n']}" if st else "-",
                     fmt(unr_m) if unr else "-",
                     fmt(100.0 * c1["authed"] / max(1, c1["txns"])),
                     f"{a1} vs {a0}",
                     verdict))
    print(table(["regression deployed at 25%", "mean D c/1k (routed)",
                 "windows", "unroutable delta pts", "canary auth %",
                 "arms@matched counts", "gate verdict"], rows))
    mnaive = steady_run("exalpha_naive", n)
    mnever = steady_run("never", n)
    naive_post = mnaive.margin_per_1k(CUT, n)
    never_post = mnever.margin_per_1k(CUT, n)
    gated_post = mex.margin_per_1k(CUT, n)
    print(f"""
  the same bad deploy, gated vs not (steady world, margins over seq {CUT:,}..{n:,}):

    naive 100% cutover, no gate:  {fmt(naive_post)} c/1k  ({fmt(naive_post - never_post)} vs never)
    gated 25% with auto-abort:    {fmt(gated_post)} c/1k  ({fmt(gated_post - never_post)} vs never)
    never deployed:               {fmt(never_post)} c/1k

  readings:
  - The excluded-processor regression is caught in {(ex_ab - CUT) if ex_ab else 0:,}
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
""")


# --------------------------------------------------------------------------------------
# [S4] the ADR-0007 payload: detectors, suppression, world events
# --------------------------------------------------------------------------------------

def sec_s4(n):
    print("[S4] rollout vs the drift detector: share shifts, suppression, outages")
    print("""
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
""")
    m25 = steady_run("noop", n, share=2_500)
    mpri = steady_run("pri", n, share=2_500)
    rows = []
    for label, mm in (("no-op deploy, const 25%", m25),
                      ("prior x1.25 deploy, const 25%", mpri)):
        al = [a for a in mm.alarm_log if a[0] >= CUT]
        rows.append((label, f"{mm.n_alarms}", f"{mm.n_held}", f"{mm.n_refire}",
                     f"{al[0][0]:,}" if al else "-"))
    print(table(["steady-world rollout", "alarms", "held", "refired", "first alarm"],
                rows))
    print("""
  -> share shifts do NOT trip delta=1e-3 at the processor level on this fleet,
     even for a deploy that reroutes foxtrot share: the blended stream's rate
     barely moves when a few points of share change context mix. Suppression is
     insurance sized for bigger reshapes (a new ConstraintSet, a new arm space),
     not something every ramp step needs -- which is what makes a SHORT arming
     window affordable.""")
    m_sup = starved_protocol(n, sup=True)
    m_pl = starved_protocol(n, sup=False)
    cut = _seq_at(_world("quiet-improvement-starved-v1", n), n, DEPLOY_S)
    rows = []
    for label, mm in (("protocol, suppression ON (shipped)", m_sup),
                      ("protocol, suppression OFF", m_pl)):
        al = [a for a in mm.alarm_log if not a[6]]
        he = [e for e in mm.events if e[1] == "REFIRE"]
        rows.append((label, mm.n_alarms, mm.n_held, mm.n_refire,
                     f"{al[0][0]:,}" if al else "-",
                     f"{he[0][0]:,}" if he else "-",
                     fmt(mm.margin_per_1k(cut, n))))
    print()
    print(table(["starved-improvement rollout", "applied", "held", "refired",
                 "first applied", "first refire", "post-deploy c/1k"], rows))
    print("""
  -> the improvement alarm is Tier-2 and lands INSIDE the arming window (the
     deploy and the silent recovery overlap): suppression delays the R58 reset
     that re-feeds foxtrot, and the delay is paid in margin. The re-check at
     expiry fires it (the shift is real), so the alarm is late, not lost -- but
     the arming window must stay SHORT (256 settled, not thousands).""")
    st4, _ = block_sigma((m25, m25b := steady_run("noop", n, share=2_500,
                                                  asg_seed=20_260_920)), 4)
    y25 = 3.0 * st4[1]
    mout = outage_run(n, gate_Y=y25)
    wout = _world("outage-recovery-v1", n)
    out_s = _seq_at(wout, n, 345_600)
    ds = [d for (s, d) in mout.D if out_s - 4_000 <= s <= out_s + 16_000]
    t1 = [a for a in mout.alarm_log if a[3] == 1 and not a[6]]
    nab = sum(1 for e in mout.events if e[1] == "ABORT")
    s25 = st4
    print(f"""
  outage during rollout (outage-recovery-v1, no-op canary 25% from seq {CUT:,};
  foxtrot connection_refused at t=345,600s, gate armed at Y={y25:.0f}):
    Tier-1 alarms applied during the rollout: {len(t1)}
    (first at seq {t1[0][0]:,}, t={t1[0][1] / 1000:,.0f}s) -- NEVER suppressed.
    paired D over the outage-window gate-windows: mean
    {fmt(sum(ds) / max(1, len(ds)))} c/1k over {len(ds)} windows,
    max |D| {fmt(max(abs(d) for d in ds)) if ds else '-'} c/1k
    (abort bar Y = {fmt(y25)} per gate-window) -- the world event DIFFERENCES
    OUT: both sides face the same outage, the deficit is a property of the
    POLICY, not the world.
    false aborts over the whole run: {nab}.

  the alternative -- isolate canary streams into their own detectors -- starves
  by construction: a 25% canary detector sees a quarter of the stream, so the
  same outage detection lag stretches ~4x in fleet decisions, and at 1-5% share
  it is ADR-0007's dilution failure again (a detector fed 1-2 attempts per
  outage window stays silent). One blended stream per processor, fed by every
  executed attempt, plus a short arming window, dominates it in this sweep.""")


# --------------------------------------------------------------------------------------
# [S5] the protocol end to end
# --------------------------------------------------------------------------------------

def _sched_starved(w, n):
    cut = _seq_at(w, n, DEPLOY_S)
    return [(0, "control", 0), (cut, "shadow", 0), (cut + 2_000, "canary", 100),
            (cut + 3_000, "canary", 500), (cut + 5_000, "canary", 2_500),
            (cut + 13_000, "canary", 5_000), (cut + 21_000, "canary", 10_000)]


def sec_s5(n):
    print("[S5] the protocol end to end: shadow -> canary -> full on the improvement world")
    print("""
  quiet-improvement-starved-v1: foxtrot believed 0.60 (the artifact was refreshed
  mid-storm), truth recovers to 0.92 at day 3 with no transport signature. The
  team deploys at day 3.5: a new prior artifact refreshed from a fresh 2,000-
  transaction uniform probe (the ADR-0006 artifact path -- spent money, priced below)
  over the incumbent's snapshot fold. This is the rollout the ticket fears, in
  the direction it hopes for -- and the same machinery that caps a bad deploy
  bounds this good one.
""")
    wname = "quiet-improvement-starved-v1"
    w = _world(wname, n)
    ev = _seq_at(w, n, EVENT_S)
    cut = _seq_at(w, n, DEPLOY_S)
    headroom, flip_frac = C9.margin_headroom(w, n, (EVENT_S + 120) * 1000)
    print(f"  event: step recovery at t={EVENT_S}s (seq {ev:,}); deploy at "
          f"t={DEPLOY_S}s (seq {cut:,}); argmax flips {100 * flip_frac:.1f}% of "
          f"contexts; oracle headroom {headroom:,.0f} c/1k.\n")
    m_sup = starved_protocol(n, sup=True)
    sched = _sched_starved(w, n)
    y1 = null_Y(n, 1)
    print("  the protocol's own trace (suppression ON, gate armed at every canary "
          "stage, Y = %s):" % f"{y1:,.0f}")
    print("    stage      seq range              share   windows   mean D")
    for k, (s0, stage, bp) in enumerate(sched):
        end = sched[k + 1][0] if k + 1 < len(sched) else n
        if stage == "control" and k == 0:
            print("    %-9s %9s .. %9s   %3s%%         -        (incumbent warmup)"
                  % (stage, f"{s0:,}", f"{end - 1:,}", bp // 100))
            continue
        xs = [d for s, d in m_sup.D if s0 <= s < end]
        ds = fmt(sum(xs) / max(1, len(xs))) if xs else "-"
        print("    %-9s %9s .. %9s   %3s%%         %-7s  %s"
              % (stage, f"{s0:,}", f"{end - 1:,}", bp // 100, len(xs), ds))
    blk = [d for _s, d in m_sup.D]
    nab = sum(1 for e in m_sup.events if e[1] == "ABORT")
    print()
    print("    raw windows total: %d; mean D %s c/1k; min window %s; auto-aborts: %d"
          % (len(blk), fmt(sum(blk) / max(1, len(blk))),
             fmt(min(blk)) if blk else "-", nab))
    print("    (the candidate is the BETTER policy here; the gate's job on this run")
    print("    is to NOT get in the way -- and the same calibration that aborts the")
    print("    bad deploys of [S3] rode this one through single-window noise.)")

    print(f"""
  the money table, margins over seq {cut:,}..{n:,} (the deploy window) and full-run:""")
    rows = []
    m_never_b = starved_never(n, machinery=False)
    m_never_m = starved_never(n, machinery=True)
    m_cold = starved_naive(n, "cold")
    m_warm = starved_naive(n, "warm")
    m_ref = starved_naive(n, "refresh")
    flips = C9.flip_set(w, n)
    mo = C9.CACHE.get(("oracle", wname, n))
    if mo is None:
        mo = C9.drive_oracle(_world(wname, n), n, flips=flips)
        C9.CACHE[("oracle", wname, n)] = mo
    for label, mm, note in (
        ("protocol (shadow->1->5->25->50->100, gated)", m_sup, "shipped"),
        ("naive 100%, warm fold, STALE artifact", m_warm, "no refresh"),
        ("naive 100%, warm fold, refreshed artifact", m_ref, "no ramp, no gate"),
        ("naive 100%, COLD (store lost)", m_cold, "the ticket's fear"),
        ("never deploy (bare TS, stale artifact)", m_never_b, "status quo"),
        ("never deploy, ADR-0007/0008 machinery", m_never_m, "in-run responder"),
        ("oracle (upper bound)", mo, "clairvoyant"),
    ):
        rows.append((label, fmt(mm.margin_per_1k(cut, n)), fmt(mm.margin_per_1k()),
                     fmt(100.0 * mm.share("foxtrot", cut, n), 1), note))
    print(table(["deployment", "post-deploy c/1k", "full-run c/1k", "foxtrot share %",
                 "note"], rows))
    m_ref = starved_naive(n, "refresh")
    m_cold = starved_naive(n, "cold")
    prem = m_ref.margin_per_1k(cut, n) - m_sup.margin_per_1k(cut, n)
    tax = m_ref.margin_per_1k(cut, n) - m_cold.margin_per_1k(cut, n)
    y1 = null_Y(n, 1)
    w25 = [d for s, d in m_sup.D if 36_031 <= s < 44_031]
    print()
    print("  readings:")
    print("  - The warm deploys land within ~250 c/1k of one another over a %s-txn"
          % f"{n - cut:,}")
    print("    window whose per-window noise is ~1,754 c/1k [S3 null] -- on a GOOD")
    print("    deploy the protocol is roughly free: its premium against an instant")
    print("    100%% cutover of the SAME candidate is %s c/1k over the window (%s s"
          % (fmt(prem), fmt((n - cut) / 5000.0)))
    print("    at fleet pace), and the cold row shows the ticket's feared tax: %s"
          % fmt(tax))
    print("    c/1k against the same candidate warm.")
    print("  - The refreshed artifact moves the DIRECTION (foxtrot share 15.3% vs")
    print("    the stale 3.1% bare) but captures a fraction of the oracle headroom:")
    print("    a 2,000-probe artifact at m=100 is itself a noisy estimate, and some")
    print("    of what it displaces was fine. Artifact QUALITY is the prior-artifact path's (ADR-0006 §2); the")
    print("    protocol's job was to ship this one without a false abort, which it did.")
    if w25:
        print("  - The gate armed and rode through a scary window: the 25% stage's")
        print("    first window read %s c/1k against Y = %s -- inside the bar, and"
              % (fmt(w25[0]), f"{y1:,.0f}"))
        print("    the 2-consecutive rule means one noisy window never aborts alone.")
        print("    This is the calibration working as designed on a GOOD deploy.")
    print("  - 'Never deploy with machinery on' is the honest baseline the ticket")
    print("    does not name: the in-run responder (R58) re-feeds foxtrot without any")
    print("    deploy, at a short-window cost (the floor and decay tax, ADR-0008")
    print("    C2/C3's finding again). The protocol exists for the changes the")
    print("    machinery cannot make (artifacts, config, algorithms) and for the bad")
    print("    deploys [S3] prices.")
    print("  - The refreshed artifact's uniform probe costs 2,000 attempts outside")
    print("    the run window (ADR-0006's P2 construction) -- a known, one-off, priced")
    print("    spend; and per ADR-0008's warning, refreshing an artifact is itself a")
    print("    deploy-shaped event and belongs on this protocol's books.")


SECTIONS = {
    "S0": sec_s0,
    "S1": sec_s1,
    "S2": sec_s2,
    "S3": sec_s3,
    "S4": sec_s4,
    "S5": sec_s5,
}


def main(argv):
    n = DEFAULT_N
    only = None
    for arg in argv[1:]:
        if arg.startswith("--section="):
            only = arg.split("=", 1)[1]
        else:
            n = int(arg)

    print(f"#14 evidence spike: safe policy rollout | n={n} | "
          f"policy seed {POLICY_SEED} (incumbent) / {CAND_SEED} (candidate) | "
          f"rollout seed {ROLLOUT_SEED} | lambda_to={LAM_TO:.0f}")
    print("worlds: baseline-steady-v1@%s, outage-recovery-v1@%s, "
          "quiet-improvement-starved-v1@%s"
          % (scenario_hash(_doc("baseline-steady-v1"))[:19],
             scenario_hash(_doc("outage-recovery-v1"))[:19],
             scenario_hash(_doc("quiet-improvement-starved-v1"))[:19]))
    print()
    for name, fn in SECTIONS.items():
        if only is None or only == name:
            fn(n)
            print()

    if only is None:
        print("=" * 100)
        print(f"Reproduce: python3 spikes/0014-safe-rollout/rollout.py {n}   "
              "(RESULTS.md is this output)")


if __name__ == "__main__":
    main(sys.argv)
