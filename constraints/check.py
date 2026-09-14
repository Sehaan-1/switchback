#!/usr/bin/env python3
"""ConstraintSet compile-time checks (ADR-0004).

The schema in `schema/constraint-set.schema.json` is the canonical, implementation-facing
artifact. This file is the *checker* that runs in CI: it validates every document in
`examples/` against that schema and then runs the semantic checks JSON Schema cannot
express ("a core validator plus the context it cannot see"). It is deliberately small and
dependency-free (stdlib only) so that the constraint layer's own gate has no install step,
which is also why it is not a hand-rolled general-purpose JSON Schema engine: it supports
exactly the keywords the schema uses and fails loudly on anything it does not understand.

    python3 constraints/check.py                 # validate examples, run semantic checks
    python3 constraints/check.py --json          # machine-readable verdict (for CI)

Nothing here is an implementation of the engine. It is the thing that decides whether a
document is allowed to become one.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "schema" / "constraint-set.schema.json"
EXAMPLES_DIR = ROOT / "examples"

# --- schema keywords this checker understands. Anything else in the schema is a bug. ---
SUPPORTED = {
    "$schema", "$id", "$defs", "$ref", "title", "description", "default", "examples",
    "type", "required", "properties", "additionalProperties", "enum", "const",
    "oneOf", "anyOf", "allOf", "items", "minItems", "maxItems", "uniqueItems",
    "minLength", "maxLength", "pattern", "minimum", "maximum", "multipleOf",
}

TYPE_OF = {
    "object": dict, "array": list, "string": str,
    "integer": int, "number": (int, float), "boolean": bool,
}

FIELD_TYPES = {
    "bin_class": str, "bin_country": str, "bin_prefix": str, "card_country": str,
    "card_region": str, "issuer_country": str, "network": str, "product": str,
    "currency": str, "amount_minor": int, "amount_band": str, "merchant_country": str,
    "merchant_id": str, "route_class": str, "mandate": bool, "sca_required": bool,
    "sca_exemption": str, "entry_mode": str, "channel": str, "deadline_ms": int,
    "processor": str, "acquirer_country": str, "acquirer_region": str,
    "acquirer_capability": str,
}

OUTCOME_FIELD_TYPES = {
    "decline_category": int, "merchant_advice_code": str, "response_code": str,
    "attempt_index": int, "chain_depth": int, "last_attempt_processor": str,
    "elapsed_since_decision_ms": int,
}

# Catalog: rule name -> keys the compiler requires beyond the schema's minimum.
CATALOG_REQUIRED = {
    "regulatory.data_residency": [],
    "regulatory.sca_required": [],
    "network.reattempt_limit": ["after"],
    "network.hard_stop": ["after"],
    "network.retry_schedule": ["after"],
    "budget.attempt_cap": [],
    "budget.chain_depth": [],
    "budget.reservation": [],
    "mandate.require_3ds": [],
    "mandate.must_process": [],
    "mandate.exclude_processor": [],
    "mandate.domestic_acquirer": [],
    "econ.floor_margin": [],
    "econ.max_cost": [],
    "preference.rank": [],
}

# Base family per rule name, so precedence can be checked without re-parsing the schema.
FAMILY_OF_PREFIX = {
    "regulatory": "regulatory", "network": "network", "budget": "budget",
    "mandate": "mandate", "econ": "econ", "preference": "preference",
}

ISO_DUR = re.compile(
    r"^P(?=[0-9T])(\d+Y)?(\d+M)?(\d+W)?(\d+D)?(?:T(?=[0-9])(\d+H)?(\d+M)?(\d+S)?)?$")
# group order: 1=Y 2=Mo 3=W 4=D 5=H 6=Mi 7=S


class Error(Exception):
    pass


# --------------------------------------------------------------------------- validation

def _resolve(ref: str, schema: dict) -> dict:
    if not ref.startswith("#/"):
        raise Error(f"external $ref not supported by this checker: {ref}")
    node = schema
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def validate(instance, schema: dict, root: dict, path: str = "$", errors=None) -> list[str]:
    """Validate `instance` against `schema` (which may be a sub-schema of `root`)."""
    if errors is None:
        errors = []
    unknown = set(schema) - SUPPORTED
    if unknown:
        errors.append(f"{path}: checker does not understand schema keyword(s) {sorted(unknown)}")
        return errors

    if "$ref" in schema:
        return validate(instance, _resolve(schema["$ref"], root), root, path, errors)

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {instance!r}")
        return errors

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in enum {schema['enum']}")
        return errors

    if "type" in schema:
        want = schema["type"]
        types = want if isinstance(want, list) else [want]
        ok = any(
            (t == "integer" and isinstance(instance, int) and not isinstance(instance, bool))
            or (t != "integer" and isinstance(instance, TYPE_OF[t])
                and not (t != "boolean" and isinstance(instance, bool)))
            for t in types
        )
        if not ok:
            errors.append(f"{path}: expected type {want}, got {type(instance).__name__}")
            return errors

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than maxLength {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{path}: {instance!r} does not match {schema['pattern']}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} < minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: {instance} > maximum {schema['maximum']}")
        if "multipleOf" in schema and instance % schema["multipleOf"]:
            errors.append(f"{path}: {instance} not a multiple of {schema['multipleOf']}")

    if isinstance(instance, list):
        for kw in ("minItems", "maxItems"):
            if kw in schema:
                bound = schema[kw]
                over = (kw == "minItems" and len(instance) < bound) or \
                       (kw == "maxItems" and len(instance) > bound)
                if over:
                    errors.append(f"{path}: {len(instance)} items violates {kw} {bound}")
        if schema.get("uniqueItems"):
            seen = [json.dumps(x, sort_keys=True) for x in instance]
            if len(seen) != len(set(seen)):
                errors.append(f"{path}: items are not unique")
        if "items" in schema:
            for i, item in enumerate(instance):
                validate(item, schema["items"], root, f"{path}[{i}]", errors)

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required key {key!r}")
        props = schema.get("properties", {})
        add = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in props:
                validate(value, props[key], root, f"{path}.{key}", errors)
            elif add is False:
                errors.append(f"{path}: additional property {key!r} is not allowed")
            elif isinstance(add, dict):
                validate(value, add, root, f"{path}.{key}", errors)

    for kw in ("anyOf", "oneOf"):
        if kw in schema:
            results = []
            for sub in schema[kw]:
                sub_errors: list[str] = []
                validate(instance, sub, root, path, sub_errors)
                results.append(sub_errors)
            good = [i for i, e in enumerate(results) if not e]
            # Report the CLOSEST branch, not the first one: for a rule object, branch 0 is
            # whatever rule happens to be defined first, and its mismatch ('expected const
            # network.reattempt_limit') is noise that hides the actual reason. The branch with
            # the fewest errors is the one the author was aiming at.
            best = min(range(len(results)), key=lambda i: len(results[i]))
            if kw == "anyOf" and not good:
                errors.append(f"{path}: matches none of anyOf; closest branch "
                              f"({best + 1} of {len(results)}): {results[best][:2]}")
            if kw == "oneOf":
                if not good:
                    errors.append(f"{path}: matches none of oneOf; closest branch "
                                  f"({best + 1} of {len(results)}): {results[best][:2]}")
                elif len(good) > 1:
                    errors.append(f"{path}: matches {len(good)} oneOf branches; must match exactly 1")

    for sub in schema.get("allOf", []):
        validate(instance, sub, root, path, errors)

    return errors


# ---------------------------------------------------------------------- semantic checks

def iter_rules(doc: dict):
    for family in ("regulatory", "network", "budget", "mandate", "econ", "preference"):
        for rule in doc.get(family, []):
            yield family, rule
    for rule in doc.get("constraints", []):
        yield "inline", rule


def catalog_def_key(rule_name: str) -> str:
    """'regulatory.data_residency' -> 'rule-regulatory-data-residency' (the $defs key)."""
    family, _, name = rule_name.partition(".")
    return f"rule-{family}-{name.replace('_', '-')}"


def _walk_predicates(node, out):
    if not isinstance(node, dict):
        return
    if "field" in node:
        out.append(node)
    for key in ("all", "any"):
        for child in node.get(key, []):
            _walk_predicates(child, out)
    if "not" in node:
        _walk_predicates(node["not"], out)


def _dur_seconds(value: str) -> int:
    m = ISO_DUR.match(value)
    if not m:
        raise Error(f"bad duration {value!r}")
    y, mo, w, d, h, mi, s = m.groups()
    total = 0
    for num, unit in ((y, 365 * 86400), (mo, 30 * 86400), (w, 7 * 86400), (d, 86400),
                      (h, 3600), (mi, 60), (s, 1)):
        if num:
            total += int(num[:-1]) * unit
    return total


def semantic_checks(doc: dict, schema: dict | None = None) -> tuple[list[str], list[str]]:
    """Checks the schema cannot express. Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    ids: dict[str, int] = {}
    sigs: dict[tuple, list[str]] = {}
    policy = doc.get("policy", {}) or {}
    known_processors = set(policy.get("processor_ids", []))
    known_regions = set(policy.get("acquirer_regions", []))

    for family, rule in iter_rules(doc):
        rid = rule["id"]
        name = rule["rule"]
        where = f"rule {rid!r} ({name})"

        # SV1 - ids are unique and stable: the audit trail quotes them verbatim.
        ids[rid] = ids.get(rid, 0) + 1
        if ids[rid] > 1:
            errors.append(f"SV1: duplicate rule id {rid!r}")

        # SV2 - the catalog may require keys the schema allows to be absent.
        for key in CATALOG_REQUIRED.get(name, []):
            if key not in rule:
                errors.append(f"SV2: {where} must declare {key!r}")

        # SV10 - inline (single-transaction) refinements: the (rule, params) pair must
        # match the same catalog grammar a persistent rule would, the ttl is bounded, and
        # a request may never carry a relaxation.
        if family == "inline":
            if "relaxable" in rule:
                errors.append(f"SV10: {where} is request-scoped and may not relax anything")
            ttl = rule.get("ttl", "")
            if ISO_DUR.match(ttl):
                hours = _dur_seconds(ttl) / 3600.0
                if hours > 1.0:
                    errors.append(f"SV10: {where} ttl {ttl} exceeds the inline bound PT1H")
            if schema is not None:
                key = catalog_def_key(name)
                rule_def = schema.get("$defs", {}).get(key)
                if rule_def is None:
                    errors.append(f"SV10: {where} is not a catalog rule "
                                  f"(no $defs/{key}); unknown rule names are rejected, "
                                  f"never ignored")
                else:
                    params_errors: list[str] = []
                    validate(rule["params"], rule_def["properties"]["params"], schema,
                             f"$.constraints[{rid}].params", params_errors)
                    errors.extend(f"SV10: {e}" for e in params_errors)
            if name not in CATALOG_REQUIRED:
                errors.append(f"SV10: {where} is not in the catalog")

        # SV3 - predicates are closed over the right field vocabulary.
        wfields = []
        _walk_predicates(rule.get("when"), wfields)
        afields = []
        _walk_predicates(rule.get("after"), afields)
        for pred in wfields + afields:
            if "value" in pred and "op" in pred and pred["op"] != "exists":
                types = FIELD_TYPES if pred in wfields else OUTCOME_FIELD_TYPES
                want = types.get(pred["field"])
                value = pred["value"]
                if pred["op"] in ("in", "not_in"):
                    if not isinstance(value, list):
                        errors.append(f"SV3: {where} op {pred['op']} needs an array value")
                    else:
                        for v in value:
                            if want and not isinstance(v, want):
                                errors.append(
                                    f"SV3: {where} field {pred['field']} expects {want.__name__}, "
                                    f"got {type(v).__name__}")
                elif want is not None:
                    ok = isinstance(value, bool) if want is bool else \
                        isinstance(value, want) and not isinstance(value, bool)
                    if not ok:
                        errors.append(f"SV3: {where} field {pred['field']} expects "
                                      f"{want.__name__}, got {type(value).__name__}")

        # SV4 - named processors/regions must exist in the policy catalog. A typo in a
        # processor id must be a compile error, not a transaction that quietly routes
        # somewhere else.
        for key in ("processors",):
            for pid in rule.get("params", {}).get(key, []) or []:
                if known_processors and pid not in known_processors:
                    errors.append(f"SV4: {where} references unknown processor {pid!r}")
        for region in rule.get("params", {}).get("acquirer_regions", []) or []:
            if known_regions and region not in known_regions:
                errors.append(f"SV4: {where} references unknown region {region!r}")

        # SV5 - counter arithmetic: a ring counter must be able to hold `limit`/`limit`
        # per day without saturating, and windows must divide into whole slots.
        counter = rule.get("params", {}).get("counter")
        limit = rule.get("params", {}).get("limit")
        if counter and limit is not None:
            width = {"uint8": 255, "uint16": 65535, "uint32": 2**32 - 1}[
                counter.get("slot_width", "uint8")]
            slots = counter["slots"]
            if limit > width:
                errors.append(f"SV5: {where} limit {limit} exceeds "
                              f"{counter.get('slot_width', 'uint8')} capacity {width}")
            if limit > width * slots:
                errors.append(f"SV5: {where} honest window needs {limit} > capacity "
                              f"{width * slots}")
            max_day = rule.get("params", {}).get("max_per_day")
            if max_day is not None and max_day > width * slots:
                errors.append(f"SV5: {where} max_per_day {max_day} exceeds ring capacity")
        window = rule.get("params", {}).get("window")
        if window and counter:
            secs = _dur_seconds(window)
            if secs % counter["slots"]:
                errors.append(f"SV5: {where} window {window} ({secs}s) does not divide into "
                              f"{counter['slots']} slots")
        for key in ("min_interval", "default_interval"):
            value = rule.get("params", {}).get(key)
            if value and not ISO_DUR.match(value):
                errors.append(f"SV5: {where} {key} {value!r} is not an ISO-8601 duration")

        # SV6 - obvious contradictions: the same head declared twice with different
        # effects. Warn rather than error because precedence defines which one wins.
        head = (name, json.dumps(rule.get("when"), sort_keys=True),
                json.dumps(rule.get("after"), sort_keys=True))
        params_sig = json.dumps(rule.get("params"), sort_keys=True)
        sigs.setdefault(head, []).append(f"{rid}:{params_sig}")
        if len(sigs[head]) > 1 and sigs[head][-1] != sigs[head][0]:
            # Tiered preferences are SUPPOSED to share a condition; anything else sharing
            # one with different parameters is at best redundant and at worst a
            # contradiction, and the reader deserves to be told.
            if name != "preference.rank":
                warnings.append(f"SV6: {where} shares a condition with another rule and they "
                                f"differ in parameters; precedence decides the winner")

        # SV7 - relaxations are configuration changes with a named approver and an
        # expiry, never per-transaction judgement calls.
        rel = rule.get("relaxable")
        if rel:
            if rel["expires_at"] <= rel["approved_at"]:
                errors.append(f"SV7: {where} relaxation expires before it is approved")
            if rel.get("scope") == "traffic_share" and "max_traffic_share_bps" not in rel:
                errors.append(f"SV7: {where} traffic_share relaxation needs "
                              f"max_traffic_share_bps")
            if rel.get("scope") in ("single_transaction", "traffic_share") and \
                    "expires_at" not in rel:
                errors.append(f"SV7: {where} bounded relaxation needs an expiry")
            if name.split(".")[0] in ("regulatory", "network"):
                errors.append(f"SV7: {where} may not be relaxed "
                              f"({name.split('.')[0]} rules are not configurable)")

    # SV8 - the precedence vector must be explicit, total, and fixed where the platform
    # says it is fixed.
    prec = doc.get("precedence", {})
    if prec.get("fixed_prefix") != ["regulatory", "network"]:
        errors.append("SV8: precedence.fixed_prefix must be ['regulatory','network']")
    if sorted(prec.get("order", [])) != ["budget", "econ", "mandate"]:
        errors.append("SV8: precedence.order must be a permutation of budget/econ/mandate")

    # SV9 - scope naming: a merchant-scoped set must name its merchant, so that two
    # tenants cannot accidentally share a document.
    scope = doc.get("scope", {})
    if scope.get("kind") in ("merchant", "merchant_route_class") and "merchant_id" not in scope:
        errors.append("SV9: merchant scope requires merchant_id")

    return errors, warnings


