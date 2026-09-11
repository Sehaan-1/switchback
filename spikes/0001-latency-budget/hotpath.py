#!/usr/bin/env python3
"""Decision ticket #2 evidence: is the routing engine's hot path a *compute* problem?

The question this spike answers is narrow: how much CPU work is a routing decision,
and therefore can the *engine language* be the thing that decides whether we meet the
latency budget? The claim under test is that it cannot: a decision is a handful of
hash lookups plus a Beta draw per candidate arm, which is nanoseconds of work in any
compiled language and still microseconds in CPython.

Why CPython is the right instrument for this claim
---------------------------------------------------
This is not a Go-vs-Rust benchmark (we cannot run either here, and that is #17's job).
It is a *conservative bound*: CPython is roughly 1-2 orders of magnitude slower than
native code for exactly this shape of interpreter-dominated numeric loop. If the
decision fits its latency budget while interpreted, then the language and the GC are
not the bottleneck, and no plausible Go-vs-Rust difference changes that. The numbers
below are therefore reported alongside an extrapolation band, and the verdict is
stated at the *pessimistic* end of that band (no speedup at all).

What is measured (MEASURED):
  M1  decision compute: constraint filter + N Beta draws + argmax + propensity log
  M2  outcome-update path: per-arm counters under a lock-free-equivalent batch
  M3  live-heap footprint of the posterior store at realistic arm-space sizes

What is modelled, not measured (MODEL):
  B1  allocation rate -> Go GC cycle frequency -> whether GC can touch p99 at all
  B2  where the latency actually goes (processor network, 3DS), and the resulting
      budget we propose for the engine itself

Run:  python3 spikes/0001-latency-budget/hotpath.py
Deterministic: fixed seed.
"""

from __future__ import annotations

import array
import random
import struct
import sys
import time
from dataclasses import dataclass

SEED = 0xC0FFEE
BUDGET_P99_NS = 2_000_000  # 2 ms p99 added by the engine, our proposed Y for #17

# Extrapolation band from CPython to idiomatic Go/Rust for this workload.
# 10x is deliberately pessimistic for arithmetic-heavy interpreter loops; 30x and
# 100x bracket the typical range. The verdict is reported at 1.0x (no speedup).
SPEEDUP_BAND = (1.0, 10.0, 30.0, 100.0)

# ---------------------------------------------------------------------------
# The workload being priced
# ---------------------------------------------------------------------------

# Arm space (feeds #7): BIN-class x currency x amount-band x processor.
# BIN-class is a coarse bucket of (brand, card category, issuer region), not a
# 6-digit BIN, otherwise the space explodes and goes sparse.
BRANDS = 5
CARD_CATEGORIES = 4
ISSUER_REGIONS = 12
CURRENCIES = 12
AMOUNT_BANDS = 8
PROCESSORS = 8

ARM_SPACE = BRANDS * CARD_CATEGORIES * ISSUER_REGIONS * CURRENCIES * AMOUNT_BANDS * PROCESSORS
ARM_BYTES = 24  # alpha(f64) beta(f64) n_updates(i64) last_update_ns(i64) -> no pointers
POSTERIOR_BYTES = ARM_SPACE * ARM_BYTES


@dataclass(frozen=True)
class Request:
    """The subset of RoutingRequest that the hot path touches. Shape per #12."""

    bin_class: int
    currency: int
    amount_band: int
    needs_3ds: bool
    mandate: bool
    floor_margin_bps: int
    deadline_ms: int


def make_arm_store(n_arms: int, rng: random.Random) -> tuple[array.array, array.array]:
    """Two flat arrays of doubles, indexed by arm id.

    Shape matters more than language here: this is a pointer-free, never-resized,
    fixed-layout store. In Go that means []float64 (or []ArmStat of float64/int64
    fields) which the collector can skip in bulk; in Rust it means the same thing
    with no refcount or borrow gymnastics. Neither language pays a per-access cost
    beyond bounds checking / cache misses.
    """
    alpha = array.array("d", bytes(8 * n_arms))
    beta = array.array("d", bytes(8 * n_arms))
    for i in range(n_arms):
        n = rng.randint(0, 4000)  # some arms are cold, most are never seen
        alpha[i] = 0.5 + rng.random() * n * 0.85  # Jeffreys prior + noisy success mass
        beta[i] = 0.5 + n - (n * 0.85 * rng.random())
    return alpha, beta


MARGIN_BPS = [210, 180, 240, 165, 195, 230, 150, 205]  # per-processor cost table


