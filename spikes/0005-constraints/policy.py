#!/usr/bin/env python3
"""Decision ticket #5 evidence: the constraint layer, compiled and evaluated.

What this does
--------------
It is not a second implementation of the engine and it is not a design document. It is a
working compiler+evaluator for the artifact the ADR proposes:
`constraints/schema/constraint-set.schema.json` and the two documents in
`constraints/examples/`. It loads them through the real CI checker
(`constraints/check.py`), compiles the closed rule catalog into evaluators, and measures
what the ticket asks about that can only be measured:

  [C1] the compiled shape of both documents: rules, scopes, relax ladder, pinned hashes;
  [M1] the hot-path cost of filtering and guarding a decision, against ADR-0001's budget;
  [M2] the audit record: fields, bytes, and the trace volume it implies;
  [M3] counters against an exact window in BOTH directions (a ring that is one bucket too
       small UNDER-counts, which is the direction scheme penalties are assessed on),
       saturation, and memory per live key;
  [F1] the conflict census, in five outcomes: how often a real ConstraintSet routes, routes
       only after a waiver, defers, is prohibited from trying, or has NO legal route at all -
       and which rules it blames;
  [F1b] the escape budget: what bounded relaxation buys (waivers) and what it cannot (the
       residual escalates rather than routing around a rule it was told to obey);
  [F2] "must-domestic-acquirer" read literally vs read as a region rule, and the residual
       floor-vs-cost conflict it does NOT explain;
  [F2b] the same census after three document edits - no code change;
  [F3] what the regulatory rules remove from the arm space (#7's input);
  [F4] retry-budget arithmetic: scheme ceiling vs velocity cap vs exponential backoff;
  [F5] the conflict ladder on one transaction, eligible set at every rung;
  [F6] precedence properties: permuting the merchant's order moves nothing, and waiving a
       rule is compared against re-scoping it;
  [PV] property tests: monotone composition, replay, the counterexample that kills a
       per-arm guard, and the enforcement-point property (only candidate/transaction
       rules may empty the set);
  [A]  the audit record, for a denial and for a normal decision.

Run:  python3 spikes/0005-constraints/policy.py [n_transactions]
      python3 spikes/0005-constraints/policy.py --section=F    # one section while iterating

Deterministic (fixed seed, stdlib only, no network). The traffic mix mirrors
spikes/0003-bandit-vs-table so a constraint result and a reward result can be read side by
side. The acquirer capability catalog is the committed fixture
`constraints/catalog/acquirer-catalog.example.json`, whose canonical hash both documents pin.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import random
import sys
import time
import zlib
from collections import deque
from pathlib import Path

SEED = 20_260_914
N_DEFAULT = 100_000
ROOT = Path(__file__).resolve().parents[2]
CONSTRAINTS = ROOT / "constraints"
TAKE_BPS = 128                      # merchant take rate, bps (same as the #3/#4 spikes)
MERCHANT_COUNTRY = "DE"
DAY_S = 86_400
HORIZON_DAYS = 30


def load_checker():
    """Import constraints/check.py by path: the spike must use the SAME gate CI uses."""
    spec = importlib.util.spec_from_file_location("sb_check", CONSTRAINTS / "check.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["sb_check"] = module
    spec.loader.exec_module(module)
    return module


CHECK = load_checker()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def canonical_hash(obj) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def dur_seconds(value: str) -> int:
    return CHECK._dur_seconds(value)


# ---------------------------------------------------------------------------- the request


class Ctx:
    """One decision's context: everything a filter is allowed to read."""

    __slots__ = ("i", "ts", "amount_minor", "currency", "card_region", "card_country",
                 "card_token", "bin_class", "bin_prefix", "route_class", "mandate",
                 "sca_required", "sca_exemption", "prior_decline_category",
                 "prior_attempt_ts", "prior_attempt_index", "prior_chain_depth",
                 "prior_mac", "prior_processor", "deadline_ms")

    def __init__(self, i, ts, amount_minor, currency, card_region, card_country, card_token,
                 bin_class, bin_prefix, route_class, mandate, sca_required, sca_exemption,
                 prior_decline_category=None, prior_attempt_ts=None, deadline_ms=900,
                 prior_attempt_index=1, prior_chain_depth=1, prior_mac=None,
                 prior_processor=None):
        self.i, self.ts = i, ts
        self.amount_minor, self.currency = amount_minor, currency
        self.card_region, self.card_country = card_region, card_country
        self.card_token, self.bin_class, self.bin_prefix = card_token, bin_class, bin_prefix
        self.route_class, self.mandate = route_class, mandate
        self.sca_required, self.sca_exemption = sca_required, sca_exemption
        self.prior_decline_category, self.prior_attempt_ts = prior_decline_category, \
            prior_attempt_ts
        self.prior_attempt_index, self.prior_chain_depth = prior_attempt_index, \
            prior_chain_depth
        self.prior_mac, self.prior_processor = prior_mac, prior_processor
        self.deadline_ms = deadline_ms

    def field(self, name):
        if name == "amount_band":
            a = self.amount_minor
            return ("micro" if a < 1000 else "small" if a < 5000 else
                    "mid" if a < 20000 else "large" if a < 100000 else "xl")
        return getattr(self, name, None)


class Arm:
    """A candidate action: (processor, flow). Flow is part of the arm because a processor
    that can run 3DS and one that cannot are different actions, and #7 sizes its posterior
    over arms, not processors."""

    __slots__ = ("pid", "flow", "acq")
    FLOWS = ("3ds", "frictionless")

    def __init__(self, pid, flow, acq):
        self.pid, self.flow, self.acq = pid, flow, acq

    def key(self):
        return (self.pid, self.flow)

    def __repr__(self):
        return f"{self.pid}/{self.flow}"


class RouteField:
    """Accessor for the predicate language: context fields AND the candidate route's own
    capability record. A `when` predicate may not read route fields (compile check SV12);
    route fields exist so a rule's rule-body can ask 'which acquirer is this?'."""

    __slots__ = ("ctx", "arm")

    def __init__(self, ctx, arm):
        self.ctx, self.arm = ctx, arm

    def get(self, name):
        if name == "processor":
            return self.arm.pid
        if name == "acquirer_country":
            return self.arm.acq["acquirer_country"]
        if name == "acquirer_region":
            return self.arm.acq["acquirer_region"]
        if name == "acquirer_capability":
            return self.arm.flow
        return self.ctx.field(name)


# ------------------------------------------------------------------------------- counters


