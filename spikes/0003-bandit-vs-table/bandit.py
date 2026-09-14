#!/usr/bin/env python3
"""Decision ticket #3 evidence: why a bandit, and why Beta-Bernoulli Thompson sampling.

What this measures, and what it deliberately does not
------------------------------------------------------
#3 asks two things: (1) why a multi-armed bandit instead of a static weight table, and
(2) within bandits, why Beta-Bernoulli Thompson sampling over UCB / epsilon-greedy / EXP3.
#4 already priced the reward; this spike holds the reward FIXED to ADR-0002's chosen form
(two-part score, timeout excluded from the auth posterior and priced at lambda_to = 45)
so that the only thing that varies is the routing algorithm. The 3DS channel is held out
(decided by #4/#11): it multiplies the same number every algorithm would see, so it cannot
change an algorithm-family comparison.

Policies, and the question each one answers:

  table_global     the literal proposal: fixed global weights ("60/30/10"), never updated.
  table_snapshot   the strong steelman: per-BIN-class argmax of a stale rate snapshot,
                   never updated. This is the fairest possible static table.
  table_snapshot_lat  the baseline ADR-0002 said must exist: the same stale snapshot PLUS
                   a static latency rule on the NOMINAL p99 -- not latency-blind, but its
                   latency table is frozen, so it cannot see a processor get slower.
  ts               Beta-Bernoulli Thompson sampling (the chosen variant).
  ucb              UCB on the auth rate (mean + c*sqrt(2 ln t / n)), scored through margin.
  eps_05/eps_10    epsilon-greedy with a FIXED noise floor (no schedule): the critique is
                   that a fixed epsilon keeps burning money on known-bad arms forever.
  exp3             EXP3 with the standard per-step [0,1] reward renormalization.
  greedy           argmax of the posterior mean -- the "deterministic policy for
                   production" half of hybrid option 3, on its own.
  freeze_k         hybrid option 3 (the costly reading): alternate TS-explore for k
                   transactions and argmax-of-a-frozen-table-exploit for k. The frozen
                   table is a printable, auditable artifact; the lag between freezes is
                   the price of that auditability.
  ab_k             alternative 4: A/B test -- explore uniformly for k transactions, exploit
                   the argmax for k, repeat. Periodic rebalancing on a clock.
  oracle           true rates through the identical loop (the regret ceiling).

Scenario: synthetic-fleet-v1 plus three nonstationarities a table cannot hold:
  (a) BIN x processor interaction -- the best processor differs by card class, so a global
      weight vector is wrong for whole classes of transactions;
  (b) time-of-day sinusoid, phase-shifted per processor -- the ranking flips over the day;
  (c) two acquirer events: charlie's auth x0.86 and latency x3 at t=0.6 (an outage at the
      margin workhorse -- what a static table cannot see), and foxtrot's auth x1.15 at
      t=0.8 (a new issuer deal at the budget processor -- what a deterministic argmax
      cannot discover).

All processor rates are invented. Magnitudes are properties of this scenario; the
orderings, gaps, and adaptation-lag structure are the findings. Coupled randomness: the
scenario (context + outcome for every processor) is generated once, policy-independent,
so policy gaps are not simulation noise. Deterministic: fixed seed, stdlib only.

Run:  python3 spikes/0003-bandit-vs-table/bandit.py [n_transactions]
"""

from __future__ import annotations

import math
import random
import sys
from dataclasses import dataclass

SEED = 20_260_914
WARMUP = 6_000           # transactions excluded from the "warmup tax" window
DRIFT_AT = 0.60          # charlie degrades (x0.86 auth, x3 latency): the margin workhorse
IMPROVE_AT = 0.80        # foxtrot improves (x1.15 auth): a new issuer deal
MAX_ATTEMPTS = 2
DEADLINE_MS = 900
LAM_TO = 45.0            # ADR-0002: price of an unresolved attempt, cents
SELL_BPS, SELL_FIXED = 128, 0
C_UCB = 2.0
TOD_CYCLES = 2           # "days" spanned by the run

AUTH, DECLINE_SOFT, DECLINE_HARD, TIMEOUT = 1, 0, 2, 3


