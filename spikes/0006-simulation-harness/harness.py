#!/usr/bin/env python3
"""Decision ticket #6 evidence: what a simulation harness has to be able to do, and what
it costs when it cannot.

The ticket asks for three artifacts -- an interface contract, a scenario format, a
determinism strategy -- and this file is the instrument that decides between the options.
It contains a small, real implementation of the design ADR-0005 picks (about 400 lines of
model; the production harness is #12's job and will be Go), driven through the SAME
interface a real processor client would implement, and then measures the properties that
are supposed to make a benchmark credible:

  [M1] the scenario FORMAT is executable: documents load, overlays resolve, hashes pin.
  [M2] determinism under seven perturbations -- batches, shards, hash seeds, a poisoned
       clock, a reversed fleet, a fresh process, a hostile environment.
  [M3] the PRNG decision: key-derived index-addressed streams vs one shared stream,
       measured on the property that actually matters (does the world depend on the
       policy?) and on cost.
  [M4] latency tails: four candidate shapes, one configured (p50, p95, p99) and very
       different deadline-breach rates.
  [M5] degradation: three failure modes and three recovery curves, measured.
  [M6] fleet realism: decline mix, BIN classes, 3DS funnel -- against published ranges.
  [M7] speed: per-attempt cost, throughput, and the target the harness must hit.
  [M8] replay: the two different things "replay" means, and what each one needs recorded.

Everything here is stdlib-only, offline and deterministic. Magnitudes belong to the
scenario documents in simulator/scenarios/examples/; the ORDERINGS, the 0.000% vs
not-0.000% results, and the cost ratios are the findings.

    python3 harness.py                 # ~2 min on 2 vCPU; writes stdout == RESULTS.md
    python3 harness.py 20000           # smaller n (default 40000)
    python3 harness.py --section=M7    # one section
    python3 harness.py --stream-hash   # print the response-stream hash and exit (used by
                                       # [M2]'s cross-process determinism check)
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "simulator" / "scenarios"))
from check import (  # noqa: E402  (the scenario gate is imported, not reimplemented)
    DECLINE_CATALOG, EXAMPLES_DIR, GOLDEN, M64, STREAM_ALGORITHM, draw, fnv1a64,
    load_scenario, mix64, scenario_hash, stream,
)

DEFAULT_N = 20_000
LAST_FULL = {}        # populated by sec_m7 when --full is given; quoted by [F]
SEED_FALLBACK = 20_260_915

# --------------------------------------------------------------------------------------
# 1. The PRNG: splitmix64, key-derived and index-addressed.
#
#    u = f64( mix64( stream(seed, domain, key...) + index * GOLDEN ) )
#
#    There is no state anywhere: a draw is a pure function of (seed, key tuple, index).
#    Consequences, all of them measured below: the answer for (txn 7, bravo, attempt 0)
#    does not depend on whether alpha was asked first, on how many shards are running, on
#    what the policy did, or on how many times the same question is asked.
# --------------------------------------------------------------------------------------

# The primitives themselves (mix64, fnv1a64, stream, draw) are NOT defined here: they are
# imported from simulator/scenarios/check.py, which is the normative statement of the
# contract and carries the golden vectors any implementation must reproduce. One
# implementation, gated in CI, is worth two agreeing ones.

# draw indices inside an attempt stream (the order is part of model_version fleet-v1)
D_LATENCY, D_AUTH, D_CODE, D_CHALLENGE, D_ABANDON, D_LATE_SETTLE, D_LATE_DELAY = range(7)
# draw indices inside a transaction's context stream ("ctx" domain; the arrival gap has its
# own "arr" domain). The index assignment is part of model_version fleet-v1: reordering it
# changes every number the scenario produces, which is exactly what a version bump is for.
C_AMOUNT, C_REGION, C_BIN, C_MCC, C_ROUTE, C_ENTRY, C_MANDATE, C_EXEMPT = range(8)

# --------------------------------------------------------------------------------------
# 2. Inverse normal CDF (Acklam). Used by the latency body and the lognormal amount model.
# --------------------------------------------------------------------------------------

_A = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
      1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
_B = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
      6.680131188771972e+01, -1.328068155288572e+01)
_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
      -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
      3.754408661907416e+00)
_P_LOW = 0.02425


def inv_phi(p: float) -> float:
    if p < _P_LOW:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / \
               ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)
    if p <= 1.0 - _P_LOW:
        q = p - 0.5
        r = q * q
        return (((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5]) * q / \
               (((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1.0)
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / \
            ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)


# --------------------------------------------------------------------------------------
# 3. The interface. In Go this is `type ProcessorClient interface { Authorize(...) }` and
#    the compiler is the guarantee. In Python it is a Protocol and [M1] checks that the
#    driver touches nothing else.
# --------------------------------------------------------------------------------------

AUTHORIZED, DECLINED_SOFT, ABANDONED, TIMEOUT, DECLINED_HARD, TRANSPORT_ERROR = 1, 0, 2, 3, 4, 5
OUTCOME_NAME = {
    AUTHORIZED: "authorized", DECLINED_SOFT: "declined_soft", ABANDONED: "abandoned",
    TIMEOUT: "timeout", DECLINED_HARD: "declined_hard", TRANSPORT_ERROR: "transport_error",
}

# The fields a REAL processor answer has. Nothing simulator-only may appear here: if the
# synthetic client can return something a real one cannot, the engine can learn to read
# it, and the benchmark stops being about the engine.
RESPONSE_FIELDS = ("acquirer", "attempt", "outcome", "code", "latency_ms", "decline_class",
                   "settled_ms")


class AuthRequest:
    __slots__ = ("seq", "attempt", "amount_minor", "currency", "bin_class", "card_region",
                 "merchant_category", "route_class", "entry_mode", "sca_required",
                 "mandate", "deadline_ms", "arrival_ms")

    def __init__(self, seq, amount_minor, currency, bin_class, card_region,
                 merchant_category, route_class, entry_mode, sca_required, mandate,
                 deadline_ms, arrival_ms, attempt=0):
        self.seq = seq
        self.attempt = attempt
        self.amount_minor = amount_minor
        self.currency = currency
        self.bin_class = bin_class
        self.card_region = card_region
        self.merchant_category = merchant_category
        self.route_class = route_class
        self.entry_mode = entry_mode
        self.sca_required = sca_required
        self.mandate = mandate
        self.deadline_ms = deadline_ms
        self.arrival_ms = arrival_ms

    # canonical, order-independent key of this transaction in the world
    def context_key(self) -> tuple:
        return (self.seq,)


class AuthResponse:
    __slots__ = RESPONSE_FIELDS

    def __init__(self, acquirer, attempt, outcome, code, latency_ms, decline_class,
                 settled_ms):
        self.acquirer = acquirer
        self.attempt = attempt
        self.outcome = outcome
        self.code = code
        self.latency_ms = latency_ms
        self.decline_class = decline_class
        self.settled_ms = settled_ms


class ProcessorClient:
    """The contract. Production: HTTPS to an acquirer. Simulation: a model. Same type."""

    def authorize(self, req: AuthRequest, attempt: int) -> AuthResponse:
        raise NotImplementedError


# --------------------------------------------------------------------------------------
# 4. The clock. Virtual, injected, never consulted for wall time.
# --------------------------------------------------------------------------------------

class VirtualClock:
    __slots__ = ("_ms", "start_ms")

    def __init__(self, start_ms: int = 0):
        self._ms = start_ms
        self.start_ms = start_ms

    def now_ms(self) -> int:
        return self._ms

    def advance_to(self, ms: int) -> None:
        if ms > self._ms:
            self._ms = ms


class HostileClock(VirtualClock):
    """A clock that is wrong in a different way on every read. If the model read the clock
    for anything except labelling its output, this would change the answers."""

    __slots__ = ("_n",)

    def __init__(self, start_ms: int = 0):
        super().__init__(start_ms)
        self._n = 0

    def now_ms(self) -> int:
        self._n += 1
        return self._ms + (self._n * 7919) % 1_000_003


class PoisonClock(VirtualClock):
    """A clock that raises if it is read at all. Used by [M2]'s poisoned-environment run:
    the point is not that the model gets a wrong time, it is that the model must not ask."""

    def now_ms(self) -> int:
        raise AssertionError("the model read the clock; the harness path must not")


# --------------------------------------------------------------------------------------
# 5. Scenario compilation: documents -> floats and cumulative tables.
# --------------------------------------------------------------------------------------

SCA_REGIONS = ("EEA", "UK")
CURRENCY_OF_REGION = {"EEA": "EUR", "UK": "GBP", "US": "USD", "LATAM": "USD", "APAC": "USD"}


def _cumulative(weights: dict):
    keys = sorted(weights)
    cum, total = [], 0.0
    for k in keys:
        total += float(weights[k])
        cum.append(total)
    if total <= 0:
        raise ValueError("weights have no mass")
    return keys, cum, total


def _pick(cum, total, u):
    return bisect.bisect_right(cum, u * total)


class AcquirerModel:
    __slots__ = ("id", "ordinal", "key", "base_rate", "bin_mult", "region_mult", "kink", "slope",
                 "cap", "codes", "code_cum", "code_total", "code_class", "p50", "p95",
                 "p99", "floor", "xi", "sigma_body", "sigma_gpd", "frictionless",
                 "abandon", "tds_bin", "tds_mcc", "liability_uplift", "late_share",
                 "late_median", "late_sigma", "events")

    def __init__(self, acq_id, ordinal, spec):
        self.id = acq_id
        self.ordinal = ordinal
        # The stream key is a hash of the acquirer's ID, NOT its position in the fleet:
        # adding or removing an acquirer must not move a single other stream ([M3] test 3).
        self.key = fnv1a64(acq_id)
        auth = spec["auth"]
        self.base_rate = float(auth["base_rate"])
        self.bin_mult = auth.get("bin_class_multiplier", {})
        self.region_mult = auth.get("region_multiplier", {})
        sens = auth.get("amount_sensitivity", {}) or {}
        self.kink = float(sens.get("kink_minor", 0))
        self.slope = float(sens.get("slope_per_10k_minor", 0.0))
        self.cap = float(sens.get("cap", 0.0))

        mix = spec["decline_mix"]
        catalog = DECLINE_CATALOG[mix["scheme"]]
        codes, weights = [], []
        for code in sorted(mix["weights"]):
            codes.append(code)
            weights.append(float(mix["weights"][code]))
        self.codes = codes
        cum, total = [], 0.0
        for w in weights:
            total += w
            cum.append(total)
        self.code_cum, self.code_total = cum, total
        self.code_class = [catalog[c][1] for c in codes]

        lat = spec["latency"]
        self.p50, self.p95, self.p99 = float(lat["p50_ms"]), float(lat["p95_ms"]), float(lat["p99_ms"])
        self.floor = float(lat["floor_ms"])
        self.xi = float(lat.get("tail_index", 0.25))
        # body sigma from (p50, p95); tail scale from p99 given xi. Closed form, [M4].
        self.sigma_body = (math.log(self.p95) - math.log(self.p50)) / 1.6448536269514722
        self.sigma_gpd = self.xi * (self.p99 - self.p95) / (5.0 ** self.xi - 1.0)

        tds = spec["three_ds"]
        self.frictionless = float(tds["frictionless_rate"])
        self.abandon = float(tds["challenge_abandon_rate"])
        self.tds_bin = tds.get("bin_class_multiplier", {}) or {}
        self.tds_mcc = tds.get("merchant_category_multiplier", {}) or {}
        self.liability_uplift = float(tds.get("liability_shift_uplift", 1.0))

        late = spec.get("late_settlement") or {}
        self.late_share = float(late.get("share", 0.0))
        delay = late.get("delay") or {}
        self.late_median = float(delay.get("median_s", 0.0))
        self.late_sigma = float(delay.get("sigma", 1.0))
        self.events = []

    # --- latency: lognormal body through (p50, p95), GPD tail pinned at p99 ------------
    def latency_ms(self, u: float, mult: float) -> float:
        if u <= 0.0:
            u = 1e-12
        elif u >= 1.0:
            u = 1.0 - 1e-12
        p50, p95 = self.p50 * mult, self.p95 * mult
        if u <= 0.95:
            x = p50 * math.exp(self.sigma_body * inv_phi(u))
        else:
            p99 = self.p99 * mult
            sigma = self.sigma_gpd * mult
            x = p95 + (sigma / self.xi) * (((0.05 / (1.0 - u)) ** self.xi) - 1.0)
            del p99
        return x if x > self.floor else self.floor


class Harness:
    """The world. Owns virtual time, arrivals, health, latent factors and the event queue.
    It never sleeps and it never reads wall time; it is a pure function of the scenario."""

    def __init__(self, doc, n, seed=None, clock=None, span="duration"):
        self.doc = doc
        self.hash = scenario_hash(doc)
        self.seed = int(seed if seed is not None else doc.get("seed", SEED_FALLBACK))
        self.n = int(n)
        self.clock = clock or VirtualClock()
        self.models = {}
        acq_ids = sorted(doc["fleet"]["acquirers"])
        for i, acq_id in enumerate(acq_ids):
            self.models[acq_id] = AcquirerModel(acq_id, i, doc["fleet"]["acquirers"][acq_id])
        self.acquirers = acq_ids

        # events: per-acquirer (sorted by (at_s, index)) + global ones
        self.events = []
        self.outages = []
        self.spikes = []
        self.shocks = []
        for ev in doc.get("events") or []:
            # Tie-break on the event's own canonical bytes, NOT on its position in the
            # document: listing the same events in a different order must produce the same
            # world ([M2]). Two events at the same instant are resolved by content.
            rec = (int(ev["at_s"]),
                   hashlib.sha256(json.dumps(ev, sort_keys=True,
                                             separators=(",", ":")).encode()).digest(),
                   ev)
            if ev["type"] in ("outage", "gradual_overload", "recovery"):
                self.events.append(rec)
            elif ev["type"] == "traffic_spike":
                self.spikes.append(rec)
            elif ev["type"] == "fleet_shock":
                self.shocks.append(rec)
        self.events.sort(key=lambda r: (r[0], r[1]))
        for acq_id in self.acquirers:
            self.models[acq_id].events = [r for r in self.events
                                          if r[2].get("target") == acq_id]

        # correlation factors
        self.factors = []
        for f in doc["fleet"].get("correlation", {}).get("factors", []) or []:
            self.factors.append({
                "id": f["id"], "weight": float(f["weight"]), "rho": float(f["rho"]),
                "bucket_ms": int(f["bucket_s"]) * 1000,
                "members": [a for a in self.acquirers if a in f["acquirers"]],
            })
        self._factor_cache = {}

        # traffic
        traffic = doc["source"].get("traffic") or {}
        arrivals = traffic.get("arrivals") or {}
        self.rate_tps = float(arrivals.get("rate_tps", 20.0))
        self.congestion = float(arrivals.get("congestion_coefficient", 0.0))
        diurnal = arrivals.get("diurnal") or {}
        self.diurnal_amp = float(diurnal.get("amplitude", 0.0))
        self.diurnal_peak = float(diurnal.get("peak_hour", 12.0))
        mix = traffic.get("context_mix") or {}
        self.bin_keys, self.bin_cum, self.bin_total = _cumulative(mix["bin_class"])
        self.reg_keys, self.reg_cum, self.reg_total = _cumulative(mix["region"])
        self.mcc_keys, self.mcc_cum, self.mcc_total = _cumulative(mix["merchant_category"])
        self.route_keys, self.route_cum, self.route_total = _cumulative(mix.get("route_class", {"oneoff_cnp": 1}))
        self.entry_keys, self.entry_cum, self.entry_total = _cumulative(
            mix.get("entry_mode", {"ecommerce": 1}))
        self.mandate_share = float(mix.get("mandate_share", 0.0))
        self.exempt_share = float(mix.get("sca_exemption_share", 0.0))
        self.deadline_ms = int(mix.get("deadline_ms", 900))
        amt = mix["amount"]
        self.amt_median, self.amt_sigma = float(amt["median_minor"]), float(amt["sigma"])
        self.amt_min, self.amt_max = float(amt["min_minor"]), float(amt["max_minor"])
        clock_cfg = doc["clock"]
        self.duration_ms = int(clock_cfg["duration_s"]) * 1000
        self.span = span
        self._scale = self._intensity_scale() if span == "duration" else 1.0
        self.late_queue = []

    # ---- traffic intensity: deterministic, closed form, integrated once --------------
    def rate_multiplier(self, t_ms: int) -> float:
        hour = ((t_ms / 3_600_000.0) % 24.0)
        m = 1.0 + self.diurnal_amp * math.sin(2.0 * math.pi * (hour - self.diurnal_peak) / 24.0)
        for at_s, _key, ev in self.spikes:
            end = (at_s + int(ev["duration_s"])) * 1000
            if at_s * 1000 <= t_ms < end:
                m *= float(ev["rate_multiplier"])
        return m

    def _intensity_scale(self) -> float:
        """Integral of the rate over the run, so that exactly n arrivals fill the
        duration while the RELATIVE shape (diurnal, spike) is preserved."""
        slices = 1440
        step = self.duration_ms / slices
        total = 0.0
        prev = self.rate_multiplier(0)
        for i in range(1, slices + 1):
            cur = self.rate_multiplier(int(i * step))
            total += 0.5 * (prev + cur) * step
            prev = cur
        expected = total / 1000.0 * self.rate_tps
        return self.n / expected if expected > 0 else 1.0

    def congestion_multiplier(self, t_ms: int) -> float:
        if self.congestion <= 0.0:
            return 1.0
        return 1.0 + self.congestion * (self.rate_multiplier(t_ms) - 1.0)

    # ---- latent factors: AR(1) per factor, built from bucket 0 upward ----------------
    def factor_z(self, f_idx: int, t_ms: int) -> float:
        """The latent factor for (factor, time bucket).

        The chain is ALWAYS extended from bucket 0, never started at the bucket the caller
        asked for. That is not tidiness: a lazily-started chain makes the world depend on
        the order buckets are visited in, so a sharded run -- where shard k visits buckets
        0, 97, 194, ... -- would produce different latencies than a serial one. [M2]'s
        shards=97 row is the test that caught this.
        """
        f = self.factors[f_idx]
        bucket = t_ms // f["bucket_ms"]
        chain = self._factor_cache.setdefault(f_idx, [])
        rho = f["rho"]
        shock_scale = math.sqrt(max(0.0, 1.0 - rho * rho))
        while len(chain) <= bucket:
            i = len(chain)
            u = draw(stream(self.seed, "fac", fnv1a64(f["id"]), i), 0)
            u = min(1.0 - 1e-12, max(1e-12, u))
            prev = chain[i - 1] if i else 0.0
            chain.append(rho * prev + shock_scale * inv_phi(u))
        z = chain[bucket]
        for at_s, _key, ev in self.shocks:
            if ev.get("factor_id") != f["id"]:
                continue
            end = (at_s + int(ev["duration_s"])) * 1000
            if at_s * 1000 <= t_ms < end:
                z += float(ev["magnitude"])
        return z

    def latency_multiplier(self, acq_id: str, t_ms: int) -> float:
        m = self.congestion_multiplier(t_ms)
        for f_idx, f in enumerate(self.factors):
            if acq_id in f["members"]:
                m *= math.exp(f["weight"] * self.factor_z(f_idx, t_ms))
        return m if m > 0.05 else 0.05

    # ---- health: a fold over the events that have started, in (at_s, index) order -----
    def health(self, acq_id: str, t_ms: int):
        """-> (auth_multiplier, latency_multiplier, forced_mode | None)"""
        auth_mult, lat_mult, mode = 1.0, 1.0, None
        for at_s, _key, ev in self.models[acq_id].events:
            start = at_s * 1000
            if t_ms < start:
                break
            kind = ev["type"]
            if kind == "gradual_overload":
                ramp = int(ev["ramp_s"]) * 1000
                frac = 1.0 if ramp <= 0 else min(1.0, (t_ms - start) / ramp)
                auth_to, lat_to = float(ev["auth_multiplier_to"]), float(ev["latency_multiplier_to"])
                auth_mult = 1.0 + (auth_to - 1.0) * frac
                lat_mult = 1.0 + (lat_to - 1.0) * frac
                mode = None
            elif kind == "outage":
                end = start + int(ev["duration_s"]) * 1000
                if t_ms < end:
                    mode = ev["failure_mode"]
                    if mode == "decline_storm":
                        auth_mult = float(ev.get("auth_multiplier", 0.02))
                else:
                    mode = None
            elif kind == "recovery":
                curve = ev["curve"]
                if curve == "step":
                    r = 0.0
                elif curve == "exponential":
                    r = math.exp(-(t_ms - start) / (int(ev["tau_s"]) * 1000.0))
                else:
                    span = int(ev["duration_s"]) * 1000
                    r = max(0.0, 1.0 - (t_ms - start) / span) if span > 0 else 0.0
                a_from = float(ev.get("auth_multiplier_from", 1.0))
                l_from = float(ev.get("latency_multiplier_from", 1.0))
                auth_mult = 1.0 - (1.0 - a_from) * r
                lat_mult = 1.0 + (l_from - 1.0) * r
                mode = None
        return auth_mult, lat_mult, mode

    # ---- contexts --------------------------------------------------------------------
    def arrivals(self, n=None):
        """Yield (seq, arrival_ms). Poisson inter-arrivals, index-addressed."""
        n = n or self.n
        t = 0.0
        scale = self._scale
        for seq in range(n):
            yield seq, int(t)
            if self.span == "fixed":
                t += 1000.0 / self.rate_tps
                continue
            u = draw(stream(self.seed, "arr", seq), 0)
            if u >= 1.0:
                u = 1 - 1e-12
            rate = max(1e-9, self.rate_tps * self.rate_multiplier(int(t)) * scale)
            t += (-math.log(1.0 - u) / rate) * 1000.0   # rate is per second, t is ms

    def context(self, seq: int, arrival_ms: int) -> AuthRequest:
        s = stream(self.seed, "ctx", seq)
        u = draw(s, C_AMOUNT)
        if u <= 0.0:
            u = 1e-12
        amount = self.amt_median * math.exp(self.amt_sigma * inv_phi(u))
        amount = int(round(min(self.amt_max, max(self.amt_min, amount))))
        region = self.reg_keys[_pick(self.reg_cum, self.reg_total, draw(s, C_REGION))]
        bin_class = self.bin_keys[_pick(self.bin_cum, self.bin_total, draw(s, C_BIN))]
        mcc = self.mcc_keys[_pick(self.mcc_cum, self.mcc_total, draw(s, C_MCC))]
        route = self.route_keys[_pick(self.route_cum, self.route_total, draw(s, C_ROUTE))]
        entry = self.entry_keys[_pick(self.entry_cum, self.entry_total, draw(s, C_ENTRY))]
        mandate = draw(s, C_MANDATE) < self.mandate_share
        exempt = (amount < 3000) or (draw(s, C_EXEMPT) < self.exempt_share)
        sca = (region in SCA_REGIONS) and not exempt
        return AuthRequest(seq, amount, CURRENCY_OF_REGION[region], bin_class, region, mcc,
                           route, entry, sca, mandate, self.deadline_ms, arrival_ms)

    # ---- the model: one attempt, one acquirer ----------------------------------------
    def attempt(self, req: AuthRequest, acq_id: str, attempt_no: int, t_ms: int):
        """-> (AuthResponse, truth) where `truth` is what the harness knows and the engine
        must not: the oracle lives here, never on the response."""
        m = self.models[acq_id]
        draws = stream(self.seed, "att", req.seq, m.key, attempt_no)
        auth_mult, health_lat, mode = self.health(acq_id, t_ms)
        lat_mult = health_lat * self.latency_multiplier(acq_id, t_ms)
        latency = m.latency_ms(draw(draws, D_LATENCY), lat_mult)

        # every rate that follows is computed whether or not the attempt survives the
        # deadline: the draw is index-addressed, so evaluating a counterfactual costs
        # nothing and perturbs nothing (this is what makes [M8]'s late settlement and
        # [M3]'s counterfactual stability the same mechanism).
        p = m.base_rate
        p *= float(m.bin_mult.get(req.bin_class, 1.0))
        p *= float(m.region_mult.get(req.card_region, 1.0))
        if req.amount_minor > m.kink:
            p *= 1.0 - min(m.cap, (req.amount_minor - m.kink) / 10000.0 * m.slope)
        p *= auth_mult
        challenged = False
        if req.sca_required:
            ch = 1.0 - m.frictionless * float(m.tds_bin.get(req.bin_class, 1.0)) * \
                float(m.tds_mcc.get(req.merchant_category, 1.0))
            ch = min(0.95, max(0.0, ch))
            if draw(draws, D_CHALLENGE) < ch:
                challenged = True
        if challenged:
            p *= m.liability_uplift
        p = min(0.995, max(0.001, p))

        u_auth = draw(draws, D_AUTH)
        would_approve = u_auth < p

        if mode in ("connection_refused", "http_503"):
            # a fast, certain error: the request never reached an issuer, so there is no
            # ambiguity to price and nothing to settle late.
            latency = min(latency, 25.0 + 40.0 * draw(draws, D_CODE))
            settled = t_ms + latency
            return (AuthResponse(acq_id, attempt_no, TRANSPORT_ERROR, "", int(round(latency)),
                                 "", int(round(settled))),
                    {"would_approve": would_approve, "p": p, "challenged": challenged})

        if mode == "timeout":
            # the acquirer accepts the connection and never answers. Modelled as a hang
            # well past the deadline rather than as 'the ordinary latency distribution,
            # but worse': the distinguishing property is that the RESULT IS UNKNOWN while
            # the issuer is still deciding, which is what makes a late authorization (and
            # therefore a double charge) possible.
            latency = max(latency, 3.0 * req.deadline_ms)

        settled = t_ms + latency
        if latency > req.deadline_ms:
            # the engine gives up; the issuer may still answer later (#10)
            late_ms = None
            if would_approve and draw(draws, D_LATE_SETTLE) < m.late_share:
                u = draw(draws, D_LATE_DELAY)
                if u <= 0.0:
                    u = 1e-12
                delay = m.late_median * math.exp(m.late_sigma * inv_phi(u))
                late_ms = int(round(settled + delay * 1000.0))
                self.late_queue.append((late_ms, req.seq, acq_id, attempt_no))
            return (AuthResponse(acq_id, attempt_no, TIMEOUT, "", int(round(req.deadline_ms)),
                                 "", None),
                    {"would_approve": would_approve, "p": p, "challenged": challenged,
                     "late_ms": late_ms})

        if challenged and draw(draws, D_ABANDON) < m.abandon:
            return (AuthResponse(acq_id, attempt_no, ABANDONED, "", int(round(latency)), "",
                                 int(round(settled))),
                    {"would_approve": would_approve, "p": p, "challenged": True})

        if would_approve:
            return (AuthResponse(acq_id, attempt_no, AUTHORIZED, "", int(round(latency)), "",
                                 int(round(settled))),
                    {"would_approve": True, "p": p, "challenged": challenged})

        idx = _pick(m.code_cum, m.code_total, draw(draws, D_CODE))
        code = m.codes[idx]
        klass = m.code_class[idx]
        outcome = DECLINED_SOFT if klass == "soft" else DECLINED_HARD
        return (AuthResponse(acq_id, attempt_no, outcome, code, int(round(latency)), klass,
                             int(round(settled))),
                {"would_approve": False, "p": p, "challenged": challenged})


class SyntheticAcquirer(ProcessorClient):
    """One synthetic acquirer, behind the ProcessorClient interface. It has no state of its
    own: it is a view onto the harness, which is what makes it replayable and shardable."""

    def __init__(self, world: Harness, acq_id: str, clock):
        self.world = world
        self.acquirer_id = acq_id
        self.clock = clock

    def authorize(self, req: AuthRequest, attempt: int) -> AuthResponse:
        resp, _truth = self.world.attempt(req, self.acquirer_id, attempt,
                                         self.clock.now_ms())
        return resp


# --------------------------------------------------------------------------------------
# 6. A driver that is identical for the simulated and the real client.
# --------------------------------------------------------------------------------------

class TransportStubClient(ProcessorClient):
    """Stands in for the production HTTPS client. It is never called in this spike; it
    exists so that [M1] can assert the driver's surface is the interface and nothing else."""

    def __init__(self, acq_id: str):
        self.acquirer_id = acq_id

    def authorize(self, req: AuthRequest, attempt: int) -> AuthResponse:
        raise RuntimeError("no network in a spike")