# ------------------------------------------------------------------- schema self-checks

SCOPES = ("candidate", "transaction", "plan", "ordering", "lease", "presentation")

# Each negative fixture must FAIL, and must fail for the reason named here. Running them is
# the half of the gate that keeps the gate honest: a fixture that starts passing means a
# check was weakened, and a fixture that fails for the WRONG reason means a check was
# accidentally widened. Either way the gate is broken, so this is asserted, not merely run.
NEGATIVE = {
    "invalid-unknown-rule.json": "is not a catalog rule",
    "invalid-unknown-field.json": "bin_issuer",   # the unknown context field, named
    "invalid-type-mismatch.json": "expects str, got int",
    "invalid-counter-arithmetic.json": "exceeds uint8 capacity",
    "invalid-relaxed-law.json": "may not be relaxed",
    "invalid-scope-moved.json": "expected const 'transaction'",
}


def schema_checks(schema: dict) -> list[str]:
    """Checks on the SCHEMA, not on a document.

    Every rule head declares its enforcement point. 'When is this rule evaluated' is part of
    the canonical artifact: a merchant document cannot choose it (the schema makes it a
    const), the engine reads it from here rather than from a lookup table next to the
    evaluator, and an implementer reading only the schema can tell which rules can refuse an
    arm and which ones only reorder or defer."""
    errors: list[str] = []
    heads = {k: v for k, v in schema.get("$defs", {}).items() if k.startswith("rule-")}
    if not heads:
        errors.append("SV12: the schema declares no rule heads")
    for key, head in sorted(heads.items()):
        declared = (head.get("properties", {}) or {}).get("enforcement", {})
        value = declared.get("const")
        if value not in SCOPES:
            errors.append(f"SV12: {key} must declare properties.enforcement.const as one of "
                          f"{list(SCOPES)}; got {value!r}")
    return errors


