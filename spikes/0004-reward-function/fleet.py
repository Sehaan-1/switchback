#!/usr/bin/env python3
"""Decision ticket #4 evidence: which terms must the reward function contain, and what
does each cost -- to omit, or to add twice?

Scenarios (config inline; #6 owns the real harness, #17 the committed definitions):

  correlated-3ds-v1     frictionless rate correlates with auth quality, as it does when
                        3DS-weak acquirers are simply weaker acquirers.
  decorrelated-3ds-v1   same costs and auth rates, 3DS quality permuted across
                        processors. The only way to tell a correct 3DS treatment from a
                        lucky one: if a term is harmless in one scenario and harmful in
                        the other, the scenario was hiding it.

The question #4 actually has to answer
---------------------------------------
Every proposal for "how 3DS enters the reward" assumes the bandit learns P(issuer
approves) and then needs a dropout multiplier on top. But the label the engine can collect
is the *transaction outcome*, in which an abandoned challenge is already a lost sale. So
the multiplier double counts. Three candidate designs, and this file prices all of them:

  A  end-to-end label (chosen)   authorized=1; declined=0; abandoned=0; timeout EXCLUDED.
                                 Score = p*win + (1-p)*lose, with attempt fees.
  B  A + 3DS multiplier           the naive fix. Over-discounts 3DS-weak processors.
  C  abandonment censored out     "#11.4 says we cannot attribute abandonment", so drop
                                 those rows. Learns P(auth | reached), not P(capture).
  D  timeout counted as decline   poisons the auth estimate to buy a routing signal.
  E  no attempt fees in the score  makes declines look free on small tickets.
  F  three-way outcome (proposed) timeout gets its own per-arm count and an explicit
                                 ambiguity price; the auth rate stays an auth rate.
  G  smooth per-ms penalty        the other way to price latency; calibrated to cost the
                                 same as F at the fleet's worst processor.

Methodology
-----------
* Coupled randomness: the scenario (context + outcome for every processor) is generated
  once, policy-independent, so policy gaps are not simulation noise.
* The oracle is a *policy*, run through the identical execution loop with true rates --
  not an expected-value upper bound. A rate-only oracle is beatable by anything that
  notices the deadline (see vsRate < 0), which is why the clairvoyant bound is reported
  alongside it and why #17 must name which oracle it means.
* Stale, noisy estimates for the static table; Jeffreys prior for the bandit. Nobody is
  handed truth except the labelled oracle.
* Deterministic: fixed seed, stdlib only, no network.

Run:  python3 spikes/0004-reward-function/fleet.py [n_transactions]
"""

from __future__ import annotations

import math
import random
import statistics
import sys
from dataclasses import dataclass, replace

SEED = 20_260_911
WARMUP = 6_000
DRIFT_AT = 0.62          # 'delta' degrades: auth x0.86, latency x3
DROPOUT = 0.22           # P(abandon | challenged); #11 owns its estimation
LIABILITY_SHIFT = 1.055  # issuer approves more when a challenge completed
NON3DS_PENALTY = 0.965   # issuer approves less when SCA was skipped
MAX_ATTEMPTS = 2
DEADLINE_MS = 900
SELL_BPS, SELL_FIXED = 128, 0

AUTH, DECLINE_SOFT, ABANDON, TIMEOUT, DECLINE_HARD = 1, 0, 2, 3, 4


@dataclass(frozen=True)
class Processor:
    name: str
    cost_bps: int          # what we pay, bps of amount
    fixed_fee: int         # cents, charged on authorization
    attempt_fee: int       # cents, charged on submission (declines are not free)
    base_auth: float       # P(approve | reached) for consumer credit, EUR, mid-ticket
    soft_decline_share: float
    frictionless: float    # P(3DS frictionless) when SCA applies
    lat_p50_ms: int
    lat_p99_ms: int
    currencies: frozenset
    eu_capable: bool


