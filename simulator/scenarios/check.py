#!/usr/bin/env python3
"""Scenario document checks (ADR-0005).

The scenario schema in `schema/scenario.schema.json` is the canonical artifact: a
simulation run is *data*, and this file is the gate that decides whether a document is
allowed to become one. It does three jobs JSON Schema cannot do alone:

  1. validates the document against the schema (the subset the schema actually uses,
     stdlib only, no dependencies, unknown keywords fail loudly);
  2. runs the semantic checks SV1-SV10 -- the things a schema cannot see, such as
     "two outages on the same acquirer may not overlap" and "the catalog hash must match
     the catalog the fleet is described against";
  3. resolves `extends` and produces the CANONICAL BYTES and content hash of the resolved
     document, which is the identifier a benchmark number is cited against.

Canonical form is the same function the constraint layer already uses for a
ConstraintSet's `policy.catalog_hash`: `json.dumps(doc, sort_keys=True,
separators=(",", ":"))` -> sha256. One canonical form in the project is worth more than
two good ones.

    python3 simulator/scenarios/check.py                 # validate examples + negatives
    python3 simulator/scenarios/check.py --json          # machine-readable verdict (CI)
    python3 simulator/scenarios/check.py --resolve ID    # print the resolved document
    python3 simulator/scenarios/check.py --hash ID       # print the content hash
    python3 simulator/scenarios/check.py --pin           # re-pin golden/scenario-hashes.json

Nothing here is the simulator. It is the thing that decides whether a document is allowed
to drive one.

Importable surface used by the spikes:

    from check import load_scenario, resolve, canonical_bytes, scenario_hash, validate
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent.parent
SCHEMA_PATH = ROOT / "schema" / "scenario.schema.json"
EXAMPLES_DIR = ROOT / "examples"
INVALID_DIR = EXAMPLES_DIR / "invalid"
GOLDEN_PATH = ROOT / "golden" / "scenario-hashes.json"

# Annotation-only keywords are permitted and ignored (they document a default for the
# author; the harness reads the resolved document, so a default that is not written down
# does not exist). Everything else must be understood or the gate fails loudly.
SUPPORTED = {
    "$schema", "$id", "title", "description", "default", "examples", "$defs", "$ref",
    "type", "required",
    "properties", "additionalProperties", "enum", "const", "oneOf", "items",
    "minItems", "maxItems", "minProperties", "maxProperties", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "pattern",
}

TYPE_OF = {
    "object": dict, "array": list, "string": str,
    "integer": int, "number": (int, float), "boolean": bool, "null": type(None),
}

# --- closed vocabularies the scenario may draw keys from (SV7) ------------------------
BIN_CLASSES = ("consumer_credit", "consumer_debit", "premium_credit", "corporate", "prepaid")
REGIONS = ("EEA", "UK", "US", "LATAM", "APAC")
MERCHANT_CATEGORIES = ("digital_goods", "travel", "retail", "marketplace", "gaming")
ROUTE_CLASSES = ("oneoff_cnp", "recurring_mit", "installment", "card_on_file")
ENTRY_MODES = ("ecommerce", "moto", "recurring", "wallet")
MIX_FIELDS = {
    "bin_class": BIN_CLASSES,
    "region": REGIONS,
    "merchant_category": MERCHANT_CATEGORIES,
    "route_class": ROUTE_CLASSES,
    "entry_mode": ENTRY_MODES,
}

# --- decline-code catalog: retryability is a SCHEME FACT, not a scenario parameter ----
# (class: 'soft' = retryable on another route, 'hard' = never retry this card.)
DECLINE_CATALOG = {
    "iso8583": {
        "51": ("insufficient_funds", "soft"),
        "61": ("exceeds_withdrawal_limit", "soft"),
        "65": ("activity_limit_exceeded", "soft"),
        "91": ("issuer_unavailable", "soft"),
        "96": ("system_error", "soft"),
        "05": ("do_not_honour", "soft"),
        "12": ("invalid_transaction", "soft"),
        "01": ("refer_to_issuer", "hard"),
        "14": ("invalid_card_number", "hard"),
        "41": ("lost_card", "hard"),
        "43": ("stolen_card", "hard"),
        "54": ("expired_card", "hard"),
        "57": ("transaction_not_permitted", "hard"),
        "59": ("suspected_fraud", "hard"),
        "62": ("restricted_card", "hard"),
        "N7": ("cvv2_failure", "hard"),
        "R1": ("stop_payment_order", "hard"),
    },
    "nacha": {
        "R01": ("insufficient_funds", "soft"),
        "R02": ("account_closed", "hard"),
        "R03": ("no_account_unable_to_locate", "hard"),
        "R04": ("invalid_account_number", "hard"),
        "R05": ("unauthorized_debit_corporate", "hard"),
        "R07": ("authorization_revoked", "hard"),
        "R08": ("payment_stopped", "hard"),
        "R09": ("uncollected_funds", "soft"),
        "R10": ("customer_advises_not_authorized", "hard"),
        "R12": ("account_sold_to_another_dfi", "hard"),
        "R13": ("invalid_ach_routing_number", "hard"),
        "R16": ("account_frozen", "soft"),
        "R29": ("corporate_customer_advises_not_authorized", "hard"),
    },
}


# ------------------------------------------------------- the deterministic stream ---
# ADR-0005 §4. This is the NORMATIVE definition of the harness's randomness, in ~25 lines,
# so that a Go implementation can be checked against it rather than against another
# Python file. It is 64-bit integer arithmetic with no library, no floating-point state and
# no platform dependence: the same (seed, keys, index) yields the same double everywhere.
#
#   stream(seed, k1, k2, ...) = mix64-fold over the keys, starting from the seed
#   u(stream, index)          = float64(mix64(stream + index * GOLDEN) >> 11) * 2**-53
#
# Strings are folded in as FNV-1a/64 of their UTF-8 bytes; integers are folded in offset by
# the golden ratio, so that key 1 and key "1" can never collide.

M64 = (1 << 64) - 1
GOLDEN = 0x9E3779B97F4A7C15
FNV_OFFSET = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3
STREAM_ALGORITHM = "splitmix64-indexed-v1"

# The domain tags. A stream key always begins with one, so no two uses of the same integer
# can ever share a stream.
DOMAINS = ("arr", "ctx", "att", "fac")


def mix64(z: int) -> int:
    """splitmix64 finaliser (Stafford variant 13). Three xors, two multiplies, one shift."""
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & M64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & M64
    return (z ^ (z >> 31)) & M64


def fnv1a64(text: str) -> int:
    h = FNV_OFFSET
    for b in text.encode("utf-8"):
        h = ((h ^ b) * FNV_PRIME) & M64
    return h


def stream(seed: int, *keys) -> int:
    h = seed & M64
    for k in keys:
        if isinstance(k, str):
            kk = fnv1a64(k)
        elif isinstance(k, int) and not isinstance(k, bool):
            kk = (k + GOLDEN) & M64
        else:
            raise TypeError(f"stream keys are str or int, got {type(k).__name__}")
        h = mix64(h ^ kk)
    return h


def draw(stream_seed_value: int, index: int) -> float:
    """One uniform in [0, 1), addressed by index. 53 bits of the mixed output."""
    if index < 0:
        raise ValueError("draw index must be non-negative")
    return (mix64((stream_seed_value + (index * GOLDEN & M64)) & M64) >> 11) \
        * (1.0 / 9007199254740992.0)


def check_stream_vectors(path: Path):
    """Verify the committed golden vectors. Returns a list of error strings."""
    if not path.exists():
        return [f"stream vectors missing: {path.name}"]
    doc = json.loads(path.read_text(encoding="utf-8"))
    errors = []
    if doc.get("algorithm") != STREAM_ALGORITHM:
        errors.append(f"stream vectors declare algorithm {doc.get('algorithm')!r}, "
                      f"expected {STREAM_ALGORITHM!r}")
    for i, v in enumerate(doc.get("vectors", [])):
        got = draw(stream(v["seed"], *v["keys"]), v["index"])
        if abs(got - v["u"]) > 1e-15:
            errors.append(f"stream vector {i} ({v['keys']} index {v['index']}): "
                          f"got {got!r}, committed {v['u']!r}")
    if len(doc.get("vectors", [])) < 12:
        errors.append("stream vectors: fewer than 12 vectors is not a conformance fixture")
    return errors


# ----------------------------------------------------------------------------- schema -
class SchemaError(Exception):
    pass


def _validate(node, schema, path, root, errors):
    for kw in schema:
        if kw not in SUPPORTED:
            raise SchemaError(f"unsupported keyword {kw!r} in schema at {path or '/'}")
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/$defs/"):
            raise SchemaError(f"only local $defs refs are supported: {ref}")
        target = root["$defs"][ref.split("/")[-1]]
        _validate(node, target, path, root, errors)
        return
    if "type" in schema:
        want = schema["type"]
        ok = isinstance(node, TYPE_OF[want]) and not (
            want == "number" and isinstance(node, bool)
        )
        if want == "integer" and isinstance(node, bool):
            ok = False
        if want == "integer" and isinstance(node, float) and node.is_integer():
            ok = True
        if not ok:
            errors.append(f"{path or '/'}: expected {want}, got {type(node).__name__}")
            return
    if "const" in schema and node != schema["const"]:
        errors.append(f"{path or '/'}: expected const {schema['const']!r}, got {node!r}")
    if "enum" in schema and node not in schema["enum"]:
        errors.append(f"{path or '/'}: {node!r} not in {schema['enum']}")
    if isinstance(node, str):
        if "minLength" in schema and len(node) < schema["minLength"]:
            errors.append(f"{path or '/'}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(node) > schema["maxLength"]:
            errors.append(f"{path or '/'}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], node):
            errors.append(f"{path or '/'}: {node!r} does not match {schema['pattern']}")
    if isinstance(node, (int, float)) and not isinstance(node, bool):
        for kw, cmp, word in (
            ("minimum", lambda v: node < v, "below minimum"),
            ("exclusiveMinimum", lambda v: node <= v, "at or below exclusiveMinimum"),
            ("maximum", lambda v: node > v, "above maximum"),
            ("exclusiveMaximum", lambda v: node >= v, "at or above exclusiveMaximum"),
        ):
            if kw in schema and cmp(schema[kw]):
                errors.append(f"{path or '/'}: {node} is {word} {schema[kw]}")
    if isinstance(node, list):
        if "minItems" in schema and len(node) < schema["minItems"]:
            errors.append(f"{path or '/'}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(node) > schema["maxItems"]:
            errors.append(f"{path or '/'}: more than {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(node):
                _validate(item, schema["items"], f"{path}[{i}]", root, errors)
    if isinstance(node, dict):
        for req in schema.get("required", []):
            if req not in node:
                errors.append(f"{path or '/'}: missing required property {req!r}")
        props = schema.get("properties", {})
        addl = schema.get("additionalProperties", True)
        for key, value in node.items():
            if key in props:
                _validate(value, props[key], f"{path}.{key}" if path else key, root, errors)
            elif addl is False:
                errors.append(f"{path or '/'}: additional property {key!r} is not allowed")
            elif isinstance(addl, dict):
                _validate(value, addl, f"{path}.{key}" if path else key, root, errors)
        if "minProperties" in schema and len(node) < schema["minProperties"]:
            errors.append(f"{path or '/'}: fewer than {schema['minProperties']} properties")
        if "maxProperties" in schema and len(node) > schema["maxProperties"]:
            errors.append(f"{path or '/'}: more than {schema['maxProperties']} properties")
    if "oneOf" in schema:
        branch_errors = []
        for branch in schema["oneOf"]:
            sub = []
            _validate(node, branch, path, root, sub)
            branch_errors.append(sub)
        if all(branch_errors):
            best = min(branch_errors, key=len)
            errors.append(
                f"{path or '/'}: matches no branch of oneOf; closest branch says: "
                + "; ".join(best[:4])
            )
        elif sum(1 for b in branch_errors if not b) > 1:
            errors.append(f"{path or '/'}: matches more than one branch of oneOf")


# --------------------------------------------------------------------------- canonical -
def canonical_bytes(doc) -> bytes:
    """The canonical serialisation. Identical to the constraint layer's canonical form."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")