@dataclass(frozen=True)
class Processor:
    name: str
    cost_bps: int
    fixed_fee: int          # cents, charged on authorization
    attempt_fee: int        # cents, charged on submission (declines are not free)
    base_auth: float        # P(approve | reached) at the reference point
    soft_decline_share: float
    lat_p50_ms: int
    lat_p99_ms: int
    currencies: frozenset
    bin_aff: tuple          # per-BIN-class affinity (weighted mean ~ 1.0 per processor)
    tod_amp: float          # amplitude of the time-of-day sinusoid
    tod_phase: float        # phase, in cycles


# (name, interchange bps, mix weight)
BIN_CLASSES = (
    ("consumer_credit", 115, 0.58),
    ("consumer_debit", 60, 0.27),
    ("corporate", 135, 0.10),
    ("prepaid", 90, 0.05),
)
REGIONS = (("SEPA", 1.000, 0.42, "EUR"), ("UK", 0.995, 0.14, "GBP"),
           ("US", 0.985, 0.26, "USD"), ("LATAM", 0.940, 0.10, "USD"),
           ("APAC", 0.950, 0.08, "USD"))

FLEET = (
    Processor("alpha",   95, 10, 6, 0.915, 0.55, 190,  640, frozenset({"EUR", "USD"}),
              (1.040, 0.980, 0.940, 0.920), 0.030, 0.00),
    Processor("bravo",   72, 10, 5, 0.884, 0.62, 240,  900, frozenset({"EUR", "USD"}),
              (0.950, 1.100, 0.950, 0.950), 0.035, 0.25),
    Processor("charlie", 64,  8, 4, 0.842, 0.71, 300, 1150, frozenset({"EUR", "GBP"}),
              (0.940, 0.950, 1.160, 0.900), 0.040, 0.50),
    Processor("delta",  110, 12, 7, 0.935, 0.48, 170,  520, frozenset({"EUR", "USD"}),
              (1.030, 0.970, 0.960, 0.900), 0.020, 0.75),
    Processor("echo",    86,  9, 5, 0.900, 0.58, 210,  760, frozenset({"USD"}),
              (1.000, 0.980, 0.940, 0.900), 0.030, 0.12),
    Processor("foxtrot", 58, 25, 9, 0.806, 0.74, 420, 1750, frozenset({"EUR", "USD"}),
              (0.950, 0.950, 0.940, 1.160), 0.050, 0.60),
)
NPROC = len(FLEET)
NCLS = len(BIN_CLASSES)
CHARLIE = 2
FOXTROT = 5


@dataclass(frozen=True)
class Ctx:
    cls_i: int
    region_i: int
    amount_c: int
    currency: str
    floor_margin_bps: int


# --- scenario model -------------------------------------------------------------

def _tod(pi: int, t: float, n: int) -> float:
    p = FLEET[pi]
    return 1.0 - p.tod_amp * math.sin(2 * math.pi * (TOD_CYCLES * t / n + p.tod_phase))


def p_true(ctx: Ctx, pi: int, t: float, n: int) -> float:
    """True P(authorize | reached) for processor pi on ctx at index t. Closed form, so the
    variance table and the staleness MAE are exact, not sampled."""
    p = FLEET[pi]
    amt = ctx.amount_c / 100.0
    auth = p.base_auth * p.bin_aff[ctx.cls_i] * REGIONS[ctx.region_i][1]
    auth *= 1.0 - min(0.09, max(0.0, (amt - 800.0) / 40_000.0))
    auth *= 1.0 - min(0.05, max(0.0, (60.0 - amt) / 4_000.0))
    auth *= _tod(pi, t, n)
    if t / n > DRIFT_AT:
        # event 1: the margin workhorse has an outage. A static table cannot see it.
        if pi == CHARLIE:
            auth *= 0.86
    if t / n > IMPROVE_AT:
        # event 2: the budget processor lands a new issuer deal. A deterministic argmax
        # that never routes to foxtrot cannot discover it; a sampler can.
        if pi == FOXTROT:
            auth *= 1.15
    return min(0.995, max(0.02, auth))


def latency(ctx: Ctx, pi: int, rng: random.Random, t: float, n: int) -> float:
    p = FLEET[pi]
    degraded = (pi == CHARLIE and t / n > DRIFT_AT)
    med = p.lat_p50_ms * (3.0 if degraded else 1.0)
    tail = p.lat_p99_ms * (3.5 if degraded else 1.0)
    u = rng.random()
    if u < 0.5:
        return med * (0.6 + 0.8 * u)
    return med + (tail - med) * (((u - 0.5) / 0.5) ** 1.6)


