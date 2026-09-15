#!/usr/bin/env python3
"""Decision ticket #7 evidence: the Thompson-sampling implementation contract.

What this measures, and what it deliberately does not
------------------------------------------------------
#7 asks for six implementation decisions behind "Thompson sampling over a Beta posterior
per arm": the arm schema (which categorical keys, how many amount bands, what happens to
unseen arms), the posterior mechanics (prior parameters, update rule, reward family --
the last already decided by ADR-0002 and only re-verified here), the hot-path draw and
propensity cost, the concurrency model, the persistence contract, and cold-start
handling. ADR-0003 already picked the ALGORITHM (Beta-Bernoulli TS) and ADR-0002 the
REWARD (two-part score, timeout excluded and priced at lambda_to). This spike therefore
holds both fixed and varies only the implementation decisions this ticket owns.

The world is not invented here. Every policy run executes against the committed,
content-addressed scenarios of simulator/scenarios/ through spikes/0006's harness
(same interface, same model), so an arm-granularity number can be cited as
baseline-steady-v1@sha256:<12> like any other benchmark claim. The one world extension
-- per-issuer auth variation, to steelman issuer-granular arms -- is local to this file,
labelled as such, and never mixed into the committed-scenario tables.

Sections, and the question each answers:

  [P1] arm space: which categorical keys pay for themselves, how many amount bands,
       and what the space costs in bytes and sparsity.
  [P2] priors: Jeffreys vs uniform vs an offline-seeded informative prior; the
       pseudo-count strength m; robustness to a miscalibrated baseline.
  [P3] update protocol: pure increments vs decayed counts; how a transport error is
       labelled; what a duplicate webhook delivery costs when not deduped.
  [P4] hot path: the Beta draw algorithm (exactness measured, not assumed), the
       key-addressed draw discipline, and what a propensity costs at decision time.
  [P5] concurrency: the sharded single-writer protocol exercised with real threads
       (order, dedupe, accounting), the cost of posterior staleness, and the
       contention model for Go.
  [P6] cold start: a processor added mid-run under three priors and three exploration
       floors; time-to-trustworthy and the price of the floor.
  [P7] persistence: the WAL-is-canonical contract -- fold equality, snapshot replay,
       re-bucketing under changed band edges, and replay throughput.

Everything is stdlib-only, offline and deterministic. Magnitudes belong to the scenario
documents; the orderings, the PASS/FAIL verdicts and the cost ratios are the findings.

    python3 posterior.py                 # all sections at n=60000, ~30 min on 2 vCPU
    python3 posterior.py 20000           # smaller n (default 60000)
    python3 posterior.py --section=P4    # one section
"""

from __future__ import annotations

import bisect
import json
import math
import sys
import threading
import time as walltime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "simulator" / "scenarios"))
from check import (  # noqa: E402  (the gate is imported, not reimplemented)
    draw, fnv1a64, load_scenario, scenario_hash, stream,
)
sys.path.insert(0, str(REPO / "spikes" / "0006-simulation-harness"))
import harness as H  # noqa: E402

# --------------------------------------------------------------------------------------
# Constants. The world's seed lives in the scenario document; the POLICY's seed is separate
# and recorded here (ADR-0005 R30). Nothing below reads wall time or the global RNG.
# --------------------------------------------------------------------------------------

POLICY_SEED = 20_260_916          # policy randomness; the world never sees it
OVERLAY_SEED = 20_260_917         # the issuer overlay (spike-local, labelled wherever used)
SELL_BPS, SELL_FIXED = 128, 0     # merchant price, as in spikes/0003 and spikes/0004
LAM_TO = 45.0                     # ADR-0002: price of an unresolved attempt, cents
MAX_ATTEMPTS = 2                  # ADR-0002/#5: chain depth held at 2, as in spike 0003
DEFAULT_N = 60_000
WARMUP = 10_000                   # the cold-start window margins are reported over

BIN_CLASSES = ("consumer_credit", "consumer_debit", "premium_credit", "corporate",
               "prepaid", "unmapped")     # engine-side enum; the harness emits the first 5
REGIONS = ("EEA", "UK", "US", "LATAM", "APAC")

CATALOG = json.loads((REPO / "constraints" / "catalog" / "acquirer-catalog.example.json")
                     .read_text(encoding="utf-8"))
ACQ = sorted(a["id"] for a in CATALOG["acquirers"])          # deterministic order
ACQ_IX = {a: i for i, a in enumerate(ACQ)}
ECON = {a["id"]: a for a in CATALOG["acquirers"]}

AUTH, DSOFT, ABANDONED, TIMEOUT, DHARD, TERR = (H.AUTHORIZED, H.DECLINED_SOFT, H.ABANDONED,
                                                H.TIMEOUT, H.DECLINED_HARD, H.TRANSPORT_ERROR)

# floor_margin_bps per transaction, the same distribution spike 0003 used (82/12/6 over
# 0/25/40). It is merchant context, not world behaviour, so it is keyed off the policy seed.
def floor_margin_bps(seq: int) -> int:
    u = draw(stream(POLICY_SEED, "flr", seq), 0)
    return 0 if u < 0.82 else (25 if u < 0.94 else 40)


def eligible(req, exclude=frozenset()) -> list:
    """Catalog capability + merchant floor margin, BEFORE sampling (ADR-0004)."""
    out = []
    for acq in ACQ:
        if acq in exclude:
            continue
        e = ECON[acq]
        if req.currency not in e["currencies"]:
            continue
        if req.card_region not in e["markets"]:
            continue
        if req.sca_required and not e["three_ds"]:
            continue
        if SELL_BPS - e["cost_bps"] < floor_margin_bps(req.seq):
            continue
        out.append(acq)
    return out


def win_amount(req, acq: str) -> float:
    return (req.amount_minor * (SELL_BPS - ECON[acq]["cost_bps"]) / 10_000.0
            + (SELL_FIXED - ECON[acq]["fixed_fee_minor"]))


def attempt_fee(req, acq: str) -> float:
    return float(ECON[acq]["attempt_fee_minor"])


# --------------------------------------------------------------------------------------
# 1. The Beta machinery: an exact sampler we own, an incomplete-beta for propensities.
#    (math/rand/v2 has no Beta; ADR-0001 said owning ~40 lines is a #7 decision.)
# --------------------------------------------------------------------------------------

def _uniform(seed: int, index: int) -> float:
    return draw(seed, index)


class Rng:
    """A draw stream addressed by (seed, index): stateless, replayable, shard-proof."""
    __slots__ = ("seed", "i")

    def __init__(self, seed: int):
        self.seed, self.i = seed, 0

    def u(self) -> float:
        self.i += 1
        return draw(self.seed, self.i - 1)

    def norm(self) -> float:
        u1, u2 = self.u(), self.u()
        return math.sqrt(-2.0 * math.log(max(u1, 1e-300))) * math.cos(6.283185307179586 * u2)


def _gamma(rng: Rng, shape: float) -> float:
    """Marsaglia-Tsang (2000), shape > 0. Boosts shape < 1 through Gamma(shape+1)."""
    if shape < 1.0:
        g = _gamma(rng, shape + 1.0)
        u = rng.u()
        return g * (u ** (1.0 / shape)) if u > 0.0 else 0.0
    d = shape - 1.0 / 3.0
    c = 1.0 / math.sqrt(9.0 * d)
    while True:
        x, v = rng.norm(), 1.0
        v = 1.0 + c * x
        if v <= 0.0:
            continue
        v = v * v * v
        u = rng.u()
        if u < 1.0 - 0.0331 * x * x * x * x:
            return d * v
        if math.log(u) < 0.5 * x * x + d * (1.0 - v + math.log(v)):
            return d * v


def beta_draw(seed: int, a: float, b: float) -> float:
    """Exact Beta(a, b) draw from the key-addressed stream at `seed`."""
    if a <= 0.0 or b <= 0.0:
        return 0.5
    r = Rng(seed)
    x = _gamma(r, a)
    y = _gamma(r, b)
    s = x + y
    return x / s if s > 0.0 else 0.5


def beta_normal(seed: int, a: float, b: float) -> float:
    """Normal approximation to Beta -- the cheap approximation, measured not assumed."""
    n = a + b
    mu = a / n
    sd = math.sqrt(max(1e-12, a * b / (n * n * (n + 1.0))))
    z = Rng(seed).norm()
    return min(1.0 - 1e-4, max(1e-4, mu + sd * z))


def beta_order_stat(seed: int, a: int, b: int) -> float:
    """Exact Beta for INTEGER parameters only: the a-th order statistic of a+b-1 uniforms.
    O(n) per draw and integer-only -- both disqualify it under Jeffreys priors and
    fractional shrink, but the cost crossover is measured anyway."""
    n = a + b - 1
    r = Rng(seed)
    xs = sorted(r.u() for _ in range(n))
    return min(1.0 - 1e-12, xs[a - 1]) if 0 < a <= n else 0.5


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the regularized incomplete beta (Lentz)."""
    tiny, eps = 1e-300, 3e-16
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < eps:
            break
    return h


def beta_cdf(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b): the Beta CDF, used for propensities."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_front = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(ln_front) * _betacf(a, b, x) / a
    # I_x(a,b) = 1 - I_{1-x}(b,a); the SAME front factor, the CF evaluated swapped.
    return 1.0 - math.exp(ln_front) * _betacf(b, a, 1.0 - x) / b


def beta_ppf(a: float, b: float, p: float) -> float:
    """Inverse CDF by bisection -- for the grid sampler's table build only (cold path)."""
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if beta_cdf(a, b, mid) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------------------------
# 2. The arm space. A closed vocabulary of categorical dimensions, one optional amount-band
#    dimension with geometric edges, and the processor ordinal: a mixed-radix index into
#    one fixed-layout array. No maps, no per-arm objects, no dynamic growth.
# --------------------------------------------------------------------------------------