FLEET = (
    Processor("alpha",   95, 10, 6, 0.915, 0.55, 0.90, 190,  640, frozenset({"EUR", "USD"}), True),
    Processor("bravo",   72, 10, 5, 0.884, 0.62, 0.78, 240,  900, frozenset({"EUR", "USD"}), True),
    Processor("charlie", 64,  8, 4, 0.842, 0.71, 0.68, 300, 1150, frozenset({"EUR", "GBP"}), True),
    Processor("delta",  110, 12, 7, 0.935, 0.48, 0.92, 170,  520, frozenset({"EUR", "USD"}), True),
    Processor("echo",    86,  9, 5, 0.900, 0.58, 0.85, 210,  760, frozenset({"USD"}), False),
    Processor("foxtrot", 58, 25, 9, 0.806, 0.74, 0.60, 420, 1750, frozenset({"EUR", "USD"}), True),
)
NPROC = len(FLEET)
DELTA = 3
HIGH_FRICTION: list[int] = []

BIN_CLASSES = (  # name, interchange bps, auth multiplier, corporate
    ("consumer_credit", 115, 1.00, False),
    ("consumer_debit", 60, 0.985, False),
    ("corporate", 135, 0.955, True),
    ("prepaid", 90, 0.905, False),
)
REGIONS = (("SEPA", 1.00, True), ("UK", 0.995, True), ("US", 0.985, False),
           ("LATAM", 0.940, False), ("APAC", 0.950, False))


@dataclass(frozen=True)
class Ctx:
    region_i: int
    cls_i: int
    amount_c: int
    currency: str
    sca: bool
    floor_margin_bps: int
    deadline_ms: int


def set_fleet(fleet):
    """Rebind the fleet for a scenario. Ugly on purpose: the module stays readable as a
    single model, and the two scenarios differ in exactly one thing -- which processor is
    good at 3DS."""
    global FLEET, HIGH_FRICTION
    FLEET = fleet
    HIGH_FRICTION = [pi for pi, p in enumerate(FLEET) if p.frictionless < 0.72]


def p_reach_and_pdrop(ctx: Ctx, pi: int, degraded: bool) -> tuple[float, float]:
    """(P(issuer approves | request reaches them), P(customer abandons the challenge)).

    Two numbers because they are observed on different events and owned by different
    tickets: the approval rate is the processor's (#7), the abandonment rate is the
    merchant funnel's (#11).
    """
    p = FLEET[pi]
    cls, reg = BIN_CLASSES[ctx.cls_i], REGIONS[ctx.region_i]
    auth = p.base_auth * cls[2] * reg[1]
    amt = ctx.amount_c / 100.0
    auth *= 1.0 - min(0.09, max(0.0, (amt - 800.0) / 40_000.0))
    auth *= 1.0 - min(0.05, max(0.0, (60.0 - amt) / 4000.0))
    auth *= 1.03 if (reg[0] == "SEPA" and p.eu_capable) else 0.97
    if degraded:
        auth *= 0.86
    pdrop = 0.0
    if ctx.sca:
        challenged = 1.0 - p.frictionless
        pdrop = min(0.85, challenged * DROPOUT * (1.0 + min(0.6, amt / 1500.0))
                    * (1.15 if cls[3] else 1.0))
        auth *= (1.0 - challenged) + challenged * LIABILITY_SHIFT
    else:
        auth *= NON3DS_PENALTY
    return min(0.995, max(0.02, auth)), pdrop


def latency(ctx: Ctx, pi: int, rng: random.Random, degraded: bool) -> float:
    p = FLEET[pi]
    med = p.lat_p50_ms * (3.0 if degraded else 1.0)
    tail = p.lat_p99_ms * (3.5 if degraded else 1.0)
    u = rng.random()
    if u < 0.5:
        return med * (0.6 + 0.8 * u)
    return med + (tail - med) * (((u - 0.5) / 0.5) ** 1.6)


def legal_set(ctx: Ctx) -> list[int]:
    """Constraint layer BEFORE the bandit, so every sampled arm is legal and propensities
    are computed over the legal set. Slice of #5's taxonomy needed by this experiment."""
    out = []
    for pi, p in enumerate(FLEET):
        if ctx.currency not in p.currencies:
            continue
        if ctx.sca and REGIONS[ctx.region_i][0] == "SEPA" and not p.eu_capable:
            continue
        if SELL_BPS - p.cost_bps < ctx.floor_margin_bps:
            continue
        out.append(pi)
    return out


def win_lose(ctx: Ctx, pi: int) -> tuple[float, float]:
    win = ctx.amount_c * (SELL_BPS - FLEET[pi].cost_bps) / 10_000.0 + (SELL_FIXED - FLEET[pi].fixed_fee)
    return win, -float(FLEET[pi].attempt_fee)