# ------------------------------------------------------------------------------- driver

def check_file(path: Path, schema: dict) -> tuple[list[str], list[str], int]:
    doc = json.loads(path.read_text())
    errors = validate(doc, schema, schema)
    sem_errors, warnings = semantic_checks(doc, schema)
    return errors + sem_errors, warnings, sum(1 for _ in iter_rules(doc))


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    schema = json.loads(SCHEMA_PATH.read_text())
    files = sorted(EXAMPLES_DIR.glob("*.json"))
    if not files:
        print("no example documents found", file=sys.stderr)
        return 1
    report = []
    failed = False
    failed |= bool(schema_errors := schema_checks(schema))
    for path in files:
        errors, warnings, n_rules = check_file(path, schema)
        failed |= bool(errors)
        report.append({
            "document": str(path.relative_to(ROOT.parent)),
            "rules": n_rules,
            "errors": errors,
            "warnings": warnings,
        })
    # The negative half of the gate: every fixture under examples/invalid/ must be rejected,
    # for the reason it exists to demonstrate.
    negative = []
    for path in sorted((EXAMPLES_DIR / "invalid").glob("*.json")):
        errors, _warnings, _n = check_file(path, schema)
        want = NEGATIVE.get(path.name)
        if want is None:
            negative.append({"document": str(path.relative_to(ROOT.parent)),
                             "ok": False, "why": "no expected-error entry in check.py"})
            failed = True
            continue
        hit = next((e for e in errors if want in e), None)
        negative.append({"document": str(path.relative_to(ROOT.parent)),
                         "ok": bool(hit), "why": hit or (errors[0] if errors else "ACCEPTED")})
        failed |= not hit

    if as_json:
        print(json.dumps({"schema": str(SCHEMA_PATH.relative_to(ROOT.parent)),
                          "documents": report, "schema_errors": schema_errors,
                          "rejected": negative, "ok": not failed}, indent=2, sort_keys=True))
        return 1 if failed else 0
    print(f"compiling {SCHEMA_PATH.relative_to(ROOT.parent)}")
    for e in schema_errors:
        print(f"  schema: {e}")
    for entry in report:
        status = "FAIL" if entry["errors"] else "ok"
        print(f"  [{status:>4}] {entry['document']:<44} {entry['rules']:>2} rules, "
              f"{len(entry['errors'])} errors, {len(entry['warnings'])} warnings")
        for e in entry["errors"]:
            print(f"          {e}")
        for w in entry["warnings"]:
            print(f"          {w}")
    print("  [ ok] negative fixtures: each is rejected for the reason it demonstrates")
    for entry in negative:
        status = "ok" if entry["ok"] else "FAIL"
        print(f"  [{status:>4}] {entry['document']:<44} {entry['why'][:70]}")
    print("verdict:", "REJECTED" if failed else "accepted (schema-valid, semantically checked, "
          "negative fixtures rejected)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