def geo_edges(lo: int, hi: int, k: int) -> list:
    """k-1 geometric edges between lo and hi, 2 significant figures. An artifact field,
    not code: derived here from the scenario's declared amount range."""
    if k < 2:
        return []
    ratio = (hi / lo) ** (1.0 / k)
    out = []
    for j in range(1, k):
        e = lo * ratio ** j
        mag = 10 ** int(math.floor(math.log10(e)) - 1)
        out.append(int(round(e / mag) * mag))
    assert all(b > a for a, b in zip(out, out[1:])) and out[0] > lo, out
    return out


def band_index(amount_minor: int, edges) -> int:
    return bisect.bisect_right(edges, amount_minor)


class ArmSpace:
    """dims: names of categorical dimensions. 'issuer' is the spike-local overlay dim."""

    def __init__(self, dims, n_bands=0, amount_range=(600, 400_000), n_issuer=12):
        dims = tuple(dims)
        if n_bands and "band" not in dims:
            dims = dims + ("band",)          # n_bands implies the band dimension
        self.dims = dims
        self.n_issuer = n_issuer
        self.edges = geo_edges(*amount_range, n_bands) if n_bands else []
        self.sizes = []
        for d in self.dims:
            if d == "bin":
                self.sizes.append(len(BIN_CLASSES))
            elif d == "region":
                self.sizes.append(len(REGIONS))
            elif d in ("sca", "mandate"):
                self.sizes.append(2)
            elif d == "band":
                self.sizes.append(max(1, n_bands))
            elif d == "issuer":
                self.sizes.append(len(REGIONS) * n_issuer)
            else:
                raise ValueError(d)
        # context radix; the processor ordinal is the last (fastest) dimension
        self.ctx_radix = []
        r = 1
        for s in reversed(self.sizes):
            self.ctx_radix.append(r)
            r *= s
        self.ctx_radix.reverse()
        self.n_ctx = r
        self.n_arms = r * len(ACQ)

    def _issuer_of(self, req) -> int:
        # card-issuer group: region x n_issuer pseudo-groups; each transaction is a new
        # card, so the group is a deterministic function of the transaction stream.
        u = draw(stream(OVERLAY_SEED, "iss", req.seq), 0)
        return REGIONS.index(req.card_region) * self.n_issuer + int(u * self.n_issuer)

    def ctx_index(self, req) -> int:
        idx = 0
        for d, rad in zip(self.dims, self.ctx_radix):
            if d == "bin":
                v = BIN_CLASSES.index(req.bin_class)
            elif d == "region":
                v = REGIONS.index(req.card_region)
            elif d == "sca":
                v = 1 if req.sca_required else 0
            elif d == "mandate":
                v = 1 if req.mandate else 0
            elif d == "band":
                v = band_index(req.amount_minor, self.edges)
            elif d == "issuer":
                v = self._issuer_of(req)
            idx += v * rad
        return idx

    def arm_index(self, ctx_i: int, acq: str) -> int:
        return ctx_i * len(ACQ) + ACQ_IX[acq]

    def describe(self) -> str:
        parts = []
        for d in self.dims:
            if d == "bin":
                parts.append(f"bin{len(BIN_CLASSES)}")
            elif d == "region":
                parts.append(f"reg{len(REGIONS)}")
            elif d == "band":
                parts.append(f"band{len(self.edges) + 1 if self.edges else 0}")
            elif d == "issuer":
                parts.append(f"iss{len(REGIONS) * self.n_issuer}")
            else:
                parts.append(d)
        return "x".join(parts)


# --------------------------------------------------------------------------------------
# 3. The router: Thompson sampling with every #7 decision as a knob.
# --------------------------------------------------------------------------------------

class Router:
    def __init__(self, space: ArmSpace, *, prior_fn=None, eta=0.0, n_min=0,
                 floor_mode="onboard", te_mode="beta", decay_halflife=None, dedupe=True,
                 ingest_batch=1, draw_alg="exact"):
        self.space = space
        n = space.n_arms
        # data counts (float64: integer increments are exact to 2^53, and #8's shrink is
        # a multiply); the PRIOR is a separate read-only array, so the effective posterior
        # is (prior + data) computed at read time and a prior artifact swap is a pointer
        # swap, not a state rewrite.
        self.a = [0.0] * n
        self.b = [0.0] * n
        self.toa = [0.0] * n          # timeouts
        self.tob = [0.0] * n          # settled (non-timeout) attempts
        self.te = [0.0] * n           # transport errors (observability + #8)
        self.pa = [0.5] * n
        self.pb = [0.5] * n
        self.ptoa = [0.5] * n
        self.ptob = [0.5] * n
        self.eta, self.n_min, self.floor_mode = eta, n_min, floor_mode
        self.proc_settled = [0] * len(ACQ)     # processor-level settled observations
        self.explore = set()                    # ordinals in the onboarding/reset state
        self.te_mode = te_mode
        self.dedupe = dedupe
        self.decay = 2.0 ** (-1.0 / decay_halflife) if decay_halflife else None
        self.ingest_batch = ingest_batch
        self.draw_alg = draw_alg
        self._buf = []
        self._seen = set()
        self._grid = {}               # (arm, purpose) -> (a_used, b_used, table)
        self.grid_rebuilds = 0
        self.exclude = frozenset()
        self.wal = []                 # the canonical op log: outcome ops as delivered
        self.dup_dropped = 0
        if prior_fn is not None:
            self._fill_prior(prior_fn)

    # ----- prior ---------------------------------------------------------------
    def _fill_prior(self, prior_fn):
        """prior_fn(bin, region, acq) -> (a0, b0, toa0, tob0), hierarchically resolved
        by the caller. Applies to every arm under that (bin, region, acq) coordinate."""
        for ctx_i in range(self.space.n_ctx):
            rem = ctx_i
            coords = {}
            for d, rad in zip(self.space.dims, self.space.ctx_radix):
                v, rem = divmod(rem, rad)
                coords[d] = v
            bin_i = coords.get("bin", 0)
            if "region" in coords:
                reg_i = coords["region"]
            elif "issuer" in coords:
                reg_i = coords["issuer"] // self.space.n_issuer
            else:
                reg_i = 0
            for acq in ACQ:
                a0, b0, ta0, tb0 = prior_fn(bin_i, reg_i, acq)
                i = self.space.arm_index(ctx_i, acq)
                self.pa[i], self.pb[i], self.ptoa[i], self.ptob[i] = a0, b0, ta0, tb0

    # ----- the hot path ----------------------------------------------------------
    def _draw(self, seq, arm, purpose, a, b):
        seed = stream(POLICY_SEED, "pol", seq, arm, purpose)
        if self.draw_alg == "exact":
            return beta_draw(seed, a, b)
        if self.draw_alg == "normal":
            return beta_normal(seed, a, b)
        if self.draw_alg == "grid":
            key = (arm, purpose)
            ent = self._grid.get(key)
            if ent is None or ent[0] != a or ent[1] != b:
                table = [beta_ppf(a, b, (j + 0.5) / 64.0) for j in range(64)]
                self._grid[key] = ent = (a, b, table)
                self.grid_rebuilds = getattr(self, "grid_rebuilds", 0) + 1
            u = draw(seed, 0)
            return ent[2][min(63, int(u * 64.0))]
        raise ValueError(self.draw_alg)

    def effective(self, i):
        return self.a[i] + self.pa[i], self.b[i] + self.pb[i]

    def mean(self, i):
        aa, bb = self.effective(i)
        return aa / (aa + bb)

    def decide(self, req):
        """-> chain (list of acq ids). Filter, sample, score, floor, EV-test."""
        elig = eligible(req, self.exclude)
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
            s = (th * win_amount(req, acq) - (1.0 - th) * attempt_fee(req, acq)
                 - pi * LAM_TO)
            scored.append((s, acq))
            if self.n_min and self.floor_mode == "arm":
                if (self.a[i] + self.b[i]) < self.n_min:
                    cold.append(acq)
            elif self.floor_mode == "threshold" and self.n_min:
                if self.proc_settled[ACQ_IX[acq]] < self.n_min:
                    cold.append(acq)
            elif ACQ_IX[acq] in self.explore:
                cold.append(acq)
        scored.sort(key=lambda t: (-t[0], t[1]))
        chain = [acq for s, acq in scored if s > 0.0][:MAX_ATTEMPTS]
        if cold and self.eta > 0.0:
            if draw(stream(POLICY_SEED, "flo", req.seq), 0) < self.eta:
                j = int(draw(stream(POLICY_SEED, "fpk", req.seq), 0) * len(cold))
                pick = cold[j]
                chain = [pick] + [c for c in chain if c != pick][:MAX_ATTEMPTS - 1]
        return chain

    # ----- the ingest path --------------------------------------------------------
    def observe(self, req, acq, att, resp):
        key = (req.seq, att)
        if self.dedupe:
            if key in self._seen:
                self.dup_dropped += 1
                return
            self._seen.add(key)
        op = (req.seq, att, acq, resp.outcome, req.bin_class, req.card_region,
              req.sca_required, req.mandate, req.amount_minor)
        self.wal.append(op)
        if self.ingest_batch > 1:
            self._buf.append(op)
            if len(self._buf) >= self.ingest_batch:
                self._flush()
        else:
            self._apply(op)

    def _flush(self):
        for op in self._buf:
            self._apply(op)
        self._buf = []

    def apply_op(self, op):
        """The fold step. Also the definition the WAL replay must reproduce exactly."""
        _seq, _att, acq, outcome, bin_c, region, sca, mandate, amount = op
        req = _StubReq(bin_c, region, sca, mandate, amount, _seq)
        ctx = self.space.ctx_index(req)
        i = self.space.arm_index(ctx, acq)
        if self.decay is not None:
            self.a[i] *= self.decay
            self.b[i] *= self.decay
            self.toa[i] *= self.decay
            self.tob[i] *= self.decay
        settled = True
        if outcome == TIMEOUT:
            self.toa[i] += 1.0
            settled = False
        elif outcome == AUTH:
            self.a[i] += 1.0
            self.tob[i] += 1.0
        elif outcome == TERR:
            self.te[i] += 1.0
            if self.te_mode == "beta":        # known failure: a real non-authorization
                self.b[i] += 1.0
                self.tob[i] += 1.0
            elif self.te_mode == "timeout":   # laundered into ambiguity (rejected variant)
                self.toa[i] += 1.0
                settled = False
            elif self.te_mode == "exclude":   # invisible to the posterior (rejected variant)
                pass
        else:                                  # declines + abandoned
            self.b[i] += 1.0
            self.tob[i] += 1.0
        if settled:
            ix = ACQ_IX[acq]
            self.proc_settled[ix] += 1
            if ix in self.explore and self.proc_settled[ix] >= self.n_min:
                self.explore.discard(ix)      # the budget is spent; TS takes over

    _apply = apply_op

    def state_checksum(self):
        """A byte-exact identity of the learned state: same state, same checksum,
        across processes and platforms (packed doubles, sha256)."""
        import hashlib
        import struct
        h = hashlib.sha256()
        for arr in (self.a, self.b, self.toa, self.tob, self.te):
            h.update(struct.pack("<%dd" % len(arr), *arr))
        return "sha256:" + h.hexdigest()[:24]