def legal_set(ctx: Ctx) -> list[int]:
    """Constraint layer BEFORE the algorithm, per ADR-0002 (filter before sampling)."""
    out = []
    for pi, p in enumerate(FLEET):
        if ctx.currency not in p.currencies:
            continue
        if SELL_BPS - p.cost_bps < ctx.floor_margin_bps:
            continue
        out.append(pi)
    return out


def win_lose(ctx: Ctx, pi: int) -> tuple[float, float]:
    win = ctx.amount_c * (SELL_BPS - FLEET[pi].cost_bps) / 10_000.0 + (SELL_FIXED - FLEET[pi].fixed_fee)
    return win, -float(FLEET[pi].attempt_fee)


def build_scenario(n: int, seed: int):
    """One coupled scenario: every (ctx, processor) outcome generated once, policy-free."""
    scen = []
    for i in range(n):
        rng = random.Random(f"{seed}:{i}")
        cls_i = rng.choices(range(NCLS), weights=[c[2] for c in BIN_CLASSES])[0]
        region_i = rng.choices(range(len(REGIONS)), weights=[r[2] for r in REGIONS])[0]
        amount_c = max(60, int(math.exp(rng.gauss(math.log(46.0), 0.95)) * 100))
        currency = REGIONS[region_i][3]
        floor = rng.choices([0, 25, 40], weights=(82, 12, 6))[0]
        ctx = Ctx(cls_i, region_i, amount_c, currency, floor)
        rows = []
        for pi in range(NPROC):
            pt = p_true(ctx, pi, i, n)
            lat = latency(ctx, pi, rng, i, n)
            u_au, u_soft = rng.random(), rng.random()
            if lat > DEADLINE_MS:
                out = TIMEOUT
            elif u_au < pt:
                out = AUTH
            elif u_soft < FLEET[pi].soft_decline_share:
                out = DECLINE_SOFT
            else:
                out = DECLINE_HARD
            realized = win_lose(ctx, pi)[0] if out == AUTH else -float(FLEET[pi].attempt_fee)
            rows.append((out, pt, int(round(realized))))
        scen.append((ctx, rows))
    return scen


# --- policies -------------------------------------------------------------------

@dataclass
class Res:
    name: str
    n: int = 0
    auth: int = 0
    timeouts: int = 0
    margin: float = 0.0
    attempts: int = 0
    early_margin: float = 0.0       # first WARMUP transactions: the exploration tax
    late_margin: float = 0.0        # after DRIFT_AT: adaptation, where the gap opens
    n_early: int = 0
    n_late: int = 0
    stale_sum: float = 0.0          # sum |p_hat_used - p_true| over decisions
    stale_n: int = 0
    disc_n: int = 0                 # decisions eligible for the discovery probe
    disc_fox: int = 0               # ... that routed to foxtrot (the improved challenger)
    fox_mean: float = float("nan")  # final estimate of foxtrot's consumer-credit rate