def score_margin(p_hat: float, ctx: Ctx, pi: int) -> float:
    win, lose = win_lose(ctx, pi)
    return p_hat * win + (1.0 - p_hat) * lose


def build_scenario(n: int, seed: int, drift_at: float = DRIFT_AT):
    scen, tsum, tn = [], [[0.0] * NPROC for _ in range(len(BIN_CLASSES))], [[0] * NPROC for _ in range(len(BIN_CLASSES))]
    for i in range(n):
        rng = random.Random(f"{seed}:{i}")
        region_i = rng.randrange(len(REGIONS))
        cls_i = rng.choices(range(len(BIN_CLASSES)), weights=(58, 27, 10, 5))[0]
        amount_c = max(60, int(math.exp(rng.gauss(math.log(46.0), 0.95)) * 100))
        currency = "EUR" if region_i == 0 else ("GBP" if region_i == 1 else "USD")
        amt = amount_c / 100.0
        sca = REGIONS[region_i][2] and (amt > 30.0 or rng.random() < 0.25)
        floor = rng.choices([0, 25, 40], weights=(82, 12, 6))[0]
        ctx = Ctx(region_i, cls_i, amount_c, currency, sca, floor, DEADLINE_MS)
        degraded = (i / n) > drift_at
        lat = [latency(ctx, pi, rng, degraded and pi == DELTA) for pi in range(NPROC)]
        rows = []
        for pi in range(NPROC):
            reach, pdrop = p_reach_and_pdrop(ctx, pi, degraded and pi == DELTA)
            u_ab, u_au, u_soft = rng.random(), rng.random(), rng.random()
            win, _ = win_lose(ctx, pi)
            if lat[pi] > ctx.deadline_ms:
                out = TIMEOUT                    # deadline masks everything: result unknown
            elif u_ab < pdrop:
                out = ABANDON                    # never reached the issuer; no retry helps
            elif u_au < reach:
                out = AUTH
            elif u_soft < FLEET[pi].soft_decline_share:
                out = DECLINE_SOFT               # retryable on a different processor
            else:
                out = DECLINE_HARD               # never retry this card
            realized = win if out == AUTH else -float(FLEET[pi].attempt_fee)
            rows.append((out, (1.0 - pdrop) * reach, int(round(realized))))
            tsum[ctx.cls_i][pi] += (1.0 - pdrop) * reach
            tn[ctx.cls_i][pi] += 1
        scen.append((ctx, rows, lat))
    return scen, tsum, tn


@dataclass
class Res:
    name: str
    n: int = 0
    auth: int = 0
    timeouts: int = 0
    abandoned: int = 0
    margin: float = 0.0
    cost: float = 0.0
    attempts: int = 0
    hi_friction: int = 0
    n_sca: int = 0
    auth_sca: int = 0
    margin_sca: float = 0.0
    hi_fric_sca: int = 0
    lat: list = None       # type: ignore[assignment]
    ours_post: float = 0.0
    n_late: int = 0
    margin_late: float = 0.0
    to_late: int = 0
    regret: float = float("nan")
    clair: float = float("nan")