# Region -> currency is 1:1 across the committed scenario family (verified against
# baseline-steady-v1 draws); the WAL records region, and eligibility re-derives the
# currency from it. If a future scenario breaks the 1:1 map, the WAL must gain a
# currency column -- that is a schema change, not a patch here.
REGION_CCY = {"EEA": "EUR", "UK": "GBP", "US": "USD", "LATAM": "USD", "APAC": "USD"}


class _StubReq:
    """Just enough context for the fold to re-derive an arm index from a WAL op."""
    __slots__ = ("bin_class", "card_region", "sca_required", "mandate", "amount_minor", "seq")

    def __init__(self, bin_c, region, sca, mandate, amount, seq):
        self.bin_class = bin_c
        self.card_region = region
        self.sca_required = sca
        self.mandate = mandate
        self.amount_minor = amount
        self.seq = seq

    @property
    def currency(self):
        return REGION_CCY[self.card_region]


def fold(wal, space, **router_kw):
    """The posterior as a pure function of the op log (and the prior, if given)."""
    r = Router(space, **router_kw)
    for op in wal:
        r.apply_op(op)
    return r


# --------------------------------------------------------------------------------------
# 4. The driver: the same loop for every policy. Context -> constraint filter -> chain ->
#    attempts -> outcomes -> posterior update. Metrics only ever see the first delivery of
#    an attempt; the posterior may see a duplicate (that is [P3]'s experiment).
# --------------------------------------------------------------------------------------

class Metrics:
    def __init__(self, n, windows=()):
        self.n = n
        self.txns = 0
        self.authed = 0
        self.attempts = 0
        self.timeouts = 0
        self.margin = 0.0
        self.early_margin, self.early_n = 0.0, 0
        self.unroutable = 0
        self.mae_sum, self.mae_n = 0.0, 0
        self.acq_attempts = {a: 0 for a in ACQ}
        self.windows = {w: [0.0, 0] for w in windows}     # name -> [margin, txns]
        self.share = {a: [0, 0] for a in ACQ}             # acq -> [eligible hits, windows]

    def add(self, seq, authed, txn_margin, attempts):
        self.txns += 1
        self.authed += authed
        self.margin += txn_margin
        if seq < WARMUP:
            self.early_margin += txn_margin
            self.early_n += 1
        for w, (m, c) in self.windows.items():
            lo, hi = w
            if lo <= seq < hi:
                self.windows[w][0] += txn_margin
                self.windows[w][1] += 1

    def report(self, label):
        per_k = 1000.0 / max(1, self.txns)
        mae = 100.0 * self.mae_sum / max(1, self.mae_n)
        return (label, self.authed / max(1, self.txns) * 100.0, self.margin * per_k,
                self.early_margin * 1000.0 / max(1, self.early_n), mae,
                self.timeouts / max(1, self.attempts) * 100.0, self.unroutable)


def theta_truth(world, req, acq, truth):
    """What the posterior is estimating: P(authorized | attempt, not timeout). The world's
    p is pre-abandonment, so an abandoned challenge removes p with the model's abandon
    rate; timeouts are excluded by construction (ADR-0002), not conditioned away here."""
    p = truth["p"]
    if truth.get("challenged"):
        p *= (1.0 - world.models[acq].abandon)
    return p


def probe_truth(world, req, acq, att, t_ms):
    """The oracle's peek: evaluate the model without keeping the response. Pure except for
    the late-settlement queue, which is restored around the probe."""
    nq = len(world.late_queue)
    resp, truth = world.attempt(req, acq, att, t_ms)
    del world.late_queue[nq:]
    return truth


def drive(world, router, n, *, overlay=False, dup_rate=0.0, metrics=None,
          chains_out=None, states_out=None, state_every=2500, onboard_at=None,
          onboard_acq=None):
    """The coupled loop. The world is policy-independent (key-addressed draws), so gaps
    between policies are not simulation noise."""
    m = metrics if metrics is not None else Metrics(n)
    for seq, arrival in world.arrivals(n):
        world.clock.advance_to(arrival)
        req = world.context(seq, arrival)
        if onboard_at is not None:
            if onboard_at == "never":
                router.exclude = frozenset({onboard_acq})
            elif seq < onboard_at:
                router.exclude = frozenset({onboard_acq})
            elif seq == onboard_at:
                router.exclude = frozenset()
                router.explore = {ACQ_IX[onboard_acq]}
        chain = router.decide(req)
        if chains_out is not None:
            chains_out.append((seq, tuple(chain)))
        if states_out is not None and seq % state_every == 0:
            states_out.append(_snapshot_decision(world, router, req, chain, seq))
        if not chain:
            m.unroutable += 1
            m.add(seq, 0, 0.0, 0)
            continue
        txn_margin, authed = 0.0, 0
        for att, acq in enumerate(chain):
            if overlay:
                resp, truth = _overlay_attempt(world, req, acq, att, world.clock.now_ms())
            else:
                resp, truth = world.attempt(req, acq, att, world.clock.now_ms())
            m.attempts += 1
            m.acq_attempts[acq] += 1
            if resp.outcome == TIMEOUT:
                m.timeouts += 1
                txn_margin -= attempt_fee(req, acq) + LAM_TO
            elif resp.outcome == AUTH:
                authed = 1
                txn_margin += win_amount(req, acq)
            else:
                txn_margin -= attempt_fee(req, acq)
            i = router.space.arm_index(router.space.ctx_index(req), acq)
            if resp.outcome != TIMEOUT:
                m.mae_sum += abs(router.mean(i) - theta_truth(world, req, acq, truth))
                m.mae_n += 1
            router.observe(req, acq, att, resp)
            if dup_rate > 0.0 and draw(stream(POLICY_SEED, "dup", seq, att), 0) < dup_rate:
                router.observe(req, acq, att, resp)     # the redelivered webhook
            if resp.outcome in H.TERMINAL:
                break
        m.add(seq, authed, txn_margin, len(chain))
    return m


def _snapshot_decision(world, router, req, chain, seq):
    """Posterior parameters of the eligible set at a decision: what a propensity is
    computed from, and all a cold-path recomputation needs ([P4])."""
    arms = []
    for acq in eligible(req, router.exclude):
        i = router.space.arm_index(router.space.ctx_index(req), acq)
        aa, bb = router.effective(i)
        ta = router.toa[i] + router.ptoa[i]
        tb = router.tob[i] + router.ptob[i]
        arms.append((acq, aa, bb, ta, tb, router.a[i] + router.b[i]))
    return (seq, arms, len(chain) and chain[0] or None)


# --- the issuer overlay: a spike-local world extension, never mixed into committed tables

def issuer_mult(region_i, issuer_i):
    """Per-(region, issuer-group) auth multiplier: lognormal sigma=0.06, deterministic."""
    u1 = draw(stream(OVERLAY_SEED, "im1", region_i, issuer_i), 0)
    u2 = draw(stream(OVERLAY_SEED, "im2", region_i, issuer_i), 0)
    z = math.sqrt(-2.0 * math.log(max(u1, 1e-300))) * math.cos(6.283185307179586 * u2)
    return min(1.5, max(0.5, math.exp(0.06 * z)))


def _overlay_attempt(world, req, acq, att, t_ms):
    resp, truth = world.attempt(req, acq, att, t_ms)
    if resp.outcome in (TIMEOUT, TERR, ABANDONED):
        return resp, truth                      # transport/timeout/3DS-side: not issuer
    space = _OVERLAY_SPACE[0]
    iss = space._issuer_of(req) % space.n_issuer
    reg_i = REGIONS.index(req.card_region)
    p2 = min(0.995, truth["p"] * issuer_mult(reg_i, iss))
    u = draw(stream(OVERLAY_SEED, "ovl", req.seq, fnv1a64(acq), att), 7)
    truth = dict(truth)
    truth["p"] = p2
    if u < p2:
        return (H.AuthResponse(acq, att, AUTH, "", resp.latency_ms, "",
                               resp.settled_ms), truth)
    if resp.outcome == AUTH:                     # an approval withdrawn by the overlay
        return (H.AuthResponse(acq, att, DSOFT, "51", resp.latency_ms, "soft",
                               resp.settled_ms), truth)
    return resp, truth


_OVERLAY_SPACE = [None]


# --------------------------------------------------------------------------------------
# 5. The offline prior: an "industry baseline" simulated the way one actually arrives --
#    a short exploration prefix on traffic the run never sees (seqs AFTER the run window),
#    aggregated at a chosen granularity. Miscalibration is a multiply on the rate.
# --------------------------------------------------------------------------------------

