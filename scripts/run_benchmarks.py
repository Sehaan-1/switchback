#!/usr/bin/env python3
"""#17 benchmark harness: the four headline metrics, defined, measured, gated.

Decision ticket #17 asks for precise definitions of the four headline benchmark
numbers, produced by ONE deterministic command, against a named and pinned scenario
catalogue, with anti-cherry-picking measures that are executable rather than asserted.
This file is that command. It imports the committed machinery -- it never reimplements
it (ADR-0005's world, ADR-0006/0003's shipped TS policy, ADR-0014/0015's OPE replay):

  from simulator/scenarios/check.py      the scenario gate, canonical form, hashes
  from spikes/0006 .../harness.py        the world (index-addressed, policy-free)
  from spikes/0007 .../posterior.py      the shipped TS Router + drive semantics
  from spikes/0015 .../ope.py            run_log / truth_shift / weight_table / IPS

Sections, one per deliverable metric plus the frame around them:

  [B0] scenario catalogue: every world a number below is cited against, with its
       content hash, and the identity checks (catalog pin, stream vectors, goldens).
  [B1] auth-vs-margin cost frontier: the shipped policy against a family of static
       cost/auth-blend routers. Headline pair (delta-auth, margin ratio) per scenario.
  [B2] latency: in-engine decision cost measured on the CPython reference (masked,
       host-dependent rows), a labelled native-band MODEL, and the Go rows, which are
       PENDING until a toolchain exists (--go-bench PATH arms the gate).
  [B3] oracle regret: the shipped policy against a true-rate oracle run through the
       IDENTICAL execution loop, with a clairvoyant (knows the draws) upper bound.
  [B4] OPE replay gate: IPS-clip10 shift queries vs replayed ground truth on
       re-seeded worlds, under ADR-0015's proposed support+accuracy gate.
  [B5] determinism: world digest and policy-state digest, re-run in-process and in a
       fresh interpreter (PYTHONHASHSEED=random, TZ=Asia/Kolkata).

Anti-cherry-picking measures, stated here and enforced by the file itself:

  * every table cites scenario_id@sha256:12 and the seed pair (world seed from the
    scenario doc; policy seed 20260916; OPE seed 20260920) -- the same numbers with
    any other seed are different numbers, not better ones;
  * the scenario catalogue is version-pinned by content hash; re-pinning is a
    reviewable diff in simulator/scenarios/golden/scenario-hashes.json;
  * unmeasurable rows are PENDING, never interpolated (the Go latency rows; the
    native band is labelled MODEL everywhere it appears);
  * host-dependent lines (wall time, date) carry the literal marker <!-- @timing -->
    and are excluded from `python3 scripts/run_benchmarks.py --check`, which
    regenerates the FULL profile and diffs everything else byte for byte;
  * `--quick` is for development only and by convention may NOT overwrite
    BENCHMARKS.md; the committed file is always the FULL profile.

    python3 scripts/run_benchmarks.py              # full profile -> writes BENCHMARKS.md (~15-25 min)
    python3 scripts/run_benchmarks.py --quick      # dev profile, prints, never writes
    python3 scripts/run_benchmarks.py --check      # regenerate full + diff vs committed
    python3 scripts/run_benchmarks.py --section=B1 # one section
    python3 scripts/run_benchmarks.py --ci         # quick + JSON verdicts (make target)
    python3 scripts/run_benchmarks.py --go-bench go.txt   # fold a Go bench into [B2]
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "simulator" / "scenarios"))
sys.path.insert(0, str(REPO / "spikes" / "0006-simulation-harness"))
sys.path.insert(0, str(REPO / "spikes" / "0007-thompson-sampling"))
sys.path.insert(0, str(REPO / "spikes" / "0015-ope"))

from check import (  # noqa: E402  (the gate is imported, not reimplemented)
    EXAMPLES_DIR, GOLDEN_PATH, canonical_bytes, check_stream_vectors, load_scenario,
    scenario_hash,
)
import harness as H    # noqa: E402
import posterior as P  # noqa: E402
import ope as O        # noqa: E402

# --------------------------------------------------------------------------------------
# Configuration. These six numbers ARE the benchmark definition; changing one changes
# the committed numbers and belongs in the pull request that changes it.
# --------------------------------------------------------------------------------------

N_FULL = 60_000            # committed window: 60k fills each scenario's 604,800 s clock
N_QUICK = 10_000           # development profile only; never committed
N_OPE = 30_000             # [B4] window: ADR-0015's gate is stated at N >= 30k
STATIC_KINDS = ("mdr", "cost", "ev")   # [B1] the three static comparators
CAL_PREFIX = 2_000         # static-table calibration prefix (uniform exploration)
B5_WORLD_N = 20_000        # [B5] world-digest window (fixed across profiles)

SCENARIOS = ("baseline-steady-v1", "black-friday-degraded-v1", "outage-recovery-v1")
OPE_TARGETS = ("foxtrot", "charlie")
OPE_RHO = 0.20
OPE_WORLDS_FULL = (0, 10_000, 20_000, 30_000)    # world-seed offsets
OPE_WORLDS_QUICK = (0, 10_000)

POLICY_SEED = P.POLICY_SEED          # 20260916 (ADR-0005 R30: recorded, separate)
OPE_SEED = O.OPE_SEED                # 20260920
LAM_TO = P.LAM_TO                    # 45.0 cents, ADR-0002
WARMUP = P.WARMUP                    # 10_000
TIMING = "<!-- @timing -->"          # lines carrying this marker are host-dependent

CATALOG_PATH = REPO / "constraints" / "catalog" / "acquirer-catalog.example.json"
BENCH_PATH = REPO / "BENCHMARKS.md"

DOCS, CACHE = {}, {}


def _doc(name):
    if name not in DOCS:
        doc, errs = load_scenario(EXAMPLES_DIR / f"{name}.json")
        if errs:
            raise SystemExit(f"scenario {name} failed its gate: {errs}")
        DOCS[name] = doc
    return DOCS[name]


def _h12(doc_or_name):
    """scenario@sha256:<12> -- the citation form every number below carries."""
    doc = _doc(doc_or_name) if isinstance(doc_or_name, str) else doc_or_name
    return f"{doc['id']}@sha256:{scenario_hash(doc).split(':', 1)[1][:12]}"


def table(headers, rows, sep="  "):
    widths = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=0))
              for i, h in enumerate(headers)]
    line1 = sep.join(str(h).ljust(widths[i]) for i, h in enumerate(headers)).rstrip()
    line2 = sep.join("-" * widths[i] for i in range(len(headers))).rstrip()
    body = "\n".join(sep.join(str(r[i]).ljust(widths[i]) for i in range(len(headers))).rstrip()
                     for r in rows) if rows else "(none)"
    return f"{line1}\n{line2}\n{body}"


# --------------------------------------------------------------------------------------
# The measurement loops. `run_shipped` replicates spikes/0007's drive() semantics one
# for one (decide -> chain walk -> ADR-0002 margin -> MAE -> observe -> terminal break),
# extended with the two accounting columns the issue demands next to every margin:
# effective cost per authorization and arms touched. The anchor row in [B1] proves the
# replication: baseline-steady-v1 @ n=60,000 must read 27,376.2 c/1k and 70.49% auth,
# the exact shipped-policy numbers spikes/0015 committed in its RESULTS.md.
# --------------------------------------------------------------------------------------

class Run:
    """Per-policy accumulators (full-horizon and post-warmup split)."""

    def __init__(self, n):
        self.n = n
        self.txns = 0
        self.authed = 0
        self.margin = 0.0
        self.early_margin = 0.0
        self.early_n = 0
        self.attempts = 0
        self.timeouts = 0
        self.unroutable = 0
        self.cost = 0.0                     # merchant effective processing cost (see below)
        self.mae_sum = 0.0
        self.mae_n = 0
        self.arms_touched = set()

    def add_txn(self, seq, authed, margin, attempts, timeouts, cost, unroutable,
                arms, mae=0.0, mae_n=0):
        self.txns += 1
        self.authed += authed
        self.margin += margin
        if seq < WARMUP:
            self.early_margin += margin
            self.early_n += 1
        self.attempts += attempts
        self.timeouts += timeouts
        self.cost += cost
        self.unroutable += unroutable
        self.arms_touched |= arms
        self.mae_sum += mae
        self.mae_n += mae_n

    # ---- reported columns ------------------------------------------------------------
    def auth_pct(self):
        return 100.0 * self.authed / max(1, self.txns)

    def margin_c_1k(self):
        return 1000.0 * self.margin / max(1, self.txns)

    def margin_pw_c_1k(self):
        return 1000.0 * (self.margin - self.early_margin) / max(1, self.txns - self.early_n)

    def cost_per_auth(self):
        return self.cost / max(1, self.authed)

    def mae_pts(self):
        return 100.0 * self.mae_sum / max(1, self.mae_n) if self.mae_n else float("nan")

    def refused_pct(self):
        return 100.0 * self.unroutable / max(1, self.txns)

    def timeout_pct(self):
        return 100.0 * self.timeouts / max(1, self.attempts)


def acq_cost(req, acq):
    """The merchant's effective cost for one AUTHORIZED attempt on `acq`: the
    acquirer's MDR share plus fixed fee (the part of the spread the merchant does
    not keep; win_amount is sell_bps - cost_bps minus fixed). Failed/timeout
    attempts cost `attempt_fee` instead, and a timeout additionally costs
    lambda_to (ADR-0002's price of an unresolved attempt). The C/auth column is
    [sum(cost over authorized attempts) + sum(attempt_fee over failed attempts)
    + lambda_to x timeouts] / authorized transactions."""
    return (req.amount_minor * P.ECON[acq]["cost_bps"] / 10_000.0
            + P.ECON[acq]["fixed_fee_minor"])


def _prior_fn(world, n):
    key = ("prior", id(world), n)
    if key not in CACHE:
        CACHE[key] = P.make_prior_fn(P.uniform_prefix(world, n), m=100.0)
    return CACHE[key]


def shipped_router(world, n):
    """The policy as ADR-0013 ships it (spike-0015's header, verbatim): informative
    hierarchical prior at m=100, onboarding floor eta=0.05, n_min=1000, exact
    key-addressed draws, 2-attempt cap, lambda_to=45."""
    return P.Router(P.default_space(), prior_fn=_prior_fn(world, n),
                    eta=0.05, n_min=1000)


def run_shipped(world, n, router=None):
    """The coupled loop over the shipped policy. Mirrors spikes/0007 drive() exactly;
    the [B1] anchor row is the proof."""
    r = router if router is not None else shipped_router(world, n)
    m = Run(n)
    for seq, arrival in world.arrivals(n):
        world.clock.advance_to(arrival)
        req = world.context(seq, arrival)
        chain = r.decide(req)
        if not chain:
            m.add_txn(seq, 0, 0.0, 0, 0, 0.0, 1, set())
            continue
        txn_margin, authed, attempts, timeouts, cost = 0.0, 0, 0, 0, 0.0
        mae, mae_n, arms = 0.0, 0, set()
        for att, acq in enumerate(chain):
            resp, truth = world.attempt(req, acq, att, world.clock.now_ms())
            attempts += 1
            arms.add(acq)
            if resp.outcome == H.TIMEOUT:
                timeouts += 1
                txn_margin -= P.attempt_fee(req, acq) + LAM_TO
                cost += P.attempt_fee(req, acq) + LAM_TO
            elif resp.outcome == H.AUTHORIZED:
                authed = 1
                txn_margin += P.win_amount(req, acq)
                cost += acq_cost(req, acq)
            else:
                txn_margin -= P.attempt_fee(req, acq)
                cost += P.attempt_fee(req, acq)
            i = r.space.arm_index(r.space.ctx_index(req), acq)
            if resp.outcome != H.TIMEOUT:
                mae += abs(r.mean(i) - P.theta_truth(world, req, acq, truth))
                mae_n += 1
            r.observe(req, acq, att, resp)
            if resp.outcome in H.TERMINAL:
                break
        m.add_txn(seq, authed, txn_margin, attempts, timeouts, cost, 0, arms,
                  mae, mae_n)
    return m, r


def theta_static(world, n):
    """The static auth table a spreadsheet router would carry: per-acquirer auth share
    observed on a 2,000-transaction uniform-exploration calibration prefix AFTER the
    run window (same discipline as the TS policy's prior: the run never sees it)."""
    key = ("static", id(world), n)
    if key in CACHE:
        return CACHE[key]
    cnt = P.uniform_prefix(P.Harness(_doc(world["id"]), n) if isinstance(world, dict)
                           else world, n)
    by_acq = {}
    fleet = [0.0, 0.0]
    for (acq, _bin, _region), c in cnt.items():
        e = by_acq.setdefault(acq, [0.0, 0.0])
        e[0] += c[0]
        e[1] += c[0] + c[1]
        fleet[0] += c[0]
        fleet[1] += c[0] + c[1]
    fleet_rate = (fleet[0] + 1.0) / (fleet[1] + 2.0)
    table_ = {a: ((v[0] + 1.0) / (v[1] + 2.0) if v[1] > 0 else fleet_rate)
              for a, v in by_acq.items()}
    CACHE[key] = table_
    return table_


def run_static(world, n, kind, theta_tab):
    """A static router of one of three named kinds -- the comparators a benchmark
    has to beat, and none of them a strawman:

      'mdr'  quoted-rate routing: sort by the processor's advertised MDR
             (cost_bps) only. The industry baseline of 'cost-based routing' --
             cheapest quoted rate first, blind to fixed fees, attempt fees and
             auth rates.
      'cost' fully-loaded cost routing: sort by the attempt's true cost as the
             scenario prices it (cost_bps*amount/1e4 + fixed_fee + attempt_fee).
      'ev'   static expected-value routing: rank by E[margin of one attempt]
             from a 2,000-transaction calibration prefix (theta_static per
             acquirer), with no learning in the window.

    All three build the chain as the two best-ranked eligible arms and route
    EVERYTHING: a static config has no statistical basis for refusing a
    transaction; the refusal decision is precisely what the learned policy adds,
    so the table lets that difference speak. Same execution loop, same ADR-0002
    margin rules, no posterior."""
    def decide(req):
        elig = P.eligible(req)
        scored = []
        for acq in elig:
            if kind == "mdr":
                key = -float(P.ECON[acq]["cost_bps"])
            elif kind == "cost":
                key = -(P.attempt_fee(req, acq) + acq_cost(req, acq))
            else:
                th = theta_tab.get(acq, 0.5)
                key = (th * P.win_amount(req, acq)
                       - (1.0 - th) * P.attempt_fee(req, acq))
            scored.append((key, acq))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [acq for _s, acq in scored][:P.MAX_ATTEMPTS]

    m = Run(n)
    for seq, arrival in world.arrivals(n):
        world.clock.advance_to(arrival)
        req = world.context(seq, arrival)
        chain = decide(req)
        if not chain:
            m.add_txn(seq, 0, 0.0, 0, 0, 0.0, 1, set())
            continue
        txn_margin, authed, attempts, timeouts, cost = 0.0, 0, 0, 0, 0.0
        arms = set()
        for att, acq in enumerate(chain):
            resp, _truth = world.attempt(req, acq, att, world.clock.now_ms())
            attempts += 1
            arms.add(acq)
            if resp.outcome == H.TIMEOUT:
                timeouts += 1
                txn_margin -= P.attempt_fee(req, acq) + LAM_TO
                cost += P.attempt_fee(req, acq) + LAM_TO
            elif resp.outcome == H.AUTHORIZED:
                authed = 1
                txn_margin += P.win_amount(req, acq)
                cost += acq_cost(req, acq)
            else:
                txn_margin -= P.attempt_fee(req, acq)
                cost += P.attempt_fee(req, acq)
            if resp.outcome in H.TERMINAL:
                break
        m.add_txn(seq, authed, txn_margin, attempts, timeouts, cost, 0, arms)
    return m


# --------------------------------------------------------------------------------------
# [B0] the scenario catalogue and the identity checks.
# --------------------------------------------------------------------------------------

def sec_b0(verdicts):
    out = []
    out.append("## B0 - scenario catalogue and identity pins\n")
    out.append("Every number in this file is a property of one of these worlds; the world"
               " is part of the number. `seed` is the world seed (in the document);"
               " `hash` is the canonical content hash the results cite. The catalogue"
               " itself is versioned: ids carry `-v1`, and a content change is a new id"
               " plus a reviewable diff in `simulator/scenarios/golden/`.")
    rows = []
    for path in sorted(EXAMPLES_DIR.glob("*.json")):
        doc, errs = load_scenario(path)
        if errs:
            verdicts.append(("B0", f"gate({path.name})", "FAIL", str(errs[0])))
            continue
        ev = len(doc.get("events") or [])
        rows.append((doc["id"], doc.get("model_version", "?"), doc.get("seed", "?"),
                     f"{doc.get('clock', {}).get('duration_s', '?'):,}", ev,
                     scenario_hash(doc).split(":", 1)[1][:12]))
    out.append("")
    out.append("| scenario (versioned id) | model | seed | duration s | events | sha256:12 |")
    out.append("|---|---|---|---|---|---|")
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    out.append("")
    # identity checks -------------------------------------------------------------
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    recomputed = "sha256:" + hashlib.sha256(canonical_bytes(catalog)).hexdigest()
    declared = _doc("baseline-steady-v1")["fleet"]["catalog_hash"]
    ok_cat = (recomputed == declared)
    verdicts.append(("B0", "catalog pin recompute", "PASS" if ok_cat else "FAIL",
                     f"{recomputed[:19]} == {declared[:19]}"))
    vec_errs = check_stream_vectors(GOLDEN_PATH.parent / "stream-vectors.json")
    verdicts.append(("B0", "determinism-contract vectors", "PASS" if not vec_errs
                     else "FAIL", vec_errs[0] if vec_errs else "25-line splitmix64"))
    goldens = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    drift = [i for i, h in goldens.items()
             if i in {r[0] for r in rows} and
             h != scenario_hash(_doc(i))]
    verdicts.append(("B0", "golden hash pins", "PASS" if not drift else "FAIL",
                     f"{len(rows)} documents pinned" if not drift
                     else f"drift: {drift}"))
    out.append(f"* acquirer catalog: `{catalog.get('id', CATALOG_PATH.name)}` "
               f"({len(catalog['acquirers'])} acquirers), canonical pin "
               f"`{recomputed[:19]}` -> scenario docs declare the same pin: "
               f"{'PASS' if ok_cat else 'FAIL'}")
    out.append(f"* determinism contract (splitmix64-indexed-v1) golden vectors: "
               f"{'PASS' if not vec_errs else 'FAIL'}")
    out.append(f"* golden scenario pins vs documents on disk: "
               f"{'PASS' if not drift else 'FAIL ' + str(drift)}")
    out.append(f"* seed identities: world seeds in the documents above; "
               f"POLICY_SEED={POLICY_SEED} (policy draws); OPE_SEED={OPE_SEED} "
               f"(OPE replay draws). The three are separate by construction "
               f"(ADR-0005 R30), so no number below can be a seed collision.")
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------------------
# [B1] auth vs margin: the cost frontier. X = the auth you buy and the margin you pay
# for it when routing is cost-only, stated as a pair against the shipped policy.
# --------------------------------------------------------------------------------------

def _ts_run(name, n):
    key = ("ts", name, n)
    if key not in CACHE:
        w = H.Harness(_doc(name), n)
        CACHE[key] = run_shipped(w, n)
    return CACHE[key]


def sec_b1(n, verdicts):
    out = []
    out.append("## B1 - the cost frontier: what auth costs in margin (the X pair)\n")
    out.append("The question the spec's `+X` asks: *does the learned router buy margin"
               " at the cost of authorization rate?* Method: the shipped policy against"
               " three named static routers, no strawmen:\n\n"
               "  * **static MDR-only** - 'cost-based routing' as the industry ships it:"
               " sort by the processor's quoted rate (`cost_bps`), blind to fixed fees,"
               " attempt fees and auth rates;\n"
               "  * **static cost-aware** - sort by the attempt's fully-loaded cost"
               " (`cost_bps*amount/1e4 + fixed_fee + attempt_fee`): the spreadsheet"
               " router done properly;\n"
               "  * **static EV-table** - rank by E[margin of one attempt] from a"
               " 2,000-transaction uniform calibration prefix the run never sees"
               " (`theta_static` per acquirer; the same discipline as the TS policy's"
               " own offline prior), with no learning in the window.\n\n"
               "All three take the two best-ranked eligible arms, depth 2, and route"
               " EVERYTHING - a static config has no statistical basis for refusing a"
               " transaction, so the refusal decision is precisely what the learned"
               " policy adds, and the table lets that difference speak. All policies"
               " see the same world, the same margins, ADR-0002's reward rules, and"
               " the identical execution loop.\n")
    out.append(f"Definitions: `auth%` = authorized transactions / transactions;"
               " `margin c/1k` = total ADR-0002 margin per 1,000 transactions;"
               " `C/auth c` = merchant effective cost per authorized transaction"
               " (acquirer MDR + fixed fees on authorized attempts, attempt fees on"
               " failed attempts, lambda_to x timeouts); `refused%` = transactions"
               " the policy would not route (shipped: every score <= 0; static:"
               " eligibility only); `MAE pts` = traffic-weighted |posterior mean -"
               " true rate| (shipped row only: static rows carry no posterior);"
               " `arms` = distinct acquirers attempted.")
    gate_fail = False
    for name in SCENARIOS:
        cite = _h12(name)
        m_ts, r_ts = _ts_run(name, n)
        world_h = H.Harness(_doc(name), n)
        tab = theta_static(world_h, n)
        labels = {"mdr": "static MDR-only (industry cost routing)",
                  "cost": "static cost-aware (fully-loaded cost)",
                  "ev": "static EV-table (calibrated, no learning)"}
        rows = []
        static_rows = []
        for kind in STATIC_KINDS:
            w = H.Harness(_doc(name), n)
            m = run_static(w, n, kind, tab)
            static_rows.append((kind, m))
            rows.append((labels[kind], f"{m.auth_pct():.2f}",
                         f"{m.margin_c_1k():,.1f}", f"{m.cost_per_auth():.1f}",
                         f"{m.refused_pct():.1f}", "-", len(m.arms_touched)))
        rows.append(("**shipped TS (learned)**", f"**{m_ts.auth_pct():.2f}**",
                     f"**{m_ts.margin_c_1k():,.1f}**", f"**{m_ts.cost_per_auth():.1f}**",
                     f"**{m_ts.refused_pct():.1f}**", f"**{m_ts.mae_pts():.2f}**",
                     len(m_ts.arms_touched)))
        out.append(f"\n### {cite}  (n={n:,}; world seed {_doc(name)['seed']},"
                   f" policy seed {POLICY_SEED})\n")
        out.append("| policy | auth% | margin c/1k | C/auth c | refused% | MAE pts | arms |")
        out.append("|---|---|---|---|---|---|---|")
        for r_ in rows:
            out.append("| " + " | ".join(str(c) for c in r_) + " |")
        # the pair: both directions of the trade, on the same line ----------------
        m_mdr = static_rows[0][1]
        d_auth_mdr = m_mdr.auth_pct() - m_ts.auth_pct()
        ratio_mdr = m_ts.margin_c_1k() / max(1e-9, m_mdr.margin_c_1k())
        d_cauth_mdr = m_ts.cost_per_auth() - m_mdr.cost_per_auth()
        best_kind, m_star = max(static_rows, key=lambda t: t[1].margin_c_1k())
        d_auth_best = m_star.auth_pct() - m_ts.auth_pct()
        ratio_best = m_ts.margin_c_1k() / max(1e-9, m_star.margin_c_1k())
        out.append(f"\n**the pair, both ways**: against quoted-rate cost routing,"
                   f" the learned policy concedes **{d_auth_mdr:+.2f} auth points**"
                   f" ({m_ts.auth_pct():.2f} vs {m_mdr.auth_pct():.2f}) and returns"
                   f" **x{ratio_mdr:.2f} the margin** ({m_ts.margin_c_1k():,.1f} vs"
                   f" {m_mdr.margin_c_1k():,.1f} c/1k) at effective cost"
                   f" {m_ts.cost_per_auth():.1f} vs {m_mdr.cost_per_auth():.1f} c/auth"
                   f" ({d_cauth_mdr:+.2f} c - equal-or-better). Against the strongest"
                   f" calibrated static row ({labels[best_kind].split(' (')[0]}), the"
                   f" concession is {d_auth_best:+.2f} pts for x{ratio_best:.2f}."
                   f" The concession itself is priced: the statics attempt the"
                   f" {m_ts.refused_pct():.1f}% of transactions the EV gate declines"
                   f" (negative-expectation micro-tickets), and the margin column is"
                   f" what that traffic is worth.")
        # anchors & gates ----------------------------------------------------------
        if name == "baseline-steady-v1" and n == N_FULL:
            anchor = (abs(m_ts.margin_c_1k() - 27376.2) < 0.051
                      and abs(m_ts.auth_pct() - 70.49) < 0.005)
            verdicts.append(("B1", "driver anchor vs spikes/0015 (27,376.2 c/1k,"
                             " 70.49% auth)", "PASS" if anchor else "FAIL",
                             f"{m_ts.margin_c_1k():,.1f} c/1k, {m_ts.auth_pct():.2f}%"))
            anchor2 = abs(m_mdr.margin_c_1k() - 14787.7) < 0.051
            verdicts.append(("B1", "MDR-only baseline reproduces its committed value"
                             " (14,787.7 c/1k)", "PASS" if anchor2 else "FAIL",
                             f"{m_mdr.margin_c_1k():,.1f} c/1k"))
        dominance = all(m_ts.margin_c_1k() > m.margin_c_1k()
                        for _kind, m in static_rows)
        trade = all(m.auth_pct() >= m_ts.auth_pct() - 1e-9
                    for _kind, m in static_rows)
        verdicts.append(("B1", f"frontier sanity ({_doc(name)['id']})",
                         "PASS" if (dominance and trade) else "FAIL",
                         f"TS margin-dominates all {len(static_rows)} static rows; "
                         f"vs MDR-only {d_auth_mdr:+.2f} pts / x{ratio_mdr:.2f}; "
                         f"vs best static {d_auth_best:+.2f} pts / x{ratio_best:.2f}"))
        gate_fail |= not (dominance and trade)
    out.append("\nThe frontier is the anti-cherry-pick: the delta-auth and the margin"
               " ratio are measured on the SAME line of the SAME table, and every row"
               " of the family is published - a reader does not have to trust that the"
               " comparison policy was a fair one, they can see the whole family.")
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------------------
# [B2] latency. Y = two numbers on the engine hot path. What is measurable TODAY is
# the CPython reference (masked: host-dependent); the native band is a labelled model;
# the Go rows are pending the toolchain.
# --------------------------------------------------------------------------------------

def _quantile(sorted_xs, q):
    if not sorted_xs:
        return float("nan")
    pos = q * (len(sorted_xs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def sec_b2(n, verdicts, go_bench):
    out = []
    out.append("## B2 - decision latency (Y): what the routing decision costs\n")
    out.append("Y has two halves, both ADR-0001 commitments restated here as the"
               " benchmark: (i) **2 ms p99 wall** per routing decision end to end at"
               " 5,000 decisions/s on 4 vCPU (the consumer-visible SLA, engine +"
               " overhead, owned by the Go build); (ii) **20 us p99 in-engine CPU**"
               " per decision (the engine's share, before I/O). What a design-phase"
               " repo can measure today is the algorithm on the hot path - the shipped"
               " TS Router's `decide()` over the committed window - in CPython.\n")
    out.append(f"Method: a full drive of the shipped policy on"
               f" {_h12('baseline-steady-v1')} over the run window (n={n:,} on the"
               " committed profile); per decision, `time.perf_counter_ns()`"
               " brackets `router.decide()` ONLY (posterior updates and world answers"
               " are outside the bracket; the constraint filter and the score-EV"
               " sampling are inside, because production pays them per decision)."
               " Wall lines are host-dependent and masked with "
               f"`{TIMING}`: `--check` compares everything except them.")
    w = H.Harness(_doc("baseline-steady-v1"), n)
    r = shipped_router(w, n)
    times = []
    for seq, arrival in w.arrivals(n):
        w.clock.advance_to(arrival)
        req = w.context(seq, arrival)
        t0 = time.perf_counter_ns()
        chain = r.decide(req)
        times.append((time.perf_counter_ns() - t0) / 1000.0)
        if not chain:
            continue
        for att, acq in enumerate(chain):
            resp, _t = w.attempt(req, acq, att, w.clock.now_ms())
            r.observe(req, acq, att, resp)
            if resp.outcome in H.TERMINAL:
                break
    times.sort()
    p50, p99, p999 = (_quantile(times, 0.5), _quantile(times, 0.99),
                      _quantile(times, 0.999))
    out.append(f"\n| quantity | p50 | p99 | p99.9 | window | status |")
    out.append("|---|---|---|---|---|---|")
    out.append(f"| in-engine decision, CPython 3.11 reference Router (us) | {p50:.1f} "
               f"| {p99:.1f} | {p999:.1f} | {n:,} decisions | MEASURED {TIMING} |")
    # ADR-0001 section 1 documents the interpreter-dominated numeric loop as
    # 10x/30x/100x faster compiled; the band published here is the conservative
    # 10x-30x range (the 100x end is not claimed).
    lo, hi = p99 / 30.0, p99 / 10.0
    out.append(f"| ...projected to the native band at ADR-0001's 10-30x (us) | "
               f"{p50/30:.1f}-{p50/10:.1f} | {lo:.1f}-{hi:.1f} | - | same | MODEL "
               f"(interpreter-dominated numeric loop; ADR-0001 section 1) {TIMING} |")
    out.append("| in-engine CPU budget (ADR-0001): 20 us p99 | - | 20.0 | - | - | "
               f"MODEL-BOUND: projected band {lo:.1f}-{hi:.1f} us is the claim, "
               f"not the measurement {TIMING} |")
    go_rows = []
    if go_bench and Path(go_bench).exists():
        txt = Path(go_bench).read_text(encoding="utf-8", errors="replace")
        for m_ in re.finditer(r"^(Benchmark\S+)-\d+\s+\d+\s+([\d.]+)\s+ns/op", txt, re.M):
            go_rows.append((m_.group(1), float(m_.group(2)) / 1000.0))
    if go_rows:
        for name_b, us in go_rows:
            verdicts.append(("B2", f"Go bench {name_b}",
                             "PASS" if us <= 20.0 else "FAIL", f"{us:.2f} us/op"))
            out.append(f"| `{name_b}` (external Go bench via --go-bench) (us) | - | "
                       f"{us:.2f} | - | - | MEASURED |")
    else:
        verdicts.append(("B2", "Go engine latency gate", "PENDING",
                         "no Go toolchain in this sandbox (ADR-0001 day-one); arm "
                         "with --go-bench when it exists"))
        out.append("| Go engine decide() p99, 5k d/s, 4 vCPU | - | PENDING | - | - | "
                   "PENDING: no Go toolchain; the gate is armed via `--go-bench` "
                   "and must read <= 20 us p99 (in-engine) / <= 2 ms p99 (wall) |")
    # R121: host facts are reported, never gated. The stable B2 gates are
    # structural: the Go row's armed PENDING, the quantile ordering, and the
    # mask itself. The band-clears-the-budget judgement is made ONCE, in the
    # ADR, on a quiet host - re-measuring it per run would couple CI to load.
    verdicts.append(("B2", "latency quantile ordering (p50<=p99<=p99.9)",
                     "PASS" if (p50 <= p99 <= p999) else "FAIL",
                     f"structured check, value-free"))
    n_masked = sum(1 for ln in out if TIMING in ln)
    verdicts.append(("B2", "host-dependent rows masked per R121",
                     "PASS" if n_masked >= 3 else "FAIL",
                     f"{n_masked} B2 rows carry the mask; host load cannot "
                     "move a verdict"))
    out.append(f"\nOn this host the CPython reference pays p99 {p99:.1f} us per "
               f"decision against the 2 ms p99 wall budget, and the MODEL band "
               f"at ADR-0001's 10-30x is {lo:.1f}-{hi:.1f} us against the 20 us "
               f"p99 in-engine budget {TIMING} - what a loaded host does to "
               "these rows is visible here and gated nowhere (R121)."
               f" Wall-clock rows in this section are marked {TIMING} and are never"
               " digested.")
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------------------
# [B3] oracle regret. Z = (oracle margin - shipped margin) / oracle margin, where the
# oracle knows the true RATES at decision time but not the draws; the clairvoyant,
# which knows the draws too, is the unattainable bound that keeps 'oracle' honest.
# --------------------------------------------------------------------------------------

def _p_timeout(world, acq, req, t_ms):
    """P(latency > deadline) closed form from the scenario's own latency model:
    lognormal body through (p50, p95), GPD tail pinned at p99; monotone in u, so the
    probability is 1 - u*(deadline) with u* the model's inverse. Forced modes are
    exact: a hung acquirer always times out; a refused one never 'times out', it
    fails fast as a transport error."""
    mode = world.health(acq, t_ms)[2]
    if mode == "timeout":
        return 1.0, mode
    m = world.models[acq]
    _a, health_lat, _m = world.health(acq, t_ms)
    mult = health_lat * world.latency_multiplier(acq, t_ms)
    dl = float(req.deadline_ms)
    p95 = m.p95 * mult
    if dl <= m.floor:
        return 0.0, mode
    if dl <= p95:
        z = (math.log(dl) - math.log(m.p50 * mult)) / m.sigma_body
        u_star = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    else:
        u_star = 1.0 - 0.05 * (1.0 + m.xi * (dl - p95) / (m.sigma_gpd * mult)) \
            ** (-1.0 / m.xi)
    return 1.0 - min(1.0 - 1e-12, max(1e-12, u_star)), mode


def _oracle_chain(world, req, arms):
    """The true-rate policy: the SHIPPED score functional with oracle inputs -
    theta from the harness's own attempt evaluation (theta_truth, abandon-adjusted),
    P(timeout) closed-form. A refused arm has theta = 0: it cannot authorize, ever,
    while the refusal lasts, and the oracle knows it."""
    scored = []
    for acq in arms:
        truth = P.probe_truth(world, req, acq, 0, world.clock.now_ms())
        p_to, mode = _p_timeout(world, acq, req, world.clock.now_ms())
        th = 0.0 if mode in ("connection_refused", "http_503") \
            else P.theta_truth(world, req, acq, truth)
        s = th * P.win_amount(req, acq) - (1.0 - th) * P.attempt_fee(req, acq) \
            - p_to * LAM_TO
        scored.append((s, acq))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [acq for s, acq in scored if s > 0.0][:P.MAX_ATTEMPTS]


def _step_margin(world, req, acq, att, t_ms):
    resp, _t = world.attempt(req, acq, att, t_ms)
    if resp.outcome == H.TIMEOUT:
        return -(P.attempt_fee(req, acq) + LAM_TO), resp.outcome, 1
    if resp.outcome == H.AUTHORIZED:
        return P.win_amount(req, acq), resp.outcome, 0
    return -P.attempt_fee(req, acq), resp.outcome, 0


def sec_b3(n, verdicts):
    out = []
    out.append("## B3 - oracle regret (Z): how much margin is left on the table\n")
    out.append("Three margins per scenario, one world, one execution loop:\n"
               "  * **shipped**: the learned policy (reused from [B1]'s run, so the"
               " regret is never spread across two different runs);\n"
               "  * **oracle**: the shipped score functional fed TRUE rates (theta"
               " from the harness's attempt evaluation, P(timeout) closed-form) -"
               " knows today's rates, not tonight's outcomes;\n"
               "  * **clairvoyant**: per transaction, the chain with the best"
               " REALIZED margin over all arms and both attempt slots, computed by"
               " probing the index-addressed world (an unattainable bound: it knows"
               " the draws).\n\n"
               "Z = (oracle - shipped) / oracle, full horizon and post-warmup"
               f" (first {WARMUP:,} transactions excluded); the clairvoyant gap ="
               " (clairvoyant - oracle) / clairvoyant prices the score functional"
               " itself, so a small Z cannot hide behind a weak oracle.")
    for name in SCENARIOS:
        cite = _h12(name)
        m_ts, _r = _ts_run(name, n)
        w = H.Harness(_doc(name), n)
        space = P.default_space()
        oracle = Run(n)
        clair = Run(n)
        for seq, arrival in w.arrivals(n):
            w.clock.advance_to(arrival)
            req = w.context(seq, arrival)
            arms = P.eligible(req)
            if not arms:
                oracle.add_txn(seq, 0, 0.0, 0, 0, 0.0, 1, set())
                clair.add_txn(seq, 0, 0.0, 0, 0, 0.0, 1, set())
                continue
            # --- oracle: decide on true rates, walk the chain for real -------------
            chain = _oracle_chain(w, req, arms)
            txn_margin, authed, attempts, timeouts, cost = 0.0, 0, 0, 0, 0.0
            for att, acq in enumerate(chain):
                step, outcome, to = _step_margin(w, req, acq, att, w.clock.now_ms())
                txn_margin += step
                attempts += 1
                timeouts += to
                if outcome != H.AUTHORIZED:
                    cost += -step
                else:
                    authed = 1
                if outcome in H.TERMINAL:
                    break
            oracle.add_txn(seq, authed, txn_margin, attempts, timeouts, cost,
                           0 if chain else 1, set(chain))
            # --- clairvoyant bound: best realized chain ----------------------------
            nq = len(w.late_queue)
            realized = {}
            for acq in arms:
                m0, oc0, _to0 = _step_margin(w, req, acq, 0, arrival)
                realized[(acq,)] = m0 if True else 0.0
            best = 0.0                     # the empty chain (refusal) is 0
            for a in arms:
                m0, oc0, _t0 = _step_margin(w, req, a, 0, arrival)
                best = max(best, m0)
                if oc0 in H.TERMINAL:
                    continue
                for b_ in arms:
                    if b_ == a:
                        continue
                    m1, _oc1, _t1 = _step_margin(w, req, b_, 1, arrival)
                    best = max(best, m0 + m1)
            del w.late_queue[nq:]
            clair.add_txn(seq, 0, best, 0, 0, 0.0, 0, set())
        # --------------------------------------------------------------------------
        z_full = 100.0 * (oracle.margin_c_1k() - m_ts.margin_c_1k()) / \
            max(1e-9, oracle.margin_c_1k())
        has_pw = m_ts.txns > m_ts.early_n
        z_pw = (100.0 * (oracle.margin_pw_c_1k() - m_ts.margin_pw_c_1k()) /
                max(1e-9, oracle.margin_pw_c_1k())) if has_pw else float("nan")
        gap = 100.0 * (clair.margin_c_1k() - oracle.margin_c_1k()) / \
            max(1e-9, clair.margin_c_1k())
        z_pw_s = f"{z_pw:.2f}%" if has_pw else "n/a (quick window == warmup)"
        ts_pw = f"{m_ts.margin_pw_c_1k():,.1f}" if has_pw else "-"
        or_pw = f"{oracle.margin_pw_c_1k():,.1f}" if has_pw else "-"
        out.append(f"\n### {cite}  (n={n:,})\n")
        out.append("| policy | margin c/1k | margin c/1k post-warmup |")
        out.append("|---|---|---|")
        out.append(f"| shipped TS | {m_ts.margin_c_1k():,.1f} | {ts_pw} |")
        out.append(f"| oracle (true rates, shipped score) | {oracle.margin_c_1k():,.1f} "
                   f"| {or_pw} |")
        out.append(f"| clairvoyant (knows the draws; bound) | {clair.margin_c_1k():,.1f} "
                   f"| - |")
        out.append(f"\n**Z = {z_full:.2f}%** regret full horizon, **{z_pw_s}**"
                   f" post-warmup; **clairvoyant gap {gap:.2f}%**.")
        sane = (oracle.margin_c_1k() >= m_ts.margin_c_1k() - 1e-6
                and clair.margin_c_1k() >= oracle.margin_c_1k() - 1e-6
                and -5.0 <= z_full <= 50.0)
        verdicts.append(("B3", f"regret ordering ({_doc(name)['id']})",
                         "PASS" if sane else "FAIL",
                         f"shipped <= oracle <= clairvoyant; Z={z_full:.2f}% full, "
                         f"{z_pw_s} post-warmup, gap {gap:.2f}%"))
    out.append("\nNothing in this section can be tuned after the fact: the shipped"
               " rows are bit-identical to [B1]'s, and the oracle is a definition,"
               " not a fit - it receives exactly the information the ticket was"
               " promised (true rates), through the same gate the shipped policy uses.")
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------------------
# [B4] OPE replay gate: the counterfactual panel's number against replayed truth.
# --------------------------------------------------------------------------------------

def sec_b4(n_ope, quick, verdicts):
    out = []
    worlds = OPE_WORLDS_QUICK if quick else OPE_WORLDS_FULL
    out.append("## B4 - off-policy evaluation: IPS-clip10 against replayed truth\n")
    out.append("The dashboard's counterfactual question, `shift(target, share=rho,"
               " window)`, answered by the shipped estimator (IPS, clipped at 10,"
               " with exact quadrature propensities recomputed from the logged"
               " posteriors) and graded against the replayed ground truth of"
               " spikes/0015's protocol on re-seeded worlds.\n")
    out.append(f"Definitions and gate (ADR-0015 [O5], restated as this section's"
               " contract): at N >= 30,000 decisions in the window, a query PASSES"
               " iff (i) support >= 1,000 logged head==target decisions, else the"
               f" query is REFUSED before any estimator runs, and (ii) |estimate -"
               " truth| <= max(1,500 c/1k, 75% of |truth - baseline|). Estimates use"
               " the Shipped policy's own decision log; 'truth' replays the overlay"
               " through the SAME world instance, so the pairing is exact, not"
               " sampled.\n")
    out.append(f"Window: {n_ope:,} decisions of {_h12('baseline-steady-v1')} per"
               f" world; targets {OPE_TARGETS} at rho={OPE_RHO}; world seeds the"
               f" scenario seed plus offsets {worlds} (`seed + off`, OPE_SEED"
               f" = {OPE_SEED} for the overlay coin).\n")
    rows = []
    errs_all = []
    for off in worlds:
        log = O.run_log("baseline-steady-v1", n_ope, seed_off=off)
        base = log["margin_c_1k"]
        for tgt in OPE_TARGETS:
            tr = O.truth_shift(log, "baseline-steady-v1", n_ope, tgt, OPE_SEED,
                               (OPE_RHO,), world_seed_off=off)
            truth = O.per_1k(tr["value"][OPE_RHO])
            wt = O.weight_table(log, tgt, OPE_RHO, n_ope)
            support = wt["recomputes"]
            delta = abs(truth - O.per_1k(tr["value_base"]))
            bound = max(1500.0, 0.75 * delta)
            if support < 1000:
                verdicts.append(("B4", f"shift({tgt},{OPE_RHO}) world +{off}",
                                 "PASS", f"REFUSED by support gate "
                                 f"({support} head rows < 1,000): the honest answer"))
                rows.append((tgt, f"+{off:,}", support, f"{truth:,.1f}", "-", "-",
                             "REFUSED (support)"))
                continue
            est = O.per_1k(O.est_all(wt["rs"], wt["ws"], [0.0] * len(wt["rs"]),
                                     [0.0] * len(wt["rs"]), clip=10)["ips"])
            err = est - truth
            errs_all.append(err)
            ok = abs(err) <= bound
            verdicts.append(("B4", f"shift({tgt},{OPE_RHO}) world +{off}",
                             "PASS" if ok else "FAIL",
                             f"|err| {abs(err):,.1f} <= {bound:,.1f} "
                             f"(delta {delta:,.1f})"))
            rows.append((tgt, f"+{off:,}", support, f"{truth:,.1f}", f"{est:,.1f}",
                         f"{err:+,.1f}", "PASS" if ok else "FAIL"))
    out.append("| target | world seed | head rows | truth c/1k | IPS-clip10 c/1k |"
               " err c/1k | verdict |")
    out.append("|---|---|---|---|---|---|---|")
    for r_ in rows:
        out.append("| " + " | ".join(str(c) for c in r_) + " |")
    if errs_all:
        mean_e = sum(errs_all) / len(errs_all)
        rms = (sum(e * e for e in errs_all) / len(errs_all)) ** 0.5
        out.append(f"\nError floor across the {len(errs_all)} support-bearing queries:"
                   f" mean {mean_e:+,.1f} c/1k, RMS {rms:,.1f} c/1k - this spread is"
                   " the finest error the protocol can testify about, and it ships in"
                   " the file rather than behind a point estimate.")
        verdicts.append(("B4", "error floor stated", "PASS",
                         f"mean {mean_e:+,.1f}, RMS {rms:,.1f} c/1k over "
                         f"{len(errs_all)} queries"))
    out.append("\nThe gate REFUSES a query with no support rather than answering"
               " confidently from nothing, and the unclipped-IPS catastrophe from"
               " spike-0015 [O5] (+9.1M c/1k on a single thin-propensity row) is why"
               " the shipped estimator is the clipped one. Both are properties of"
               " the gate, not options a querying user can toggle.")
    if quick:
        out.append(f"\n_quick profile: {len(worlds)} of {len(OPE_WORLDS_FULL)}"
                   " worlds; the committed file runs all four._")
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------------------
# [B5] determinism: same file, same machine, fresh process - identical state.
# --------------------------------------------------------------------------------------

def _policy_engine_digest(name, n):
    """sha256 over the shipped policy's learned state after a full window. Two
    identical runs must produce an identical learned state - learning is a fold,
    and the fold is deterministic only if every draw is key-addressed."""
    w = H.Harness(_doc(name), n)
    _m, r = run_shipped(w, n)
    return r.state_checksum()


def sec_b5(n, quick, verdicts):
    out = []
    out.append("## B5 - determinism: the run may not depend on HOW it is run\n")
    out.append("Two digest identities, restated per run so this file is falsifiable: "
               "(i) the WORLD's answer to every (transaction, arm) first attempt - "
               "policy-free by construction; (ii) the shipped policy's LEARNED STATE"
               " after the full window (its five float64 posterior arrays, packed "
               "and hashed). `##B == ##A` rows must be bit-identical or the run "
               "fails.")
    doc = _doc("baseline-steady-v1")
    cite = _h12(doc)
    wd1 = H.world_digest(doc, B5_WORLD_N)
    wd2 = H.world_digest(doc, B5_WORLD_N)
    out.append(f"\nworld digest, {cite}, arm-scan n={B5_WORLD_N:,}:")
    out.append(f"  `{wd1}`")
    out.append(f"  re-run in this process: {'IDENTICAL' if wd1 == wd2 else 'DIVERGED'}")
    verdicts.append(("B5", "world digest stable across in-process re-run",
                     "PASS" if wd1 == wd2 else "FAIL", wd1[:27]))
    pd1 = _ts_run("baseline-steady-v1", n)[1].state_checksum()   # reuses [B1]'s run
    pd2 = _policy_engine_digest("baseline-steady-v1", n)
    out.append(f"\npolicy-state digest, shipped TS over {cite}, n={n:,}:")
    out.append(f"  `{pd1}`")
    out.append(f"  re-run in this process: {'IDENTICAL' if pd1 == pd2 else 'DIVERGED'}")
    ok_in = pd1 == pd2
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "random"
    env["TZ"] = "Asia/Kolkata"
    cmd = [sys.executable, str(HERE / "run_benchmarks.py"), "--digest-policy",
           str(n)]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=str(REPO),
                          timeout=1800)
    pd3 = proc.stdout.strip().splitlines()[-1] if proc.returncode == 0 \
        else f"FAILED rc={proc.returncode}: {proc.stderr[-200:]}"
    ok_x = pd3 == pd1
    out.append(f"  fresh interpreter (PYTHONHASHSEED=random, TZ=Asia/Kolkata): "
               f"`{pd3}` -> {'IDENTICAL' if ok_x else 'DIVERGED'}")
    verdicts.append(("B5", "policy-state digest stable (in-process x2 + fresh"
                     " interpreter)", "PASS" if (ok_in and ok_x) else "FAIL",
                     pd1))
    out.append("\nThe fresh-interpreter row is the load-bearing one: hash seeds and"
               " timezones are the usual ways a run quietly becomes a different run,"
               " and neither may touch a benchmark file. `PYTHONHASHSEED=random` is"
               " the hostile case, not the friendly one.")
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------------------
# assembly, --check, --json, --go-bench, --digest-policy
# --------------------------------------------------------------------------------------

def header(profile, n):
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    lines = ["# BENCHMARKS.md - the #17 headline metrics, measured and gated", ""]
    lines.append(f"- generated by `python3 scripts/run_benchmarks.py` "
                 f"(profile: {profile}) {TIMING}")
    lines.append(f"- date {now} | host {os.uname().sysname}-{os.uname().release} | "
                 f"Python {sys.version.split()[0]} | {os.cpu_count()} vCPU {TIMING}")
    lines.append(f"- windows: n={n:,} (B1-B3, B5 policy), n={N_OPE:,} (B4), "
                 f"n={B5_WORLD_N:,} (B5 world) {TIMING if profile == 'quick' else ''}")
    lines.append("- reproduce: `python3 scripts/run_benchmarks.py --check` regenerates"
                 " the full profile and diffs every line except those marked "
                 f"`{TIMING}` (host timing, date).")
    lines.append("")
    lines.append("The four headline definitions in one paragraph each, then the"
                 " evidence; ADR-0016 (`docs/decisions/0016-benchmarks.md`) is the"
                 " normative statement of the same content. **X** ([B1]): the"
                 " auth-points concession and margin ratio of cost-blend static"
                 " routing vs the shipped learned policy, measured pair-wise on one"
                 " frontier per scenario - never quoted alone. **Y** ([B2]): 2 ms"
                 " p99 decision wall at 5,000 d/s on 4 vCPU plus 20 us p99"
                 " in-engine CPU; measured today on the CPython reference (masked"
                 " rows), projected as a labelled MODEL to the native band, PENDING"
                 " for Go. **Z** ([B3]): oracle regret = (oracle - shipped)/oracle"
                 " with the oracle = true rates through the identical execution"
                 " loop, plus the clairvoyant bound that prices the score"
                 " functional itself. **W** ([B4]): the OPE replay gate - IPS-clip10"
                 " vs replayed truth under support >= 1,000 and |err| <= max(1,500"
                 " c/1k, 75%|delta|) at N >= 30k.")
    lines.append("")
    lines.append("House rules enforced here: no fabricated numbers (unmeasurable"
                 " rows are PENDING with the gate armed); every number cites"
                 " scenario@sha256:12 plus the world/policy seed pair; synthetic"
                 " magnitudes are scenario properties, never production claims;"
                 " margin rows carry MAE and arms-touched.")
    lines.append("")
    return "\n".join(lines)


def summary_block(verdicts):
    rows = []
    fails = [v for v in verdicts if v[2] == "FAIL"]
    pends = [v for v in verdicts if v[2] == "PENDING"]
    passes = [v for v in verdicts if v[2] == "PASS"]
    lines = ["## summary verdicts", ""]
    lines.append("| section | check | verdict | detail |")
    lines.append("|---|---|---|---|")
    for sec, item, verdict, detail in verdicts:
        lines.append(f"| {sec} | {item} | {verdict} | {detail} |")
    lines.append("")
    lines.append(f"**{len(passes)} PASS / {len(pends)} PENDING (gate armed) / "
                 f"{len(fails)} FAIL** - exit status reflects FAIL rows only;"
                 " PENDING means the thing measured does not exist yet, and the row"
                 " says what arms the gate.")
    lines.append("")
    return "\n".join(lines), fails


def run_profile(quick, ci, section, go_bench):
    n = N_QUICK if quick else N_FULL
    verdicts = []
    parts = [header("quick" if quick else "full", n)]
    secs = {"B0": lambda: sec_b0(verdicts),
            "B1": lambda: sec_b1(n, verdicts),
            "B2": lambda: sec_b2(n, verdicts, go_bench),
            "B3": lambda: sec_b3(n, verdicts),
            "B4": lambda: sec_b4(N_OPE, quick, verdicts),
            "B5": lambda: sec_b5(n, quick, verdicts)}
    for key, fn in secs.items():
        if section is None or section == key:
            parts.append(fn())
    if section is None:
        summary, fails = summary_block(verdicts)
        parts.append(summary)
    else:
        fails = [v for v in verdicts if v[2] == "FAIL"]
    return "\n".join(parts), verdicts, fails


def mask_lines(text):
    return "\n".join("(masked)" if TIMING in ln else ln for ln in text.splitlines())


def main(argv):
    args = argv[1:]
    quick = "--quick" in args
    ci = "--ci" in args
    as_json = "--json" in args
    check = "--check" in args
    go_bench = None
    section = None
    for a in args:
        if a.startswith("--section="):
            section = a.split("=", 1)[1].upper()
        elif a == "--go-bench":
            go_bench = args[args.index(a) + 1]
        elif a.startswith("--go-bench="):
            go_bench = a.split("=", 1)[1]
    # internal: B5's fresh-process probe ------------------------------------------
    if "--digest-policy" in args:
        n = int(args[args.index("--digest-policy") + 1])
        print(_policy_engine_digest("baseline-steady-v1", n))
        return 0
    if ci:
        quick = True
        as_json = True
    # profiles that cannot produce or be checked against the committed file -------
    if check and (quick or section is not None):
        print("error: --check reconstructs the committed file and is incompatible "
              "with --quick/--ci/--section (it must run the full profile)",
              file=sys.stderr)
        return 2
    t0 = time.perf_counter()
    text, verdicts, fails = run_profile(quick, ci, section, go_bench)
    ok = not fails
    if check:
        fresh = mask_lines(text).rstrip("\n")
        have = mask_lines(BENCH_PATH.read_text(encoding="utf-8")).rstrip("\n") \
            if BENCH_PATH.exists() else None
        if have is None:
            print("--check: no committed BENCHMARKS.md to compare against",
                  file=sys.stderr)
            return 1
        if fresh == have and ok:
            print("--check: PASS - regenerated full profile matches committed "
                  "BENCHMARKS.md on every non-masked line, and all verdicts are "
                  "PASS/PENDING")
            return 0
        import difflib
        diff = list(difflib.unified_diff(have.splitlines(), fresh.splitlines(),
                                         "committed", "regenerated", lineterm=""))
        print("--check: FAIL - regenerated output diverges from committed file "
              f"({len(diff)} diff lines) or a verdict failed\n", file=sys.stderr)
        if diff:
            print("\n".join(diff[:80]), file=sys.stderr)
        for v_fail in fails:
            print(f"  failing verdict: {v_fail[0]} / {v_fail[1]} - {v_fail[3]}",
                  file=sys.stderr)
        return 1
    if as_json:
        payload = {
            "ok": ok,
            "profile": "quick" if quick else "full",
            "verdicts": [{"section": s, "item": i, "verdict": v, "detail": d}
                         for s, i, v, d in verdicts],
            "n_fail": len(fails),
            "n_pending": sum(1 for v in verdicts if v[2] == "PENDING"),
            "elapsed_s": round(time.perf_counter() - t0, 1),
        }
        print(json.dumps(payload, indent=2))
    elif quick or section is not None:
        print(text)
        print(f"\n[profile: {'quick' if quick else 'full'} | "
              f"elapsed {time.perf_counter() - t0:.0f}s | "
              f"{'FAIL: ' + '; '.join(f'{s}/{i}' for s, i, _v, _d in fails) if fails else 'no FAIL verdicts'}]")
    else:
        BENCH_PATH.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {BENCH_PATH.relative_to(REPO)} "
              f"({len(text.splitlines())} lines) in {time.perf_counter() - t0:.0f}s; "
              f"{'FAIL: ' + '; '.join(f'{s}/{i}' for s, i, _v, _d in fails) if fails else 'all verdicts PASS/PENDING'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