def scenario_hash(doc) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(doc)).hexdigest()


# ------------------------------------------------------------------------------ loading -
def load_raw(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def deep_merge(base, over):
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def resolve(doc, examples_dir: Path = EXAMPLES_DIR, _seen=()):
    """Resolve `extends` chains. The resolved document has no `extends` key: that is the
    document that gets hashed, so two ways of writing the same world hash the same."""
    if "extends" not in doc:
        return dict(doc), []
    parent_id = doc["extends"]
    if parent_id in _seen:
        return dict(doc), [f"SV2: extends cycle involving {parent_id!r}"]
    parent_path = examples_dir / f"{parent_id}.json"
    if not parent_path.exists():
        return dict(doc), [f"SV2: extends {parent_id!r} but {parent_path.name} does not exist"]
    parent_raw = load_raw(parent_path)
    parent, errs = resolve(parent_raw, examples_dir, _seen + (parent_id,))
    if errs:
        return dict(doc), errs
    over = {k: v for k, v in doc.items() if k != "extends"}
    return deep_merge(parent, over), []


def load_scenario(path: Path):
    """Load, resolve, validate. Returns (resolved_doc, errors).

    `extends` always resolves against the examples ROOT, not the file's own directory:
    a negative fixture in examples/invalid/ extends a valid example, and the parent of an
    invalid document is never another invalid document.
    """
    raw = load_raw(path)
    resolved, errs = resolve(raw, EXAMPLES_DIR)
    return resolved, errs + validate(resolved)


# ---------------------------------------------------------------------------- semantics -
def find_catalog(catalog_ref: str):
    p = (REPO_ROOT / catalog_ref)
    return p if p.exists() else None


def _mix_weights_errors(mix, errors):
    for field, allowed in MIX_FIELDS.items():
        weights = mix.get(field)
        if weights is None:
            continue
        for key in weights:
            if key not in allowed:
                errors.append(
                    f"SV7: context_mix.{field} has unknown key {key!r} "
                    f"(closed vocabulary: {', '.join(allowed)})"
                )
        if not any(v > 0 for v in weights.values()):
            errors.append(f"SV7: context_mix.{field} has no positive weight")


def validate(doc):
    """Schema validation + semantic checks SV1-SV10. Returns a list of error strings."""
    errors = []
    schema = load_raw(SCHEMA_PATH)
    _validate(doc, schema, "", schema, errors)

    # SV1 - identity: an id, a model contract, an integer seed.
    if not doc.get("id"):
        errors.append("SV1: id is required")
    if doc.get("model_version") not in ("fleet-v1",):
        errors.append("SV1: unknown model_version (a benchmark is only comparable inside one)")
    seed = doc.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 1:
        errors.append("SV1: seed must be a positive integer (derivation is integer arithmetic)")

    # SV2 - extends: stripped by the resolver; a child may not move the world's identity.
    if "extends" in doc:
        errors.append("SV2: document still carries `extends` after resolution")

    clock = doc.get("clock", {})
    duration_s = clock.get("duration_s", 0)

    # SV3 - the fleet and the catalog it is described against move together.
    fleet = doc.get("fleet", {})
    acquirers = fleet.get("acquirers", {})
    declared = fleet.get("catalog_hash")
    if isinstance(declared, str) and not re.fullmatch(r"sha256:[0-9a-f]{64}", declared):
        errors.append("SV3: catalog_hash must be sha256:<64 hex>")
    catalog_path = find_catalog(fleet.get("catalog", "")) if fleet.get("catalog") else None
    if fleet.get("catalog") and catalog_path is None:
        errors.append(f"SV3: catalog {fleet['catalog']!r} not found in the repo")
    catalog_ids = set()
    if catalog_path is not None:
        catalog = load_raw(catalog_path)
        catalog_ids = {a["id"] for a in catalog.get("acquirers", [])}
        actual = "sha256:" + hashlib.sha256(canonical_bytes(catalog)).hexdigest()
        if declared != actual:
            errors.append(
                f"SV3: catalog_hash {declared} != hash of {fleet['catalog']} ({actual}). "
                "The fleet and the catalog it was written for move together."
            )
    if catalog_ids:
        for acq_id in acquirers:
            if acq_id not in catalog_ids:
                errors.append(f"SV3: acquirer {acq_id!r} is not in the referenced catalog")

    # SV4 - decline mix: codes exist in the scheme's catalog; retryability is not ours to say.
    for acq_id, acq in acquirers.items():
        mix = acq.get("decline_mix") if isinstance(acq, dict) else None
        if not mix:
            continue
        scheme = mix.get("scheme")
        if scheme not in DECLINE_CATALOG:
            continue
        for code in mix.get("weights", {}):
            if code not in DECLINE_CATALOG[scheme]:
                errors.append(
                    f"SV4: {acq_id}: decline code {code!r} is not in the {scheme} catalog. "
                    "A scenario invents frequencies, never codes."
                )
        if not mix.get("weights"):
            errors.append(f"SV4: {acq_id}: decline_mix.weights is empty")

    # SV5 - latency quantiles are monotone and the tail index is in a sane range.
    for acq_id, acq in acquirers.items():
        lat = acq.get("latency") if isinstance(acq, dict) else None
        if not lat:
            continue
        p50, p95, p99 = lat.get("p50_ms"), lat.get("p95_ms"), lat.get("p99_ms")
        floor = lat.get("floor_ms")
        if None not in (p50, p95, p99) and not (p50 <= p95 <= p99):
            errors.append(f"SV5: {acq_id}: latency quantiles must satisfy p50 <= p95 <= p99")
        if p50 is not None and floor is not None and floor >= p50:
            errors.append(f"SV5: {acq_id}: floor_ms must be below p50_ms")
        xi = lat.get("tail_index", 0.25)
        if not (0.05 <= xi <= 0.6):
            errors.append(f"SV5: {acq_id}: tail_index {xi} outside [0.05, 0.6]")

    # SV6 - events: inside the run, on a real acquirer, non-overlapping outages, and a
    # recovery that follows something.
    outages = {}
    degraded = {}
    for i, ev in enumerate(doc.get("events", []) or []):
        if not isinstance(ev, dict):
            continue
        kind = ev.get("type")
        at_s = ev.get("at_s", 0)
        target = ev.get("target")
        if isinstance(at_s, int) and at_s > duration_s:
            errors.append(f"SV6: events[{i}] at_s={at_s} is past the run's duration ({duration_s}s)")
        if target is not None and acquirers and target not in acquirers:
            errors.append(f"SV6: events[{i}] targets {target!r}, which is not in the fleet")
        if kind == "outage":
            span = (at_s, at_s + ev.get("duration_s", 0))
            for prev in outages.get(target, []):
                if span[0] < prev[1] and prev[0] < span[1]:
                    errors.append(
                        f"SV6: events[{i}] outage on {target!r} overlaps an earlier outage "
                        f"[{prev[0]}, {prev[1]}) -- an acquirer cannot go down twice"
                    )
            outages.setdefault(target, []).append(span)
            degraded.setdefault(target, []).append(at_s + ev.get("duration_s", 0))
        elif kind == "gradual_overload":
            degraded.setdefault(target, []).append(at_s + ev.get("ramp_s", 0))
        elif kind == "recovery":
            curve = ev.get("curve")
            if curve == "exponential" and not ev.get("tau_s"):
                errors.append(f"SV6: events[{i}] exponential recovery needs tau_s")
            if curve == "linear" and not ev.get("duration_s"):
                errors.append(f"SV6: events[{i}] linear recovery needs duration_s")
            if curve in ("exponential", "linear") and not (
                "auth_multiplier_from" in ev or "latency_multiplier_from" in ev
            ):
                errors.append(
                    f"SV6: events[{i}] {curve} recovery states no residual "
                    "(auth_multiplier_from / latency_multiplier_from): that is a step "
                    "recovery wearing a curve, and it makes a benchmark optimistic"
                )
            if target not in degraded or ev.get("at_s", 0) < min(degraded[target]):
                errors.append(
                    f"SV6: events[{i}] recovery on {target!r} does not follow a degradation"
                )
        elif kind == "fleet_shock":
            factors = {f.get("id") for f in fleet.get("correlation", {}).get("factors", [])}
            if ev.get("factor_id") not in factors:
                errors.append(
                    f"SV6: events[{i}] fleet_shock names factor {ev.get('factor_id')!r}, "
                    "which the fleet does not declare"
                )

    # SV7 - traffic: closed vocabularies, a sane amount model.
    source = doc.get("source", {})
    traffic = source.get("traffic") or {}
    if traffic:
        _mix_weights_errors(traffic.get("context_mix", {}), errors)
        amount = traffic.get("context_mix", {}).get("amount", {})
        if amount.get("min_minor", 1) >= amount.get("max_minor", 2):
            errors.append("SV7: amount.min_minor must be below amount.max_minor")
        if amount.get("median_minor") and not (
            amount["min_minor"] <= amount["median_minor"] <= amount["max_minor"]
        ):
            errors.append("SV7: amount.median_minor must lie between min and max")

    # SV8 - correlation: a factor with one member is a typo; members must exist.
    for f in fleet.get("correlation", {}).get("factors", []) or []:
        if len(set(f.get("acquirers", []))) < 2:
            errors.append(f"SV8: factor {f.get('id')!r} has fewer than two distinct members")
        for member in f.get("acquirers", []):
            if acquirers and member not in acquirers:
                errors.append(f"SV8: factor {f.get('id')!r} names unknown acquirer {member!r}")

    # SV9 - recording / portability: a document may not depend on where it is run.
    recording = doc.get("recording", {})
    if source.get("kind") == "trace" and recording.get("mode") != "all_arms":
        errors.append(
            "SV9: a trace replay must record all_arms. Replaying reality through a "
            "chosen_only recorder cannot answer a counterfactual (#15)."
        )
    for key in ("trace_path",):
        value = recording.get(key) or ""
        if value.startswith("/") or ".." in value:
            errors.append(f"SV9: recording.{key} must be repo-relative ({value!r})")
    trace = source.get("trace") or {}
    if trace.get("path") and (trace["path"].startswith("/") or ".." in trace["path"]):
        errors.append(f"SV9: source.trace.path must be repo-relative ({trace['path']!r})")

    # SV10 - nothing in the document may read the environment.
    text = canonical_bytes(doc).decode("utf-8")
    for banned, why in (
        ("$ENV", "environment substitution"),
        ("file://", "absolute URI"),
        ("${", "shell-style interpolation"),
    ):
        if banned in text:
            errors.append(f"SV10: document contains {banned!r} ({why} makes a run unreproducible)")
    return errors


# --------------------------------------------------------------------------------- CLI -
EXPECTED_INVALID = {
    "invalid-decline-code-unknown.json": "not in the iso8583 catalog",
    "invalid-outage-overlap.json": "overlaps an earlier outage",
    "invalid-latency-nonmonotonic.json": "p50 <= p95 <= p99",
    "invalid-replay-chosen-only.json": "must record all_arms",
}


def main(argv):
    args = [a for a in argv[1:]]
    as_json = "--json" in args
    schema = load_raw(SCHEMA_PATH)
    _validate(schema, schema, "", schema, [])  # the schema must validate its own shape

    if "--pin" in args:
        # Re-pin the golden hashes deliberately. This is a reviewable diff: the golden
        # file is committed, so "the world changed" is a line in a pull request and not
        # a silent drift in the benchmark baseline.
        pinned = {}
        for path in sorted(EXAMPLES_DIR.glob("*.json")):
            doc, errs = load_scenario(path)
            if errs:
                print(f"refusing to pin {path.name}: {errs[0]}", file=sys.stderr)
                return 1
            pinned[doc["id"]] = scenario_hash(doc)
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(json.dumps(pinned, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8")
        print(f"pinned {len(pinned)} scenario hashes to {GOLDEN_PATH.relative_to(REPO_ROOT)}")
        return 0

    if "--resolve" in args:
        doc_id = args[args.index("--resolve") + 1]
        doc, errs = load_scenario(EXAMPLES_DIR / f"{doc_id}.json")
        print(json.dumps(doc, indent=2, sort_keys=True))
        return 1 if errs else 0
    if "--hash" in args:
        doc_id = args[args.index("--hash") + 1]
        doc, errs = load_scenario(EXAMPLES_DIR / f"{doc_id}.json")
        if errs:
            print("\n".join(errs), file=sys.stderr)
            return 1
        print(scenario_hash(doc))
        return 0

    vec_errors = check_stream_vectors(GOLDEN_PATH.parent / "stream-vectors.json")
    results = [{
        "file": "golden/stream-vectors.json",
        "id": STREAM_ALGORITHM,
        "expect": "accept (conformance fixture for the determinism contract)",
        "ok": not vec_errors,
        "errors": vec_errors,
        "hash": None,
        "lines": 0,
        "resolved_lines": 0,
    }]
    paths = sorted(EXAMPLES_DIR.glob("*.json"))
    for path in paths:
        doc, errs = load_scenario(path)
        results.append({
            "file": f"examples/{path.name}",
            "id": doc.get("id", "?"),
            "expect": "accept",
            "ok": not errs,
            "errors": errs,
            "hash": scenario_hash(doc) if not errs else None,
            "lines": len(path.read_text(encoding="utf-8").splitlines()),
            "resolved_lines": len(json.dumps(doc, indent=2, sort_keys=True).splitlines()),
        })
    for path in sorted(INVALID_DIR.glob("*.json")):
        doc, errs = load_scenario(path)
        expect = EXPECTED_INVALID.get(path.name, "")
        ok = bool(errs) and any(expect in e for e in errs) if expect else bool(errs)
        results.append({
            "file": f"examples/invalid/{path.name}",
            "id": doc.get("id", "?"),
            "expect": f"reject ({expect})" if expect else "reject",
            "ok": ok,
            "errors": errs,
            "hash": None,
            "lines": len(path.read_text(encoding="utf-8").splitlines()),
            "resolved_lines": 0,
        })

    goldens = {}
    if GOLDEN_PATH.exists():
        goldens = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    for r in results:
        if r["hash"] and r["id"] in goldens and goldens[r["id"]] != r["hash"]:
            r["ok"] = False
            r["errors"].append(
                f"SV0: content hash {r['hash']} != committed golden {goldens[r['id']]}. "
                "Either the document changed on purpose (re-pin) or the canonical form moved."
            )

    if as_json:
        print(json.dumps({"ok": all(r["ok"] for r in results), "results": results}, indent=2))
        return 0 if all(r["ok"] for r in results) else 1

    print("=" * 88)
    print("scenario document gate (ADR-0005) -- schema v1.0.0, checks SV1-SV10")
    print("=" * 88)
    width = max(len(r["file"]) for r in results)
    for r in results:
        verdict = "ok  " if r["ok"] else "FAIL"
        print(f"  {verdict}  {r['file']:<{width}}  {r['expect']}")
        for e in r["errors"][:6]:
            print(f"          - {e}")
    print()
    for r in results:
        if r["hash"]:
            print(f"  {r['id']:<38} {r['hash']}   ({r['lines']} lines -> "
                  f"{r['resolved_lines']} resolved)")
    print()
    bad = [r for r in results if not r["ok"]]
    print(f"  {len(results) - len(bad)}/{len(results)} documents pass")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