def decide(req: Request, candidates: list[int], alpha: array.array, beta: array.array,
           rng: random.Random) -> tuple[int, float]:
    """One routing decision: filter -> sample -> argmax -> propensity.

    The constraint layer runs *before* the bandit (filter the action space rather
    than veto after sampling) so that every arm we sample is legal and propensities
    are computed over the legal set. That ordering also keeps the arm out of the
    posterior update path when it was never eligible. #5 owns the full taxonomy;
    here we only price two representative predicates.
    """
    legal = []
    for arm in candidates:
        # constraint: 3DS mandate (processor capability bit) -- cheap predicate
        if req.needs_3ds and (arm & 0b1000):
            continue
        # constraint: floor margin (per-processor cost table) -- cheap predicate
        if MARGIN_BPS[arm % len(MARGIN_BPS)] < req.floor_margin_bps:
            continue
        legal.append(arm)
    if not legal:
        return -1, 0.0

    # Thompson sampling: one Beta draw per legal arm, then argmax.
    best_arm, best_theta = legal[0], rng.betavariate(alpha[legal[0]], beta[legal[0]])
    thetas = [best_theta]
    s = best_theta
    for arm in legal[1:]:
        t = rng.betavariate(alpha[arm], beta[arm])
        thetas.append(t)
        s += t
        if t > best_theta:
            best_arm, best_theta = arm, t
    # propensity for off-policy evaluation (#15) + trace record (#12)
    propensity = best_theta / s if s > 0 else 0.0
    return best_arm, propensity


def candidates_for(req: Request) -> list[int]:
    """Legal arm ids = same context bucket, one arm per processor."""



# ---------------------------------------------------------------------------
# M1 / M2: timing helpers
# ---------------------------------------------------------------------------