def uniform_prefix(world, n, n_prefix=2000, base_seq=None):
    """Uniform exploration over eligible arms; returns per-(acq, bin, region) counts."""
    base = base_seq if base_seq is not None else n
    cnt = {}
    for k in range(n_prefix):
        seq = base + k
        req = _synth_req(world, seq)
        elig = eligible(req)
        if not elig:
            continue
        att = 0
        # one uniform attempt; a second if the first soft-declined (chain depth 2)
        while att < MAX_ATTEMPTS and elig:
            j = int(draw(stream(POLICY_SEED, "uni", seq, att), 0) * len(elig))
            acq = elig[j]
            resp, truth = world.attempt(req, acq, att, 0)
            key = (acq, req.bin_class, req.card_region)
            c = cnt.setdefault(key, [0.0, 0.0, 0.0, 0.0])  # auth, fail, to, settled
            if resp.outcome == TIMEOUT:
                c[2] += 1.0
            elif resp.outcome == AUTH:
                c[0] += 1.0
                c[3] += 1.0
            else:
                c[1] += 1.0
                c[3] += 1.0
            if resp.outcome in H.TERMINAL:
                break
            att += 1
    return cnt


def _synth_req(world, seq):
    """A context for a seq without running the arrival clock (contexts are pure)."""
    return world.context(seq, 0)


def make_prior_fn(prefix_counts, m=100.0, m_to=None, bias=1.0, bias_acq=None):
    """Hierarchical fallback: (proc x bin x region) -> (proc x bin) -> (proc) -> fleet.
    m is the prior strength in pseudo-attempts; bias multiplies the auth rate
    (miscalibration: uniform = rank-preserving, bias_acq = one processor's baseline is
    wrong, which is the case that kills exploration). The deepest level with >= 4
    settled prefix observations wins."""
    m_to = m if m_to is None else m_to
    levels = {}
    fleet = [0.0, 0.0, 0.0, 0.0]                     # auth, fail, timeout, settled
    for (acq, bin_c, region), c in prefix_counts.items():
        for lk in ((acq, bin_c, region), (acq, bin_c), (acq,)):
            e = levels.setdefault(lk, [0.0, 0.0, 0.0, 0.0])
            for i in range(4):
                e[i] += c[i]
        for i in range(4):
            fleet[i] += c[i]

    def rates(e, b_=1.0):
        if e is None or e[0] + e[1] < 4.0:
            return None
        r = min(0.99, max(0.01, (e[0] / (e[0] + e[1])) * b_))
        to = min(0.9, max(0.005, e[2] / max(1.0, e[0] + e[1] + e[2])))
        return r, to

    def fn(bin_i, reg_i, acq):
        if bias_acq is None:
            b_ = bias                    # uniform: rank-preserving miscalibration
        else:
            b_ = bias if acq == bias_acq else 1.0   # one processor's baseline is wrong
        for lk in ((acq, BIN_CLASSES[bin_i], REGIONS[reg_i]),
                   (acq, BIN_CLASSES[bin_i]), (acq,)):
            r = rates(levels.get(lk), b_)
            if r is not None:
                rate, to = r
                return (m * rate, m * (1.0 - rate), m_to * to, m_to * (1.0 - to))
        r = rates(fleet, b_)                          # last resort: the fleet margin
        if r is not None:
            rate, to = r
            return (m * rate, m * (1.0 - rate), m_to * to, m_to * (1.0 - to))
        return (0.5, 0.5, 0.5, 0.5)                   # nothing anywhere: Jeffreys

    return fn


# --------------------------------------------------------------------------------------
# 6. Sections. Each prints its own block; RESULTS.md is the concatenation.
# --------------------------------------------------------------------------------------

DOCS = {}
DEFAULT_SPACE = None      # set on first use: bin x region x sca x mandate x band6
CACHE = {}                # runs shared between sections (default run, prefix, oracle)


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


def default_space():
    global DEFAULT_SPACE
    if DEFAULT_SPACE is None:
        DEFAULT_SPACE = ArmSpace(("bin", "region", "sca", "mandate", "band"), n_bands=6)
    return DEFAULT_SPACE


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