class Policy:
    KAPPA_G = 0.0069       # cents/ms: makes G cost the same as F(18) at foxtrot's 1750ms p99

    def __init__(self, name: str, kind: str, variant: str = "A", q: float = 0.0,
                 lam_to: float = 18.0, retry: str = "ev", gate_to: float = 0.05):
        self.name, self.kind, self.variant, self.q = name, kind, variant, q
        self.lam_to, self.retry, self.gate_to = lam_to, retry, gate_to
        nc = len(BIN_CLASSES)
        self.alpha = [[0.5] * NPROC for _ in range(nc)]
        self.beta = [[0.5] * NPROC for _ in range(nc)]
        self.to = [[0.5] * NPROC for _ in range(nc)]
        # learned P(timeout | processor): the deadline-collision rate. Counted, not timed --
        # a latency *mean* cannot see a tail, which was this file's first bug.
        self.to_n = [0.5] * NPROC
        self.n_proc = [1] * NPROC
        self.n_updates = 0

    def rate(self, ctx: Ctx, pi: int):
        """The policy's own point estimate of P(authorize). Used to price a retry."""
        if self.kind == "bandit":
            a, b = self.alpha[ctx.cls_i][pi], self.beta[ctx.cls_i][pi]
            return a / (a + b)
        return None

    def chain(self, ctx: Ctx, legal: list[int], est, p_true, rng) -> list[int]:
        if not legal:
            return []
        if self.kind == "cost_only":
            return sorted(legal, key=lambda pi: FLEET[pi].cost_bps * ctx.amount_c / 1e4 + FLEET[pi].fixed_fee)
        if self.kind == "cost_only_q":
            priced = sorted((FLEET[pi].cost_bps * ctx.amount_c / 1e4 + FLEET[pi].fixed_fee, pi)
                            for pi in legal if est[pi][ctx.cls_i] >= self.q)
            return [pi for _, pi in priced]
        if self.kind == "auth_only":
            return sorted(legal, key=lambda pi: -est[pi][ctx.cls_i])
        if self.kind == "oracle":
            return sorted(legal, key=lambda pi: -score_margin(p_true[pi], ctx, pi))
        if self.kind == "static_table":
            return sorted(legal, key=lambda pi: -score_margin(est[pi][ctx.cls_i], ctx, pi))
        if self.kind == "bandit":
            if self.variant in ("H", "H+F"):
                # eligibility gate on the learned timeout rate: the constraint layer using
                # engine-observed state. A gate before a price, because a gate removes the
                # ambiguity at the source and costs nothing in the objective.
                ok = [pi for pi in legal if self.to_n[pi] / self.n_proc[pi] <= self.gate_to]
                legal = ok or legal
            scored = []
            for pi in legal:
                a, b = self.alpha[ctx.cls_i][pi], self.beta[ctx.cls_i][pi]
                theta = rng.betavariate(a, b)
                if self.variant == "B" and ctx.sca:
                    # the naive design from #4.3: discount again by P(challenge) x P(drop).
                    # The learned theta already contains the abandonment and the liability
                    # shift, so this subtracts a cost the outcome already paid for.
                    ch = 1.0 - FLEET[pi].frictionless
                    theta = theta * (1.0 - ch * DROPOUT)
                s = score_margin(theta, ctx, pi)
                if self.variant in ("F", "H+F"):
                    t = self.to[ctx.cls_i][pi]
                    s -= self.lam_to * rng.betavariate(t, a + b - 1.0 + t)
                if self.variant == "G":
                    s -= self.KAPPA_G * FLEET[pi].lat_p99_ms
                scored.append((s, pi))
            return [pi for _, pi in sorted(scored, reverse=True)]
        raise ValueError(self.kind)


def run(policy: Policy, scen, est) -> Res:
    r = Res(policy.name, lat=[])
    for i, (ctx, rows, lat) in enumerate(scen):
        legal = legal_set(ctx)
        if not legal:
            continue
        r.n += 1
        if ctx.sca:
            r.n_sca += 1
        ours = 0.0
        chain = policy.chain(ctx, legal, est, [rows[pi][1] for pi in range(NPROC)],
                             random.Random(f"p:{i}"), )[:MAX_ATTEMPTS]
        if not chain:
            chain = legal[:1]
        if chain[0] in HIGH_FRICTION:
            r.hi_friction += 1
            if ctx.sca:
                r.hi_fric_sca += 1
        for step, pi in enumerate(chain):
            out = rows[pi][0]
            r.attempts += 1
            policy.n_proc[pi] += 1          # denominator of the learned timeout rate
            r.cost += FLEET[pi].cost_bps * ctx.amount_c / 1e4 + FLEET[pi].attempt_fee
            r.lat.append(lat[pi])
            if out == TIMEOUT:
                r.timeouts += 1
                policy.to_n[pi] += 1.0
                ours -= FLEET[pi].attempt_fee
                break
            if out == AUTH:
                r.auth += 1
                if ctx.sca:
                    r.auth_sca += 1
                ours += rows[pi][2]
                break
            if out == ABANDON:
                r.abandoned += 1
                ours -= FLEET[pi].attempt_fee
                break
            ours -= FLEET[pi].attempt_fee
            if out == DECLINE_HARD or step == len(chain) - 1:
                break
            # --- should we submit again? #4.4: a retry has a price, not just a benefit.
            if policy.retry == "never":
                break
            if policy.retry == "ev":
                nxt = chain[step + 1]
                p_n = policy.rate(ctx, nxt)
                est_p = p_n if p_n is not None else est[nxt][ctx.cls_i]
                w_n, l_n = win_lose(ctx, nxt)
                if est_p * w_n + (1.0 - est_p) * l_n <= 0.0:
                    break        # a submission that loses money on failure is not free
            elif policy.retry != "always":
                raise ValueError(policy.retry)
        r.margin += ours
        if i >= int(len(scen) * 0.75):
            r.n_late += 1
            r.margin_late += ours
            if rows[chain[0]][0] == TIMEOUT:
                r.to_late += 1
        if ctx.sca:
            r.margin_sca += ours
        if i >= WARMUP:
            r.ours_post += ours
        if policy.kind == "bandit":
            for pi in chain:
                out = rows[pi][0]
                if out == TIMEOUT:
                    if policy.variant in ("F", "H+F"):
                        policy.to[ctx.cls_i][pi] += 1.0
                    label = 0 if policy.variant == "D" else None
                elif out == ABANDON:
                    label = None if policy.variant == "C" else 0
                else:
                    label = 1 if out == AUTH else 0
                if label is None:
                    continue
                policy.alpha[ctx.cls_i][pi] += label
                policy.beta[ctx.cls_i][pi] += 1 - label
                policy.n_updates += 1
    return r