class Policy:
    """One routing policy. Learners keep per-(BIN class x processor) Beta posteriors and a
    separate timeout count; the score is ADR-0002's. Subclasses change only the arm index
    and the rate they can quote for retries / audits."""

    kind = ""
    rate_est = True                 # whether the policy can quote a per-arm auth rate
    tracks_timeout = True           # whether it maintains a learned timeout rate

    def __init__(self, name: str, est=None):
        self.name = name
        self.est = est
        self.alpha = [[0.5] * NPROC for _ in range(NCLS)]
        self.beta = [[0.5] * NPROC for _ in range(NCLS)]
        self.to_a = [[0.5] * NPROC for _ in range(NCLS)]
        self.to_b = [[0.5] * NPROC for _ in range(NCLS)]
        self.pulls = [[0] * NPROC for _ in range(NCLS)]
        self.t_cls = [0] * NCLS
        self._i = 0
        self.truth = None

    # --- posterior helpers ---
    def mean(self, ctx: Ctx, pi: int) -> float:
        a, b = self.alpha[ctx.cls_i][pi], self.beta[ctx.cls_i][pi]
        return a / (a + b)

    def to_mean(self, ctx: Ctx, pi: int) -> float:
        return self.to_a[ctx.cls_i][pi] / (self.to_a[ctx.cls_i][pi] + self.to_b[ctx.cls_i][pi])

    def score(self, theta: float, ctx: Ctx, pi: int) -> float:
        win, lose = win_lose(ctx, pi)
        to = self.to_mean(ctx, pi) if self.tracks_timeout else 0.0
        return theta * win + (1.0 - theta) * lose - LAM_TO * to

    def index(self, ctx: Ctx, pi: int, rng: random.Random) -> float:
        raise NotImplementedError

    def used_estimate(self, ctx: Ctx, pi: int) -> float:
        return self.mean(ctx, pi)

    def retry_rate(self, ctx: Ctx, pi: int) -> float:
        return self.used_estimate(ctx, pi)

    def chain(self, ctx: Ctx, legal: list[int], rng: random.Random) -> list[int]:
        scored = sorted(((self.index(ctx, pi, rng), pi) for pi in legal), reverse=True)
        return [pi for _, pi in scored][:MAX_ATTEMPTS]

    def update(self, ctx: Ctx, pi: int, out: int) -> None:
        self.t_cls[ctx.cls_i] += 1
        self.pulls[ctx.cls_i][pi] += 1
        if out == TIMEOUT:
            self.to_a[ctx.cls_i][pi] += 1.0
            return
        self.to_b[ctx.cls_i][pi] += 1.0
        if out == AUTH:
            self.alpha[ctx.cls_i][pi] += 1.0
        else:
            self.beta[ctx.cls_i][pi] += 1.0


class Thompson(Policy):
    kind = "ts"

    def index(self, ctx, pi, rng):
        return self.score(rng.betavariate(self.alpha[ctx.cls_i][pi], self.beta[ctx.cls_i][pi]),
                          ctx, pi)


class UCB(Policy):
    kind = "ucb"

    def index(self, ctx, pi, rng):
        n = self.pulls[ctx.cls_i][pi]
        t = max(1, self.t_cls[ctx.cls_i])
        bonus = C_UCB * math.sqrt(2.0 * math.log(t) / max(1, n)) if n > 0 else 2.0
        theta = min(0.995, max(0.02, self.mean(ctx, pi) + bonus))
        return self.score(theta, ctx, pi)


class EpsGreedy(Policy):
    kind = "eps"

    def __init__(self, name, eps, est=None):
        super().__init__(name, est)
        self.eps = eps

    def chain(self, ctx, legal, rng):
        if rng.random() < self.eps:
            order = legal[:]
            rng.shuffle(order)
            return order[:MAX_ATTEMPTS]
        return super().chain(ctx, legal, rng)

    def index(self, ctx, pi, rng):
        return self.score(self.mean(ctx, pi), ctx, pi)


class Greedy(Policy):
    kind = "greedy"

    def index(self, ctx, pi, rng):
        return self.score(self.mean(ctx, pi), ctx, pi)


class EXP3(Policy):
    """Adversarial-bandit baseline. Maintains importance weights, not a rate estimate, so
    it can quote neither a posterior rate nor a credible interval."""
    kind = "exp3"
    rate_est = False

    def __init__(self, name, est=None):
        super().__init__(name, est)
        self.w = [1.0] * NPROC
        self.eta = 0.02          # small: importance-weighted rewards are high-variance here
        self.gamma = 0.05

    def chain(self, ctx, legal, rng):
        ws = [self.w[pi] for pi in legal]
        total = sum(ws) or len(legal)
        k = len(legal)
        probs = [(1.0 - self.gamma) * w / total + self.gamma / k for w in ws]
        pick = rng.choices(legal, weights=probs)[0]
        rest = sorted((self.score(self.retry_rate(ctx, pi), ctx, pi), pi)
                      for pi in legal if pi != pick)
        self._last = (legal, pick, probs[legal.index(pick)])
        return [pick] + [pi for _, pi in reversed(rest)][:MAX_ATTEMPTS - 1]

    def reward(self, ctx: Ctx, realized: float, win: float) -> None:
        _, pick, prob = self._last
        lo = -(FLEET[pick].attempt_fee + LAM_TO)
        hi = max(win, 1.0)
        r = (realized - lo) / (hi - lo)
        r = max(0.0, min(1.0, r))
        self.w[pick] *= math.exp(self.eta * r / max(prob, 1e-6))
        hi_w = max(self.w)
        if hi_w > 1e6:            # rescale to keep weights finite over long runs
            self.w = [w / hi_w for w in self.w]

    def retry_rate(self, ctx, pi):
        return self.est[pi][ctx.cls_i] if self.est is not None else self.mean(ctx, pi)