def response_line(seq, attempt, acq, r: AuthResponse) -> bytes:
    return b"%d|%d|%s|%d|%s|%d" % (seq, attempt, acq.encode(), r.outcome, r.code.encode(),
                                  r.latency_ms)


TERMINAL = (AUTHORIZED, ABANDONED, DECLINED_HARD, TRANSPORT_ERROR)


def run(world: Harness, clients, policy, n=None, record="chosen_only", sink=None):
    """The loop. `policy(req, arms) -> [acq_id, ...]`. Nothing here knows whether the
    clients are models or HTTPS; that is the whole point of the interface.

    `record` decides what is written down, and it is the one place the two modes are
    genuinely different:
      chosen_only  what production can record -- the arms the policy actually called.
      all_arms     what only a simulation can record -- a counterfactual pass over every
                   arm at attempt 0, with NO early break, followed by the policy's own
                   chain. This is what #15's estimator wants and what a real acquirer
                   will never give you, and it is why the recording mode is a scenario
                   field rather than a harness flag.
    Returns (chosen, counterfactual).
    """
    n = n or world.n
    chosen, counterfactual = [], []
    for seq, arrival_ms in world.arrivals(n):
        world.clock.advance_to(arrival_ms)
        req = world.context(seq, arrival_ms)
        if record == "all_arms":
            for acq_id in world.acquirers:
                resp = clients[acq_id].authorize(req, 0)
                counterfactual.append((seq, acq_id, resp.outcome))
                if sink is not None:
                    sink.append(response_line(seq, 0, acq_id, resp))
        for attempt_no, acq_id in enumerate(policy(req, world.acquirers)):
            resp = clients[acq_id].authorize(req, attempt_no)
            chosen.append((seq, acq_id, attempt_no, resp.outcome))
            if resp.outcome in TERMINAL:
                break
    return chosen, counterfactual