def census(space, wal=None, n=DEFAULT_N, world_name="baseline-steady-v1"):
    """Structural emptiness (never eligible for any context) vs empirical sparsity
    (eligible but never observed under the default policy)."""
    w = _world(world_name, n)
    elig_ever = set()
    for seq, arrival in w.arrivals(n):
        req = w.context(seq, arrival)
        ctx = space.ctx_index(req)
        for acq in eligible(req):
            elig_ever.add(space.arm_index(ctx, acq))
    observed = set()
    if wal is not None:
        for op in wal:
            _seq, _att, acq, _o, bin_c, region, sca, mandate, amount = op
            req = _StubReq(bin_c, region, sca, mandate, amount, _seq)
            observed.add(space.arm_index(space.ctx_index(req), acq))
    counts = {}
    if wal is not None:
        for op in wal:
            _seq, _att, acq, _o, bin_c, region, sca, mandate, amount = op
            req = _StubReq(bin_c, region, sca, mandate, amount, _seq)
            i = space.arm_index(space.ctx_index(req), acq)
            counts[i] = counts.get(i, 0) + 1
    n_elig = len(elig_ever)
    zero_obs = n_elig - len(observed & elig_ever)
    thin = sum(1 for i in elig_ever if counts.get(i, 0) < 30)
    nz = sorted(counts.get(i, 0) for i in elig_ever if counts.get(i, 0) > 0)
    med = nz[len(nz) // 2] if nz else 0
    return dict(n_arms=space.n_arms, n_elig=n_elig,
                never_elig=space.n_arms - n_elig,
                zero_obs=zero_obs, thin=thin, med_obs=med,
                bytes=(space.n_arms * 32, space.n_arms * 16))


def sec_p1(n):
    print("[P1] arm space: which keys pay, how many bands, what the space costs")
    print("""
  Every variant runs the same policy (TS, Jeffreys -- the ADR-0003 default when no
  offline table exists) on baseline-steady-v1, so the only variable is the arm key.
  'vs default' is margin cents/1k; MAE is the traffic-weighted error of the arm's
  posterior mean against the world's true P(authorized|attempt, not timeout).
""")
    variants = [
        ("proc only", ArmSpace(())),
        ("+bin", ArmSpace(("bin",))),
        ("+bin+region", ArmSpace(("bin", "region"))),
        ("+bin+region+sca", ArmSpace(("bin", "region", "sca"))),
        ("+bin+region+sca+mandate (no bands)", ArmSpace(("bin", "region", "sca", "mandate"))),
        ("  + 2 bands", ArmSpace(("bin", "region", "sca", "mandate"), n_bands=2)),
        ("  + 4 bands", ArmSpace(("bin", "region", "sca", "mandate"), n_bands=4)),
        ("  + 6 bands  <- default", default_space()),
        ("  + 8 bands", ArmSpace(("bin", "region", "sca", "mandate"), n_bands=8)),
    ]
    rows, margins = [], {}
    wal_default = None
    for label, space in variants:
        if "default" in label:
            m, r = _ensure_default(n)
        else:
            r = Router(space)
            m = drive(_world("baseline-steady-v1", n), r, n)
        label_, auth, margin, early, mae, to, unroute = m.report(label.strip())
        margins[label.strip()] = margin
        c = census(space, r.wal, n)
        rows.append((label.strip(), space.n_arms, f"{c['n_elig']}",
                     f"{100.0 * c['zero_obs'] / max(1, c['n_elig']):.0f}%",
                     f"{100.0 * c['thin'] / max(1, c['n_elig']):.0f}%",
                     f"{c['med_obs']}", fmt(auth, 2), fmt(margin), "", fmt(mae, 2)))
    base = margins["+bin+region+sca+mandate (no bands)"]
    out_rows = []
    for row in rows:
        mg = float(row[7])
        out_rows.append((row[0], *row[1:7], row[7], f"{mg - base:+.0f}", row[9]))
    print(table(["arm key", "arms", "elig", "zero-obs", "<30obs", "med obs",
                 "auth%", "margin c/1k", "vs no-bands", "MAE pts"], out_rows))
    print(f"""
  The +6-bands row is the shipped default; its edges are the geometric ladder between
  the scenario's declared amount range (600..400000 minor), 2 significant figures:
  {default_space().edges}

  Data scaling: the band optimum moves with volume, so the default and the coarsest
  informative key re-run at 2n.""")
    rows = []
    for label, nn in (("bin only", n), ("bin only", 2 * n),
                      ("full key, 6 bands", n), ("full key, 6 bands", 2 * n)):
        space = (ArmSpace(("bin",)) if label.startswith("bin")
                 else ArmSpace(("bin", "region", "sca", "mandate"), n_bands=6))
        r = Router(space)
        m = drive(_world("baseline-steady-v1", nn), r, nn)
        _l, auth, margin, early, mae, to, _u = m.report(label)
        rows.append((label, nn, fmt(auth, 2), fmt(margin), fmt(mae, 2)))
    print(table(["arm key", "n", "auth%", "margin c/1k", "MAE pts"], rows))

    # the band sweep re-run at the informative prior (m=100): the starvation of fine
    # arms under Jeffreys is a cold-start artifact; the deployed system has a table
    print("""
  The same band sweep with the offline prior (m=100, hierarchical, from [P2]'s
  prefix): fine arms that starve under Jeffreys get their prior from the table, so
  the band count is decided where it will actually run.""")
    _ensure_prefix(n)
    pfn = make_prior_fn(CACHE["prefix"], m=100.0)
    rows = []
    for nb in (0, 2, 4, 6, 8):
        space = ArmSpace(("bin", "region", "sca", "mandate"), n_bands=nb)
        r = Router(space, prior_fn=pfn)
        m = drive(_world("baseline-steady-v1", n), r, n)
        _l, auth, margin, early, mae, to, _u = m.report(f"bands={nb}")
        rows.append((nb or "none", fmt(auth, 2), fmt(margin), fmt(early), fmt(mae, 2)))
    print(table(["bands (informative prior)", "auth%", "margin c/1k", "cold c/1k",
                 "MAE pts"], rows))

    # issuer granularity: the steelman, on the spike-local issuer overlay
    print("""
  Issuer granularity (the ticket's '6-digit BIN -> IIN prefix' question), steelmanned
  on issuer-overlay-v1: a spike-local world extension giving each (region, issuer
  group) a lognormal(sigma=0.06) auth multiplier -- NOT a committed scenario. With a
  real issuer effect present, does keying the arm on the issuer group beat keying it
  on the region?""")
    iss_space = ArmSpace(("bin", "issuer", "sca", "mandate", "band"), n_bands=6)
    _OVERLAY_SPACE[0] = iss_space
    reg_space = ArmSpace(("bin", "region", "sca", "mandate", "band"), n_bands=6)
    rows = []
    for label, space in (("region key (60 groups)", reg_space),
                         ("issuer key (60 groups)", iss_space)):
        r = Router(space)
        m = drive(_world("baseline-steady-v1", n), r, n, overlay=True)
        _l, auth, margin, early, mae, to, _u = m.report(label)
        c = census(space, r.wal, n)
        rows.append((label, space.n_arms, f"{100.0 * c['zero_obs'] / max(1, c['n_elig']):.0f}%",
                     fmt(auth, 2), fmt(margin), fmt(mae, 2)))
    print(table(["key (issuer-overlay-v1)", "arms", "zero-obs", "auth%",
                 "margin c/1k", "MAE pts"], rows))
    print("""
  -> FINDINGS (magnitudes belong to baseline-steady-v1 / issuer-overlay-v1):
     see ADR-0006 sections 1-2. The orderings are the claim: each categorical key pays
     for itself or is kept for a reason the fixture cannot measure (mandate); bands
     help until per-arm starvation dominates; issuer-level granularity starves at this
     volume even when the effect is real.""")


def sec_p2(n):
    print("[P2] priors: Jeffreys vs uniform vs offline-seeded, and the strength m")
    print("""
  The informative prior is seeded the way a real one arrives: a 2,000-transaction
  uniform-exploration prefix on traffic AFTER the run window (the run never sees it),
  aggregated hierarchically (proc x bin x region -> proc x bin -> proc -> fleet).
  m = pseudo-attempts of strength. 'cold' = margin over the first 10,000 txns.
""")
    space = default_space()
    prefix = _ensure_prefix(n)

    def run(label, prior_fn):
        r = Router(space, prior_fn=prior_fn) if prior_fn else Router(space)
        m = drive(_world("baseline-steady-v1", n), r, n)
        _l, auth, margin, early, mae, to, _u = m.report(label)
        return (label, fmt(auth, 2), fmt(margin), fmt(early), fmt(mae, 2))

    rows = [run("Jeffreys Beta(0.5,0.5)", None),
            run("uniform Beta(1,1)", lambda bi, ri, acq: (1.0, 1.0, 1.0, 1.0))]
    for m_ in (10, 30, 100, 300, 1000):
        rows.append(run(f"informative m={m_}", make_prior_fn(prefix, m=float(m_))))
    rows.append(run("informative m=100, rate x0.90", make_prior_fn(prefix, m=100.0, bias=0.90)))
    rows.append(run("informative m=100, rate x1.08", make_prior_fn(prefix, m=100.0, bias=1.08)))
    rows.append(run("m=100, foxtrot x0.80 only", make_prior_fn(prefix, m=100.0, bias=0.80,
                                                               bias_acq="foxtrot")))
    print(table(["prior", "auth%", "margin c/1k", "cold c/1k", "MAE pts"], rows))
    print("""
  -> Jeffreys vs uniform is within noise (as ADR-0003 predicted); the informative
     prior's value is concentrated in the cold window; strength beyond ~the cold-start
     window's own volume buys nothing and starts costing when the baseline is wrong.
     The miscalibration rows are the reason m is a KNOB, not a constant: a pessimistic
     prior costs more than an optimistic one at equal error, because TS under-explores
     arms it believes are confidently bad (ADR-0003 [F3]).""")


def sec_p3(n):
    print("[P3] update protocol: increments vs decay, transport-error label, dedupe")
    print("""
  (a) The ticket's 'weighted update to discount old data': exponential decay with
  half-life H (applied to the DATA counts of the arm being updated, so decay shrinks
  toward the current prior, not toward 0.5) vs pure increments, on the steady world
  (the cost side) and on outage-recovery-v1 at 2x n (the benefit side). #8 owns
  event-driven resets; this decides whether a standing decay is also worth carrying.
""")
    space = default_space()
    n_out = max(n, 120_000)      # the outage windows are 600-1200s of a 7-day clock
    rows = []
    for label, hl, world_name, nn in (("increments", None, "baseline-steady-v1", n),
                                      ("decay H=500", 500, "baseline-steady-v1", n),
                                      ("decay H=5000", 5000, "baseline-steady-v1", n),
                                      ("increments", None, "outage-recovery-v1", n_out),
                                      ("decay H=500", 500, "outage-recovery-v1", n_out),
                                      ("decay H=5000", 5000, "outage-recovery-v1", n_out)):
        r = Router(space, decay_halflife=hl)
        m = drive(_world(world_name, nn), r, nn)
        _l, auth, margin, early, mae, to, _u = m.report(label)
        settled = [r.a[i] + r.b[i] for i in range(space.n_arms) if r.a[i] + r.b[i] > 0]
        vol = sum(settled) / len(settled) if settled else 0.0
        rows.append((world_name, label, fmt(auth, 2), fmt(margin), fmt(mae, 2),
                     fmt(vol, 0)))
    print(table(["world", "update", "auth%", "margin c/1k", "MAE pts",
                 "mean settled/arm"], rows))

    print("""
  (b) How a transport error is labelled (ADR-0005 added the class; ADR-0002 priced it
  as -fee with NO ambiguity price, so the label question is #7's). long-refused-v1
  (this spike's gated scenario document, extending baseline-steady-v1): foxtrot --
  the cheapest, lowest-auth, slowest acquirer -- refuses every connection for six
  hours at t=0.286. 'share post' is the 12 hours after the step recovery -- the
  re-entry hysteresis each label buys. Three labelings: 'beta' counts the error as
  the settled failure it is (the posterior's estimand is P(authorized | attempt, not
  timeout)); 'timeout' launders it into ambiguity (excluded, priced via lambda_to);
  'exclude' makes it invisible to learning.
""")
    rows = []
    t0_frac, t1_frac = 0.286, 0.322        # [at_s, at_s+21600] / 604800
    w = (int(t0_frac * n), int(t1_frac * n))
    w2 = (int(0.304 * n), int(t1_frac * n))   # the second half: after the reaction time
    w3 = (int(t1_frac * n), int(t1_frac * n) + int(0.05 * n))   # 12h after recovery
    space_br = ArmSpace(("bin", "region"))
    for mode in ("beta", "timeout", "exclude"):
        r = Router(space_br, te_mode=mode)
        m = Metrics(n, windows=(w,))
        mm = drive(_world("long-refused-v1", n), r, n, metrics=m)
        def share(win):
            allw = sum(1 for op in r.wal if win[0] <= op[0] < win[1])
            fox = sum(1 for op in r.wal if win[0] <= op[0] < win[1] and op[2] == "foxtrot")
            return 100.0 * fox / max(1, allw)
        rows.append((mode, fmt(share(w), 2), fmt(share(w2), 2), fmt(share(w3), 2),
                     fmt(mm.margin * 1000.0 / max(1, mm.txns)),
                     fmt(mm.windows[w][0] * 1000.0 / max(1, mm.windows[w][1])),
                     fmt(100.0 * mm.mae_sum / max(1, mm.mae_n), 2)))
    print(table(["te label (bin x region arms)", "share 1st half %", "share 2nd half %",
                 "share post %", "margin c/1k", "outage-window c/1k", "MAE pts"], rows))
    # the same experiment at the default (fine) granularity: the dilution finding
    fine = []
    for mode in ("beta", "exclude"):
        r = Router(space, te_mode=mode)
        mm = drive(_world("long-refused-v1", n), r, n)
        allw = sum(1 for op in r.wal if w[0] <= op[0] < w[1])
        fox = sum(1 for op in r.wal if w[0] <= op[0] < w[1] and op[2] == "foxtrot")
        allw2 = sum(1 for op in r.wal if w2[0] <= op[0] < w2[1])
        fox2 = sum(1 for op in r.wal if w2[0] <= op[0] < w2[1] and op[2] == "foxtrot")
        fine.append((mode, fmt(100.0 * fox / max(1, allw), 2),
                     fmt(100.0 * fox2 / max(1, allw2), 2),
                     fmt(mm.margin * 1000.0 / max(1, mm.txns))))
    print()
    print("  The same two labelings at the DEFAULT (fine) arm space -- the dilution")
    print("  finding, which is a #8 input, not a #7 one:")
    print(table(["te label (default arms)", "share 1st half %", "share 2nd half %",
                 "margin c/1k"], fine))

    print("""
  (c) At-least-once delivery: 1% of attempts arrive twice during the run, and at the end
  the consumer rebalances and redelivers the last 10% of the log (the Kafka-rebalance
  failure mode). With dedupe on (seq, attempt) nothing changes; without it the run's
  learned volume is inflated and every replayed arm is biased toward its tail
  outcomes. Final-state MAE is measured against oracle probes over the last 20% of
  contexts, after the burst.
""")
    rows = []
    for dedupe in (True, False):
        # a fresh world per drive: the harness's outcome stream continues across
        # drives on a shared instance, which would confound the label with a
        # different outcome realization (caught at n=60k: margins moved +-40%
        # on the shared world; +-0.3% on fresh ones)
        world = _world("baseline-steady-v1", n)
        r = Router(space, dedupe=dedupe)
        m = drive(world, r, n, dup_rate=0.01)
        wal_len = len(r.wal)
        # the rebalance burst: redeliver the last 10% of the log
        class _R:
            __slots__ = ("outcome",)
            def __init__(self, o):
                self.outcome = o
        for op in r.wal[int(0.90 * len(r.wal)):]:
            _s, _at, acq, o, bin_c, region, sca, mandate, amount = op
            req = _StubReq(bin_c, region, sca, mandate, amount, _s)
            r.observe(req, acq, _at, _R(o))
        total_learned = sum(r.a) + sum(r.b) + sum(r.toa)
        # final-state MAE vs oracle probes on the last 20% of contexts
        errs = []
        for seq in range(int(0.8 * n), n, 13):
            req = _synth_req(world, seq)
            ctx = space.ctx_index(req)
            for acq in eligible(req):
                i = space.arm_index(ctx, acq)
                if r.a[i] + r.b[i] < 30:      # cold arms are prior-dominated anyway
                    continue
                truth = probe_truth(world, req, acq, 0, 0)
                pt = theta_truth(world, req, acq, truth)
                errs.append(abs(r.mean(i) - pt))
        rows.append((str(dedupe), r.dup_dropped, m.attempts, int(total_learned),
                     "PASS" if abs(total_learned - m.attempts) < 1e-9 else "FAIL",
                     fmt(100.0 * sum(errs) / len(errs), 2),
                     fmt(m.margin * 1000.0 / max(1, m.txns))))
    print(table(["dedupe", "dupes dropped", "attempts made", "counts learned",
                 "reconciles", "final MAE pts", "margin c/1k"], rows))

    print("""
  (d) Representation: float64 counters. Integer increments are EXACT in float64 up to
  2^53 (no drift, no accumulation error), and #8's shrink is a multiply -- so one
  representation serves pure increments, decay and event-driven resets alike. Checked
  directly:""")
    x = 0.0
    for _ in range(1_000_000):
        x += 1.0
    ok_int = (x == 1_000_000.0)
    y = 5_000_000_000.0
    y += 1.0
    ok_big = (y == 5_000_000_001.0)
    print(f"     1,000,000 increments of 1.0 from 0.0 == 1000000.0 exactly : {ok_int}")
    print(f"     5e9 + 1 == 5000000001.0 exactly (2^53 = 9.0e15)          : {ok_big}")
    print("""
  -> Pure increments + #8's event-driven response; a standing decay buys little
     adaptation on these worlds and permanently caps confidence (a half-life H bounds
     the effective sample size near H/ln2), so it is a #8 tool, not a default.
     te -> beta (a settled failure is a failure; the counter is separate for #8).
     Dedupe on (seq, attempt) is mandatory, and it is a property of the INGEST
     protocol, not the store: #13 inherits it as a requirement, not an option.""")


def sec_p4(n):
    print("[P4] hot path: the Beta draw, the draw discipline, and the propensity")
    print("""
  (a) Exactness first. Four samplers, 40,000 draws each, chi-square over 32
  equal-probability bins under the exact Beta CDF (df=31; reject at p<0.01 ~ chi2>52.2).
  TV = total variation distance between the sample's binned law and the exact one.
""")
    cases = ((0.5, 0.5), (2.0, 17.0), (50.0, 50.0), (300.0, 1200.0))
    rows = []
    for a, b in cases:
        for name, fn in (("gamma-ratio (ours)", lambda s: beta_draw(s, a, b)),
                         ("order-statistic", lambda s: beta_order_stat(s, int(a), int(b))),
                         ("normal approx", lambda s: beta_normal(s, a, b)),
                         ("grid 64", None)):
            if fn is None:
                tab = [beta_ppf(a, b, (j + 0.5) / 64.0) for j in range(64)]
                def fn(s, tab=tab):
                    return tab[min(63, int(draw(s, 0) * 64.0))]
            xs = [fn(stream(4242, "smp", i)) for i in range(40_000)]
            bins = [0] * 32
            edges = [beta_ppf(a, b, (j + 1) / 32.0) for j in range(31)]
            for x in xs:
                bins[bisect.bisect_right(edges, x)] += 1
            exp = 40_000 / 32
            chi2 = sum((o - exp) ** 2 / exp for o in bins)
            tv = 0.5 * sum(abs(o - exp) / 40_000 for o in bins)
            rows.append((f"Beta({a},{b})", name, fmt(chi2, 1),
                         "PASS" if chi2 <= 52.2 else "FAIL", fmt(tv * 100, 2) + "%"))
    print(table(["target", "sampler", "chi2(df=31)", "verdict@1%", "TV"], rows))
    print("""
  (b) Cost. CPython microseconds per draw (order 2,000,000 draws / sampler), and the
  Go projection using ADR-0001 section 1's 10/30/100x interpreter band. The budget
  line is ADR-0001's <= 20 us p99 in-engine CPU per decision.
""")
    import random as _random
    rows = []

    def bench(label, fn, count=40_000):
        t0 = walltime.perf_counter()
        for i in range(count):
            fn(i)
        us = (walltime.perf_counter() - t0) / count * 1e6
        rows.append((label, fmt(us, 2), fmt(us / 10.0, 1), fmt(us / 30.0, 1),
                     fmt(us / 100.0, 1)))
        return us

    rng = _random.Random(7)
    a, b = 50.0, 50.0
    bench("gamma-ratio (ours), a+b=100", lambda i: beta_draw(stream(4242, "b1", i), a, b))
    bench("CPython betavariate (C impl), same", lambda i: rng.betavariate(a, b))
    bench("normal approx", lambda i: beta_normal(stream(4242, "b2", i), a, b))
    tab = [beta_ppf(a, b, (j + 0.5) / 64.0) for j in range(64)]
    bench("grid 64 (table built)", lambda i: tab[min(63, int(draw(stream(4242, "b3", i), 0)) * 64)])
    ai, bi = 2, 17
    bench("order-statistic n=18", lambda i: beta_order_stat(stream(4242, "b4", i), ai, bi))
    bench("gamma-ratio, same n=19", lambda i: beta_draw(stream(4242, "b5", i), ai + 0.0, bi + 0.0))
    print(table(["sampler", "us/draw CPython", "Go @10x", "Go @30x", "Go @100x"], rows))
    print("""
  (c) The draw discipline: key-addressed vs a shared stream. Every policy draw is a
  pure function of (policy_seed, seq, arm, purpose) -- the same discipline ADR-0005
  imposed on the world, applied to the policy. Property check: draws are identical
  under permuted evaluation order and repeated evaluation, by construction; the
  end-to-end consequence (replay equality) is [P7]'s. The cost of the discipline is
  the stream key per draw, measured as the delta between the two rows above and:
""")
    rng2 = _random.Random(7)
    t0 = walltime.perf_counter()
    for i in range(40_000):
        rng2.betavariate(50.0, 50.0)
    us_stream = (walltime.perf_counter() - t0) / 40_000 * 1e6
    t0 = walltime.perf_counter()
    for i in range(40_000):
        beta_draw(stream(4242, "b6", i), 50.0, 50.0)
    us_keyed = (walltime.perf_counter() - t0) / 40_000 * 1e6
    print(f"     shared-stream draw {us_stream:.2f} us   key-addressed draw {us_keyed:.2f} us"
          f"   -> the discipline costs ~{us_keyed - us_stream:.2f} us/draw in CPython"
          f" (one stream() key per draw; in Go that is one fnv+splitmix64 chain,"
          f" a model-estimated 5-15 ns)")
    keys = [(7, 0, 0), (7, 1, 0), (12345, 3, 1), (99, 2, 0), (7, 0, 0)]
    fwd = [beta_draw(stream(POLICY_SEED, "pol", s, a, pu), 3.5, 9.0) for s, a, pu in keys]
    rev = list(reversed([beta_draw(stream(POLICY_SEED, "pol", s, a, pu), 3.5, 9.0)
                         for s, a, pu in reversed(keys)]))
    print(f"     draws under permuted evaluation order and repeats identical: "
          f"{'PASS' if fwd == rev else 'FAIL'} (stateless by construction, verified)")
    print("""
  (d) Approximate draws at the POLICY level: the same run, three draw algorithms
  (capped at 20,000 txns -- the grid variant's table rebuilds make it the expensive
  one, which is itself the finding). 'arms touched' = distinct eligible arms ever
  attempted: the coverage number that explains any margin an approximation 'wins',
  because an approximation that stops exploring is epsilon-greedy with epsilon=0.
""")
    space = default_space()
    n_algo = min(n, 20_000)
    rows = []
    for alg in ("exact", "normal", "grid"):
        r = Router(space, draw_alg=alg)
        m = drive(_world("baseline-steady-v1", n_algo), r, n_algo)
        _l, auth, margin, early, mae, to, _u = m.report(alg)
        touched = len({(op[0], op[2]) for op in r.wal})
        rows.append((alg, fmt(auth, 2), fmt(margin), fmt(mae, 2), touched,
                     r.grid_rebuilds if alg == "grid" else ""))
    print(table(["draw", "auth%", "margin c/1k", "MAE pts", "arms touched",
                 "grid rebuilds"], rows))
    print("""
  (e) The propensity. TS propensity of arm i = P(i's sampled score is best) over the
  eligible set. Options measured on decision states recorded from the default run:
  MC with R rounds (R x k draws), the plug-in estimator (uses the decision's own
  draws: p_i = prod_j F_j(theta_i), k-1 CDF evals per arm, no extra sampling), and a
  200k-round MC reference. Cost per decision at the catalog's largest eligible set
  (k=5); accuracy = mean and max |delta p| against the reference over the sampled
  states (chosen arm and any-arm, including the cold arms that never win).
""")
    _ensure_default(n)
    states = CACHE["states"]
    rows = []

    def prop_mc(arms, R, seed_base):
        wins = [0] * len(arms)
        for r_ in range(R):
            best, bestv = -1, -1e30
            for j, (acq, aa, bb, *_r) in enumerate(arms):
                th = beta_draw(stream(seed_base, "mc", r_, j), aa, bb)
                if th > bestv:
                    bestv, best = th, j
            wins[best] += 1
        return [w / R for w in wins]

    def prop_plugin(arms, seed_base):
        ths = [beta_draw(stream(seed_base, "pol", 0, j), aa, bb)
               for j, (acq, aa, bb, *_r) in enumerate(arms)]
        out = []
        for j in range(len(arms)):
            p = 1.0
            for j2 in range(len(arms)):
                if j2 != j:
                    p *= beta_cdf(arms[j2][1], arms[j2][2], ths[j])
            out.append(p)
        return out      # per-row unbiased for P(i wins); does NOT sum to 1 in a row

    # accuracy
    acc = {"mc8": [], "mc64": [], "plugin": []}
    acc_chosen = {"mc8": [], "mc64": [], "plugin": []}
    for idx, (seq, arms, chosen) in enumerate(states):
        if len(arms) < 2 or chosen is None:
            continue
        names = [a[0] for a in arms]
        if chosen not in names:
            continue
        ref = prop_mc(arms, 200_000, stream(999, "ref", idx))
        ci = names.index(chosen)
        for nm, est in (("mc8", prop_mc(arms, 8, stream(999, "e8", idx))),
                        ("mc64", prop_mc(arms, 64, stream(999, "e64", idx))),
                        ("plugin", prop_plugin(arms, stream(999, "pl", idx)))):
            acc[nm].append(max(abs(e - r) for e, r in zip(est, ref)))
            acc_chosen[nm].append(abs(est[ci] - ref[ci]))
    # cost, on the largest eligible set the catalog actually produces
    arms6 = max(states, key=lambda s: len(s[1]))[1]
    k = len(arms6)

    def cost(fn, reps=200):
        t0 = walltime.perf_counter()
        for i in range(reps):
            fn(i)
        return (walltime.perf_counter() - t0) / reps * 1e6

    costs = {}
    for nm, fn in (("mc8", lambda i: prop_mc(arms6, 8, stream(999, "c8", i))),
                   ("mc64", lambda i: prop_mc(arms6, 64, stream(999, "c64", i))),
                   ("plugin", lambda i: prop_plugin(arms6, stream(999, "cpl", i)))):
        costs[nm] = cost(fn)
    mc256 = cost(lambda i: prop_mc(arms6, 256, stream(999, "c256", i)))
    rows = []
    for nm, label in (("mc8", "MC R=8"), ("mc64", "MC R=64"), ("plugin", "plug-in")):
        v, vc = acc[nm], acc_chosen[nm]
        rows.append((label, fmt(costs[nm], 1), fmt(costs[nm] / 30.0, 1),
                     fmt(sum(vc) / len(vc) * 100, 2), fmt(max(v) * 100, 2)))
    rows.append(("MC R=256 (rejected)", fmt(mc256, 1), fmt(mc256 / 30.0, 1), "", ""))
    print(table(["method", "us/decision CPython", "Go @30x us", "chosen |dp| pts",
                 "any-arm max |dp| pts"], rows))
    print("""
  -> DECISION: exact gamma-ratio draws, key-addressed. The hot-path propensity is
     the plug-in estimate: it reuses the decision's own draws (zero extra sampling,
     the cheapest row measured) and is per-row unbiased, but NO cheap estimator --
     not MC-8, not MC-64, not plug-in -- is accurate enough to be an IPS weight
     (E[1/p_hat] != 1/p), so #15 MUST recompute exact propensities offline from the
     logged posteriors; the logged value is labelled with its method and is for
     dashboards, alerting and the audit trail. The propensity of the CHOSEN arm is
     the one that must be right in the log; the cold-arm columns are what force the
     offline recomputation.""")


def _ensure_prefix(n):
    if "prefix" not in CACHE:
        CACHE["prefix"] = uniform_prefix(_world("baseline-steady-v1", n), n)
    return CACHE["prefix"]


def _ensure_default(n):
    """The default run, once: Jeffreys prior, no floor, batch-1 ingest. Cached for
    P4 (decision states), P5 (the WAL), P7 (the WAL + recorded chains)."""
    if "default_run" in CACHE:
        return CACHE["default_run"]
    space = default_space()
    r = Router(space)
    states, chains = [], []
    m = drive(_world("baseline-steady-v1", n), r, n, states_out=states,
              chains_out=chains)
    CACHE["default_run"] = (m, r)
    CACHE["states"] = states
    CACHE["default_chains"] = chains
    return CACHE["default_run"]


def sec_p5(n):
    print("[P5] concurrency: the sharded single-writer protocol, staleness, contention")
    print("""
  The protocol (the shape ADR-0001 R5 already fixed; this section prices it):
  the arm array is sharded by index; each shard has ONE ingest writer fed by a bounded
  queue; readers never lock -- they read counters with atomic loads. A (alpha, beta)
  pair read while its writer lands between the two updates can be off by one count for
  one ingest interval: bounded staleness, not corruption. Order within an arm is FIFO
  because one writer owns it.

  (a) The protocol exercised with real threads (4 shard writers, 1 dispatcher, 2
  readers), 100,000 ops from the default run's WAL. CPython's GIL means the TIMING is
  not the claim; the PROTOCOL is: no lost ops, per-arm order preserved, and the folded
  WAL equals the writers' final state bit for bit.
""")
    if "default_run" not in CACHE:
        _ensure_default(n)
    _m, r0 = CACHE["default_run"]
    wal = r0.wal[:100_000]
    space = default_space()
    n_shard = 4
    live = Router(space)                       # the in-memory state the writers mutate
    queues = [[] for _ in range(n_shard)]
    tears = [0]
    reads = [0]
    stop = threading.Event()

    # dispatch: shard = arm index % n_shard (a fixed stride; one writer owns an arm)
    for op in wal:
        _s, _at, acq, _o, bin_c, region, sca, mandate, amount = op
        req = _StubReq(bin_c, region, sca, mandate, amount, _s)
        idx = space.arm_index(space.ctx_index(req), acq)
        queues[idx % n_shard].append(op)
    dispatched = sum(len(q) for q in queues)

    def writer(s):
        applied = 0
        for op in queues[s]:
            live.apply_op(op)                  # single writer per shard: no lock needed
            applied += 1
        return applied

    def reader():
        while not stop.is_set():
            for _ in range(500):
                i = (reads[0] * 2654435761) % live.space.n_arms
                a1 = live.a[i]
                b1 = live.b[i]
                a2 = live.a[i]
                reads[0] += 1
                if a1 != a2:                   # the arm was written between our loads
                    tears[0] += 1

    wres = [[] for _ in range(n_shard)]
    readers = [threading.Thread(target=reader) for _ in range(2)]
    for t in readers:
        t.start()
    wthreads = []
    for s in range(n_shard):
        t = threading.Thread(target=lambda s=s: wres[s].append(writer(s)))
        t.start()
        wthreads.append(t)
    for t in wthreads:
        t.join()
    stop.set()
    for t in readers:
        t.join()
    applied_total = sum(x[0] for x in wres)
    # the fold of the same WAL, serially
    folded = fold(wal, space)
    same = (folded.state_checksum() == live.state_checksum())
    print(f"     ops dispatched {dispatched}   applied by writers {applied_total}"
          f"   lost {dispatched - applied_total}")
    print(f"     fold(WAL) == writers' final state, bit-exact           : "
          f"{'PASS' if same else 'FAIL'}")
    print(f"     reader samples {reads[0]}   observed mid-pair writes {tears[0]}"
          f"  ({100.0 * tears[0] / max(1, reads[0]):.3f}% of reads; each is <= 1 count"
          f" for <= 1 ingest interval)")
    print("""
  (b) What staleness costs the DECISION, not the protocol: the posterior refreshed
  every K outcomes instead of every outcome (batched ingest). Same run, same world.
""")
    rows = []
    for K in (1, 64, 1024, 8192):
        r = Router(space, ingest_batch=K)
        m = drive(_world("baseline-steady-v1", n), r, n)
        _l, auth, margin, early, mae, to, _u = m.report(f"K={K}")
        rows.append((K, fmt(auth, 2), fmt(margin), fmt(mae, 2)))
    print(table(["ingest batch K", "auth%", "margin c/1k", "MAE pts"], rows))
    print("""
  (c) The contention model for Go, constants inline and labelled as a MODEL (order-of-
  magnitude, not a measurement of this sandbox):
       atomic load ~1 ns; channel send (uncontended, buffered) ~25-100 ns;
       mutex lock/unlock uncontended ~20 ns, contended ~1-5 us.
     At 5,000 decisions/s x 1.3 attempts = 6,500 updates/s over 8 shards = ~810/s per
     shard. A writer doing one apply (~50 ns) per event is ~0.005% busy; the queue is
     the buffer, not the bottleneck. Readers do 2 atomic loads per eligible arm per
     decision: ~12 ns at k=6, wait-free. A per-arm MUTEX instead would put a lock in
     the read path (Decide must never block on ingest), and a global lock would
     serialize 6,500 updates/s through ~40 ns of critical section -- it would still
     'work', which is exactly why the rule exists: it works until it doesn't.
     CAS on the (alpha, beta) pair needs a 16-byte compare-and-swap Go does not
     portably expose; two 8-byte CASes re-create the torn pair with a retry loop.
     The channel-per-shard design is the one that has no lock in the reader path, no
     CAS loop in the writer path, and a queue to absorb bursts.""")


def sec_p6(n):
    print("[P6] cold start: a processor added mid-run, three priors, three floors")
    print("""
  Charlie (the EEA/UK margin workhorse: second-cheapest, mid auth, eligible for 56%
  of traffic) is withheld from the eligible set until t=0.7, then onboarded cold --
  the world supported it all along, so 'always available' at the SAME prior is the
  counterfactual ceiling and 'never available' prices the processor. The pessimistic
  priors (x0.85 and x0.60, charlie-targeted) are the case the floor exists for: an
  industry baseline that is wrong about the new processor. 'share' = charlie's share
  of attempts over the post-onboarding window; 'settled' = charlie observations by
  end of run. Floors: 'onboard' = while the processor is in the onboarding state
  (entered at onboarding or a #8 reset, cleared at n_min settled observations), with
  probability eta attempt 0 goes to a uniformly chosen eligible onboarding processor;
  'arm' = the same but keyed on per-ARM settled counts; 'threshold' = any eligible
  processor under n_min settled, standing. The selection probability stays exactly
  computable in every variant (a known mixture).
""")
    space = default_space()
    prefix = _ensure_prefix(n)
    onboard = int(0.7 * n)
    target = "charlie"

    def run(label, prior_fn, eta, n_min, onboard_at=None, floor_mode="onboard"):
        r = Router(space, prior_fn=prior_fn, eta=eta, n_min=n_min,
                   floor_mode=floor_mode)
        m = drive(_world("baseline-steady-v1", n), r, n, onboard_at=onboard_at,
                  onboard_acq=target)
        post = sum(1 for op in r.wal if op[0] >= onboard and op[2] == target)
        post_all = sum(1 for op in r.wal if op[0] >= onboard)
        settled = sum(r.a[i] + r.b[i] for i in range(space.n_arms)
                      if i % len(ACQ) == ACQ_IX[target])
        return (label, f"{eta:.2f}", n_min or "-", floor_mode if eta else "-",
                fmt(100.0 * post / max(1, post_all), 2), int(settled),
                fmt(m.margin * 1000.0 / max(1, m.txns)),
                fmt(100.0 * m.mae_sum / max(1, m.mae_n), 2))

    calib = make_prior_fn(prefix, m=100.0)
    rows = [run("always available (ceiling)", calib, 0.0, 0),
            run("never available (its value)", calib, 0.0, 0, onboard_at="never")]
    pess85 = make_prior_fn(prefix, m=100.0, bias=0.85, bias_acq="charlie")
    pess60 = make_prior_fn(prefix, m=100.0, bias=0.60, bias_acq="charlie")
    for pname, pfn in (("jeffreys", None), ("calibrated", calib),
                       ("charlie-pessimistic x0.85", pess85),
                       ("charlie-pessimistic x0.60", pess60)):
        for eta, n_min, fm in ((0.0, 0, "onboard"), (0.05, 200, "onboard"),
                               (0.10, 1000, "onboard")):
            rows.append(run(pname, pfn, eta, n_min, onboard_at=onboard,
                            floor_mode=fm))
    for eta, n_min, fm in ((0.05, 200, "arm"), (0.05, 200, "threshold")):
        rows.append(run("charlie-pessimistic x0.60", pess60, eta, n_min,
                        onboard_at=onboard, floor_mode=fm))
    print(table(["prior", "eta", "n_min", "floor", "share post %", "settled obs",
                 "margin c/1k", "MAE pts"], rows))
    print("""
  -> READINGS: (1) with Jeffreys or a calibrated table, bare TS gives a new processor
     meaningful traffic immediately -- the wide prior IS the exploration budget, and
     the floor is redundant. (2) The floor earns its keep exactly when the prior is
     confidently wrong: the pessimistic row starves the workhorse and the floor
     un-starves it, recovering most of the always-available margin. (3) The floor's
     cost is bounded twice: eta caps its share of decisions, and it switches itself
     off when no eligible arm is under n_min -- in steady state it costs nothing.
     n_min is therefore 'how many observations make an arm trustworthy' (the posterior
     sd at n_min is ~sqrt(p(1-p)/n_min)), not a tuning dial.""".replace(
        "READINGS:", "READINGS:"))


def sec_p7(n):
    print("[P7] persistence: the WAL is canonical, snapshots are checkpoints of the fold")
    print("""
  The outcome event is written ONCE and serves as both the trace row and the write-
  ahead log of the learned state; the posterior is a fold over it. Snapshots do not
  add a second source of truth -- they bound replay time. Checks, on the default run:
""")
    space = default_space()
    _ensure_default(n)
    m0, r0 = CACHE["default_run"]
    wal = r0.wal
    t0 = walltime.perf_counter()
    folded = fold(wal, space)
    t_fold = walltime.perf_counter() - t0
    ok1 = folded.state_checksum() == r0.state_checksum()

    # snapshot at 50% of the WAL + tail replay. Because ingest is in-order and the
    # policy's draws are key-addressed, the snapshot-continued router must reproduce
    # the live run's decisions for every transaction in the tail, bit for bit.
    half = len(wal) // 2
    cont = fold(wal[:half], space)             # the snapshot: a checkpoint of the fold
    replay_chains = {}
    for op in wal[half:]:
        _seq, _att, acq, _o, bin_c, region, sca, mandate, amount = op
        if _seq not in replay_chains:
            # state here == fold(ops with seq < _seq) == what the live router saw
            req = _StubReq(bin_c, region, sca, mandate, amount, _seq)
            replay_chains[_seq] = tuple(cont.decide(req))
        cont.apply_op(op)
    chain_by_seq = dict(CACHE["default_chains"])
    compared = mismatches = 0
    for seq, ch in replay_chains.items():
        live = chain_by_seq.get(seq)
        if live is not None:
            compared += 1
            if tuple(live) != tuple(ch):
                mismatches += 1
    ok2 = (mismatches == 0)
    ok3 = (cont.state_checksum() == r0.state_checksum())

    print(f"     ops in WAL                       : {len(wal)}")
    print(f"     fold(WAL) == live state, bit-exact: {'PASS' if ok1 else 'FAIL'}")
    print(f"     snapshot@50% + tail fold == live  : {'PASS' if ok3 else 'FAIL'}")
    print(f"     decisions replayed from snapshot  : {compared} compared,"
          f" {mismatches} mismatches -> {'PASS' if ok2 else 'FAIL'}")
    print(f"""
     fold throughput (CPython, this sandbox): {len(wal) / t_fold:,.0f} ops/s
     -> Go projection on ADR-0001's 10/30/100x band:
        {len(wal) / t_fold * 10:,.0f} / {len(wal) / t_fold * 30:,.0f} / {len(wal) / t_fold * 100:,.0f} ops/s
        A 10M-op WAL (a long week at 20 TPS x 1.3 attempts) replays in
        {10_000_000 / (len(wal) / t_fold * 100):.1f}-{10_000_000 / (len(wal) / t_fold * 10):.1f} s native.
     snapshot size: {space.n_arms} arms x 32 B data = {space.n_arms * 32 / 1024:.0f} KiB
     (+16 B/arm prior array, read-only, swapped with the artifact, never persisted).
     Durability: the WAL is group-committed (fsync interval is #13's knob); a crash
     loses at most the un-fsynced tail. Snapshot cadence bounds REPLAY TIME, not data.
""")
    print("""
  Re-bucketing: the WAL carries the raw context, so changing the arm-space (band
  edges, or collapsing a dimension) is a RE-FOLD of the same log, not a cold start.
  Posterior quality on the last 20% of traffic, collapsed to a 4-band space -- the
  re-fold (with and without the shipped prior) vs the realistic alternative, a cold
  start at the informative prior:
""")
    prefix = _ensure_prefix(n)
    space4 = ArmSpace(("bin", "region", "sca", "mandate"), n_bands=4)
    refold = fold(wal, space4, prior_fn=make_prior_fn(prefix, m=100.0))
    noprior = fold(wal, space4)
    cold = Router(space4, prior_fn=make_prior_fn(prefix, m=100.0))
    world = _world("baseline-steady-v1", n)
    maes = {"refold": [], "noprior": [], "cold": []}
    for seq in range(int(0.8 * n), n, 7):
        req = _synth_req(world, seq)
        ctx4 = space4.ctx_index(req)
        for acq in eligible(req):
            truth = probe_truth(world, req, acq, 0, 0)
            pt = theta_truth(world, req, acq, truth)
            for nm, rr in (("refold", refold), ("noprior", noprior),
                           ("cold", cold)):
                i = space4.arm_index(ctx4, acq)
                maes[nm].append(abs(rr.mean(i) - pt))
    tot = len(maes["refold"])
    print(table(["posterior (4-band space)", "arms", "MAE pts (last 20% of traffic)"], [
        ("re-folded WAL + informative prior", space4.n_arms,
         fmt(100.0 * sum(maes["refold"]) / tot, 2)),
        ("re-folded WAL, no prior (Jeffreys)", space4.n_arms,
         fmt(100.0 * sum(maes["noprior"]) / tot, 2)),
        ("cold start at informative prior", space4.n_arms,
         fmt(100.0 * sum(maes["cold"]) / tot, 2)),
    ]))
    print("""
  -> The persistence contract for #13: one append-only log (the trace), snapshots as
     fold checkpoints, boot = snapshot + tail replay, and the log carries enough
     context to re-bucket. The backend (SQLite WAL vs files vs both) is #13's; the
     REQUIREMENT is that nothing else writes the posterior.""")


SECTIONS = {"P1": sec_p1, "P2": sec_p2, "P3": sec_p3, "P4": sec_p4, "P5": sec_p5,
            "P6": sec_p6, "P7": sec_p7}


def main(argv):
    n = DEFAULT_N
    only = None
    for arg in argv[1:]:
        if arg.startswith("--section="):
            only = arg.split("=", 1)[1]
        else:
            n = int(arg)
    print(f"#7 evidence spike: Thompson sampling implementation | "
          f"n={n} | policy seed {POLICY_SEED} | lambda_to={LAM_TO:.0f}")
    print(f"worlds: baseline-steady-v1@{scenario_hash(_doc('baseline-steady-v1'))[:19]}, "
          f"outage-recovery-v1@{scenario_hash(_doc('outage-recovery-v1'))[:19]}"
          f" (committed scenarios, spikes/0006 harness)")
    print()
    for name, fn in SECTIONS.items():
        if only is None or only == name:
            fn(n)
            print()
    if only is None:
        print("=" * 100)
        print("Reproduce: python3 spikes/0007-thompson-sampling/posterior.py"
              f" {n}   (RESULTS.md is this output)")


if __name__ == "__main__":
    main(sys.argv)