class RingCounter:
    """Fixed-layout rolling-window counter. `slots` buckets of window/slots each, summed on
    read; pointer-free, self-expiring, allocation-free (ADR-0001 R5).

    The physical ring holds ONE MORE bucket than the rule declares, and that guard bucket is
    the whole reason the counter is allowed near a scheme rule. With exactly `slots` physical
    buckets, the slot being entered holds events from exactly one window ago, so entering it
    discards events that are still inside the window: measured in [M3], that is a 1-count
    UNDER-count on 30% of daily reads. Under-counting is the direction that gets a merchant
    billed for excessive reattempts, so the extra byte is not an optimisation - it is the
    difference between 'never under-counts' being true and being a slogan.

    The guard bucket costs `window/slots` of extra retained history: an event is counted until
    its bucket leaves the ring, i.e. up to one bucket longer than the window. Over-counting is
    safe (it refuses an attempt that was legal); under-counting is not.
    """

    __slots__ = ("slots", "width_s", "cap", "counts", "cur_slot", "cur_start", "total",
                 "guard")

    def __init__(self, slots: int, window_s: int, width: str = "uint8", guard: bool = True):
        self.slots = slots
        self.guard = 1 if guard else 0
        self.width_s = max(1, window_s // slots)
        self.cap = {"uint8": 255, "uint16": 65535, "uint32": 2 ** 32 - 1}[width]
        self.counts = [0] * (slots + self.guard)
        self.cur_slot = 0
        self.cur_start = 0
        self.total = 0

    def _advance(self, now: int):
        if now < self.cur_start:
            return
        step = (now - self.cur_start) // self.width_s
        if step <= 0:
            return
        n = len(self.counts)
        for k in range(1, min(step, n) + 1):
            idx = (self.cur_slot + k) % n
            self.total -= self.counts[idx]
            self.counts[idx] = 0
        self.cur_slot = (self.cur_slot + step) % n
        self.cur_start += step * self.width_s

    def add(self, now: int, n: int = 1):
        self._advance(now)
        self.counts[self.cur_slot] = min(self.cap, self.counts[self.cur_slot] + n)
        self.total += n

    def count(self, now: int) -> int:
        self._advance(now)
        return min(self.total, len(self.counts) * self.cap)

    def bytes(self) -> int:
        return len(self.counts) + 16          # counts + header (slot, start, total, cap)


class CounterSpec:
    """What one counting rule needs: the window, the ring geometry, and whether a per-day
    sub-limit rides along."""

    __slots__ = ("rid", "window_s", "slots", "width", "max_per_day")

    def __init__(self, rid, window_s, slots, width, max_per_day=None):
        self.rid, self.window_s, self.slots, self.width = rid, window_s, slots, width
        self.max_per_day = max_per_day


class Counters:
    """All state the layer reads besides the request and the catalog. In-process, per
    ADR-0001 R1 (no store hop inside Decide()); rebuilt from the trace on restart, and a key
    that failed to rebuild is `counter_unavailable`, not a silent zero."""

    def __init__(self, now: int = 0):
        self.rings: dict[tuple, RingCounter] = {}
        self.reservations: dict[tuple, list] = {}
        self.last_attempt: dict[tuple, int] = {}
        self.stopped: set = set()
        self.now = now

    def ring(self, rid, key, window_s, slots, width) -> RingCounter:
        k = (rid, key)
        r = self.rings.get(k)
        if r is None:
            r = self.rings[k] = RingCounter(slots, window_s, width)
        return r

    def count(self, rid, key, now, window_s, slots, width) -> int:
        return self.ring(rid, key, window_s, slots, width).count(now)

    def day_count(self, rid, key, now, slots=24) -> int:
        return self.ring(rid + "#day", key, DAY_S, slots, "uint8").count(now)

    def reserved(self, key, now) -> int:
        live = [e for e in self.reservations.get(key, []) if e > now]
        if len(live) != len(self.reservations.get(key, [])):
            self.reservations[key] = live
        return len(live)

    def reserve(self, key, expiry):
        self.reservations.setdefault(key, []).append(expiry)

    def record_attempt(self, key, now, specs, day_slots=24):
        """One attempt: every counting rule that watches this key advances. `in_flight`
        reservations are added separately by `reserve`, because an unsettled attempt must
        hold its slot (see the lease rule)."""
        for spec in specs:
            self.ring(spec.rid, key, spec.window_s, spec.slots, spec.width).add(now)
            if spec.max_per_day is not None:
                self.ring(spec.rid + "#day", key, DAY_S, day_slots, "uint8").add(now)
        self.last_attempt[key] = now


# ---------------------------------------------------------------------------- compilation


# Which part of the pipeline a rule belongs to. This is what makes "enforcement point" a
# property of the RULE rather than a global architectural choice:
#   candidate    - reads request + route, filters arms              (per decision, hot path)
#   transaction  - reads request + counters, can empty the set      (per decision, hot path)
#   plan         - shapes the fallback chain                        (per decision)
#   ordering     - constrains order, never membership               (per decision)
#   lease        - in-flight accounting for the counters            (per attempt)
#   presentation - gates the retry scheduler, not this decision     (deferred, cold path)
def load_scopes() -> dict[str, str]:
    """The enforcement point of every rule head comes from the SCHEMA, not from a table next
    to this evaluator. 'When is this rule evaluated' is part of the canonical artifact
    (checked by SV12), so moving a rule to a cheaper enforcement point is a schema change
    with a reviewer, not an edit to a compiler constant."""
    schema = load_json(CONSTRAINTS / "schema" / "constraint-set.schema.json")
    out = {}
    for key, head in schema["$defs"].items():
        if not key.startswith("rule-"):
            continue
        name = head["properties"]["rule"]["const"]
        out[name] = head["properties"]["enforcement"]["const"]
    return out


SCOPE = load_scopes()


def compile_predicate(node, value_getter):
    """Compile the closed predicate language to a closure. No eval, no interpreter, no clock,
    no randomness: the same node yields the same verdict for the same context, which is what
    makes a decision replayable from the audit record."""
    if node is None:
        return None
    if "all" in node:
        parts = [compile_predicate(p, value_getter) for p in node["all"]]
        return lambda f: all(p(f) for p in parts)
    if "any" in node:
        parts = [compile_predicate(p, value_getter) for p in node["any"]]
        return lambda f: any(p(f) for p in parts)
    if "not" in node:
        inner = compile_predicate(node["not"], value_getter)
        return lambda f: not inner(f)

    field, op, value = node["field"], node["op"], node.get("value")
    if op == "eq":
        fn = lambda v: v == value
    elif op == "ne":
        fn = lambda v: v != value
    elif op == "in":
        vs = frozenset(value)
        fn = lambda v: v in vs
    elif op == "not_in":
        vs = frozenset(value)
        fn = lambda v: v not in vs
    elif op == "lt":
        fn = lambda v: v is not None and v < value
    elif op == "lte":
        fn = lambda v: v is not None and v <= value
    elif op == "gt":
        fn = lambda v: v is not None and v > value
    elif op == "gte":
        fn = lambda v: v is not None and v >= value
    elif op == "prefix":
        fn = lambda v: v is not None and str(v).startswith(str(value))
    elif op == "exists":
        fn = lambda v: v is not None
    else:
        raise ValueError(f"unknown op {op}")
    return lambda f: fn(value_getter(f, field))


MISSING = object()


def compile_outcome_predicate(node):
    """Compile an `after` predicate into a THREE-state evaluator: True, False, or None for
    'the outcome does not carry that field'. The third state is load-bearing: 'the issuer did
    not tell us why' is a different fact from 'the issuer said do not retry', and Visa's rule
    is about the first one (absent category => do not reattempt by default)."""
    if node is None:
        return None
    if "all" in node:
        parts = [compile_outcome_predicate(n) for n in node["all"]]
        def all_fn(s):
            unknown = False
            for f in parts:
                v = f(s)
                if v is False:
                    return False
                unknown = unknown or v is None
            return None if unknown else True
        return all_fn
    if "any" in node:
        parts = [compile_outcome_predicate(n) for n in node["any"]]
        def any_fn(s):
            unknown = False
            for f in parts:
                v = f(s)
                if v is True:
                    return True
                unknown = unknown or v is None
            return None if unknown else False
        return any_fn
    if "not" in node:
        inner = compile_outcome_predicate(node["not"])
        return lambda s: None if (v := inner(s)) is None else (not v)

    field, op, value = node["field"], node["op"], node.get("value")

    def leaf(s):
        v = s.get(field, MISSING)
        if v is MISSING and op != "exists":
            return None
        if op == "eq":
            return v == value
        if op == "ne":
            return v != value
        if op == "in":
            return v in frozenset(value)
        if op == "not_in":
            return v not in frozenset(value)
        if op == "lt":
            return v < value
        if op == "lte":
            return v <= value
        if op == "gt":
            return v > value
        if op == "gte":
            return v >= value
        if op == "exists":
            return v is not MISSING
        raise ValueError(f"unknown op {op}")
    return leaf


class ConstraintSet:
    """A compiled ConstraintSet. `rules` is ordered by the document's declared precedence:
    fixed prefix (regulatory, network), then the merchant's order, then preference. That
    order is the RELAXATION order - the eligible set is the intersection - so permuting the
    merchant's order must not move the eligible set (property PV1 in [F6])."""

    def __init__(self, doc: dict, catalog: dict):
        self.doc = doc
        self.id = doc["id"]
        self.revision = doc["revision"]
        self.doc_hash = canonical_hash(doc)
        self.catalog = {a["id"]: a for a in catalog["acquirers"]}
        self.catalog_hash = canonical_hash(catalog)
        order = list(doc["precedence"]["fixed_prefix"]) + list(doc["precedence"]["order"])
        order.append("preference")
        self.order = order
        self.rules = []
        for family in order:
            for raw in doc.get(family, []):
                if raw.get("enabled", True):
                    rule = dict(raw)
                    rule["family"] = family
                    rule["scope"] = SCOPE[rule["rule"]]
                    rule["when_fn"] = compile_predicate(
                        rule.get("when"), lambda f, name: f.get(name))
                    rule["after_fn"] = compile_outcome_predicate(rule.get("after"))
                    self.rules.append(rule)
        # Request-scoped refinements are NOT part of the persistent set: they are applied
        # only to the request they were issued for (matched by request_ref), so a pin logged
        # for one stuck transaction cannot silently narrow everyone else's routing.
        self.request_constraints = []
        for raw in doc.get("constraints", []):
            rule = dict(raw)
            rule["family"] = raw["rule"].split(".")[0]
            rule["scope"] = SCOPE[rule["rule"]]
            rule["when_fn"] = compile_predicate(rule.get("when"),
                                                lambda f, name: f.get(name))
            rule["after_fn"] = compile_outcome_predicate(rule.get("after"))
            self.request_constraints.append(rule)
        self.fail = doc["fail_mode"]
        self.all_arms = [Arm(a["id"], flow, a) for a in catalog["acquirers"]
                         for flow in Arm.FLOWS]
        self.counter_specs = self._counter_specs()

    def _counter_specs(self):
        specs = []
        for rule in self.rules:
            if rule["rule"] in ("network.reattempt_limit", "budget.attempt_cap"):
                p = rule["params"]
                specs.append(CounterSpec(rule["id"], dur_seconds(p["window"]),
                                         p["counter"]["slots"],
                                         p["counter"].get("slot_width", "uint8"),
                                         p.get("max_per_day")))
        return specs

    # -- precedence helpers -------------------------------------------------------------
    def relax_ladder(self):
        """Lowest precedence is waived first. regulatory and network are not in the ladder at
        all: the schema does not let them declare `relaxable`."""
        return [r for r in reversed(self.rules)
                if r["rule"].split(".")[0] in ("budget", "econ", "mandate")
                and r["scope"] in ("candidate", "transaction", "plan", "ordering")]

    def _matches(self, rule, ctx) -> bool:
        fn = rule["when_fn"]
        return fn is None or fn(RouteField(ctx, self.all_arms[0]))

    # -- capability: unconditional, not merchant policy ---------------------------------
    @staticmethod
    def capability_ok(ctx, arm) -> bool:
        acq = arm.acq
        if ctx.currency not in acq["currencies"]:
            return False
        if ctx.card_region not in acq["markets"]:
            return False
        if arm.flow == "3ds" and not acq["three_ds"]:
            return False
        return True

    # -- outcome snapshot ---------------------------------------------------------------
    @staticmethod
    def snapshot(ctx: Ctx):
        """What the LAST attempt returned, as the rule language sees it. None means 'there is
        no last attempt', which is not the same as 'the last attempt carried no category':
        a fresh authorization is not a reattempt, and conflating the two refuses every first
        attempt in the book (this spike shipped that bug for one revision, which is the
        argument for the census in [F1])."""
        if ctx.prior_attempt_ts is None:
            return None
        snap = {"attempt_index": ctx.prior_attempt_index,
                "chain_depth": ctx.prior_chain_depth,
                "elapsed_since_decision_ms": ctx.ts - ctx.prior_attempt_ts}
        if ctx.prior_decline_category is not None:
            snap["decline_category"] = ctx.prior_decline_category
        if ctx.prior_mac is not None:
            snap["merchant_advice_code"] = ctx.prior_mac
        if ctx.prior_processor is not None:
            snap["last_attempt_processor"] = ctx.prior_processor
        return snap

    # -- the evaluator ------------------------------------------------------------------
    def evaluate(self, ctx: Ctx, counters: Counters, waived=None, arm_subset=None,
                 full_trace=False, request_ref=None, only=None):
        """The filter. Returns (arms, denial, blocks, info):
             arms    - the eligible set (empty means 'no legal route', handled by the caller)
             denial  - arm key -> the FIRST rule that denied it: the audit's reason code
             blocks  - rule id -> the set of arms that rule denied (a transaction rule denies
                       all of them, which is exactly what makes it a member of a conflict set)
             info    - emptied_by, strict_empty, waivers, whether the mandate was met
        """
        waived = waived or set()
        all_arms = arm_subset if arm_subset is not None else self.all_arms
        arms, denial, blocks = [], {}, {}
        for arm in all_arms:
            if self.capability_ok(ctx, arm):
                arms.append(arm)
            else:
                denial[arm.key()] = "capability"
                blocks.setdefault("capability", set()).add(arm.key())
        emptied_by = None
        now = ctx.ts
        snap = self.snapshot(ctx)
        active = self.rules
        if request_ref is not None:
            active = active + [r for r in self.request_constraints
                               if r.get("request_ref") == request_ref]

        def deny_all(rule, tag=None):
            nonlocal emptied_by, arms
            rid = rule["id"] + (tag or "")
            emptied_by = emptied_by or rid
            blocks.setdefault(rid, set()).update(
                a.key() for a in (all_arms if arm_subset is None else arm_subset))
            arms = []

        for rule in active:
            if rule["id"] in waived or (only is not None and rule["id"] not in only) \
                    or not self._matches(rule, ctx):
                continue
            name, scope, p = rule["rule"], rule["scope"], rule["params"]
            if scope in ("presentation", "lease", "plan", "ordering"):
                continue

            if scope == "transaction":
                key = self._key(ctx, p.get("key", "card"))
                if name == "network.hard_stop":
                    # A decision-time read of state the INGEST path wrote when `after` last
                    # matched. The stop is card-scoped, so it denies the whole candidate set.
                    if key in counters.stopped:
                        deny_all(rule)
                    continue
                if name == "network.reattempt_limit":
                    if snap is None:
                        continue          # a fresh attempt is not a reattempt
                    verdict = True
                    after_fn = rule.get("after_fn")
                    if after_fn is not None:
                        verdict = after_fn(snap)
                    if verdict is None:
                        # the last attempt carried no decline category: Visa's default is
                        # not to retry, and `missing_category` is where that default lives.
                        if p.get("missing_category", "deny") == "allow":
                            continue
                        verdict = True
                    if verdict is False:
                        continue
                    n = counters.count(rule["id"], key, now, dur_seconds(p["window"]),
                                       p["counter"]["slots"],
                                       p["counter"].get("slot_width", "uint8"))
                    n += counters.reserved(key, now)
                    over = n >= p["limit"]
                    if p.get("max_per_day") is not None and \
                            counters.day_count(rule["id"], key, now) >= p["max_per_day"]:
                        over = True
                    if p.get("min_interval") and ctx.prior_attempt_ts is not None and \
                            now - ctx.prior_attempt_ts < dur_seconds(p["min_interval"]):
                        over = True
                    if over:
                        deny_all(rule)
                    continue
                if name == "budget.attempt_cap":
                    # Applies to every attempt: this is the merchant's velocity budget, not
                    # a reaction to a decline.
                    n = counters.count(rule["id"], key, now, dur_seconds(p["window"]),
                                       p["counter"]["slots"],
                                       p["counter"].get("slot_width", "uint8"))
                    n += counters.reserved(key, now)
                    if n >= p["limit"]:
                        deny_all(rule, ":queue" if p.get("on_exceeded") == "queue" else None)
                    continue

            if scope == "candidate":
                kept = []
                for arm in arms:
                    if self._arm_ok(ctx, arm, rule):
                        kept.append(arm)
                    else:
                        denial.setdefault(arm.key(), rule["id"])
                        blocks.setdefault(rule["id"], set()).add(arm.key())
                if arms and not kept:
                    emptied_by = emptied_by or rule["id"]
                arms = kept
                if not arms and not full_trace:
                    break

        info = {"emptied_by": emptied_by, "strict_empty": not arms}
        return arms, denial, blocks, info

    def _arm_ok(self, ctx, arm, rule) -> bool:
        """One candidate rule against one arm. The rule catalog's semantic content lives
        here; everything else in this file is plumbing for it."""
        name, p = rule["rule"], rule["params"]
        rf = RouteField(ctx, arm)
        if name == "regulatory.data_residency":
            return rf.get("acquirer_region") in p["acquirer_regions"]
        if name == "regulatory.sca_required":
            mode = p["mode"]
            if mode == "always":
                need = True
            elif mode == "unless_exempt_evidence":
                allowed = p.get("allowed_exemptions", [])
                need = not (ctx.sca_exemption and ctx.sca_exemption in allowed)
                if ctx.sca_required is False and not ctx.sca_exemption and \
                        p.get("on_missing_evidence") == "deny":
                    return False
            else:  # unless_request_declares_not_required
                need = ctx.sca_required is not False
            return (not need) or rf.get("acquirer_capability") == "3ds"
        if name == "mandate.require_3ds":
            mode = p["mode"]
            if mode == "if_supported_by_capability":
                need = arm.acq["three_ds"]
            elif mode == "unless_exempt_evidence":
                allowed = p.get("allowed_exemptions", [])
                need = not (ctx.sca_exemption and ctx.sca_exemption in allowed)
            else:
                need = True
            return (not need) or rf.get("acquirer_capability") == "3ds"
        if name == "mandate.must_process":
            return arm.pid in p["processors"]
        if name == "mandate.exclude_processor":
            return arm.pid not in p["processors"]
        if name == "mandate.domestic_acquirer":
            if "acquirer_regions" in p:
                # The region-list form: the acquirer's own domicile region must be in the
                # list. It is the same catalog field and the same shape the residency rule
                # reads, which is why a merchant can express "EEA or UK" and keep a checkable
                # guarantee instead of waiving the rule ([F6]).
                return rf.get("acquirer_region") in set(p["acquirer_regions"])
            match = p["match"]
            if match == "card_country":
                ok = rf.get("acquirer_country") == ctx.card_country
                return ok or (bool(p.get("allow_same_region")) and
                              rf.get("acquirer_region") == ctx.card_region)
            if match == "card_region":
                return rf.get("acquirer_region") == ctx.card_region
            return rf.get("acquirer_country") == MERCHANT_COUNTRY
        if name == "econ.floor_margin":
            return TAKE_BPS - arm.acq["cost_bps"] >= p["value_bps"]
        if name == "econ.max_cost":
            return arm.acq["cost_bps"] <= p["value_bps"]
        raise ValueError(f"rule {name} has no arm semantics")

    def evaluate_with_fallback(self, ctx: Ctx, counters: Counters, request_ref=None):
        """Rung 1, then `if_unavailable: fall_back`: a must_process whose intersection emptied
        by its own fault is waived for the whole set, not per arm. That distinction is what
        [PV] P3 is about."""
        arms, denial, blocks, info = self.evaluate(ctx, counters, request_ref=request_ref)
        if arms:
            return arms, denial, blocks, info, None
        for rule in self.rules:
            if rule["rule"] != "mandate.must_process" or not self._matches(rule, ctx):
                continue
            if rule["params"].get("if_unavailable") != "fall_back":
                continue
            arms2, denial2, blocks2, info2 = self.evaluate(
                ctx, counters, waived={rule["id"]}, request_ref=request_ref)
            if arms2:
                info2["mandate_unmet"] = rule["id"]
                return arms2, denial2, blocks2, info2, rule["id"]
        return [], denial, blocks, info, None

    def preference_tier(self, ctx, arms):
        """Ordering scope: `restrict` narrows only when the tier is non-empty, so a preference
        can never make a request unroutable."""
        rules = [r for r in self.rules if r["rule"] == "preference.rank" and
                 self._matches(r, ctx)]
        rules.sort(key=lambda r: r["params"].get("tier", 1))
        for r in rules:
            p = r["params"]
            if p["mode"] != "restrict":
                continue
            subset = [a for a in arms if a.pid in p["processors"]]
            if subset:
                return subset, (r["id"], len(arms) - len(subset))
        return arms, None

    @staticmethod
    def _key(ctx, kind):
        if kind == "card":
            return ("card", ctx.card_token)
        if kind == "bin":
            return ("bin", ctx.bin_prefix)
        if kind == "transaction":
            return ("txn", ctx.card_token, ctx.i)
        return ("merchant", MERCHANT_COUNTRY)


class EscapeBudget:
    """A bounded relaxation, in two layers: a GLOBAL ceiling on waivers (the operator's
    appetite, first come first served) and each rule's own declared bound from the document
    (`single_transaction` = once, `traffic_share` = its share, `unbounded` = no share bound
    but still expiring). The residual escalates, so the operator gets numbers - escaped
    share, escalated share - instead of a silent routing-around."""

    def __init__(self, bps: int, n_total: int):
        self.cap = int(bps / 10000.0 * n_total)
        self.spent: dict[str, int] = {}
        self.total = 0
        self.n_total = n_total

    def allow(self, rule) -> bool:
        if self.total >= self.cap:
            return False
        rel = rule["relaxable"]
        if rel.get("scope") == "single_transaction" and self.spent.get(rule["id"], 0) >= 1:
            return False
        if rel.get("scope") == "traffic_share":
            share = rel.get("max_traffic_share_bps", 0) / 10000.0
            if self.spent.get(rule["id"], 0) >= int(share * self.n_total):
                return False
        return True

    def spend(self, rid):
        self.spent[rid] = self.spent.get(rid, 0) + 1
        self.total += 1


def relax_ladder(cs: ConstraintSet, ctx, counters, budget: EscapeBudget):
    """Rung 2: waive the lowest-precedence relaxable rule that denied something, recompute,
    repeat. Never touches regulatory or network."""
    if budget is None:
        return [], []
    waived: list[str] = []
    for rule in cs.relax_ladder():
        if not cs._matches(rule, ctx) or "relaxable" not in rule:
            continue
        if not budget.allow(rule):
            continue
        waived.append(rule["id"])
        arms, _denial, _blocks, _info = cs.evaluate(ctx, counters, waived=set(waived))
        arms, _ = cs.preference_tier(ctx, arms)
        if arms:
            for rid in waived:
                budget.spend(rid)
            return arms, waived
    return [], []


_CONFLICT_CACHE: dict = {}


def _ctx_signature(cs: ConstraintSet, ctx: Ctx) -> tuple:
    """Everything a candidate/transaction rule can read, apart from the counters. Two
    decisions with the same signature have the same conflict set, which is what makes
    enumerating subsets affordable in a census."""
    return (cs.id, ctx.card_region, ctx.card_country, ctx.currency, ctx.bin_class,
            ctx.route_class, bool(ctx.mandate), ctx.sca_required, ctx.sca_exemption,
            ctx.amount_minor // 1000, ctx.prior_decline_category, ctx.prior_attempt_ts is None)


def minimal_conflict(cs: ConstraintSet, ctx, counters) -> tuple:
    """The MINIMAL set of rules whose conjunction leaves no legal route - what an operator
    needs from an escalation, as opposed to a wall of denials. Computed by re-evaluating the
    pipeline with only the subset active, because a sequential filter's per-rule denial sets
    do not compose: an arm removed by rule A is never seen by rule B, so reading the denial
    map would attribute a one-rule fault to a four-rule conflict."""
    sig = _ctx_signature(cs, ctx)
    if sig in _CONFLICT_CACHE:
        return _CONFLICT_CACHE[sig]
    if not any(cs.capability_ok(ctx, a) for a in cs.all_arms):
        _CONFLICT_CACHE[sig] = ("capability_coverage",)
        return _CONFLICT_CACHE[sig]
    culprits = sorted({r["id"] for r in cs.rules
                       if r["scope"] in ("candidate", "transaction") and cs._matches(r, ctx)})
    result = tuple(culprits)
    for size in range(1, len(culprits) + 1):
        for subset in _combinations(culprits, size):
            arms, _d, _b, _i = cs.evaluate(ctx, counters, only=set(subset))
            if not arms:
                result = tuple(subset)
                break
        else:
            continue
        break
    _CONFLICT_CACHE[sig] = result
    return result


def _combinations(items, k):
    if k == 0:
        yield ()
        return
    for i in range(len(items) - k + 1):
        for rest in _combinations(items[i + 1:], k - 1):
            yield (items[i],) + rest


# ------------------------------------------------------------------------------ scenarios

REGIONS = (
    ("SEPA", "EEA", "EUR", 0.42, (("DE", .25), ("FR", .18), ("NL", .12), ("IE", .10),
                                  ("ES", .12), ("IT", .10), ("SE", .07), ("PL", .06))),
    ("UK", "UK", "GBP", 0.14, (("GB", 1.0),)),
    ("US", "US", "USD", 0.26, (("US", 1.0),)),
    ("LATAM", "LATAM", "USD", 0.10, (("BR", .45), ("MX", .30), ("AR", .15), ("CL", .10))),
    ("APAC", "APAC", "USD", 0.08, (("SG", .35), ("JP", .30), ("AU", .20), ("HK", .15))),
)
BIN_CLASSES = (("consumer_credit", 0.58), ("consumer_debit", 0.27), ("corporate", 0.10),
               ("prepaid", 0.05))
PRIOR_CATEGORIES = (2, 2, 2, 3, 1)     # decline categories seen on the prior attempt


def build_contexts(n: int, seed: int = SEED):
    """A context stream shaped like the #3 spike's traffic, spread over 30 days. 12% of cards
    are on a multi-attempt recovery journey and carry prior-attempt state, which is what makes
    the network and budget rules do anything at all."""
    ctxs = []
    for i in range(n):
        rng = random.Random(f"{seed}:ctx:{i}")
        reg = rng.choices(REGIONS, weights=[r[3] for r in REGIONS])[0]
        cls_i, (cls, _w) = rng.choices(list(enumerate(BIN_CLASSES)),
                                       weights=[c[1] for c in BIN_CLASSES])[0]
        amount = max(60, int(math.exp(rng.gauss(math.log(46.0), 0.95)) * 100))
        country = rng.choices([c for c, _ in reg[4]], weights=[w for _, w in reg[4]])[0]
        route_class = "recurring_mit" if rng.random() < 0.22 else "card_ecom"
        ts = int(i * (HORIZON_DAYS * DAY_S / n))
        repeat = rng.random() < 0.12
        card_token = f"card:{i // 3}" if repeat else f"card:{i}"
        sca_required = reg[1] in ("EEA", "UK") and route_class != "recurring_mit"
        if route_class == "recurring_mit":
            exemption = "recurring_mit"           # MIT: out of SCA scope by construction
        elif sca_required and rng.random() < 0.55:
            exemption = "tra"
        else:
            exemption = None
        prior_cat = rng.choice(PRIOR_CATEGORIES) if repeat else None
        prior_ts = None
        if repeat:
            prior_ts = ts - (1 + rng.randrange(3)) * DAY_S
            if prior_ts < 0:
                prior_ts = None
        currency = "USD" if (reg[0] == "SEPA" and rng.random() < 0.12) else reg[2]
        ctxs.append(Ctx(i, ts, amount, currency, reg[1], country, card_token, cls,
                        f"BIN{zlib.crc32(cls.encode()) % 900000 + 100000:06d}",
                        route_class, route_class == "recurring_mit", sca_required,
                        exemption, prior_cat, prior_ts))
    return ctxs


def pre_seed(counters: Counters, ctxs, cs: ConstraintSet, seed=SEED):
    """History before the window: some cards arrive having already burned part of their
    budget, and a few are hard-stopped. Without this the counters are always empty and the
    counting rules never fire - which is exactly how a review misses them."""
    rng = random.Random(seed + 1)
    seeded = set()
    for ctx in ctxs:
        key = ("card", ctx.card_token)
        if key in seeded or ctx.prior_decline_category is None:
            continue
        seeded.add(key)
        for spec in cs.counter_specs:
            prior = rng.choice([0, 1, 2, 5, 10, 13, 14, 15, 16, 20])
            if prior:
                counters.ring(spec.rid, key, spec.window_s, spec.slots,
                              spec.width).add(max(0, ctx.ts - DAY_S), prior)
            if spec.max_per_day is not None and rng.random() < 0.3:
                counters.ring(spec.rid + "#day", key, DAY_S, 24, "uint8").add(ctx.ts, 1)
        if ctx.prior_decline_category == 1 and rng.random() < 0.25:
            counters.stopped.add(key)


# --------------------------------------------------------------------------------- census


def eligible_stats(cs: ConstraintSet, ctxs, counters, budget=None, record=True):
    """Run the whole layer over a stream and collect the census. Five outcomes, because
    'nothing is legal' is not one thing:
      routed     - at least one legal route
      waived     - legal only after the ladder waives a relaxable rule (a bounded escape)
      deferred   - a velocity/reattempt rule handed the attempt to the scheduler (`queue`)
      prohibited - a scheme/budget rule that exists to STOP the attempt (hard stop, scheme
                   ceiling, velocity cap with deny) did exactly that
      unroutable - candidate rules leave the set empty and nothing may be waived: the only
                   genuine conflict, and the only one that escalates
    Conflating the last three is how a review concludes the router is 'broken 30% of the
    time' when it is in fact behaving exactly as configured."""
    out = {"n": 0, "routed": 0, "waived": 0, "deferred": 0, "prohibited": 0, "unroutable": 0,
           "arms_total": 0, "arms_min": 10 ** 9, "denied_by": {}, "waived_by": {},
           "conflict_sigs": {}, "zero_by_ccy": {}, "zero_by_region": {}, "zero_by_country": {},
           "tx_denied": {}, "arms_hist": {}}
    for ctx in ctxs:
        out["n"] += 1
        arms, denial, blocks, info, _unmet = cs.evaluate_with_fallback(ctx, counters)
        arms, _tier = cs.preference_tier(ctx, arms)
        if arms:
            out["routed"] += 1
        else:
            arms, waived_ids = relax_ladder(cs, ctx, counters, budget)
            if arms:
                out["waived"] += 1
                for rid in waived_ids:
                    out["waived_by"][rid] = out["waived_by"].get(rid, 0) + 1
            else:
                emptied = info["emptied_by"] or ""
                head = emptied.split(":")[0]
                rule = next((r for r in cs.rules if r["id"] == head), None)
                if emptied.endswith(":queue"):
                    out["deferred"] += 1
                elif rule is not None and rule["scope"] == "transaction":
                    out["prohibited"] += 1
                else:
                    out["unroutable"] += 1
                    sig = "+".join(minimal_conflict(cs, ctx, counters)) or "capability_only"
                    out["conflict_sigs"][sig] = out["conflict_sigs"].get(sig, 0) + 1
                    out["zero_by_ccy"][ctx.currency] = \
                        out["zero_by_ccy"].get(ctx.currency, 0) + 1
                    out["zero_by_region"][ctx.card_region] = \
                        out["zero_by_region"].get(ctx.card_region, 0) + 1
                    out["zero_by_country"][ctx.card_country] = \
                        out["zero_by_country"].get(ctx.card_country, 0) + 1
        if arms:
            out["arms_total"] += len(arms)
            out["arms_min"] = min(out["arms_min"], len(arms))
            out["arms_hist"][len(arms)] = out["arms_hist"].get(len(arms), 0) + 1
            if record:
                counters.record_attempt(("card", ctx.card_token), ctx.ts, cs.counter_specs)
        for why in denial.values():
            out["denied_by"][why] = out["denied_by"].get(why, 0) + 1
        if info["emptied_by"]:
            out["tx_denied"][info["emptied_by"]] = \
                out["tx_denied"].get(info["emptied_by"], 0) + 1
    return out


def audit_record(cs: ConstraintSet, ctx: Ctx, arms, denial, blocks=None, conflict=()) -> dict:
    """The record the ADR specifies: flat, hashable, and replayable without the engine. It is
    simultaneously the explanation, the OPE reweighting input (#15) and the dispute file."""
    blocks = blocks or {}
    trace = []
    for rule in cs.rules:
        if not cs._matches(rule, ctx):
            continue
        denied = len(blocks.get(rule["id"], ()))
        trace.append({
            "rule": rule["id"], "rule_name": rule["rule"], "scope": rule["scope"],
            "verdict": "deny" if (denied and not arms) else "partial" if denied else "pass",
            "denied_arms": denied,
        })
    return {
        "decision_id": f"dec_{ctx.i:08d}",
        "ts": ctx.ts,
        "constraint_set": {"id": cs.id, "revision": cs.revision, "hash": cs.doc_hash[:32]},
        "catalog": {"version": cs.doc["policy"]["catalog_version"],
                    "hash": cs.catalog_hash[:32]},
        "scope": {"merchant": cs.doc["scope"].get("merchant_id"),
                  "route_class": ctx.route_class},
        "precedence": [r["id"] for r in cs.rules if r["scope"] != "presentation"],
        "request": {"currency": ctx.currency, "amount_minor": ctx.amount_minor,
                    "card_region": ctx.card_region, "card_country": ctx.card_country,
                    "bin_class": ctx.bin_class, "mandate": ctx.mandate,
                    "sca_required": ctx.sca_required,
                    "sca_exemption": ctx.sca_exemption},
        "eligible": [list(a.key()) for a in arms],
        "denied": sorted(f"{pid}/{flow}:{why}" for (pid, flow), why in denial.items()),
        "pass_trace": trace,
        "conflict_set": list(conflict),
        "action": {"kind": "escalate" if not arms else "route",
                   "processor": None if not arms else arms[0].pid,
                   "flow": None if not arms else arms[0].flow},
        "fail_mode": cs.fail["empty_eligible_set"],
    }


def retry_schedule(gaps_days, horizon=HORIZON_DAYS, cap=None, fee_after=15, fee=0.10):
    """Re-presentment arithmetic for one soft-declining card. No recovery probabilities are
    invented here: this is the fee and ceiling side, which is arithmetic, not a model."""
    day, attempts, fees = 0, 0, 0.0
    i = 0
    while True:
        step = gaps_days[min(i, len(gaps_days) - 1)]
        if day + step > horizon:
            break
        day += step
        i += 1
        if cap is not None and attempts >= cap:
            break
        attempts += 1
        if attempts > fee_after:
            fees += fee
    return attempts, max(0, attempts - fee_after), fees, day


# ---------------------------------------------------------------------------------- report


def pct(x, n):
    return 100.0 * x / max(n, 1)


def main(argv) -> int:
    n = N_DEFAULT
    only = None
    for j, arg in enumerate(argv):
        if arg.startswith("--section="):
            only = arg.split("=", 1)[1]
        elif arg == "--section":
            only = argv[j + 1]
        elif arg.isdigit():
            n = int(arg)

    def section(tag):
        return only is None or tag in only

    catalog = load_json(CONSTRAINTS / "catalog/acquirer-catalog.example.json")
    catalog_hash = canonical_hash(catalog)
    docs_path = CONSTRAINTS / "examples"
    docs = {p.stem: load_json(p) for p in sorted(docs_path.glob("*.json"))}
    northwind = ConstraintSet(docs["merchant-default"], catalog)
    aurora = ConstraintSet(docs["marketplace-strict"], catalog)

    print("#5 evidence spike: the constraint layer, compiled and evaluated")
    print(f"python {sys.version.split()[0]} | {n:,} decisions | seed {SEED} | "
          f"catalog {catalog['catalog_version']}")
    print()

    # ---------------------------------------------------------------- [C1] compiled shape
    if section("C"):
        print("[C1] what the document compiles to")
        for cs in (northwind, aurora):
            scopes: dict[str, int] = {}
            for r in cs.rules:
                scopes[r["scope"]] = scopes.get(r["scope"], 0) + 1
            print(f"     {cs.id:<26} rev {cs.revision}  {len(cs.rules):>2} rules  "
                  f"doc_hash {cs.doc_hash[:12]}  pinned catalog {cs.doc['policy']['catalog_hash'][:12]}")
            print(f"     {'':<26} enforcement points (read from the schema, SV12): "
                  + ", ".join(f"{k}={v}" for k, v in sorted(scopes.items())))
            print(f"     {'':<26} relax ladder (lowest precedence first): "
                  + " -> ".join(r["id"] for r in cs.relax_ladder()))
            print(f"     {'':<26} + {len(cs.doc.get('constraints', []))} inline constraint "
                  "template(s): bound to a request_ref, never standing rules (SV10)")
        pin_ok = (catalog_hash == northwind.doc["policy"]["catalog_hash"] ==
                  aurora.doc["policy"]["catalog_hash"])
        print(f"     both documents pin the fixture catalog hash: {pin_ok}")
        print(f"     candidate arms before capability filtering: {len(northwind.all_arms)} "
              f"({len(catalog['acquirers'])} processors x {len(Arm.FLOWS)} flows)")
        print(f"     counting rules: " + "; ".join(
            f"{s.rid}(window={s.window_s//DAY_S}d,{s.slots}x{s.width}"
            + (f",max/day={s.max_per_day}" if s.max_per_day else "") + ")"
            for s in northwind.counter_specs))
        print()

    ctxs = build_contexts(n)
    measured_total = 0.0

    # ------------------------------------------------------------------- [M1] hot path cost
    if section("M"):
        print("[M1] hot-path cost, CPython 3.11 (an upper bound on any compiled language)")
        sample = ctxs[:20_000]
        for label, cs in ((f"northwind ({len(northwind.rules)} rules)", northwind),
                          (f"aurora ({len(aurora.rules)} rules)", aurora)):
            counters = Counters()
            pre_seed(counters, sample, cs)
            t0 = time.perf_counter()
            filt = []
            for ctx in sample:
                arms, _d, _b, _i, _u = cs.evaluate_with_fallback(ctx, counters)
                arms, _t = cs.preference_tier(ctx, arms)
                filt.append(arms)
            t1 = time.perf_counter()
            guard_hits = guard_n = 0
            for ctx, arms in zip(sample, filt):
                live = {a.key() for a in cs.evaluate(ctx, counters)[0]}
                for a in arms:
                    guard_n += 1
                    guard_hits += a.key() in live
            t2 = time.perf_counter()
            per_f = (t1 - t0) / len(sample) * 1e6
            per_g = (t2 - t1) / max(1, guard_n) * 1e6
            mean_arms = guard_n / len(sample)
            measured_total = per_f
            if label.startswith("aurora"):
                measured_total = per_f + per_g * mean_arms
            print(f"     {label:<20} filter {per_f:7.2f} us/decision   "
                  f"guard {per_g:5.2f} us/arm x {mean_arms:4.2f} arms   "
                  f"guards passed {pct(guard_hits, guard_n):.2f}%")
        print(f"     per-decision total (filter + one guard), aurora: "
              f"{measured_total:6.2f} us CPython")
        print("     -> the filter is O(candidate arms), reads the request, the route and a")
        print("        counter snapshot, and allocates nothing but the pre-sized arm slice")
        print("        (ADR-0001 R1/R3). Extrapolation, against the 20 us p99 in-engine budget:")
        for factor in (1.0, 10.0, 30.0, 100.0):
            print(f"       {factor:>5.1f}x faster (compiled)  ->  {measured_total/factor:6.2f} us "
                  f"({pct(measured_total/factor, 20):5.2f}% of the in-engine budget)")
        print()

    # --------------------------------------------------------------- [M2] the audit record
    if section("M"):
        print("[M2] the audit record")
        for label, cs, idx in (("northwind", northwind, 7), ("aurora (denied)", aurora, 3)):
            ctx = ctxs[idx]
            arms, denial, blocks, _i, _u = cs.evaluate_with_fallback(ctx, Counters())
            arms, _t = cs.preference_tier(ctx, arms)
            rec = audit_record(cs, ctx, arms, denial, blocks)
            blob = json.dumps(rec, sort_keys=True, separators=(",", ":"))
            print(f"     {label:<16} fields {len(rec):>2}  canonical bytes {len(blob):>4}  "
                  f"sha256 {hashlib.sha256(blob.encode()).hexdigest()[:16]}")
        print("     at 5,000 decisions/s the trace is ~7 MB/s, ~600 GB/day uncompressed -")
        print("     which is why ADR-0001 R1 keeps the trace writer off Decide()'s path and")
        print("     #13 owns compression/retention (400 days in these documents).")
        print()

    # ------------------------------------------------------------------ [M3] the counters
    if section("M"):
        print("[M3] counters: ring vs an exact window, in BOTH directions")
        for guard in (False, True):
            rng = random.Random(SEED)
            exact: dict[str, deque] = {}
            rings: dict[tuple, RingCounter] = {}

            def get(key):
                r = rings.get(key)
                if r is None:
                    r = rings[key] = RingCounter(30, 30 * DAY_S, "uint8", guard=guard)
                return r

            over = under = 0
            for step in range(200_000):
                key = f"card:{rng.randrange(4000)}"
                now = step * 41
                dq = exact.setdefault(key, deque())
                while dq and now - dq[0] > 30 * DAY_S:
                    dq.popleft()
                n_exact = len(dq)
                dq.append(now)
                r = get(key)
                n_ring = r.count(now)
                over += n_ring > n_exact
                under += n_ring < n_exact
                r.add(now)
            # adversarial: a card on a daily re-presentment cadence, read every day
            dq2: deque = deque()
            over2 = under2 = n2 = 0
            r2 = RingCounter(30, 30 * DAY_S, "uint8", guard=guard)
            for day in range(1, 121):
                now = day * DAY_S
                while dq2 and now - dq2[0] > 30 * DAY_S:
                    dq2.popleft()
                n_exact = len(dq2)
                n_ring = r2.count(now)
                n2 += 1
                over2 += n_ring > n_exact
                under2 += n_ring < n_exact
                dq2.append(now)
                r2.add(now)
            label = "with the guard bucket" if guard else "slots physical buckets"
            print(f"     {label:<24} traffic-shaped: over {over:>6,} under {under:>6,} of "
                  f"200,000   daily-cadence: over {over2:>3} under {under2:>3} of {n2}")
        print("       read the 'under' column: a ring with exactly `slots` physical buckets")
        print("       drops an attempt that is still inside the window, so the layer UNDER-")
        print("       counts - the direction a scheme penalty is assessed on. The guard bucket")
        print("       flips the error to the safe direction (over-count: it can refuse an")
        print("       attempt that was still legal, up to one bucket early).")
        print(f"     guard-bucket cost: {23.0/30:.2f} MB per 500k keys x 30-day window "
              f"(one byte per key, fixed layout)")
        burst = Counters()
        for _ in range(40):
            burst.ring("rule#day", ("card", "stress"), DAY_S, 24, "uint8").add(0)
        print(f"     saturation probe: 40 attempts in one day -> uint8 slot reads "
              f"{burst.day_count('rule', ('card', 'stress'), 0)}")
        print("       and the compiler refuses a config the counter cannot hold: limit 400")
        print("       with uint8 slots is rejected by SV5 (see examples/invalid/).")
        n_keys = 500_000
        ring_bytes = n_keys * (31 + 16)
        print(f"     memory at {n_keys:,} live keys x 30-slot uint8 ring (31 physical): "
              f"{ring_bytes/1e6:.1f} MB fixed layout, bounded by the KEY COUNT;")
        print(f"       a timestamps-in-a-list design grows with attempt volume instead "
              f"(>= {n_keys*468/1e6:.0f} MB at the same key count).")
        print()

    # ------------------------------------------------------------- [F1] the conflict census
    if section("F"):
        print("[F1] conflict census: what actually happens when nothing is legal?")
        for label, cs in (("northwind (default)", northwind), ("aurora (strict)", aurora)):
            counters = Counters()
            pre_seed(counters, ctxs, cs)
            _CONFLICT_CACHE.clear()
            st = eligible_stats(cs, ctxs, counters)
            routed = max(1, st["routed"] + st["waived"])
            print(f"     {label:<22} routed {pct(st['routed'], st['n']):6.2f}%   "
                  f"waived {pct(st['waived'], st['n']):5.2f}%   "
                  f"deferred {pct(st['deferred'], st['n']):5.2f}%   "
                  f"prohibited {pct(st['prohibited'], st['n']):5.2f}%   "
                  f"UNROUTABLE {pct(st['unroutable'], st['n']):5.2f}%")
            print(f"     {'':<22} mean arms/decision {st['arms_total']/routed:5.2f}   "
                  f"1-arm {pct(st['arms_hist'].get(1, 0), st['n']):5.2f}%   "
                  f"min arms {st['arms_min'] if st['arms_min'] < 10**9 else 0}")
            if st["waived"] == 0:
                pad = "     " + " " * 22
                print(pad + "waived 0.00% is structural, not a verdict on the ladder: this census")
                print(pad + "grants no escape budget, so the layer is fail-closed by construction.")
                print(pad + "[F1b] grants one; [F5] walks the rungs one at a time.")
            if st["conflict_sigs"]:
                for sig, cnt in sorted(st["conflict_sigs"].items(),
                                       key=lambda kv: -kv[1])[:4]:
                    print(f"       minimal conflict {sig:<52} {pct(cnt, st['n']):6.3f}%")
                print("       unroutable by currency: " + ", ".join(
                    f"{k} {pct(v, st['n']):.3f}%" for k, v in
                    sorted(st["zero_by_ccy"].items(), key=lambda kv: -kv[1])))
                print("       unroutable by region:   " + ", ".join(
                    f"{k} {pct(v, st['n']):.3f}%" for k, v in
                    sorted(st["zero_by_region"].items(), key=lambda kv: -kv[1])))
            if st["tx_denied"]:
                print("       transaction-rule trips: " + ", ".join(
                    f"{k}={pct(v, st['n']):.2f}%"
                    for k, v in sorted(st["tx_denied"].items(), key=lambda kv: -kv[1])))
            if st["denied_by"]:
                top = sorted(st["denied_by"].items(), key=lambda kv: -kv[1])[:6]
                print("       arm-denials by rule: " + ", ".join(
                    f"{k}={v:,}" for k, v in top))
        print()

        print("[F1b] the escape budget: bounded relaxation vs escalation (aurora)")
        print(f"     {'global budget':<16} {'waived':>9} {'UNROUTABLE':>11} "
              f"{'waived rules':>46}")
        for bps in (0, 10, 100, 200, 3000, 10000):
            counters = Counters()
            pre_seed(counters, ctxs, aurora)
            _CONFLICT_CACHE.clear()
            st = eligible_stats(aurora, ctxs, counters,
                                budget=EscapeBudget(bps, len(ctxs)))
            label = "unbounded" if bps == 10000 else f"{bps/100:.2f}%"
            rules = ", ".join(f"{k}={v:,}" for k, v in
                              sorted(st["waived_by"].items(), key=lambda kv: -kv[1])[:2]) or "-"
            print(f"     {label:<16} {pct(st['waived'], st['n']):>8.3f}% "
                  f"{pct(st['unroutable'], st['n']):>10.3f}% {rules:>46}")
        print("     -> the global budget and the per-rule bound in the document are two")
        print("        different promises: the first is the operator's appetite for")
        print("        exceptions, the second is what the rule's owner signed for. The")
        print("        ladder obeys both, and what is left escalates instead of routing")
        print("        around a rule it was told to obey.")
        print()

        # ------------------------------------------------------------------- [F2] domestic
        print("[F2] 'must-domestic-acquirer', read literally vs read as a region rule")
        variants = (("as shipped (card_country)", "card_country", False),
                    ("widen (card_country + region)", "card_country", True),
                    ("re-scope (card_region)", "card_region", False))
        rows = []
        for label, match, allow_region in variants:
            doc = json.loads(json.dumps(aurora.doc))
            for r in doc["mandate"]:
                if r["rule"] == "mandate.domestic_acquirer":
                    r["params"]["match"] = match
                    r["params"]["allow_same_region"] = allow_region
            doc["id"] = f"aurora.variant.{match}.{allow_region}"
            cs = ConstraintSet(doc, catalog)
            counters = Counters()
            pre_seed(counters, ctxs, cs)
            st = eligible_stats(cs, ctxs, counters, budget=EscapeBudget(0, len(ctxs)))
            kept = eea_n = 0
            for ctx in ctxs:
                if ctx.card_region != "EEA":
                    continue
                eea_n += 1
                arms, _d, _b, _i, _u = cs.evaluate_with_fallback(ctx, Counters())
                if arms and all(a.acq["acquirer_region"] == "EEA" for a in arms):
                    kept += 1
            print(f"     {label:<32} unroutable {pct(st['unroutable'], st['n']):6.3f}%   "
                  f"waived {pct(st['waived'], st['n']):6.3f}%   "
                  f"EEA volume entirely on EEA acquirers {pct(kept, eea_n):6.2f}%")
            rows.append((st["unroutable"] / st["n"], kept / max(1, eea_n)))
        print("     -> the literal reading is unroutable for most EEA cards: the fleet has four")
        print("        EEA acquirers in four countries, so 'the acquirer must be in the card's")
        print("        own country' cannot be satisfied for FR, IT, SE or PL cards. Widening to")
        print(f"        the region keeps the intent (an EEA acquirer: {rows[0][1]*100:.1f}% -> "
              f"{rows[1][1]*100:.1f}% of EEA")
        print(f"        volume) and removes {(rows[0][0]-rows[1][0])*100:.1f} pts of unroutable "
              f"traffic. The {rows[1][0]*100:.1f}%")
        print("        that remains is the OTHER conflict - floor 40 bps against a processor")
        print("        that earns 33 - which is F2b's job.")
        print()
        print("[F2b] what the census buys: three config edits, no code change")
        doc = json.loads(json.dumps(aurora.doc))
        for r in doc["econ"]:
            if r["rule"] == "econ.floor_margin":
                r["params"]["value_bps"] = 30
        for r in doc["mandate"]:
            if r["rule"] == "mandate.domestic_acquirer":
                r["params"]["match"] = "card_region"
                r["params"]["allow_same_region"] = True
        for r in doc["mandate"]:
            if r["rule"] == "mandate.must_process":
                r["params"]["if_unavailable"] = "fall_back"
        doc["id"] = "aurora.fixed"
        doc["revision"] = 4
        fixed = ConstraintSet(doc, catalog)
        for label, cs in (("rev 3 (as shipped)", aurora), ("rev 4 (floor 30bp + region rule + fallback)", fixed)):
            counters = Counters()
            pre_seed(counters, ctxs, cs)
            _CONFLICT_CACHE.clear()
            st = eligible_stats(cs, ctxs, counters)
            routed = max(1, st["routed"] + st["waived"])
            print(f"     {label:<20} routed {pct(st['routed'], st['n']):6.2f}%   "
                  f"deferred {pct(st['deferred'], st['n']):5.2f}%   "
                  f"prohibited {pct(st['prohibited'], st['n']):5.2f}%   "
                  f"UNROUTABLE {pct(st['unroutable'], st['n']):5.2f}%   "
                  f"mean arms {st['arms_total']/routed:4.2f}")
        print("     -> the residual is not a conflict: it is the velocity cap and the scheme")
        print("        ceiling doing their jobs. That distinction is the whole point of")
        print("        separating 'unroutable' from 'prohibited' in the census.")
        print()

        # ------------------------------------------------------------ [F3] regulatory effect
        print("[F3] what the regulatory rules actually remove (measured by waiving one rule)")
        sca_id = next(r["id"] for r in northwind.rules
                      if r["rule"] == "regulatory.sca_required")
        for mode in ("always", "unless_exempt_evidence"):
            doc = json.loads(json.dumps(northwind.doc))
            for r in doc["regulatory"]:
                if r["rule"] == "regulatory.sca_required":
                    r["params"]["mode"] = mode
            doc["id"] = f"northwind.sca.{mode}"
            cs = ConstraintSet(doc, catalog)
            with_arms = without_arms = changed = emptied = 0
            for ctx in ctxs:
                a_with = cs.evaluate(ctx, Counters())[0]
                a_without = cs.evaluate(ctx, Counters(), waived={sca_id})[0]
                with_arms += len(a_with)
                without_arms += len(a_without)
                changed += len(a_with) != len(a_without)
                emptied += bool(a_without) and not a_with
            print(f"     sca_required.mode={mode:<26} arms {without_arms/n:5.2f} (rule waived)"
                  f" -> {with_arms/n:5.2f}  changes the set on {pct(changed, n):5.2f}%"
                  f"  empties it on {pct(emptied, n):4.2f}%")
        res_id = next(r["id"] for r in northwind.rules
                      if r["rule"] == "regulatory.data_residency")
        with_arms = without_arms = changed = emptied = 0
        for ctx in ctxs:
            if ctx.card_region != "EEA":
                continue
            a_with = northwind.evaluate(ctx, Counters())[0]
            a_without = northwind.evaluate(ctx, Counters(), waived={res_id})[0]
            with_arms += len(a_with)
            without_arms += len(a_without)
            changed += len(a_with) != len(a_without)
            emptied += bool(a_without) and not a_with
        eea_n = sum(1 for c in ctxs if c.card_region == "EEA")
        print(f"     data_residency on EEA cards            arms {without_arms/eea_n:5.2f} "
              f"(rule waived) -> {with_arms/eea_n:5.2f}  changes the set on "
              f"{pct(changed, eea_n):5.2f}%  empties it on {pct(emptied, eea_n):4.2f}%")
        print("       (five of six acquirers are 3DS-capable, so the SCA rule costs arms only")
        print("        where a processor cannot do 3DS at all - and there it costs the whole")
        print("        arm, both flows, not one.)")
        print("     -> 'must-3DS' is an arm-count question for #7, not a request flag, and the")
        print("        eligible set is what #15's propensities are computed over.")
        print()

        # --------------------------------------------------------------- [F4] retry budgets
        print("[F4] retry budget arithmetic: 30-day window, one soft-declining card")
        print("     fees per the scheme fee schedules: 0.10 USD per excessive reattempt")
        print("     (domestic), and the ceiling is free until the 15th reattempt")
        print()
        print(f"     {'recovery schedule':<30} {'attempts':>8} {'fee-bearing':>11} "
              f"{'fee USD':>8} {'last day':>9}")
        rows = (
            ("daily, no cap (naive)", [1], None),
            ("daily + 15/30d cap (ours)", [1], 15),
            ("every 2 days", [2], None),
            ("weekly", [7], None),
            ("exponential 1,2,4,8,16d", [1, 2, 4, 8, 16], None),
        )
        for label, gaps, cap in rows:
            attempts, fee_bearing, fees, day = retry_schedule(gaps, cap=cap)
            print(f"     {label:<30} {attempts:>8} {fee_bearing:>11} {fees:>8.2f} "
                  f"{day:>9}")
        print("     -> 'add exponential backoff' is a statement about pacing, not compliance:")
        print("        it spends the free part of the budget slowest (5 of 15 attempts in 30")
        print("        days). The velocity cap and the cumulative ceiling answer different")
        print("        questions, so a merchant who implements one of them has implemented")
        print("        neither: 15/30d is the scheme's, 3/24h is the issuer's.")
        print()
        print("     in-flight accounting (why an unsettled attempt still holds its slot):")
        counters = Counters()
        counters.record_attempt(("card", "c1"), 0, aurora.counter_specs)
        counters.reserve(("card", "c1"), 60_000)          # attempt 1: in flight
        counters.record_attempt(("card", "c1"), 1, aurora.counter_specs)
        counters.reserve(("card", "c1"), 60_000)          # attempt 2: in flight
        settled = counters.count("auth-cap-3-per-day", ("card", "c1"), 30, DAY_S, 24, "uint8")
        live = counters.reserved(("card", "c1"), 30)
        print(f"       three attempts, none settled: recorded {settled} (provisional) + "
              f"{live} reserved = {settled+live} of 3 -> the fourth is DEFERRED, not declined")
        print(f"       at t=+120s both leases have expired: reserved "
              f"{counters.reserved(('card', 'c1'), 120_000)}, recorded still "
              f"{settled} -> a late authorization is what releases the slot for real")
        print(f"       at limit-1 the layer is deciding on a count that is an upper bound, and")
        print(f"       erring toward DEFERRING an attempt is the only safe direction: the")
        print(f"       alternative is a double charge (#10), which cost 45 c of ambiguity price")
        print(f"       per unresolved attempt in ADR-0002.")
        print()

    # ------------------------------------------------------------- [F5] the conflict ladder
    if section("F"):
        print("[F5] the ladder on one transaction (aurora; EEA card, USD, no domestic acquirer)")
        ctx = next(c for c in ctxs if c.card_region == "EEA" and c.currency == "USD")
        counters = Counters()
        for rung, waived in (("0. as shipped", set()),
                             ("1. + waive usd-goes-to-alpha", {"usd-goes-to-alpha"}),
                             ("2. + waive domestic-card-country",
                              {"usd-goes-to-alpha", "domestic-card-country"}),
                             ("3. + waive floor-margin-40",
                              {"usd-goes-to-alpha", "domestic-card-country",
                               "floor-margin-40"})):
            arms, _d, _b, _i = aurora.evaluate(ctx, counters, waived=waived)
            names = ", ".join(repr(a) for a in arms[:6]) + (" ..." if len(arms) > 6 else "")
            print(f"     {rung:<40} {len(arms):>2} arms  {names}")
        arms, denial, blocks, info = aurora.evaluate(ctx, counters)
        print(f"     minimal conflict set : {minimal_conflict(aurora, ctx, counters)}")
        print(f"     without the pin       : "
              f"{aurora.evaluate(ctx, counters, request_ref='txn_01J8ZW7M2P')[0] or 'still empty (demanded by the pin, blocked by floor)'}")
        print(f"     fail_mode            : {aurora.fail['empty_eligible_set']} "
              f"(the decision is parked, NOT declined: an unroutable request must not")
        print(f"                            update a posterior, ADR-0002 R10)")
        print()

    # ------------------------------------------------------------ [F6] precedence properties
    if section("F"):
        print("[F6] precedence properties")
        doc = json.loads(json.dumps(aurora.doc))
        doc["precedence"]["order"] = ["mandate", "econ", "budget"]
        permuted = ConstraintSet(doc, catalog)
        probe = ctxs[:20_000]
        same = 0
        for ctx in probe:
            a = [x.key() for x in aurora.evaluate(ctx, Counters())[0]]
            b = [x.key() for x in permuted.evaluate(ctx, Counters())[0]]
            same += a == b
        print(f"     permuting the merchant's precedence order: eligible set identical on "
              f"{pct(same, len(probe)):.2f}% of {len(probe):,} decisions")
        print("       precedence decides which rule is WAIVED when nothing is legal; it never")
        print("       decides what is legal, so it cannot be used to reorder policy by mood")
        # waive vs re-scope, and the day 'region' means two different things
        eea = [c for c in ctxs if c.card_region == "EEA"][:20_000]
        res_cs = ConstraintSet(re_scope(aurora.doc), catalog)
        base = {"eea-only-residency", "floor-margin-40"}   # waived in every row
        shipped_arms = waived_arms = rescoped_arms = 0
        for ctx in eea:
            a_s, _d, _b, _i = aurora.evaluate(ctx, Counters(), waived=set(base))
            a_w, _d1, _b1, _i1 = aurora.evaluate(
                ctx, Counters(), waived=base | {"domestic-card-country"})
            a_r, _d2, _b2, _i2 = res_cs.evaluate(ctx, Counters(), waived=set(base))
            shipped_arms += len(a_s)
            waived_arms += len(a_w)
            rescoped_arms += len(a_r)
        k = len(eea)
        print("     waive-vs-re-scope on the domestic rule, EEA traffic. Residency AND the")
        print("     margin floor are waived in every row, so the only variable is the domestic")
        print("     rule itself:")
        print(f"       as shipped (card_country) : {shipped_arms/k:5.2f} arms/decision")
        print(f"       waive the rule entirely   : {waived_arms/k:5.2f} arms/decision")
        print(f"       re-scope  (card_region)   : {rescoped_arms/k:5.2f} arms/decision")
        ship, wav, res = shipped_arms / k, waived_arms / k, rescoped_arms / k
        print("     -> three different worlds from one rule, and the middle one is the point:")
        print(f"        waiving recovers all {wav-ship:.2f} arms the domestic rule was holding")
        print(f"        back but stops constraining anything; re-scoping recovers {res-ship:.2f} of")
        print("        them and keeps a checkable regional guarantee. A ladder that can only")
        print("        waive has no middle rung, and an audit log that records 'relaxed'")
        print("        without saying WHICH of the two happened cannot tell them apart after")
        print("        the fact.")
        # the ambiguity: 'region' is geography (where the card is) or legality (which
        # acquirers may serve it). UK cards are the case where these differ.
        doc_uk = json.loads(json.dumps(aurora.doc))
        for r in doc_uk["mandate"]:
            if r["rule"] == "mandate.domestic_acquirer":
                r["when"] = {"field": "card_region", "op": "in", "value": ["EEA", "UK"]}
        doc_uk["id"] = "aurora.domestic-eea-uk"
        shipped = ConstraintSet(doc_uk, catalog)
        doc_uk2 = re_scope(doc_uk)
        reshipped = ConstraintSet(doc_uk2, catalog)
        # the fix the schema now has: the SAME rule head, with the region list it can carry
        doc_uk3 = json.loads(json.dumps(doc_uk))
        for r in doc_uk3["mandate"]:
            if r["rule"] == "mandate.domestic_acquirer":
                r["params"] = {"acquirer_regions": ["EEA", "UK"]}
        doc_uk3["id"] = "aurora.domestic-region-list"
        as_list = ConstraintSet(doc_uk3, catalog)
        uk = [c for c in ctxs if c.card_region == "UK"][:20_000]
        rows = (("as shipped (card_country)", shipped, set()),
                ("waived", shipped, {"domestic-card-country"}),
                ("re-scoped (card_region)", reshipped, set()),
                ("expressed as a region LIST", as_list, set()))
        print("     the UK case, where 'region' stops meaning one thing:")
        for label, cs, waived in rows:
            total = sum(len(cs.evaluate(ctx, Counters(), waived=waived)[0]) for ctx in uk)
            print(f"       {label:<28} {total/len(uk):5.2f} arms/decision")
        print("     -> for a UK card, neither 'same country' nor 'same geographic region' is")
        print("        satisfiable: no acquirer is domiciled in GB, and 'UK' is not an acquirer")
        print("        region in the catalog, so both readings of 'domestic' leave the set")
        print("        empty. What the merchant actually means is a REGULATORY region, which")
        print("        `acquirer_regions` expresses with the same shape and the same catalog")
        print("        field the residency rule reads. Same rule head, same audit story, one")
        print("        param - which is why the schema grew that param instead of the ladder")
        print("        growing a waiver the compliance team never signed.")
        print()

    # --------------------------------------------------------------------- [PV] properties
    if section("PV"):
        print("[PV] property tests")
        cs = northwind
        doc2 = json.loads(json.dumps(cs.doc))
        doc2["mandate"].append({"id": "extra", "rule": "mandate.exclude_processor",
                               "rationale": "test-only extra constraint",
                               "params": {"processors": ["delta"]}})
        stricter = ConstraintSet(doc2, catalog)
        violations = 0
        for ctx in ctxs[:5_000]:
            full = {a.key() for a in cs.evaluate(ctx, Counters())[0]}
            extra = {a.key() for a in stricter.evaluate(ctx, Counters())[0]}
            if not extra <= full:
                violations += 1
        print(f"     P1 monotone composition: adding a constraint never ADDS an arm - "
              f"{violations} violations / 5,000 decisions")
        h1 = [canonical_hash([[a.key() for a in cs.evaluate(ctx, Counters())[0]],
                              sorted(cs.evaluate(ctx, Counters())[1].items())])[:16]
              for ctx in ctxs[:2_000]]
        h2 = [canonical_hash([[a.key() for a in cs.evaluate(ctx, Counters())[0]],
                              sorted(cs.evaluate(ctx, Counters())[1].items())])[:16]
              for ctx in ctxs[:2_000]]
        print(f"     P2 determinism: verdicts and reason codes identical on replay: {h1 == h2}")

        doc3 = json.loads(json.dumps(northwind.doc))
        for r in doc3["mandate"]:
            if r["rule"] == "mandate.must_process":
                r["params"]["if_unavailable"] = "fall_back"
        fallback_cs = ConstraintSet(doc3, catalog)
        probe_n = refused_by_perarm = diverged = 0
        for ctx in ctxs[:20_000]:
            if ctx.bin_class != "corporate":
                continue
            probe_n += 1
            opts = fallback_cs.evaluate_with_fallback(ctx, Counters())
            set_arms = {a.key() for a in opts[0]}
            per_arm = {a.key() for a in fallback_cs.all_arms
                       if fallback_cs.evaluate(ctx, Counters(), arm_subset=[a])[0]}
            if set_arms and not per_arm:
                refused_by_perarm += 1
            if set_arms != per_arm:
                diverged += 1
        print(f"     P3 a per-arm guard is NOT a filter: over {probe_n:,} corporate-card")
        print(f"        decisions where `if_unavailable: fall_back` fires, a guard that asks")
        print(f"        'does THIS arm satisfy the mandate?' refuses the transaction entirely")
        print(f"        on {pct(refused_by_perarm, probe_n):4.1f}% of them, while the set filter "
              f"routes it.")
        print(f"        Divergence between the two guards: {pct(diverged, probe_n):.1f}% of "
              f"decisions.")
        print("        -> the guard must re-run the SAME evaluator over the whole set, not a")
        print("           per-arm copy of the rule. Both must read one code path, or the")
        print("           filter and the seal disagree exactly where money is.")
        empties = {}
        for ctx in ctxs[:5_000]:
            _a, _d, _b, info = aurora.evaluate(ctx, Counters())
            if info["emptied_by"]:
                head = info["emptied_by"].split(":")[0]
                empties[head] = empties.get(head, 0) + 1
        by_id = {r["id"]: r for r in aurora.rules}
        illegal = {k: v for k, v in empties.items()
                   if by_id.get(k, {}).get("scope") not in ("candidate", "transaction")}
        print(f"     P4 only candidate/transaction rules can empty the set: "
              f"{sum(empties.values()):,} empty-set decisions across "
              f"{len(empties)} rule(s), {len(illegal)} violation(s)")
        print(f"        (the schema fixes each head's enforcement point; a rule that merely "
              f"orders")
        print(f"         or defers cannot refuse the whole set - SV12 is what keeps that "
              f"true)")
        bad = sum(1 for r in aurora.rules
                  if r["rule"].split(".")[0] in ("regulatory", "network") and "relaxable" in r)
        print(f"     P5 platform rules declaring a relaxation: {bad} "
              f"(schema-level prohibition, not a convention)")
        print(f"     P6 unknown rule names / context fields: rejected at compile time "
              f"(the negative fixtures in")
        print(f"        examples/invalid/ are run by the gate, each asserted to fail for the "
              f"reason it names)")
        print()

    # -------------------------------------------------------------------------- [A] audit
    if section("A"):
        print("[A] audit record, strict denial (aurora; EEA card, USD)")
        ctx = next(c for c in ctxs if c.card_region == "EEA" and c.currency == "USD")
        arms, denial, blocks, _i = aurora.evaluate(ctx, Counters())
        rec = audit_record(aurora, ctx, arms, denial, blocks,
                           conflict=minimal_conflict(aurora, ctx, Counters()))
        print(json.dumps(rec, indent=2, sort_keys=True))
        print()
        print("[A2] audit record, normal decision (northwind)")
        ctx = ctxs[7]
        arms, denial, blocks, _i, _u = northwind.evaluate_with_fallback(ctx, Counters())
        arms, _t = northwind.preference_tier(ctx, arms)
        print(json.dumps(audit_record(northwind, ctx, arms, denial, blocks), indent=2,
                         sort_keys=True))
        print()

    print("=" * 100)
    return 0


def re_scope(doc: dict) -> dict:
    doc = json.loads(json.dumps(doc))
    doc["id"] = "aurora.rescoped"
    for r in doc["mandate"]:
        if r["rule"] == "mandate.domestic_acquirer":
            r["params"]["match"] = "card_region"
            r["params"]["allow_same_region"] = True
    return doc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