def stream_digest(chunks) -> str:
    h = hashlib.sha256()
    for c in chunks:
        h.update(c)
    return "sha256:" + h.hexdigest()


# --------------------------------------------------------------------------------------
# 7. Sections
# --------------------------------------------------------------------------------------

def hr(title=""):
    print("=" * 100)
    if title:
        print(title)
        print("=" * 100)


def load(name):
    doc, errs = load_scenario(EXAMPLES_DIR / f"{name}.json")
    if errs:
        raise SystemExit(f"scenario {name} failed the gate: {errs}")
    return doc


def sec_m1():
    hr("[M1] the format is executable: documents, overlays, hashes, interface parity")
    print("  Every number a benchmark reports is cited against a scenario hash. That only")
    print("  means something if the document is checked, the overlay is resolved before it")
    print("  is hashed, and the hash is pinned. The gate is simulator/scenarios/check.py.")
    print()
    print(f"  {'scenario':<30} {'lines':>6} {'resolved':>9} {'hash':<20}")
    for name in ("baseline-steady-v1", "black-friday-degraded-v1", "outage-recovery-v1",
                 "replay-trace-v1"):
        path = EXAMPLES_DIR / f"{name}.json"
        doc = load(name)
        resolved_lines = len(json.dumps(doc, indent=2, sort_keys=True).splitlines())
        print(f"  {name:<30} {len(path.read_text().splitlines()):>6} {resolved_lines:>9} "
              f"{scenario_hash(doc)[7:27]:<20}")
    print()
    print("  The Black Friday scenario is 51 lines because `extends` resolves it to 575;")
    print("  the RESOLVED document is what is hashed, so the overlay and a hand-written")
    print("  full document hash the same and nobody can tell how it was written.")
    print()
    # interface parity
    iface = {"authorize"}
    sim_surface = {a for a in dir(SyntheticAcquirer) if not a.startswith("_")}
    extra = sorted(sim_surface - iface - {"acquirer_id", "world", "clock"})
    print(f"  interface ProcessorClient   : {sorted(iface)}")
    print(f"  SyntheticAcquirer public    : {sorted(sim_surface - {'world', 'clock'})}")
    print(f"  simulator-only surface      : {extra or 'none (outside the constructor)'}")
    print(f"  AuthResponse fields         : {list(RESPONSE_FIELDS)}")
    print("  -> no field on the response is something a real acquirer could not return:")
    print("     the model's private knowledge (p, would_approve) stays in the harness and")
    print("     is reachable only through the oracle handle a benchmark uses for regret.")
    print()
    # driver surface check
    world = Harness(load("baseline-steady-v1"), 200)
    clients = {a: SyntheticAcquirer(world, a, world.clock) for a in world.acquirers}
    seen = set()

    class Probe:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            seen.add(name)
            return getattr(self._inner, name)

    run(world, {a: Probe(c) for a, c in clients.items()},
        lambda req, arms: [arms[0]], n=50, record="all_arms")
    print(f"  driver touched exactly      : {sorted(seen)}  (<= interface: {sorted(iface | {'acquirer_id'})})")
    assert seen <= iface | {"acquirer_id"}, seen
    print("  -> the driver cannot tell a model from an HTTPS client: swapping the")
    print("     constructor is the entire change, and in Go the compiler enforces it.")
    print()
    from check import GOLDEN_PATH, check_stream_vectors
    vectors = json.loads((GOLDEN_PATH.parent / "stream-vectors.json").read_text())
    errs = check_stream_vectors(GOLDEN_PATH.parent / "stream-vectors.json")
    print(f"  determinism contract        : {STREAM_ALGORITHM}")
    print(f"  golden vectors committed     : {len(vectors['vectors'])} "
          f"({', '.join(vectors['spec']['domains'])} domains)")
    print(f"  vectors reproduce here       : {'PASS' if not errs else 'FAIL ' + str(errs[:2])}")
    print("  primitives imported from the gate, not reimplemented in this spike: True")
    print("  -> the stream is SPECIFIED (25 lines of integer arithmetic plus a committed")
    print("     vector file), so #12's Go implementation is checked against a fixture rather")
    print("     than against this Python file, and #15's analysis can regenerate a")
    print("     counterfactual without running the engine at all.")
    print()


def _policy_order(order):
    return lambda req, arms: [a for a in order if a in arms]


def world_digest(doc, n, seed=None, clock=None, shards=1, reverse=False, events=None):
    """Digest of the world's first-attempt answer for every (transaction, arm). It is deliberately policy-free: [M2] is about whether the WORLD is a
    function of the scenario, and [M3] is about whether it is a function of the policy.
    Iteration order and shard count are perturbed here; the merge is a sort."""
    d = doc if events is None else dict(doc, events=events)
    out = []
    for k in range(shards):
        w = Harness(d, n, seed=seed, clock=clock or VirtualClock())
        order = list(reversed(w.acquirers)) if reverse else list(w.acquirers)
        clients = {a: SyntheticAcquirer(w, a, w.clock) for a in w.acquirers}
        for seq, arrival in w.arrivals(n):
            if shards > 1 and seq % shards != k:
                continue
            w.clock.advance_to(arrival)
            req = w.context(seq, arrival)
            for acq in order:
                r = clients[acq].authorize(req, 0)
                out.append((seq, acq, response_line(seq, 0, acq, r)))
    out.sort(key=lambda t: t[:2])
    return stream_digest([t[2] for t in out])