# (name, kind, label-variant, q-for-frontier, lambda_timeout, retry-mode)
POLICIES = (
    ("cost_only", "cost_only", "A", 0.0, 0.0, "ev", 0.05),
    ("auth_only", "auth_only", "A", 0.0, 0.0, "ev"),
    ("static_table", "static_table", "A", 0.0, 0.0, "ev"),
    ("A chosen", "bandit", "A", 0.0, 0.0, "ev"),
    ("B +3DS multiplier", "bandit", "B", 0.0, 0.0, "ev"),
    ("C censor abandons", "bandit", "C", 0.0, 0.0, "ev"),
    ("D timeout=decline", "bandit", "D", 0.0, 0.0, "ev"),
    ("E retry unpriced", "bandit", "A", 0.0, 0.0, "always"),
    ("A_never retry", "bandit", "A", 0.0, 0.0, "never"),
    ("F to=18", "bandit", "F", 0.0, 18.0, "ev"),
    ("F to=45", "bandit", "F", 0.0, 45.0, "ev"),
    ("G smooth per-ms", "bandit", "G", 0.0, 0.0, "ev"),
    ("H gate 5%", "bandit", "H", 0.0, 0.0, "ev", 0.05),
    ("H gate 15%", "bandit", "H", 0.0, 0.0, "ev", 0.15),
    ("H+F gate+price", "bandit", "H+F", 0.0, 18.0, "ev", 0.15),
    ("oracle rate-only", "oracle", "A", 0.0, 0.0, "ev"),
)