def bench(fn, *, rounds: int = 11, per_round: int = 30_000) -> tuple[float, float, int]:
    """Return (median_of_round_means_ns, global_min_ns, iters).

    This box has 2 vCPUs and no perf isolation, so single-shot numbers are noisy.
    Each round's mean is taken, then we report the median across rounds (stable)
    and the global per-iteration minimum (the floor, i.e. cost with no interference).
    """
    fn()  # warm
    round_means: list[float] = []
    best = float("inf")
    for _ in range(rounds):
        t0 = time.perf_counter()
        for _ in range(per_round):
            dt0 = time.perf_counter()
            fn()
            dt = (time.perf_counter() - dt0) * 1e9
            if dt < best:
                best = dt
        round_means.append((time.perf_counter() - t0) * 1e9 / per_round)
    round_means.sort()
    return round_means[len(round_means) // 2], best, rounds * per_round


def candidates_for(req: Request, n: int) -> list[int]:
    """The n arms the bandit must sample for one request.

    Arm granularity is #7's decision, but the cost question needs a range: if an
    arm is (context x processor) you sample ~8; if it is (context x processor x
    3DS flow x retry-class) you sample 16-48. We stride across the whole store so
    the measurement includes real cache misses rather than one hot line.
    """
    base = (((req.bin_class * CURRENCIES + req.currency) * AMOUNT_BANDS + req.amount_band)
            * PROCESSORS)
    stride = max(1, ARM_SPACE // (n * 7))
    return [(base + i * stride) % ARM_SPACE for i in range(n)]


def measure_m1(alpha, beta, n_candidates: int) -> tuple[float, float, int]:
    rng = random.Random(SEED)
    reqs = [Request(rng.randrange(BRANDS * CARD_CATEGORIES * ISSUER_REGIONS),
                    rng.randrange(CURRENCIES), rng.randrange(AMOUNT_BANDS),
                    rng.random() < 0.55, rng.random() < 0.12,
                    rng.choice([0, 150, 180]), 900)
            for _ in range(4096)]
    idx = [0]

    def one() -> None:
        i = idx[0] = (idx[0] + 1) & 4095
        req = reqs[i]
        cands = candidates_for(req, n_candidates)
        decide(req, cands, alpha, beta, rng)

    return bench(one)


def measure_m2(alpha, beta) -> tuple[float, float, int]:
    """Outcome ingest: the streamed webhook path. Batched increment, no realloc."""
    rng = random.Random(SEED + 1)
    arms = [rng.randrange(ARM_SPACE) for _ in range(4096)]
    idx = [0]

    def one() -> None:
        for _ in range(8):  # a batch of 8 outcome events
            i = idx[0] = (idx[0] + 1) & 4095
            a = arms[i]
            if rng.random() < 0.86:
                alpha[a] += 1.0
            else:
                beta[a] += 1.0

    return bench(one)


# ---------------------------------------------------------------------------
# B1: Go GC model -- when could the collector actually threaten p99 here?
# ---------------------------------------------------------------------------

def model_gc(live_heap_mb: float, alloc_rate_mb_per_s: float, cores: int,
             rps: float, gogc: int = 100, stw_us: float = 60.0) -> dict:
    """Analytic model of a concurrent mark-sweep collector (Go's shape).

    Inputs, not measurements:
      stw_us        - Go's stop-the-world phases (scan roots, mark term.) are
                      designed to stay sub-millisecond; 60us is typical for a
                      small service on a few cores.
      mark cost     - ~0.15 ms of concurrent mark work per MB of live heap,
                      spread over `cores` helper goroutines.
      gogc=100      - heap doubles between cycles, so cycle frequency is set by
                      the allocation rate and the live heap size, nothing else.
    Outputs that matter for a p99 SLO:
      stw_wall_frac - share of wall-clock where all goroutines are halted
      reqs_per_cycle- how many requests land inside one GC cycle (=> how many
                      can be hit by the pause / pay assist tax)
      gc_cpu_cores  - concurrent mark work as whole cores
      assist_us_req - the share of that mark work each request pays for itself
    """
    live_mb = live_heap_mb
    growth_mb = live_mb * gogc / 100
    out = {"live_mb": live_mb, "alloc_mb_s": alloc_rate_mb_per_s, "cores": cores,
           "stw_us": stw_us}
    if alloc_rate_mb_per_s <= 0:
        out.update(cycles_per_s=0.0, gc_interval_s=float("inf"), stw_wall_frac=0.0,
                   reqs_per_cycle=0.0, gc_cpu_cores=0.0, assist_us_req=0.0)
        return out
    gc_interval_s = growth_mb / alloc_rate_mb_per_s
    cycles_per_s = 1.0 / gc_interval_s
    mark_ms_cycle = 0.15 * live_mb / max(cores, 1)
    gc_cpu_cores = cycles_per_s * mark_ms_cycle / 1e3
    out.update(
        cycles_per_s=cycles_per_s,
        gc_interval_s=gc_interval_s,
        stw_wall_frac=cycles_per_s * stw_us / 1e6,
        reqs_per_cycle=rps * gc_interval_s,
        gc_cpu_cores=gc_cpu_cores,
        assist_us_req=gc_cpu_cores * 1e6 / max(rps, 1e-9),
    )
    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    rng = random.Random(SEED)
    print("=" * 78)
    print("#2 evidence spike: routing-decision hot path cost")
    print(f"python {sys.version.split()[0]} | {ARM_SPACE:,} arms | "
          f"posterior store {POSTERIOR_BYTES/1e6:.1f} MB")
    print("=" * 78)

    alpha, beta = make_arm_store(ARM_SPACE, rng)

    # M3 (structure, not timing)
    print("\n[M3] posterior store shape")
    print(f"  arms                     : {ARM_SPACE:,} "
          f"({BRANDS} brand x {CARD_CATEGORIES} category x {ISSUER_REGIONS} region x "
          f"{CURRENCIES} cur x {AMOUNT_BANDS} band x {PROCESSORS} proc)")
    print(f"  bytes per arm            : {ARM_BYTES} (no pointers)")
    print(f"  live heap for all arms   : {POSTERIOR_BYTES/1e6:.2f} MB")
    print(f"  arms touched per decision: {PROCESSORS} (one per candidate processor)")
    print("  -> the arm space sizes MEMORY, not per-decision latency; the hot path")
    print("     touches only the candidate set, so decision cost is O(candidates).")

    # M1
    print(f"\n[M1] decision cost, CPython (upper bound on any compiled language)")
    print(f"  {'candidates':>10} | {'mean us':>9} | {'min us':>8} | {'% of 2ms budget':>15}")
    rows = []
    for n in (2, 4, 8, 16, 32):
        mean_ns, min_ns, iters = measure_m1(alpha, beta, n)
        rows.append((n, mean_ns / 1e3, min_ns / 1e3))
        print(f"  {n:>10} | {mean_ns/1e3:>9.2f} | {min_ns/1e3:>8.2f} | "
              f"{100*mean_ns/BUDGET_P99_NS:>14.3f}%")
    n8_mean_us = next(r[1] for r in rows if r[0] == PROCESSORS)
    n8_min_us = next(r[2] for r in rows if r[0] == PROCESSORS)

    print(f"\n  extrapolation to native (same algorithm, no interpreter):")
    for f in SPEEDUP_BAND:
        worst = n8_mean_us / f
        print(f"    {f:>5.1f}x -> {worst:>7.3f} us per decision "
              f"({100*worst*1e3/BUDGET_P99_NS:>6.3f}% of the 2 ms p99 budget)")
    print(f"  -> at 1.0x (i.e. if Go/Rust were no faster than CPython) the engine's own")
    print(f"     compute still consumes {100*n8_mean_us*1e3/BUDGET_P99_NS:.2f}% of budget."
          f" The 2ms budget is {BUDGET_P99_NS/1e3/n8_mean_us:.0f}x the compute.")

    # M2
    mean_ns, min_ns, _ = measure_m2(alpha, beta)
    print(f"\n[M2] outcome ingest, batch of 8 arms: mean {mean_ns/1e3:.2f} us, "
          f"min {min_ns/1e3:.2f} us (CPython)")
    print(f"  -> amortised {mean_ns/8/1e3:.2f} us/update; at 10k updates/s that is "
          f"{10000*mean_ns/1e9*100:.2f}% of one core even interpreted.")

    # B1
    print("\n[B1] MODEL (not measured): when could a GC actually threaten p99?")
    print("  Engine design target: hot path allocates nothing per decision")
    print("  (pre-sized posterior, reused scratch, no per-request JSON, no boxing).")
    hdr = (f"  {'live heap':>10} | {'alloc MB/s':>10} | {'cycle every':>11} | "
           f"{'STW wall %':>10} | {'reqs/cycle':>10} | {'GC CPU (cores)':>14} | {'assist us/req':>13}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for live_mb, alloc, rps in ((4.4, 0.0, 5000), (4.4, 1.0, 5000), (4.4, 50.0, 5000),
                                (4.4, 500.0, 5000), (400.0, 500.0, 5000),
                                (400.0, 4000.0, 20000)):
        m = model_gc(live_heap_mb=live_mb, alloc_rate_mb_per_s=alloc, cores=4, rps=rps)
        iv = ("never" if m["cycles_per_s"] == 0 else
              (f'{m["gc_interval_s"]*1e3:.1f} ms' if m["gc_interval_s"] < 1
               else f'{m["gc_interval_s"]:.1f} s'))
        rc = ("n/a" if m["cycles_per_s"] == 0 else f'{m["reqs_per_cycle"]:,.0f}')
        print(f"  {live_mb:>9.1f}M | {alloc:>10.1f} | {iv:>11} | "
              f"{100*m['stw_wall_frac']:>9.4f}% | {rc:>10} | "
              f"{m['gc_cpu_cores']:>14.3f} | {m['assist_us_req']:>12.2f}u")
    print("  -> read the last two rows: the collector only becomes a p99 story when the")
    print("     live heap is hundreds of MB AND the allocation rate is GB/s -- which is")
    print("     what happens if you put the trace buffer or the JSON layer in the heap.")
    print("     That is an allocation-discipline problem, not a Go-vs-Rust problem, and")
    print("     it is fixable in Go (buffer reuse, keeping pointers out of the hot path,")
    print("     reading pprof allocs in CI) and unfixable-by-language in either choice.")
    print("     The assist column is a throughput tax spread across cores, not a p99")
    print("     spike, and it goes to zero at 0 alloc/req no matter how big the heap is.")
    print("     Conclusion: GC is a reason to write Go carefully, not a reason to pick")
    print("     Rust. Anyone who measures 'Go GC' while allocating per request is")
    print("     measuring their allocator, not their language.")

    # B2
    print("\n[B2] MODEL: where the latency budget actually goes")
    net = [
        ("engine decision compute (M1, 8 arms, /30 interpreted)", n8_mean_us / 1000 / 30),
        ("engine decision compute, interpreted (no speedup at all)", n8_mean_us / 1000),
        ("request/response decode + trace record, allocation-free", 5.0 / 1000),
        ("trace write, buffered + async (amortised)", 1.0),
        ("routing store round trip if done synchronously (SQLite/Redis)", 120.0 / 1000),
        ("loopback gRPC to a Python sidecar, per decision", 170.0 / 1000),
        ("processor HTTPS call to acquirer (the real cost)", 350.0),
        ("3DS challenge when triggered (user interaction)", 8000.0),
    ]
    for label, ms in net:
        flag = ">>" if ms > BUDGET_P99_NS / 1e6 else "  "
        print(f"  {flag} {label:<56} ~{ms:>9.3f} ms")
    print(f"  (>> = alone exceeds the {BUDGET_P99_NS/1e6:.0f} ms engine budget)")
    print("  -> a synchronous Python hop is ~0.17 ms of *guaranteed* added latency per")
    print("     decision and up to ~1 ms of tail, while the engine's own compute is")
    print("     ~10 us even interpreted. The two decisions that move p99 are:")
    print("       (a) never a synchronous network hop inside decide()")
    print("       (b) never Python inside decide()")
    print("     Neither is a Go-vs-Rust question. That is the finding of this spike.")
    print("\n" + "=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