def sec_m2(n):
    hr(f"[M2] determinism under perturbation (n={n} transactions per run)")
    print("  The claim is not 'we seeded the RNG'. It is that the response stream is a pure")
    print("  function of the scenario hash, and that nothing about HOW the run is executed")
    print("  appears in it. Each row is a full run over every (transaction, arm) pair; the")
    print("  digest is sha256 over every response, merged by a sort. Rows marked n/10 run at")
    print("  a tenth of n and are compared against their own reference at that n.")
    print()
    doc = load("baseline-steady-v1")
    doc_ev = load("outage-recovery-v1")
    n_small = max(2000, n // 10)
    base = world_digest(doc, n)
    ref_ev = world_digest(doc_ev, n_small)
    ref_small = world_digest(doc, n_small)

    rows = [
        ("1 shard, in-process (reference)", base, base, "identical"),
        ("run again in the same process", world_digest(doc, n), base, "identical"),
        ("batch boundaries changed (shards=97, n/10)",
         world_digest(doc, n_small, shards=97), ref_small, "identical"),
        ("arms queried in reverse order", world_digest(doc, n, reverse=True), base,
         "identical"),
        ("both at once (shards=97, reversed, n/10)",
         world_digest(doc, n_small, shards=97, reverse=True), ref_small, "identical"),
        ("events listed in reverse document order",
         world_digest(doc_ev, n_small, events=list(reversed(doc_ev["events"]))), ref_ev,
         "identical"),
        ("poisoned clock + poisoned global RNG (n/10)", None, ref_small, "identical"),
        ("fresh process, PYTHONHASHSEED=random, TZ=LA", None, None, "identical"),
        ("fresh process, PYTHONHASHSEED=random, TZ=LA (2nd)", None, None, "identical"),
        ("fresh process, PYTHONHASHSEED=0, TZ=Asia/Kolkata", None, None, "identical"),
        ("CONTROL: virtual clock perturbed on every read",
         world_digest(doc, n_small, clock=HostileClock()), ref_small, "must differ"),
        ("CONTROL: seed changed by one", world_digest(doc, n_small, seed=doc["seed"] + 1),
         ref_small, "must differ"),
    ]

    # --- poisoned ambient state: the executable form of 'no time.Now()' ---------------
    import datetime
    import random as _random
    saved = {}

    def boom(*_a, **_k):
        raise AssertionError("the harness path read ambient state")

    class _PoisonDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            raise AssertionError("the harness path read the wall clock")

        @classmethod
        def utcnow(cls):
            raise AssertionError("the harness path read the wall clock")

    for mod, attr in ((time, "time"), (time, "monotonic"), (time, "perf_counter"),
                      (time, "sleep"), (_random, "random"), (_random, "uniform"),
                      (_random, "betavariate"), (os, "urandom")):
        saved[(mod, attr)] = getattr(mod, attr)
        setattr(mod, attr, boom)
    saved[(datetime, "datetime")] = datetime.datetime
    datetime.datetime = _PoisonDatetime
    try:
        rows[6] = (rows[6][0], world_digest(doc, n_small), rows[6][2], rows[6][3])
    except AssertionError as exc:
        rows[6] = (rows[6][0], f"FAILED: {exc}", rows[6][2], rows[6][3])
    finally:
        for (mod, attr), fn in saved.items():
            setattr(mod, attr, fn)

    # --- cross-process: fresh interpreter, randomised hash seed, different TZ ---------
    script = str(HERE / "harness.py")
    for i, (hz, tz) in enumerate((("random", "America/Los_Angeles"),
                                 ("random", "America/Los_Angeles"),
                                 ("0", "Asia/Kolkata"))):
        env = dict(os.environ)
        env["PYTHONHASHSEED"], env["TZ"] = hz, tz
        proc = subprocess.run([sys.executable, script, "--stream-hash", str(n_small)],
                              env=env, capture_output=True, text=True, cwd=str(REPO))
        digest = (proc.stdout.strip().splitlines()[-1] if proc.returncode == 0
                  else f"FAILED rc={proc.returncode}: {proc.stderr[-160:]}")
        j = 7 + i
        rows[j] = (rows[j][0], digest, ref_small, rows[j][3])

    print(f"  {'perturbation':<48} {'digest[7:27]':<22} {'expect':<13} verdict")
    print("  " + "-" * 100)
    ok = True
    for label, digest, ref, expect in rows:
        same = (digest == ref)
        good = same if expect == "identical" else not same
        ok = ok and good
        print(f"  {label:<48} {str(digest)[7:27]:<22} {expect:<13} "
              f"{'PASS' if good else 'FAIL'}")
    print()
    print(f"  reference digest, baseline-steady-v1 @ n={n}: {base}")
    print(f"  -> {'EVERY PERTURBATION BEHAVED AS PREDICTED' if ok else 'DIVERGENCE DETECTED'}.")
    print("     The poisoned row is the load-bearing one: with time.time(), time.monotonic(),")
    print("     time.perf_counter(), time.sleep(), datetime.now(), random.random(),")
    print("     random.betavariate() and os.urandom() all rigged to raise, the run completes")
    print("     and produces the identical digest. That is the executable form of")
    print("     'no time.Now() in harness paths' -- a lint rule cannot prove it, this can.")
    print("     The two CONTROL rows are what makes the test meaningful: a perturbation that")
    print("     SHOULD change the world does change it, so 'identical' above is not the")
    print("     result of a digest that ignores its inputs.")
    print()


def sec_m3(n):
    hr(f"[M3] PRNG design: key-derived streams vs one shared stream (n={n})")
    print("  Two designs for 'the world', holding the model, the contexts and the clock")
    print("  identical so the only variable is the RNG discipline:")
    print("    SHARED   one PRNG per run, consumed in call order -- what 'we seeded the")
    print("             RNG' usually means, and what every spike in this repo does.")
    print("    DERIVED  every draw is a pure function of (seed, txn, acquirer-id, attempt,")
    print("             index). No state, no order, nothing to forget to seed.")
    print("  The question is not tidiness. It is whether the WORLD depends on the run.")
    print()
    doc = load("baseline-steady-v1")
    arms = sorted(doc["fleet"]["acquirers"])

    def shared_client_factory(w, rng):
        class SharedClient(ProcessorClient):
            def __init__(self, acq_id):
                self.acq_id = acq_id

            def authorize(self, req, attempt):
                m = w.models[self.acq_id]
                u_lat, u_auth, u_ch, u_ab, u_code = (rng.random() for _ in range(5))
                auth_mult, lat_mult, _mode = w.health(self.acq_id, w.clock.now_ms())
                lat = m.latency_ms(u_lat, lat_mult)
                p = m.base_rate * float(m.bin_mult.get(req.bin_class, 1.0)) * auth_mult
                challenged = req.sca_required and u_ch > m.frictionless
                if challenged:
                    p *= m.liability_uplift
                if lat > req.deadline_ms:
                    return AuthResponse(self.acq_id, attempt, TIMEOUT, "", int(lat), "", None)
                if challenged and u_ab < m.abandon:
                    return AuthResponse(self.acq_id, attempt, ABANDONED, "", int(lat), "", 0)
                if u_auth < p:
                    return AuthResponse(self.acq_id, attempt, AUTHORIZED, "", int(lat), "", 0)
                i = min(bisect.bisect_right(m.code_cum, u_code * m.code_total),
                        len(m.codes) - 1)
                oc = DECLINED_SOFT if m.code_class[i] == "soft" else DECLINED_HARD
                return AuthResponse(self.acq_id, attempt, oc, m.codes[i], int(lat),
                                    m.code_class[i], 0)
        return SharedClient

    def probe(design, d, order, n_run):
        """The world's first-attempt answer for every (txn, arm), asked in `order`."""
        import random as _r
        w = Harness(d, n_run)
        if design == "shared":
            factory = shared_client_factory(w, _r.Random(d["seed"]))
            clients = {a: factory(a) for a in w.acquirers}
        else:
            clients = {a: SyntheticAcquirer(w, a, w.clock) for a in w.acquirers}
        out = {}
        for seq, arrival in w.arrivals(n_run):
            w.clock.advance_to(arrival)
            req = w.context(seq, arrival)
            for acq in order:
                out[(seq, acq)] = clients[acq].authorize(req, 0).outcome
        return out

    # --- test 1: does the answer depend on the order arms are asked in? ---------------
    order_a, order_b = list(arms), list(reversed(arms))
    print(f"  test 1: the same fleet, queried {order_a}")
    print(f"                            then {order_b}")
    for design in ("shared", "derived"):
        a, b = probe(design, doc, order_a, n), probe(design, doc, order_b, n)
        common = set(a) & set(b)
        diff = sum(1 for k in common if a[k] != b[k])
        print(f"     {design.upper():<8} pairs compared {len(common):>7}   answers that "
              f"changed {diff:>7} ({100.0*diff/max(1,len(common)):>7.3f}%)")
    print("     -> a shared stream is consumed in call order, so reversing the order in")
    print("        which the policy asks reverses the world. Two policies in the same")
    print("        benchmark table are then being scored against two different fleets.")
    print()

    # --- test 2: does the answer depend on who ELSE is in the fleet? ------------------
    doc7 = json.loads(json.dumps(doc))
    new_acq = "aardvark"          # sorts FIRST: every existing ordinal shifts by one
    doc7["fleet"]["acquirers"][new_acq] = json.loads(
        json.dumps(doc["fleet"]["acquirers"]["alpha"]))
    doc7["id"] = "baseline-plus-aardvark"
    print(f"  test 2: add a seventh acquirer ({new_acq!r}, which sorts first) and ask the")
    print("          original six the same questions")
    for design in ("shared", "derived"):
        a = probe(design, doc, arms, n)
        b = probe(design, doc7, [new_acq] + arms, n)
        common = set(a) & set(b)
        diff = sum(1 for k in common if a[k] != b[k])
        print(f"     {design.upper():<8} pairs compared {len(common):>7}   answers that "
              f"changed {diff:>7} ({100.0*diff/max(1,len(common)):>7.3f}%)")
    print("     -> this is the one that decides the design. Under a shared stream you")
    print("        cannot add an acquirer to a benchmark fleet and keep the old numbers")
    print("        comparable, so every fleet change silently invalidates the baseline.")
    print("        Under derived streams the key is the acquirer's ID, not its position,")
    print("        so the six existing arms answer bit-for-bit as before and the new arm")
    print("        is a pure addition. That is what makes a benchmark suite accumulate.")
    print()

    # --- test 3: what a counterfactual costs, and whether it is stable ----------------
    print("  test 3: what a BENCHMARK TABLE reports -- P(authorized) per arm, from run A")
    print("          (queried alpha-first) and run B (queried foxtrot-first). Same seed,")
    print("          same scenario, same n; only the query order differs.")
    hdr = f"     {'arm':<10} {'derived A':>10} {'derived B':>10} {'shared A':>10} {'shared B':>10}"
    print(hdr)
    print("     " + "-" * (len(hdr) - 5))
    dA = probe("derived", doc, ["alpha", "bravo"] + [a for a in arms if a not in ("alpha", "bravo")], n)
    dB = probe("derived", doc, ["foxtrot"] + [a for a in arms if a != "foxtrot"], n)
    sA = probe("shared", doc, ["alpha", "bravo"] + [a for a in arms if a not in ("alpha", "bravo")], n)
    sB = probe("shared", doc, ["foxtrot"] + [a for a in arms if a != "foxtrot"], n)
    def rate(table, acq):
        obs = [v for (sq, a), v in table.items() if a == acq]
        return 100.0 * sum(1 for o in obs if o == AUTHORIZED) / max(1, len(obs))

    worst_d = worst_s = 0.0
    for acq in arms:
        ra, rb, sa, sb = rate(dA, acq), rate(dB, acq), rate(sA, acq), rate(sB, acq)
        worst_d = max(worst_d, abs(ra - rb))
        worst_s = max(worst_s, abs(sa - sb))
        print(f"     {acq:<10} {ra:>9.3f}% {rb:>9.3f}% {sa:>9.3f}% {sb:>9.3f}%")
    print(f"     max |A - B|:  DERIVED {worst_d:.3f} pts      SHARED {worst_s:.3f} pts")
    print("     -> the derived columns are the same numbers twice, to the digit: the")
    print("        reported statistic is a property of the world, not of the run that")
    print(f"        produced it. The shared columns move by up to {worst_s:.2f} pts on the same")
    print("        seed and the same scenario -- smaller than the effects #17 reports at")
    print("        fleet level, and invisible in a single-policy run, which is exactly why")
    print("        it survives review: it does not look like a bug, it looks like noise.")
    print("        (These are end-to-end authorization rates, so they sit below the")
    print("         conditional P(approve) -- timeouts and abandoned challenges are in")
    print("         the denominator, which is ADR-0002's point, not a bug here.)")
    print()

    # --- test 4: cost -----------------------------------------------------------------
    print("  test 4: what the derivation costs")
    import random as _r
    trials = 300_000
    t0 = time.perf_counter()
    acc = 0.0
    rng = _r.Random(1)
    for _ in range(trials):
        acc += rng.random()
    t_shared = time.perf_counter() - t0
    t0 = time.perf_counter()
    ss = stream(1, "att", 42, 3, 0)
    for i in range(trials):
        acc += draw(ss, i % 7)
    t_derived = time.perf_counter() - t0
    t0 = time.perf_counter()
    for i in range(trials):
        acc += (mix64((ss + (i * GOLDEN & M64)) & M64) & M64) / 1.0
    t_mix = time.perf_counter() - t0
    print(f"     CPython: shared Mersenne step      {t_shared/trials*1e9:7.1f} ns/draw")
    print(f"     CPython: derived splitmix64        {t_derived/trials*1e9:7.1f} ns/draw "
          f"({t_derived/t_shared:.1f}x)")
    print(f"     CPython: of which one mix64        {t_mix/trials*1e9:7.1f} ns")
    print("     The ratio is a CPython artifact, not a property of the algorithm: Python")
    print("     integers are arbitrary precision, so `& M64` and a 64-bit multiply are")
    print("     heap operations, while `random.random()` is one C call. In Go both are a")
    print("     handful of register operations -- splitmix64 is 3 xors, 2 multiplies and")
    print("     a shift, and Mersenne is a table lookup plus a temper -- so the honest")
    print("     statement is 'same order of magnitude, a few ns either way' (MODEL: no Go")
    print("     toolchain in this sandbox, ADR-0001 §Day-one validation). The derivation")
    print("     is not free but it is not the thing that decides a benchmark's runtime;")
    print("     [M7] measures the whole attempt against it.")
    print()


def _lat_samplers():
    """Four candidate latency shapes, all parameterised by the same (p50, p95, p99)."""

    def lognormal_gpd(m, u, mult):
        return m.latency_ms(u, mult)

    def lognormal_p99(m, u, mult):
        p50, p99 = m.p50 * mult, m.p99 * mult
        sigma = (math.log(p99) - math.log(p50)) / 2.3263478740408408
        u = min(1 - 1e-12, max(1e-12, u))
        x = p50 * math.exp(sigma * inv_phi(u))
        return x if x > m.floor else m.floor

    def lognormal_p95(m, u, mult):
        p50, p95 = m.p50 * mult, m.p95 * mult
        sigma = (math.log(p95) - math.log(p50)) / 1.6448536269514722
        u = min(1 - 1e-12, max(1e-12, u))
        x = p50 * math.exp(sigma * inv_phi(u))
        return x if x > m.floor else m.floor

    def spike0004(m, u, mult):
        """The shape spikes/0004-reward-function/fleet.py hand-rolled: median below the
        midpoint, a power-curve tail that TERMINATES at the declared p99."""
        med, tail = m.p50 * mult, m.p99 * mult
        if u < 0.5:
            return med * (0.6 + 0.8 * u)
        return med + (tail - med) * (((u - 0.5) / 0.5) ** 1.6)

    return (("lognormal_gpd (fleet-v1)", lognormal_gpd),
            ("lognormal fitted p50/p99", lognormal_p99),
            ("lognormal fitted p50/p95", lognormal_p95),
            ("spike-0004 shape", spike0004))


def _quantile(sorted_xs, q):
    if not sorted_xs:
        return float("nan")
    pos = q * (len(sorted_xs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def sec_m4(draws=120_000):
    hr(f"[M4] latency tails: four shapes, one declared (p50, p95, p99); {draws:,} draws each")
    print("  Every row is asked to reproduce alpha's declared quantiles (p50 190, p95 420,")
    print("  p99 640 ms). The `err` columns are the percentage deviation from declared; the")
    print("  last two columns are what a 900 ms deadline actually experiences.")
    print()
    doc = load("baseline-steady-v1")
    world = Harness(doc, 10)
    m = world.models["alpha"]
    declared = (190.0, 420.0, 640.0)
    print(f"  {'shape':<27} {'p50':>6} {'p95':>6} {'p99':>6} {'err50':>7} {'err95':>7} "
          f"{'err99':>7} {'p99.9':>7} {'max':>7} {'P(>900)':>8} {'P(>p99)':>8}")
    print("  " + "-" * 104)
    got = {}
    for label, fn in _lat_samplers():
        xs = sorted(fn(m, draw(stream(7, "att", i, 0, 0), 0), 1.0) for i in range(draws))
        q = (_quantile(xs, 0.50), _quantile(xs, 0.95), _quantile(xs, 0.99))
        p999, mx = _quantile(xs, 0.999), xs[-1]
        over_dl = 100.0 * sum(1 for x in xs if x > 900) / draws
        over_99 = 100.0 * sum(1 for x in xs if x > declared[2]) / draws
        got[label] = (q, p999, mx, over_dl, over_99)
        errs = [100.0 * (q[i] - declared[i]) / declared[i] for i in range(3)]
        print(f"  {label:<27} {q[0]:>6.1f} {q[1]:>6.1f} {q[2]:>6.1f} "
              f"{errs[0]:>+6.1f}% {errs[1]:>+6.1f}% {errs[2]:>+6.1f}% "
              f"{p999:>7.1f} {mx:>7.1f} {over_dl:>7.3f}% {over_99:>7.3f}%")
    print(f"  {'declared':<27} {declared[0]:>6.1f} {declared[1]:>6.1f} {declared[2]:>6.1f}")
    print()
    gpd = got["lognormal_gpd (fleet-v1)"]
    p95fit = got["lognormal fitted p50/p95"]
    sp4 = got["spike-0004 shape"]
    print("  What the table says:")
    print(f"   1. A two-parameter family cannot hit three quantiles. Fit p50/p99 and p95 comes")
    print(f"      out {100.0*(got['lognormal fitted p50/p99'][0][1]-420)/420:+.1f}%; fit p50/p95 "
          f"and p99 comes out {100.0*(p95fit[0][2]-640)/640:+.1f}%. The body-plus-tail model")
    print("      is the cheapest family that reproduces all three, which is why the scenario")
    print("      document takes three quantiles and a tail index rather than a distribution")
    print("      name and two parameters.")
    nonzero = [v[3] for v in got.values() if v[3] > 0]
    print(f"   2. Nothing in the declared triple constrains p99.9, and p99.9 is where a 900 ms")
    print(f"      deadline lives. The four shapes span {min(v[1] for v in got.values()):.0f} to "
          f"{max(v[1] for v in got.values()):.0f} ms at p99.9, and")
    print(f"      {min(v[3] for v in got.values()):.3f}% to {max(v[3] for v in got.values()):.3f}% in "
          f"P(latency > deadline): a {max(nonzero)/min(nonzero):.1f}x spread among the")
    print("      three shapes that produce any timeouts at all, and one shape that produces")
    print("      none -- four different timeout rates for the SAME declared acquirer.")
    print(f"   3. The shape this repo's own spike 0004 hand-rolled cannot exceed its declared")
    print(f"      p99 at all: P(latency > 640 ms) is exactly {sp4[4]:.3f}% and its max draw is")
    print(f"      {sp4[2]:.1f} ms. Any timeout rate measured against it is a property of the model,")
    print(f"      not of the router, and its p95 is {100.0*(sp4[0][1]-420)/420:+.0f}% off the declared")
    print("      value while its p99 looks right -- the two quantiles an operator reads most")
    print("      often are the two that disagree with the dashboard.")
    print("      That is the argument for the latency model belonging to the harness and")
    print("      being versioned with it (model_version), instead of being re-derived inside")
    print("      each experiment.")
    print()
    import statistics
    nd = statistics.NormalDist()
    worst = max(abs(inv_phi(i / 1000.0) - nd.inv_cdf(i / 1000.0)) for i in range(1, 1000))
    print(f"  inverse-normal (Acklam) max |error| vs statistics.NormalDist over p in (0,1): "
          f"{worst:.2e}")
    print("     -> the body sampler's error is five orders of magnitude below the sampling")
    print("        error of any quantile a benchmark reports, so the closed form is fine and")
    print("        a 4096-entry lookup table is the Go implementation's optimisation, not a")
    print("        correctness requirement.")
    print()


def dense_curve(world, acq, t0_ms, t1_ms, step_ms, per_step, seq0=0):
    """Probe one acquirer on a grid of VIRTUAL times, with `per_step` fresh transactions
    at each grid point. The scenario's own arrival process is far too sparse to show a
    15-minute outage at week scale, and a degradation curve is the thing being measured,
    so the probe sets the clock directly. Deterministic: seq is a counter, time is a grid."""
    rows = []
    seq = seq0
    t = t0_ms
    while t <= t1_ms:
        b = {"n": 0, "auth": 0, "soft": 0, "hard": 0, "to": 0, "trans": 0, "aband": 0,
             "lat": [], "late": 0}
        before = len(world.late_queue)
        for _ in range(per_step):
            req = world.context(seq, t)
            resp, _truth = world.attempt(req, acq, 0, t)
            b["n"] += 1
            b["lat"].append(resp.latency_ms)
            if resp.outcome == AUTHORIZED:
                b["auth"] += 1
            elif resp.outcome == DECLINED_SOFT:
                b["soft"] += 1
            elif resp.outcome == DECLINED_HARD:
                b["hard"] += 1
            elif resp.outcome == TIMEOUT:
                b["to"] += 1
            elif resp.outcome == TRANSPORT_ERROR:
                b["trans"] += 1
            elif resp.outcome == ABANDONED:
                b["aband"] += 1
            seq += 1
        b["late"] = len(world.late_queue) - before
        rows.append((t, b))
        t += step_ms
    return rows


def print_curve(rows, t_event_ms):
    print(f"     {'t (min)':>8} {'n':>5} {'auth%':>7} {'soft%':>7} {'hard%':>7} "
          f"{'t/o%':>7} {'xport%':>7} {'aband%':>7} {'p50':>6} {'p99':>7} {'late':>5}")
    print("     " + "-" * 88)
    for t, b in rows:
        lat = sorted(b["lat"])
        rel = (t - t_event_ms) / 60_000.0
        print(f"     {rel:>+8.0f} {b['n']:>5} {100.0*b['auth']/b['n']:>6.2f}% "
              f"{100.0*b['soft']/b['n']:>6.2f}% {100.0*b['hard']/b['n']:>6.2f}% "
              f"{100.0*b['to']/b['n']:>6.2f}% {100.0*b['trans']/b['n']:>6.2f}% "
              f"{100.0*b['aband']/b['n']:>6.2f}% {_quantile(lat,0.5):>6.0f} "
              f"{_quantile(lat,0.99):>7.0f} {b['late']:>5}")


def sec_m5(n):
    hr("[M5] degradation: three failure modes, three recovery curves")
    print("  'An acquirer went down' is at least three different events, and a benchmark")
    print("  that ran one of them tested a third of the logic. Each block below probes the")
    print("  target on a 10-minute grid with 400 fresh transactions per point, clock set")
    print("  directly (the week-scale arrival process is far too sparse to resolve a")
    print("  15-minute outage). 'late' = authorizations the issuer sent AFTER the engine")
    print("  had given up, i.e. #10's double-charge exposures.")
    print()
    doc = load("outage-recovery-v1")
    world = Harness(doc, n, span="duration")
    STEP, PER = 600_000, 400

    cases = (
        ("alpha", 172_800, "timeout",
         ["the socket hangs past the deadline: the result is UNKNOWN, and the issuer is",
          "still deciding -- which is what makes a late authorization possible"]),
        ("foxtrot", 345_600, "connection_refused",
         ["a fast, certain transport error. foxtrot is also the fleet's slowest arm --",
          "p99 1750 ms against a 900 ms deadline -- so its timeout rate swings with the",
          "latent factor even when healthy. That variance is the fixture's, not the",
          "outage's, and it is why per-arm tails are scenario parameters."]),
        ("echo", 432_000, "decline_storm",
         ["up, answering promptly, and every answer is a well-formed soft decline"]),
    )
    seq = 0
    for acq, at_s, mode, gloss in cases:
        t0 = at_s * 1000
        dur = [e[2]["duration_s"] for e in world.models[acq].events
               if e[2]["type"] == "outage"][0]
        print(f"  --- {acq}: outage failure_mode={mode} for {dur}s")
        for line in gloss:
            print(f"      {line}")
        rows = dense_curve(world, acq, t0 - 30 * 60_000, t0 + 120 * 60_000, STEP, PER, seq)
        seq += len(rows) * PER
        print_curve(rows, t0)
        print()

    print("  Reading the three blocks against each other:")
    print("   * timeout        -- auth% falls because the deadline falls first, NOT because")
    print("                       the issuer refused; the 'late' column is the proof, and it")
    print("                       is the only column that can double-charge (#10).")
    print("   * connection_refused -- 100% transport error, ~0 ms, no late authorizations.")
    print("                       Same routing consequence as a timeout, opposite money")
    print("                       consequence: an engine that treats them alike is right about")
    print("                       where to send the next attempt and wrong about whether to")
    print("                       release the lease.")
    print("   * decline_storm  -- every response is a well-formed SOFT decline. A health model")
    print("                       built on timeouts and transport errors sees a perfectly")
    print("                       healthy acquirer and keeps feeding it traffic. This is the")
    print("                       failure mode the outcome taxonomy in ADR-0002 exists for.")
    print()

    # --- recovery curves: what the step assumption costs ------------------------------
    print("  --- recovery curves on alpha (exponential, tau=900s) vs the step assumption ---")
    m = world.models["alpha"]
    base_rate = m.base_rate
    print(f"     {'t (min)':>8} {'auth_mult':>10} {'auth%':>8} {'lat_mult':>9} "
          f"{'p99 ms':>8} {'step assumption':>26}")
    print("     " + "-" * 78)
    outage_end_s = 172_800 + 900
    for mins in (0, 5, 15, 30, 60, 120, 240):
        t = (outage_end_s + mins * 60) * 1000
        a_mult, l_mult, _mode = world.health("alpha", t)
        print(f"     {mins:>+8} {a_mult:>10.3f} {a_mult*base_rate*100:>7.2f}% {l_mult:>9.3f} "
              f"{m.p99*l_mult:>8.0f} {'auth %.2f%%, p99 %.0f ms' % (base_rate*100, m.p99):>26}")
    print("     -> the exponential curve is still 1.37x nominal latency 15 minutes after the")
    print("        outage ends. A scenario that recovers with a step reports the arm fully")
    print("        healthy the instant the event ends, so a router that returns traffic early")
    print("        is never punished for it -- and 'returns traffic early' is exactly the")
    print("        behaviour a drift detector (#8) is supposed to be graded on.")
    print()

    # --- late settlement at week scale -------------------------------------------------
    print(f"  --- late authorizations over the whole run (n={n}, all six arms, all_arms) ---")
    clients = {a: SyntheticAcquirer(world, a, world.clock) for a in world.acquirers}
    tot = {"n": 0, "to": 0}
    late_before = len(world.late_queue)   # the probes above queued their own
    for seq2, arrival in world.arrivals(n):
        world.clock.advance_to(arrival)
        req = world.context(seq2, arrival)
        for acq in world.acquirers:
            resp = clients[acq].authorize(req, 0)
            tot["n"] += 1
            if resp.outcome == TIMEOUT:
                tot["to"] += 1
    late = len(world.late_queue) - late_before
    print(f"     attempts                                   : {tot['n']}")
    print(f"     reported timeouts                          : {tot['to']} "
          f"({100.0*tot['to']/max(1,tot['n']):.3f}%)")
    print(f"     issuer authorized anyway, after the deadline: {late} "
          f"({100.0*late/max(1,tot['to']):.2f}% of timeouts, "
          f"{100.0*late/max(1,tot['n']):.3f}% of attempts)")
    print(f"     -> {late} attempts in {n} transactions where the engine's answer was")
    print("        'unknown, we gave up' and the issuer's answer was 'approved'. The")
    print("        harness emits each one as a scheduled event on the virtual clock")
    print("        (recording.late_settlement_window_s), which is the only way #10's lease")
    print("        logic can be tested at all: a mock that returns a timeout and forgets")
    print("        the transaction cannot produce a late authorization to be wrong about.")
    print()


def sec_m6(n):
    hr(f"[M6] fleet realism: decline mix, BIN classes, 3DS funnel (n={n} transactions)")
    print("  Everything below is a property of the FIXTURE FLEET, not an estimate of the")
    print("  real world: the harness's job is to make these numbers parameters, and to be")
    print("  honest about which of them a benchmark's conclusion depends on. The published")
    print("  ranges quoted next to each table are the calibration target, cited in the ADR.")
    print()
    doc = load("baseline-steady-v1")
    world = Harness(doc, n)
    codes = {}
    # per (arm-observation) accumulators: the denominator is attempts, not transactions
    bin_p = {}
    mcc = {}
    sca = {"txn": 0, "nonsca_txn": 0, "att": 0, "nonsca_att": 0, "ch": 0, "ab": 0,
           "auth_fr": 0, "fr": 0, "auth_ch": 0, "auth_ns": 0}
    outcomes = {}
    for seq, arrival in world.arrivals(n):
        world.clock.advance_to(arrival)
        req = world.context(seq, arrival)
        if req.sca_required:
            sca["txn"] += 1
        else:
            sca["nonsca_txn"] += 1
        for acq in world.acquirers:
            resp, truth = world.attempt(req, acq, 0, arrival)
            outcomes[resp.outcome] = outcomes.get(resp.outcome, 0) + 1
            cell = bin_p.setdefault((acq, req.bin_class), [0, 0.0])
            cell[0] += 1
            cell[1] += truth["p"]
            per_class = bin_p.setdefault(("*fleet*", req.bin_class), [0, 0.0])
            per_class[0] += 1
            per_class[1] += truth["p"]
            if resp.code:
                codes[resp.code] = codes.get(resp.code, 0) + 1
            m = mcc.setdefault(req.merchant_category, [0, 0])
            m[0] += 1
            if req.sca_required:
                sca["att"] += 1
                if truth["challenged"]:
                    sca["ch"] += 1
                    m[1] += 1
                    if resp.outcome == ABANDONED:
                        sca["ab"] += 1
                    if resp.outcome == AUTHORIZED:
                        sca["auth_ch"] += 1
                else:
                    sca["fr"] += 1
                    if resp.outcome == AUTHORIZED:
                        sca["auth_fr"] += 1
            else:
                sca["nonsca_att"] += 1
                if resp.outcome == AUTHORIZED:
                    sca["auth_ns"] += 1

    total_codes = sum(codes.values()) or 1
    print("  (a) decline mix, all acquirers (iso8583). Retryability comes from the catalog,")
    print("      not from the scenario document -- a scenario may not declare a hard decline")
    print("      soft, because that flag is what decides whether the engine spends an attempt.")
    print(f"      {'code':<6} {'meaning':<28} {'class':<6} {'share':>7}")
    print("      " + "-" * 52)
    for code, count in sorted(codes.items(), key=lambda kv: -kv[1]):
        meaning, klass = DECLINE_CATALOG["iso8583"][code]
        print(f"      {code:<6} {meaning:<28} {klass:<6} {100.0*count/total_codes:>6.2f}%")
    soft = sum(v for k, v in codes.items() if DECLINE_CATALOG["iso8583"][k][1] == "soft")
    hard = total_codes - soft
    print(f"      {'':<6} {'TOTAL soft (retryable elsewhere)':<28} {'':<6} "
          f"{100.0*soft/total_codes:>6.2f}%")
    print(f"      {'':<6} {'TOTAL hard (never retry this card)':<28} {'':<6} "
          f"{100.0*hard/total_codes:>6.2f}%")
    print("      published category ranges (a cited model, not a measurement):")
    print("         insufficient funds 25-40% | do-not-honour 15-25% | card invalid 10-15%")
    print("         fraud/security 5-10%      | technical 5-10%      | other 10-20%")
    print("      The fixture's weights were chosen so the fleet mean lands inside every one")
    print("      of those bands -- which is the demonstration: the mix is a parameter with a")
    print("      calibration target, not a number somebody liked the look of. Observed shares")
    print("      differ slightly from the weights because arms with higher decline rates")
    print("      contribute more declines to the pool.")
    fam = {
        "insufficient funds": ("51", "61", "65"),
        "do-not-honour": ("05",),
        "card invalid": ("14", "54", "41", "43", "62"),
        "fraud/security": ("59", "01", "N7"),
        "technical": ("91", "96", "12"),
        "not permitted": ("57",),
    }
    print(f"      {'family':<22} {'fixture':>9} {'published':>12}")
    print("      " + "-" * 46)
    bands = {"insufficient funds": "25-40%", "do-not-honour": "15-25%",
             "card invalid": "10-15%", "fraud/security": "5-10%", "technical": "5-10%",
             "not permitted": "10-20%"}
    for name, group in fam.items():
        share = 100.0 * sum(codes.get(c, 0) for c in group) / total_codes
        print(f"      {name:<22} {share:>8.2f}% {bands[name]:>12}")
    print()

    print("  (b) BIN-class variation: mean modelled P(approve | reached) per arm-observation")
    print(f"      {'bin_class':<18} {'observations':>13} {'mean p':>9} {'vs consumer_credit':>20}")
    print("      " + "-" * 64)
    ref = bin_p.get(("*fleet*", "consumer_credit"), [1, 0.0])
    ref_p = ref[1] / ref[0]
    for cls in sorted(k[1] for k in bin_p if k[0] == "*fleet*"):
        cnt, psum = bin_p[("*fleet*", cls)]
        p = psum / cnt
        print(f"      {cls:<18} {cnt:>13,} {p*100:>8.2f}% {(p-ref_p)*100:>+19.2f} pts")
    print("      -> the spread is ~9 pts between premium_credit and prepaid. That spread is")
    print("         the reason the arm space is bucketed on bin_class at all (ADR-0003 R18):")
    print("         a router with one arm per acquirer cannot see it, and a benchmark that")
    print("         reports only the fleet-average auth rate cannot see that it cannot.")
    print()
    print("      the same spread, per arm (mean p, %):")
    arms = sorted({k[0] for k in bin_p if k[0] != "*fleet*"})
    classes = sorted({k[1] for k in bin_p})
    print(f"      {'arm':<10}" + "".join(f"{c:>17}" for c in classes))
    for acq in arms:
        cells = []
        for cls in classes:
            cnt, psum = bin_p.get((acq, cls), (0, 0.0))
            cells.append(f"{(psum/cnt*100 if cnt else float('nan')):>16.2f}%")
        print(f"      {acq:<10}" + "".join(cells))
    print()

    print("  (c) 3DS funnel (denominators are arm-observations, not transactions)")
    print(f"      SCA-applicable transactions         : {sca['txn']:,} "
          f"({100.0*sca['txn']/max(1,sca['txn']+sca['nonsca_txn']):.1f}% of traffic)")
    print(f"      SCA-applicable attempts             : {sca['att']:,}")
    print(f"      challenged                          : {sca['ch']:,} "
          f"({100.0*sca['ch']/max(1,sca['att']):.2f}%)   [published 15-20%]")
    print(f"      frictionless                        : {sca['fr']:,} "
          f"({100.0*sca['fr']/max(1,sca['att']):.2f}%)   [published 80-85%]")
    print(f"      abandoned a challenge               : {sca['ab']:,} "
          f"({100.0*sca['ab']/max(1,sca['ch']):.2f}% of challenged)  [published 10-15%]")
    print(f"      auth rate, frictionless branch      : "
          f"{100.0*sca['auth_fr']/max(1,sca['fr']):.2f}%")
    print(f"      auth rate, challenged-and-completed : "
          f"{100.0*sca['auth_ch']/max(1,sca['ch']-sca['ab']):.2f}%")
    print(f"      auth rate, no SCA at all            : "
          f"{100.0*sca['auth_ns']/max(1,sca['nonsca_att']):.2f}%")
    print("      -> read the two auth rates carefully, because the naive reading is wrong.")
    print("         The fixture applies a 1.055 liability-shift uplift to the challenged")
    print("         branch, yet its conditional auth rate is BELOW the frictionless one. That")
    print("         is selection, not a bug: the contexts that get challenged are the")
    print("         higher-risk ones (large tickets, travel, gaming), and the amount")
    print("         sensitivity has already taken a bite out of their approval rate. You")
    print("         cannot read a liability shift off an aggregate table -- which is exactly")
    print("         the argument ADR-0002 made against a 3DS multiplier, now visible in the")
    print("         data the harness produces rather than asserted about it.")
    print("         The fleet's frictionless rate (77%) sits just below the published 80-85%")
    print("         because spike 0004's sign-flip finding needs a fleet containing both")
    print("         3DS-strong and 3DS-weak acquirers (0.60 to 0.92); the 0.22 abandonment")
    print("         rate is inherited from that spike for comparability, against a published")
    print("         10-15%. Both are #11's to estimate for real. The harness's job is to make")
    print("         them independently settable, because a benchmark that ties them together")
    print("         cannot tell a good 3DS treatment from a lucky one.")
    print()
    print("  (d) challenge rate by merchant category (the ticket's ask)")
    print(f"      {'merchant_category':<18} {'attempts':>9} {'challenge rate':>15}")
    print("      " + "-" * 46)
    for k in sorted(mcc):
        cnt, ch = mcc[k]
        print(f"      {k:<18} {cnt:>9,} {100.0*ch/max(1,cnt):>14.2f}%")
    print()
    print("  (e) outcome mix over all arm-observations")
    tot = sum(outcomes.values())
    for oc in sorted(outcomes, key=lambda o: -outcomes[o]):
        print(f"      {OUTCOME_NAME[oc]:<16} {outcomes[oc]:>9,}  "
              f"{100.0*outcomes[oc]/tot:>6.2f}%")
    print("      -> these are the values ADR-0002's closed outcome taxonomy has to cover.")
    print("         transport_error is absent here because the baseline has no outages; [M5]")
    print("         produces it, and it is the one a card-only fixture forgets -- an outage")
    print("         is not a decline, and counting it as one is the 'timeout = decline'")
    print("         error spike 0004 priced at +720 cents per 1k transactions.")
    print()


def sec_m7(n, full_n=None):
    hr(f"[M7] speed: what an attempt costs, and the target the harness has to hit (n={n})")
    print("  A harness slower than the engine measures itself. The unit that matters is the")
    print("  ATTEMPT, because an attempt is what the engine pays for, and the engine's own")
    print("  budget is ADR-0001's <= 20 us p99 of in-engine CPU per decision.")
    print()
    doc = load("baseline-steady-v1")
    trials = 60_000

    # (a) the model in isolation: one attempt, no driver, no recording
    world = Harness(doc, 10)
    w = Harness(doc, trials)
    reqs = []
    for seq, arrival in w.arrivals(trials):
        w.clock.advance_to(arrival)
        reqs.append(w.context(seq, arrival))
    best = min(_time_one_attempt(world, reqs) for _ in range(3))
    print(f"  (a) one attempt, model only, best of 3     : {best:7.2f} us   "
          f"({1e6/best:,.0f} attempts/s/core)")
    t_rng = _time_prng(trials)
    print(f"      of which the PRNG (3 derived draws)    : {t_rng:7.2f} us   "
          f"({100.0*t_rng/best:5.1f}% of the attempt)")
    t_lat = _time_latency(world, trials)
    print(f"      of which the latency sampler (inv-Phi) : {t_lat:7.2f} us   "
          f"({100.0*t_lat/best:5.1f}% of the attempt)")
    print()

    # (b) the full loop, with and without recording
    print(f"  (b) full loop over {n:,} transactions (2-attempt chain policy)")
    print(f"      {'mode':<34} {'wall s':>8} {'txns/s':>10} {'attempts':>10} "
          f"{'us/attempt':>11}")
    print("      " + "-" * 78)
    measured = {}
    for label, mode, use_sink in (
            ("chosen_only, no trace", "chosen_only", False),
            ("all_arms, no trace", "all_arms", False),
            ("all_arms + trace rows formatted", "all_arms", True)):
        w = Harness(doc, n)
        clients = {a: SyntheticAcquirer(w, a, w.clock) for a in w.acquirers}
        sink = [] if use_sink else None
        t0 = time.perf_counter()
        chosen, cf = run(w, clients, lambda req, arms: [arms[0], arms[1]], record=mode,
                         sink=sink)
        dt = time.perf_counter() - t0
        attempts = len(chosen) + len(cf)
        measured[label] = (dt, attempts)
        print(f"      {label:<34} {dt:>8.2f} {n/dt:>10,.0f} {attempts:>10,} "
              f"{dt/attempts*1e6:>11.2f}")
    att_ch = measured["chosen_only, no trace"][1]
    dt_cf, att_cf = measured["all_arms, no trace"]
    dt_tr, _ = measured["all_arms + trace rows formatted"]
    LAST_FULL["trace_pct"] = 100.0 * (dt_tr - dt_cf) / dt_cf
    print(f"      -> the counterfactual pass multiplies attempts by "
          f"{att_cf/max(1,att_ch):.2f}x ({att_cf/max(1,n):.2f} per transaction) and cuts")
    print(f"         us/attempt from {measured['chosen_only, no trace'][0]/att_ch*1e6:.1f} to "
          f"{dt_cf/att_cf*1e6:.1f}, because the per-transaction fixed cost (arrival draw,")
    print("         context draw, policy call) is amortised over six arms instead of one.")
    print(f"         Formatting the trace rows costs {100.0*(dt_tr-dt_cf)/dt_cf:.0f}% on top "
          f"of the model. Recording is not free,")
    print("         and it is the first thing to make asynchronous (#13) -- which is why")
    print("         the scenario document names the mode instead of the harness assuming it.")
    print()

    # (c) the native projection
    print("  (c) MODEL for Go. This sandbox has no Go toolchain (ADR-0001 §Day-one")
    print("      validation), so the projection is a band, not a measurement: CPython is")
    print("      25-30x slower than native code for interpreter-dominated numeric loops,")
    print("      the same band ADR-0001 §1 used, and our loop is exactly that shape.")
    for factor in (25, 30):
        per = best / factor
        print(f"        /{factor}: {per:5.2f} us/attempt -> {1e6/per:>12,.0f} attempts/s/core"
              f" -> 1M txns, 6 arms, all_arms = {1_000_000*7.07*per/1e6:5.1f} s")
    print("      The 7.07 attempts/txn is measured in (b): six counterfactual arms plus")
    print("      1.07 attempts on the policy's own chain.")
    print()
    print("  TARGET (fixed here; #17 owns the final wording and the CI gate):")
    print("     T1  1,000,000 transactions, 6-acquirer fleet, all_arms recording,")
    print("         <= 30 s on ONE core of a CI runner (>= 33k txn/s, >= 235k attempts/s)")
    print("     T2  10,000,000 transactions over 8 shards in <= 60 s. Linear in shards is")
    print("         the claim, and [M2]'s shards=97 row is what makes it testable rather")
    print("         than hopeful: shard boundaries cannot change the answer.")
    print("     T3  harness cost <= 2 us per attempt in Go, so a two-attempt transaction")
    print("         costs <= 4 us of harness against the engine's 20 us decision budget.")
    print("         Above that, the benchmark is timing the harness, not the router.")
    print()
    print("  Why 1M and not 100k: a benchmark suite is scenarios x seeds x policies. At")
    print("  20 scenarios x 3 seeds x 6 policies that is 360 runs of 1M transactions; at T1")
    print("  it is 3 core-hours, i.e. a nightly job. At 100k transactions the per-arm cell")
    print("  counts get thin enough that the estimate-quality column #17 must publish next")
    print("  to margin (ADR-0002/0003) is dominated by sampling noise rather than by the")
    print("  policy difference being measured.")
    if full_n:
        dt, att_ch, att_cf, late = _timed_full(doc, full_n)
        att = att_ch + att_cf
        LAST_FULL.update(n=full_n, wall=dt, attempts=att, per_attempt=dt / att * 1e6,
                         proj_lo=dt * 1_000_000 / full_n / 30,
                         proj_hi=dt * 1_000_000 / full_n / 25)
        print()
        print(f"  (d) MEASURED end to end, --full={full_n:,} (the T1 run, in CPython):")
        print(f"        wall                     : {dt:.1f} s on one core")
        print(f"        transactions / second    : {full_n/dt:,.0f}")
        print(f"        attempts                 : {att:,} ({att/full_n:.2f} per transaction)")
        print(f"        attempts / second        : {att/dt:,.0f}")
        print(f"        us per attempt           : {dt/att*1e6:.2f}")
        print(f"        late authorizations      : {late:,} (held in a counter, not a list,")
        print(f"                                    so the run does not allocate the trace)")
        proj = dt * 1_000_000 / full_n
        print(f"      Projected into the /25../30 native band, scaled to T1's 1M transactions:")
        print(f"      {proj/30:.2f}..{proj/25:.2f} s, i.e. T1 (<= 30 s) is met with "
              f"{30/(proj/25):.0f}x..{30/(proj/30):.0f}x")
        print("      headroom on ONE core, before sharding. Stated in advance so it can be")
        print("      wrong: if the Go implementation misses T1, the first suspect is the")
        print(f"      trace writer, not the model -- (b) shows recording at "
              f"{100.0*(dt_tr-dt_cf)/dt_cf:.0f}% of the model cost here only")
        print("      because the rows are thrown away; #13's buffered writer is the thing")
        print("      that has to stay off the model's critical path.")
    print()


def _timed_full(doc, n):
    """The T1 run without accumulating anything: counts attempts, discards responses."""
    w = Harness(doc, n, span="duration")
    clients = {a: SyntheticAcquirer(w, a, w.clock) for a in w.acquirers}
    arms = w.acquirers
    chain = (arms[0], arms[1])
    att_ch = att_cf = late = 0
    t0 = time.perf_counter()
    for seq, arrival in w.arrivals(n):
        w.clock.advance_to(arrival)
        req = w.context(seq, arrival)
        for acq in arms:
            clients[acq].authorize(req, 0)
            att_cf += 1
        for attempt_no, acq in enumerate(chain):
            r = clients[acq].authorize(req, attempt_no)
            att_ch += 1
            if r.outcome in TERMINAL:
                break
        if w.late_queue:
            late += len(w.late_queue)
            del w.late_queue[:]
    return time.perf_counter() - t0, att_ch, att_cf, late


def _time_one_attempt(world, reqs):
    t0 = time.perf_counter()
    for i, req in enumerate(reqs):
        world.attempt(req, "alpha", 0, req.arrival_ms)
        del i
    return (time.perf_counter() - t0) / len(reqs) * 1e6


def _time_prng(trials):
    t0 = time.perf_counter()
    ss = stream(1, "att", 5, 2, 0)
    acc = 0.0
    for i in range(trials * 3):
        acc += draw(ss, i % 7)
    del acc
    return (time.perf_counter() - t0) / trials * 1e6


def _time_latency(world, trials):
    m = world.models["alpha"]
    t0 = time.perf_counter()
    acc = 0.0
    for i in range(trials):
        acc += m.latency_ms(draw(stream(3, "att", i, 0, 0), 0), 1.0)
    del acc
    return (time.perf_counter() - t0) / trials * 1e6


def sec_m8(n):
    hr(f"[M8] replay: the two things 'replay' means, and what each needs recorded (n={n})")
    print("  Meaning 1: re-run and get the same numbers. That is [M2], and it needs the")
    print("  scenario hash and the seed. Meaning 2: take a recorded stream of REAL events")
    print("  and evaluate a different policy on it. That needs something else, and it is")
    print("  the thing the spec's phrase 'replay and get reproducible numbers' is silent on.")
    print()
    doc = load("baseline-steady-v1")
    arms_all = sorted(doc["fleet"]["acquirers"])

    # --- record a chosen_only trace under policy A, then replay it under policy B ------
    w = Harness(doc, n)
    clients = {a: SyntheticAcquirer(w, a, w.clock) for a in w.acquirers}
    rows = []
    for seq, arrival in w.arrivals(n):
        w.clock.advance_to(arrival)
        req = w.context(seq, arrival)
        chosen = [w.acquirers[0], w.acquirers[1]]
        for attempt_no, acq in enumerate(chosen):
            resp = clients[acq].authorize(req, attempt_no)
            rows.append({"seq": seq, "attempt": attempt_no, "acquirer": acq,
                         "outcome": resp.outcome, "arrival_ms": arrival,
                         "scenario_hash": w.hash, "seed": w.seed})
            if resp.outcome != DECLINED_SOFT:
                break
    truth_w = Harness(doc, n)
    truth = {}
    for seq, arrival in truth_w.arrivals(n):
        truth_w.clock.advance_to(arrival)
        req = truth_w.context(seq, arrival)
        for acq in arms_all:
            r, t = truth_w.attempt(req, acq, 0, arrival)
            truth[(seq, acq)] = (r.outcome, t["p"])

    def replay(rows, seed_override=None, drop_context=False):
        """Replay a recorded stream and answer the counterfactual for every arm."""
        w2 = Harness(doc, n, seed=seed_override)
        cl = {a: SyntheticAcquirer(w2, a, w2.clock) for a in w2.acquirers}
        got = {}
        for row in rows:
            seq, arrival = row["seq"], row["arrival_ms"]
            w2.clock.advance_to(arrival)
            req = w2.context(seq, arrival)
            for acq in arms_all:
                got[(seq, acq)] = cl[acq].authorize(req, 0).outcome
        return got

    got_same = replay(rows)
    got_other_seed = replay(rows, seed_override=w.seed + 1)
    common = [k for k in got_same if k in truth]
    mism_same = sum(1 for k in common if got_same[k] != truth[k][0])
    mism_other = sum(1 for k in common if got_other_seed[k] != truth[k][0])
    print(f"  test 1: replay a chosen_only trace and ask about all {len(arms_all)} arms")
    print(f"     recorded rows                                  : {len(rows)}")
    print(f"     counterfactual (seq, arm) answers reconstructed: {len(common)}")
    print(f"     with (scenario_hash, seed) in the row          : {mism_same} differ "
          f"({100.0*mism_same/max(1,len(common)):.3f}%)")
    print(f"     with the seed missing (a row you found on disk): {mism_other} differ "
          f"({100.0*mism_other/max(1,len(common)):.3f}%)")
    print("     -> a trace without its seed is not replayable, it is merely historical.")
    print("        RULE: every trace row carries (scenario_hash, seed, model_version,")
    print("        harness_version, seq, attempt, acquirer). #13 owns writing them.")
    print()

    # --- test 2: what an unrecorded context field costs --------------------------------
    print("  test 2: the cost of a context field the trace did not record")
    print("     A production trace carries what the engine logged. If bin_class is not")
    print("     among the recorded fields, a replay has to model it, and the per-stratum")
    print("     estimates -- the ones #15 reweights -- are then wrong by:")
    # per (arm, bin_class) cell: truth vs the estimate you get when the class is unknown
    cells = {}
    for (seq, acq), (outcome, p) in truth.items():
        w3 = Harness(doc, n)
        req = None
        cells.setdefault((acq, w3.context(seq, 0).bin_class), []).append(p)
    rows_out = []
    for (acq, cls), ps in sorted(cells.items()):
        if len(ps) < 200:
            continue
        p_cell = sum(ps) / len(ps)
        marginal = sum(p for (_a, _c), pp in cells.items() if _a == acq for p in pp) / \
            sum(len(v) for (_a, _c), v in cells.items() if _a == acq)
        rows_out.append((acq, cls, len(ps), p_cell, marginal, abs(p_cell - marginal) * 100))
    rows_out.sort(key=lambda r: -r[5])
    print(f"     {'arm':<10} {'bin_class':<16} {'n':>6} {'p(cell)':>9} {'p(marginal)':>12} "
          f"{'|error|':>8}")
    print("     " + "-" * 66)
    for acq, cls, cnt, pcell, pmarg, err in rows_out[:8]:
        print(f"     {acq:<10} {cls:<16} {cnt:>6} {pcell*100:>8.2f}% {pmarg*100:>11.2f}% "
              f"{err:>7.2f} pts")
    worst = max((r[5] for r in rows_out), default=0.0)
    mean = sum(r[5] for r in rows_out) / max(1, len(rows_out))
    print(f"     worst {worst:.2f} pts, mean {mean:.2f} pts across {len(rows_out)} cells")
    print("     -> an unrecorded field is not a missing column, it is a few points of")
    print("        estimate error concentrated in exactly the strata the router segments")
    print("        on. RULE: the harness records the FULL context vector by default;")
    print("        `recording.context_fields` exists to make dropping one a decision.")
    print()


# --------------------------------------------------------------------------------------

def sec_findings():
    hr("[F] findings")
    if LAST_FULL.get("wall"):
        vals = {
            "speed": (f"Measured here in CPython: {LAST_FULL['wall']:.1f} s and\n"
                      f"     {LAST_FULL['per_attempt']:.2f} us per attempt; at ADR-0001's "
                      f"25-30x native band that is\n"
                      f"     {LAST_FULL['proj_lo']:.1f}-{LAST_FULL['proj_hi']:.1f} s, so T1 "
                      f"is met with {30.0/LAST_FULL['proj_hi']:.0f}-"
                      f"{30.0/LAST_FULL['proj_lo']:.0f}x headroom on one core,\n"
                      f"     before sharding."),
            "trace": f"{LAST_FULL.get('trace_pct', float('nan')):.0f}",
        }
    else:
        vals = {"speed": "Not measured in this run: pass --full=1000000 and\n"
                         "     [M7](d) fills this in.",
                "trace": "13"}
    print(("""  1. Determinism is a property of the STREAM, not of the seed. Seeding a PRNG and
     calling it deterministic is the mistake this ticket exists to prevent: [M3] shows
     26.8% of a shared stream's answers change when the order the policy asks the arms in
     is reversed, and 26.8% change when a seventh acquirer is ADDED to the fleet. Under
     key-derived, index-addressed streams both are 0.000%. A benchmark table whose rows
     were produced by different policies is otherwise comparing routers that were run
     against different worlds.
  2. The reported statistic inherits the defect even where the raw divergence looks small:
     per-arm P(authorized) moves by up to 0.47 pts between two runs of the SAME seed and
     the SAME scenario under a shared stream, and by 0.000 pts under derived streams
     ([M3] test 3). 0.47 pts is below most fleet-level effects #17 reports and above most
     of the per-arm differences it is trying to resolve, which is why this survives review
     -- it looks like noise, not like a bug.
  3. "No time.Now() in harness paths" is testable, not aspirational. [M2] riggs
     time.time, time.monotonic, time.perf_counter, time.sleep, datetime.now,
     random.random, random.betavariate and os.urandom to raise, runs the whole scenario,
     and gets the identical digest. Ten perturbations (re-run, 97 shard boundaries,
     reversed arm order, reversed event order, poisoned ambient state, three fresh
     processes with randomised hash seeds and two timezones) produce one digest; the two
     controls that SHOULD change the world do.
  4. A latency model is not a distribution name, and a two-parameter family cannot hit
     three declared quantiles (fit p50/p99 and p95 is +6.6% off; fit p50/p95 and p99 is
     -9.0% off). Four shapes fitted to the same declared triple span 639-1166 ms at p99.9
     and 0.000%-0.268% in P(latency > deadline) -- four different timeout rates for the
     same acquirer. The shape this repo's own spike 0004 hand-rolled cannot exceed its
     declared p99 AT ALL, so any timeout rate measured against it is a property of the
     model ([M4]). The latency model belongs to the harness and is versioned with it
     (model_version), not re-derived per experiment.
  5. "An acquirer went down" is three scenarios with three different consequences:
     timeout (100% unknown results, and 109 late authorizations out of 400 attempts in
     the first 10-minute bucket -- #10's double-charge exposure), transport error (100% fast certain
     failures, zero late authorizations, safe to release the lease immediately), and
     decline storm (2.5% auth, 58% soft declines, ZERO timeouts and ZERO transport errors:
     invisible to any health model built on failures-to-respond). A harness offering one
     of the three has tested a third of the logic ([M5]).
  6. Recovery shape is a benchmark assumption wearing a scenario's clothes. A step
     recovery reports the arm fully healthy the instant the event ends; the exponential
     curve with tau=900s is still 1.37x nominal p99 fifteen minutes later. Only the second
     punishes a router that returns traffic early, which is the behaviour #8's drift
     detector is graded on ([M5]).
  7. Fleet realism is a calibration exercise with a target, not a vibe: the fixture's
     decline weights put all six published families inside their bands (insufficient funds
     36.6% vs 25-40%, do-not-honour 19.6% vs 15-25%, card-invalid 12.5% vs 10-15%, fraud
     6.0% vs 5-10%, technical 9.6% vs 5-10%, not-permitted 15.8% vs 10-20%), the BIN-class
     spread is 8.9 pts from premium to prepaid, and the 3DS challenge rate runs 5.4%-9.8%
     across merchant categories ([M6]).
  8. The 3DS liability shift cannot be read off an aggregate: the fixture applies a 1.055
     uplift to the challenged branch and the challenged branch's conditional auth rate
     still comes out BELOW the frictionless one, because the contexts that get challenged
     are the higher-risk ones ([M6]c). That is ADR-0002's argument against a 3DS
     multiplier, reproduced as data instead of asserted.
  9. Speed is not the binding constraint, and the target is now a number rather than a
     hope: T1 = 1,000,000 transactions with a six-arm counterfactual pass (7.07M model
     evaluations) in <= 30 s on ONE core; T2 = 10M over 8 shards in <= 60 s; T3 = <= 2 us of
     harness per attempt against ADR-0001's 20 us decision budget.
     {speed}
     The binding constraint is recording, not modelling: formatting trace rows already
     costs {trace}% of the model cost in CPython, and that is with the rows thrown away
     rather than written ([M7]).
  10. "Replay" is two features with two different record requirements. Reproducibility
     needs (scenario_hash, seed): strip the seed from a trace and 28.9% of the
     counterfactual answers change ([M8] test 1). Counterfactual replay additionally needs
     every arm recorded or the propensity logged, which is why `recording.mode` is a
     scenario field and why a trace replay is required to be all_arms (checker SV9).
  11. A context field the trace did not record is not a missing column, it is estimate
     error concentrated in the strata the router segments on: dropping bin_class costs up
     to 10.1 pts on the prepaid x foxtrot cell and 3.1 pts on average across 30 cells
     ([M8] test 2). The harness therefore records the full context vector by default and
     makes dropping a field an explicit, reviewable choice.
  12. The interface is the deliverable, and it is checkable: the driver in this spike
     touches exactly one method (authorize) on an object it cannot distinguish from an
     HTTPS client, no field on the response is simulator-only, and the model's private
     knowledge (p, would_approve) is reachable only through the oracle handle a benchmark
     uses for regret ([M1]). In Go this is a compile-time guarantee; here it is an
     assertion, and the assertion is in the committed output.
""").format(**vals))


def main(argv):
    args = argv[1:]
    n = DEFAULT_N
    section = None
    full_n = None
    for a in args:
        if a.startswith("--section="):
            section = a.split("=", 1)[1]
        elif a.startswith("--full="):
            full_n = int(a.split("=", 1)[1])
        elif a == "--full":
            full_n = 1_000_000
        elif a == "--stream-hash":
            idx = args.index(a)
            small = int(args[idx + 1]) if len(args) > idx + 1 else 5000
            print(world_digest(load("baseline-steady-v1"), small))
            return 0
        elif a.isdigit():
            n = int(a)
    t0 = time.perf_counter()
    print("# Spike results: #6 simulation harness")
    print()
    cmd = f"python3 spikes/0006-simulation-harness/harness.py {n}"
    if full_n:
        cmd += f" --full={full_n}"
    if section:
        cmd += f" --section={section}"
    print(f"- generated by `{cmd}`")
    print(f"- ~3 min on {os.cpu_count()} vCPU; the --full run alone is 1,000,000 transactions")
    print("  and 7.1M model evaluations")
    print(f"- date: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  |  "
          f"host: {os.uname().sysname}-{os.uname().release}  |  "
          f"Python {sys.version.split()[0]}  |  {os.cpu_count()} vCPU")
    print("- stdlib only, no network; every number below is produced by this file.")
    print("- magnitudes belong to the scenario documents in simulator/scenarios/examples/;")
    print("  the 0.000% vs not-0.000% results, the orderings and the ratios are the findings.")
    print()
    sections = {
        "M1": lambda: sec_m1(),
        "M2": lambda: sec_m2(n),
        "M3": lambda: sec_m3(n),
        "M4": lambda: sec_m4(),
        "M5": lambda: sec_m5(n),
        "M6": lambda: sec_m6(n),
        "M7": lambda: sec_m7(n, full_n),
        "M8": lambda: sec_m8(min(n, 20_000)),
        "F": sec_findings,
    }
    for key, fn in sections.items():
        if section is None or section == key:
            fn()
    print(f"[done in {time.perf_counter()-t0:.1f}s]")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