def experiment(label: str, fleet, n: int) -> dict:
    set_fleet(fleet)
    rng = random.Random(SEED)
    est = [[min(0.99, max(0.05, FLEET[pi].base_auth * BIN_CLASSES[ci][2] + rng.gauss(0.0, 0.03)))
            for ci in range(len(BIN_CLASSES))] for pi in range(NPROC)]
    scen, tsum, tn = build_scenario(n, SEED)
    policies = [Policy(*args) for args in POLICIES]
    res = [run(p, scen, est) for p in policies]

    oracle_post = next(r.ours_post for r in res if r.name == "oracle rate-only")
    clair = 0.0
    for i, (ctx, rows, _lat) in enumerate(scen):
        if i < WARMUP:
            continue
        legal = legal_set(ctx)
        if legal:
            clair += max(rows[pi][2] for pi in legal)
    for r in res:
        r.regret = 100.0 * (oracle_post - r.ours_post) / max(abs(oracle_post), 1e-9)
        r.clair = 100.0 * (clair - r.ours_post) / max(abs(clair), 1e-9)

    bias = {}
    for pol, r in zip(policies, res):
        if pol.kind != "bandit":
            continue
        e, nn, worst = 0.0, 0, ("", 0.0)
        for ci in range(len(BIN_CLASSES)):
            for pi in range(NPROC):
                if tn[ci][pi] < 40:
                    continue
                d = abs(pol.alpha[ci][pi] / (pol.alpha[ci][pi] + pol.beta[ci][pi])
                        - tsum[ci][pi] / tn[ci][pi])
                e, nn = e + d * tn[ci][pi], nn + tn[ci][pi]
                if d > worst[1]:
                    worst = (FLEET[pi].name, d)
        bias[r.name] = (100.0 * e / max(nn, 1), worst)

    print("=" * 100)
    print(f"scenario {label} | seed {SEED} | {n:,} txns | {sum(1 for c, _, _ in scen if legal_set(c)):,} legal")
    print("fleet   " + ", ".join(f"{p.name}({p.cost_bps}bps,auth{p.base_auth:.3f},fric{p.frictionless:.2f})"
                                 for p in FLEET))
    print("=" * 100)
    hdr = (f"{'policy':<20} {'auth%':>6} {'margin c/1k':>11} {'cost c/1k':>9} {'att':>6} "
           f"{'aband%':>7} {'to%':>5} {'p99':>5} {'3DS-auth%':>10} {'vsRate%':>8} {'vsClair%':>9}")
    print(hdr)
    print("-" * len(hdr))
    A = next(r for r in res if r.name == "A chosen")
    for r in res:
        p99 = statistics.quantiles(r.lat, n=100)[98] if len(r.lat) > 500 else float("nan")
        sca_auth = 100.0 * r.auth_sca / max(r.n_sca, 1)
        print(f"{r.name:<20} {100*r.auth/r.n:>6.2f} {1000*r.margin/r.n:>10.1f} "
              f"{1000*r.cost/r.n:>8.1f} {r.attempts/r.n:>6.3f} {100*r.abandoned/r.n:>7.2f} "
              f"{100*r.timeouts/r.n:>5.2f} {p99:>5.0f} {sca_auth:>10.2f} {r.regret:>8.2f} {r.clair:>9.2f}")
    print("\n  gaps vs A (margin cents/1k, auth pts, SCA-subset auth pts, |p_hat-p_true| pts):")
    for nm in ("B +3DS multiplier", "C censor abandons", "D timeout=decline",
               "E retry unpriced", "A_never retry", "F to=18", "F to=45",
               "G smooth per-ms", "H gate 5%", "H gate 15%", "H+F gate+price"):
        x = next(r for r in res if r.name == nm)
        print(f"    {nm:<20} {1000*x.margin/x.n - 1000*A.margin/A.n:+8.1f} "
              f"{100*x.auth/x.n - 100*A.auth/A.n:+8.2f} "
              f"{100*x.auth_sca/x.n_sca - 100*A.auth_sca/A.n_sca:+8.2f} "
              f"{bias[nm][0] - bias[A.name][0]:+8.2f}  [worst arm {bias[nm][1][0]} "
              f"{100*bias[nm][1][1]:+.1f} pts]")
    # Estimate quality needs a drift-free window: with the drift event on, every posterior
    # lags the truth and the ranking measures adaptation, not label handling.
    nd_scen, nd_tsum, nd_tn = build_scenario(n, SEED, drift_at=float("inf"))
    nd_bias: dict[str, tuple[float, tuple[str, float]]] = {}
    print("\n  |p_hat - p_true| on the SAME policies with the drift event turned OFF, so the")
    print("  number is label handling and not adaptation lag (arms with <40 obs excluded):")
    print(f"     {'variant':<20} {'MAE pts':>8} {'worst arm':>22}")
    for nm, kind, var, q, lam, ret, *gate in POLICIES:
        if kind != "bandit":
            continue
        pol = Policy(nm, kind, var, q, lam, ret, *(gate or [0.05]))
        rr = run(pol, nd_scen, est)
        e, nn, worst = 0.0, 0, ("", 0.0)
        for ci in range(len(BIN_CLASSES)):
            for pi in range(NPROC):
                if nd_tn[ci][pi] < 40:
                    continue
                d = abs(pol.alpha[ci][pi] / (pol.alpha[ci][pi] + pol.beta[ci][pi])
                        - nd_tsum[ci][pi] / nd_tn[ci][pi])
                e, nn = e + d * nd_tn[ci][pi], nn + nd_tn[ci][pi]
                if d > worst[1]:
                    worst = (FLEET[pi].name, d)
        if nn:
            nd_bias[nm] = (100 * e / nn, worst)
            print(f"     {nm:<20} {100*e/nn:>8.2f} {worst[0] + ' ' + format(100*worst[1], '.1f') + ' pts':>22}")
        nd_bias.setdefault(nm, (float("nan"), ("", 0.0)))
    print("\n  last 25% of the run, i.e. AFTER the drift event. G is handed the static latency")
    print("  table and cannot see a processor degrade; F and H learn it. This window is the")
    print("  only place that difference is visible, so it is where the comparison belongs.")
    print(f"     {'policy':<20} {'margin c/1k':>12} {'to%':>7}")
    for r in res:
        if not r.n_late:
            continue
        print(f"     {r.name:<20} {1000*r.margin_late/r.n_late:>12.1f} {100*r.to_late/r.n_late:>7.2f}")
    by_name = {r.name: r for r in res}
    return {"res": res, "bias": bias, "nd_bias": nd_bias, "A": A, "by_name": by_name,
            "scen": scen, "est": est, "n": n}