class TableGlobal(Policy):
    """Literal '60/30/10': one fixed global weight vector, routed by weighted choice."""
    kind = "table_global"
    rate_est = False
    tracks_timeout = False

    def __init__(self, name, weights, est=None):
        super().__init__(name, est)
        self.weights = weights

    def chain(self, ctx, legal, rng):
        ws = [self.weights[pi] for pi in legal]
        pick = rng.choices(legal, weights=ws)[0]
        rest = [pi for _, pi in sorted((self.weights[pi], pi) for pi in legal if pi != pick)]
        return [pick] + rest[:MAX_ATTEMPTS - 1]

    def retry_rate(self, ctx, pi):
        return self.est[pi][ctx.cls_i] if self.est is not None else 0.5

    def update(self, ctx, pi, out):
        pass


class TableSnapshot(Policy):
    """Strong steelman: per-BIN-class argmax of a stale rate snapshot, never updated."""
    kind = "table_snapshot"
    tracks_timeout = False

    def index(self, ctx, pi, rng):
        return self.score(self.est[pi][ctx.cls_i], ctx, pi)

    def used_estimate(self, ctx, pi):
        return self.est[pi][ctx.cls_i]

    def update(self, ctx, pi, out):
        pass


class TableSnapshotLat(TableSnapshot):
    """The baseline ADR-0002 said must exist: static table PLUS a static latency rule.
    Same stale rate snapshot, minus a static per-ms penalty on the NOMINAL p99. It is not
    latency-blind -- but its latency table is frozen, so it cannot see a processor get
    slower. (KAPPA is fleet.py's static-latency-penalty constant.)"""
    kind = "table_snapshot_lat"
    KAPPA = 0.0069  # cents/ms

    def index(self, ctx, pi, rng):
        return self.score(self.est[pi][ctx.cls_i], ctx, pi) - self.KAPPA * FLEET[pi].lat_p99_ms


class Freeze(Policy):
    """Hybrid option 3 (the costly reading): alternate TS-explore and frozen-table-exploit.
    Each phase is `k` transactions; the exploit phase routes argmax of the means frozen at
    the phase boundary (a table you can print and audit), the explore phase is TS."""
    kind = "freeze"

    def __init__(self, name, k, est=None):
        super().__init__(name, est)
        self.k = k
        self.frozen = None

    def _means(self):
        return [[self.alpha[ci][pi] / (self.alpha[ci][pi] + self.beta[ci][pi])
                 for pi in range(NPROC)] for ci in range(NCLS)]

    def chain(self, ctx, legal, rng):
        idx = sum(self.t_cls)
        if (idx // self.k) % 2 == 0:                       # explore phase: TS sampling
            self.frozen = None
            return super().chain(ctx, legal, rng)          # Policy.chain -> self.index
        if self.frozen is None or idx % self.k == 0:       # freeze at the phase boundary
            self.frozen = self._means()
        return sorted(legal, key=lambda pi: -self.score(self.frozen[ctx.cls_i][pi], ctx, pi))[:MAX_ATTEMPTS]

    def used_estimate(self, ctx, pi):
        if self.frozen is not None:
            return self.frozen[ctx.cls_i][pi]
        return self.mean(ctx, pi)

    def index(self, ctx, pi, rng):
        # the explore-phase draw: real Thompson sampling, not argmax-of-mean
        return self.score(rng.betavariate(self.alpha[ctx.cls_i][pi], self.beta[ctx.cls_i][pi]),
                          ctx, pi)


class AB(Policy):
    """Alternative 4 (A/B with periodic rebalancing): alternate uniform-explore and
    argmax-exploit, k transactions each. The rebalance happens on a clock."""
    kind = "ab"

    def __init__(self, name, k, est=None):
        super().__init__(name, est)
        self.k = k

    def chain(self, ctx, legal, rng):
        idx = sum(self.t_cls)
        if (idx // self.k) % 2 == 0:                       # explore phase: uniform
            order = legal[:]
            rng.shuffle(order)
            return order[:MAX_ATTEMPTS]
        return sorted(legal, key=lambda pi: -self.score(self.mean(ctx, pi), ctx, pi))[:MAX_ATTEMPTS]

    def index(self, ctx, pi, rng):
        return self.score(self.mean(ctx, pi), ctx, pi)


class Oracle(Policy):
    kind = "oracle"
    tracks_timeout = False        # rate-only oracle: knows true auth rates, not latency

    def chain(self, ctx, legal, rng):
        return [pi for _, pi in sorted(((self.score(self.truth[self._i][pi], ctx, pi), pi)
                                        for pi in legal), reverse=True)][:MAX_ATTEMPTS]

    def used_estimate(self, ctx, pi):
        return self.truth[self._i][pi]


# --- run ------------------------------------------------------------------------

def run(policy: Policy, scen, n: int, truth=None, rng_seed=SEED) -> Res:
    r = Res(policy.name)
    for i, (ctx, rows) in enumerate(scen):
        legal = legal_set(ctx)
        if not legal:
            continue
        r.n += 1
        policy._i = i
        policy.truth = truth
        chain_rng = random.Random(f"p:{rng_seed}:{i}")
        chain = policy.chain(ctx, legal, chain_rng)
        if not chain:
            chain = legal[:1]
        txn_reward = 0.0
        attempted = []
        for step, pi in enumerate(chain):
            out, pt, realized = rows[pi]
            attempted.append((pi, out))
            r.attempts += 1
            if policy.rate_est:
                r.stale_sum += abs(policy.used_estimate(ctx, pi) - pt)
                r.stale_n += 1
            txn_reward += realized
            if out == TIMEOUT:
                r.timeouts += 1
                break
            if out == AUTH:
                r.auth += 1
                break
            if out == DECLINE_HARD or step == len(chain) - 1:
                break
            nxt = chain[step + 1]
            w_n, l_n = win_lose(ctx, nxt)
            if policy.retry_rate(ctx, nxt) * w_n + (1.0 - policy.retry_rate(ctx, nxt)) * l_n <= 0.0:
                break
        r.margin += txn_reward
        if i < WARMUP:
            r.early_margin += txn_reward
            r.n_early += 1
        if i >= int(n * DRIFT_AT):
            r.late_margin += txn_reward
            r.n_late += 1
        # discovery probe: consumer-credit, large tickets, after the challenger improved.
        if ctx.cls_i == 0 and i >= int(n * IMPROVE_AT) and ctx.amount_c >= 8000:
            r.disc_n += 1
            if chain[0] == FOXTROT:
                r.disc_fox += 1
        for pi, out in attempted:
            policy.update(ctx, pi, out)
        if isinstance(policy, EXP3):
            policy.reward(ctx, txn_reward, win_lose(ctx, chain[0])[0])
    return r


# --- reporting ------------------------------------------------------------------

def var_table(scen, n):
    """Consideration 1: quantify auth-rate variance across BIN x currency x time."""
    print("[V1] true auth-rate spread, per processor (pts = percentage points)")
    print("     rows at (median amount, mid-day); ranges are across the dimension named")
    hdr = (f"     {'proc':<9} {'base':>5} {'BIN cls':>9} {'region':>9} {'time-of-day':>12} "
           f"{'drift step':>11}")
    print(hdr)
    for pi, p in enumerate(FLEET):
        median_amt = 4600
        per_cls = [p_true(Ctx(ci, 0, median_amt, "EUR", 0), pi, n * 0.25, n) for ci in range(NCLS)]
        per_reg = [p_true(Ctx(0, ri, median_amt, REGIONS[ri][3], 0), pi, n * 0.25, n)
                   for ri in range(len(REGIONS))]
        tod = [1.0 - p.tod_amp * math.sin(2 * math.pi * (TOD_CYCLES * t / n + p.tod_phase))
               for t in range(0, n, max(1, n // 200))]
        cls_sp = max(per_cls) - min(per_cls)
        reg_sp = max(per_reg) - min(per_reg)
        tod_sp = max(tod) - min(tod)
        drift = 0.86 if pi == CHARLIE else (1.15 if pi == FOXTROT else 1.0)
        drift_s = f"x{drift:.2f}" if drift != 1.0 else "-"
        print(f"     {p.name:<9} {100*p.base_auth:>5.1f} {100*cls_sp:>8.1f} "
              f"{100*reg_sp:>8.1f} {100*tod_sp:>11.1f} {drift_s:>11}")

    print("\n     BIN x processor interaction: rank of each processor by auth rate per class")
    print("     (1 = best; the best processor differs by class, so one global weight vector")
    print("      cannot be right for all of them)")
    print(f"     {'cls':<16} " + "".join(f"{p.name:>9}" for p in FLEET))
    for ci, (nm, _ib, _w) in enumerate(BIN_CLASSES):
        rates = [(p.base_auth * p.bin_aff[ci], p.name) for p in FLEET]
        ranks = sorted(rates, reverse=True)
        rank_of = {name: i + 1 for i, (_, name) in enumerate(ranks)}
        print(f"     {nm:<16} " + "".join(f"{rank_of[p.name]:>9d}" for p in FLEET))


def report(title, scen, n, reses):
    print("=" * 100)
    print(title)
    print("=" * 100)
    hdr = (f"{'policy':<16} {'auth%':>6} {'margin c/1k':>11} {'post-drift c/1k':>15} "
           f"{'warmup c/1k':>11} {'staleness MAE pts':>17} {'to%':>5}")
    print(hdr)
    print("-" * len(hdr))
    for r in reses:
        mae = 100 * r.stale_sum / max(r.stale_n, 1) if r.stale_n else float("nan")
        mae_s = f"{mae:>8.2f}" if r.stale_n else f"{'n/a':>8}"
        print(f"{r.name:<16} {100*r.auth/r.n:>6.2f} {1000*r.margin/r.n:>11.1f} "
              f"{1000*r.late_margin/max(r.n_late,1):>15.1f} "
              f"{1000*r.early_margin/max(r.n_early,1):>11.1f} {mae_s:>17} {100*r.timeouts/r.n:>5.2f}")
    print()


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40_000
    scen = build_scenario(n, SEED)
    truth = {i: {pi: rows[pi][1] for pi in range(NPROC)} for i, (_, rows) in enumerate(scen)}

    print("#3 evidence spike: bandit vs static table; TS vs UCB / eps-greedy / EXP3")
    print(f"python {sys.version.split()[0]} | {n:,} txns | seed {SEED} | lambda_to={LAM_TO:.0f}")
    print()
    var_table(scen, n)
    print()

    # Stale, noisy snapshot for the two static tables (nobody is handed truth except oracle).
    rng = random.Random(SEED)
    CLS_PRIOR = (1.000, 0.985, 0.955, 0.905)  # the merchant's plausible per-class prior
    est = [[min(0.99, max(0.05, FLEET[pi].base_auth * CLS_PRIOR[ci] + rng.gauss(0.0, 0.03)))
            for ci in range(NCLS)] for pi in range(NPROC)]

    # Global "60/30/10" weights from stale priors at the median ticket: the merchant's best
    # hand-set table. Mix-weighted mean rate per processor, EV at the median amount.
    amt = 4600.0
    w = []
    for pi, p in enumerate(FLEET):
        mix_rate = sum(est[pi][ci] * BIN_CLASSES[ci][2] for ci in range(NCLS))
        win = amt * (SELL_BPS - p.cost_bps) / 1e4 + (SELL_FIXED - p.fixed_fee)
        ev = max(0.0, mix_rate * win - (1 - mix_rate) * p.attempt_fee)
        w.append(ev)
    tot = sum(w)
    gw = [x / tot for x in w]

    policies = [
        TableGlobal("table_global", gw, est),
        TableSnapshot("table_snapshot", est),
        TableSnapshotLat("table_snapshot_lat", est),
        Thompson("ts", est),
        UCB("ucb", est),
        EpsGreedy("eps_05", 0.05, est),
        EpsGreedy("eps_10", 0.10, est),
        EXP3("exp3", est),
        Greedy("greedy", est),
        Freeze("freeze_1k", 1000, est),
        Freeze("freeze_4k", 4000, est),
        Freeze("freeze_16k", 16000, est),
        AB("ab_1k", 1000, est),
        AB("ab_4k", 4000, est),
        Oracle("oracle", est),
    ]
    reses = [run(p, scen, n, truth=truth) for p in policies]
    # final estimate of foxtrot's consumer-credit rate, per policy (the discovery probe)
    fox_ctx = Ctx(0, 0, 8000, "EUR", 0)
    for p, r in zip(policies, reses):
        if isinstance(p, TableSnapshot):
            r.fox_mean = est[FOXTROT][0]
        elif p.rate_est and not isinstance(p, Oracle):
            r.fox_mean = p.mean(fox_ctx, FOXTROT)

    report(f"scenario synthetic-fleet-v1 | seed {SEED} | {n:,} txns | drift at t={DRIFT_AT}",
           scen, n, reses)

    ts = next(r for r in reses if r.name == "ts")
    print("[F1] gaps vs Thompson sampling (margin cents/1k)")
    print("     warmup = first 6k txns (where cold-start exploration is paid);")
    print("     post-drift = after t=0.6 (where adaptation is earned)")
    print(f"     {'policy':<16} {'full-run':>9} {'post-drift':>11} {'warmup':>9}")
    for r in reses:
        if r is ts:
            continue
        g_full = 1000 * r.margin / r.n - 1000 * ts.margin / ts.n
        g_late = 1000 * r.late_margin / max(r.n_late, 1) - 1000 * ts.late_margin / max(ts.n_late, 1)
        g_early = 1000 * r.early_margin / max(r.n_early, 1) - 1000 * ts.early_margin / max(ts.n_early, 1)
        print(f"  {r.name:<16} {g_full:+9.1f} {g_late:+11.1f} {g_early:+9.1f}")

    print("""
  Reading it: the warmup column is the cold-start exploration tax; the post-drift column
  is adaptation. table_global and both table_snapshot variants pay no tax and then miss
  the outage (each snapshot loses ~42-44 euros per 1k post-drift; the latency-aware one
  knows charlie's nominal ~10% timeout rate but not the x3 degradation); ucb and eps pay
  a permanent exploration tax (a bonus / a noise floor); exp3 pays a variance tax on
  every decision; ab pays a uniform re-measurement tax each cycle; greedy matches TS on
  margin but has no uncertainty to quote (see F3); freeze approximates TS while routing
  through a stale table between freezes. That is the "bandit, conditional on pricing
  what a table cannot learn" framing ADR-0002 handed this ticket.
  Note the oracle row: it is a RATE-only oracle (true auth rates, no latency knowledge),
  so the deadline collision beats it -- TS prices learned timeouts via pi_hat*lambda_to
  and edges it by +17.6 c/1k. Negative regret against a rate-only oracle is not a win;
  it is the mis-specified-oracle case ADR-0002 already documented.""")

    print("\n[F3] why the architecture is TS + drift detection, not TS alone")
    print("     Share of large consumer-credit tickets routed to foxtrot after it improved")
    print("     at t=0.8, and each policy's final estimate of foxtrot's cc rate. The rate-")
    print("     only oracle routes it 60%+ of the time (it knows foxtrot improved); every")
    print("     online learner is slow to re-discover it, because its posterior is confident")
    print("     from pre-event data. TS's posterior-proportional exploration does NOT rescue")
    print("     this -- a posterior reset on detected drift (#8) is what does. That is the")
    print("     dependency, and it is also why 'deterministic argmax for production' is")
    print("     rejected: greedy has no exploration AND no uncertainty to trigger a reset.")
    print(f"     {'policy':<16} {'foxtrot share':>14} {'foxtrot cc estimate':>20}")
    for r in reses:
        if r.disc_n == 0:
            continue
        share = 100.0 * r.disc_fox / r.disc_n
        est_s = f"{r.fox_mean:>8.3f}" if r.fox_mean == r.fox_mean else f"{'n/a':>8}"
        print(f"     {r.name:<16} {share:>13.1f}% {est_s:>20}")

    print("\n[F2] the frozen-table hybrid: what does auditability-by-freezing cost?")
    print("     (freeze_k = alternate TS-explore k txns / argmax-of-frozen-table k txns)")
    print(f"     {'policy':<16} {'margin c/1k':>11} {'post-drift':>11} {'staleness MAE pts':>17}")
    for r in reses:
        if not r.name.startswith(("freeze", "table_snapshot")):
            continue
        mae = 100 * r.stale_sum / max(r.stale_n, 1) if r.stale_n else float("nan")
        print(f"     {r.name:<16} {1000*r.margin/r.n:>11.1f} "
              f"{1000*r.late_margin/max(r.n_late,1):>11.1f} {mae:>17.2f}")
    print("     -> a table you can audit on paper is a table that is stale between freezes;")
    print("        the staleness MAE is the number on the audit/explainability tradeoff.")

    print("\n" + "=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