def frontier_report(out: dict, n: int) -> None:
    """F2: make '+X pts auth rate at equal effective cost' computable."""
    scen, est = out["scen"], out["est"]
    print("\n[F2] '+X pts authorization rate at equal effective cost', as a procedure")
    print("     baseline family: cheapest processor subject to p_stale >= q, q swept.")
    print("     matched on cost per AUTHORIZED transaction: cost-per-request is gameable by")
    print("     declining more, and declines still cost submission fees.")
    front = []
    for q in (0.00, 0.55, 0.68, 0.75, 0.80, 0.84, 0.87, 0.90, 0.93, 0.95):
        r = run(Policy(f"q{q}", "cost_only_q", q=q), scen, est)
        front.append((r.cost / max(r.auth, 1), 100.0 * r.auth / max(r.n, 1),
                      1000.0 * r.margin / max(r.n, 1), q))
    print(f"     {'q':>5} | {'cents/auth txn':>14} | {'auth%':>7} | {'margin/1k':>10}")
    for c, a, m, q in sorted(front):
        print(f"     {q:>5.2f} | {c:>14.3f} | {a:>7.2f} | {m:>10.1f}")
    A = out["A"]
    bc = A.cost / max(A.auth, 1)
    ba = 100.0 * A.auth / A.n
    sp = sorted(front)
    interp = None
    for (c0, a0, _, _), (c1, a1, _, _) in zip(sp, sp[1:]):
        if c0 <= bc <= c1 and c1 > c0:
            interp = a0 + (a1 - a0) * (bc - c0) / (c1 - c0)
            break
    if interp is None:
        side = "left" if bc < sp[0][0] else "right"
        interp = min(sp, key=lambda t: abs(t[0] - bc))[1]
        print(f"     note: bandit cost/auth {bc:.2f} lies off the swept frontier ({side});")
        print("           reporting the nearest frontier point, which understates the gap")
    print(f"     bandit A: {bc:.3f} cents/authorized txn, auth {ba:.2f}%")
    print(f"     frontier at matched cost: auth {interp:.2f}%   ->  {ba-interp:+.2f} pts")
    return ba - interp


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 25_000
    prim = experiment("correlated-3ds-v1", FLEET, n)
    perm = (0.60, 0.92, 0.85, 0.68, 0.78, 0.90)   # 3DS quality shuffled across processors
    dec = tuple(replace(p, frictionless=perm[pi]) for pi, p in enumerate(FLEET))
    sec = experiment("decorrelated-3ds-v1", dec, n)
    print("\n--- frontier, primary fleet (cost and auth correlated) ---")
    prim["frontier"] = frontier_report(prim, n)
    print("\n--- frontier, 3DS-decorrelated fleet (same costs, same auth rates) ---")
    sec["frontier"] = frontier_report(sec, n)

    print("\n[F5] cross-scenario check: is the 3DS treatment load-bearing or scenario-luck?")
    for key, label in (("B +3DS multiplier", "the naive multiplier"),
                       ("C censor abandons", "the censored label")):
        x1 = next(r for r in prim["res"] if r.name == key)
        x2 = next(r for r in sec["res"] if r.name == key)
        g1 = 1000 * x1.margin / x1.n - 1000 * prim["A"].margin / prim["A"].n
        g2 = 1000 * x2.margin / x2.n - 1000 * sec["A"].margin / sec["A"].n
        print(f"     {key:<20} vs A: correlated {g1:+7.1f}/1k  decorrelated {g2:+7.1f}/1k   ({label})")
    print("     A term that changes sign between scenarios is not decoration you can skip:")
    print("     it is a term whose error is invisible in the fleet you happened to simulate.")
    print("     -> ship the label taxonomy (A) plus the explicit ambiguity price (F); reject")
    print("        the multiplier (B), the censoring (C), and the poisoned posterior (D).")
    def gap(out, nm):
        x, a = out["by_name"][nm], out["A"]
        return (1000 * x.margin / x.n - 1000 * a.margin / a.n,
                100 * x.auth / x.n - 100 * a.auth / a.n,
                out["nd_bias"].get(nm, (float("nan"), ("", 0)))[0])

    print("\n[F6] findings")
    for nm, why in (("A chosen", "reference point: the chosen design (end-to-end label)"),
                    ("B +3DS multiplier", "the multiplier double counts, but the damage shows"
                                              " up only when 3DS quality is decorrelated"),
                    ("C censor abandons", "'we cannot attribute abandonment' makes the posterior"
                                          " estimate the wrong quantity"),
                    ("D timeout=decline", "buys margin by poisoning the rate estimate"),
                    ("E retry unpriced", "retries priced by the fee term vs ignored"),
                    ("A_never retry", "no fallback at all"),
                    ("H gate 5%", "hard gate on a LEARNED statistic starves excluded arms"),
                    ("F to=45", "soft price on the same statistic")):
        g1, a1, b1 = gap(prim, nm)
        g2, a2, b2 = gap(sec, nm)
        print(f"  {nm:<20} margin {g1:+7.1f} (corr) / {g2:+6.1f} (decor)   "
              f"auth {a1:+5.2f}/{a2:+5.2f}   MAE {b1:5.2f}/{b2:5.2f} pts")
        print(f"      {why}")
    print("""
  1. The 3DS multiplier (B) leaves the posterior untouched -- it is a scoring change, not
     a labelling one, so MAE is identical to A (2.25 vs 2.25). What it distorts is the
     RANKING, and the distortion flips sign with the fleet: +64.8 c/1k correlated,
     -11.5 decorrelated. A term you cannot tune because its sign depends on which
     processors are 3DS-strong-but-auth-weak is a term to drop: the label already
     carries the information, at zero cost.
  2. Censoring abandons (C) raises estimate error 2.2x (2.25 -> 5.05 MAE pts) for a margin
     effect of +2.3 c/1k -- i.e. nothing -- and learns
     P(auth | reached the issuer), which is not the quantity the business is paid on.
  3. Counting timeouts as declines (D) is the highest-margin ablation on this fleet
     (+719.9 c/1k) AND the worst estimator (MAE 8.79, 57.0 pts on foxtrot -- the arm it
     punishes). Any benchmark that
     reports only margin or regret selects for it. That is the single strongest argument
     for #17 reporting estimate quality next to reward.
  4. F (own count for the ambiguous class + an explicit price) captures 43% of D's margin
     gain with LOWER estimate error than A (1.92 vs 2.25), and it beats static_table by
     +311 c/1k post-drift where plain A loses to it. Unlike G it adapts when a processor's
     latency degrades mid-run: G is handed a static latency table it cannot revise.
  5. A hard gate on a learned statistic (H) is the worst design here: the arms it
     excludes stop receiving traffic, so the estimate that gates them never refreshes.
     Eligibility filters that depend on learned state need forced exploration (#7.6)
     and must be logged as eligibility, not just choice, or #15's OPE is biased.
  6. Retry pricing: never retrying costs 1802 cents per 1k requests on this fleet, and
     leaving the submission fee out of the retry test buys +2.1 auth pts for -77 cents/1k.
     The fee term is what makes that trade explicit and tunable instead of accidental.
  7. '+X pts at equal effective cost' is a procedure, not a number: the swept frontier is
     stepwise, and here the bandit sits off its left edge (cheaper AND higher-auth than every
     swept point), so the interpolated figure is a lower bound. #17 must publish the whole
     frontier plus the scenario id, never a lone number.""")
    print(f"     measured: {prim['frontier']:+.2f} pts on the correlated fleet vs "
          f"{sec['frontier']:+.2f} pts on the 3DS-decorrelated fleet")
    print("\n" + "=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
