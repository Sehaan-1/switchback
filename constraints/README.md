# The constraint layer

This directory holds the canonical artifact of
[ADR-0004](../docs/decisions/0004-constraint-layer.md): the JSON Schema a merchant's
constraints are written against, the closed rule catalog the schema defines, the fixture
acquirer catalog those rules read, two worked merchant documents, and the CI checker that
gates all of them. It is data and a validator, not an engine — the Go evaluator that consumes
a compiled document is [#12](https://github.com/Sehaan-1/switchback/issues/12)'s job. The
simulator that drives a compiled document through a decision is
[ADR-0005](../docs/decisions/0005-simulation-harness.md), and it reads this directory's
`catalog/acquirer-catalog.example.json` as its fixture fleet: a scenario pins that file by
hash, so the constraint census and the harness benchmark are measured against the same six
acquirers.

```
schema/constraint-set.schema.json    the document grammar, v1.0.0 (JSON Schema draft 2020-12)
catalog/acquirer-catalog.example.json  fixture fleet: 6 processors, capability + economics
examples/merchant-default.json         "the ordinary merchant": 15 standing rules + 1 inline
examples/marketplace-strict.json       "the regulated merchant": 10 standing rules + 1 inline
examples/invalid/*.json                6 documents that MUST fail, for stated reasons
check.py                               the gate: schema subset + SV1-SV12 + negative fixtures
```

## Run it

```bash
python3 constraints/check.py            # human output, non-zero exit on failure
python3 constraints/check.py --json     # the same verdict, machine-readable (CI)
```

Both example documents must be accepted, every `examples/invalid/*.json` must be rejected
*for the reason it exists to demonstrate* (the expected error fragment is in `check.py`), and
the exit code is the verdict. Running the negative fixtures is what keeps the gate honest: a
fixture that starts passing means a check was weakened. The checker has no dependencies: it validates the JSON
Schema subset the schema actually uses, so it runs in any environment with a Python
interpreter — including a machine where `jsonschema` cannot be installed. Unknown keywords
fail loudly rather than being ignored, because a silently-unchecked keyword is a rule that
looks enforced and is not. The semantic checks are SV1–SV12 (SV12 is a check on the schema
itself: every rule head must declare its enforcement point).

Because a `oneOf` mismatch is reported against the branch with the *fewest* errors, the
message points at the rule head the author was aiming at instead of at whichever head happens
to be defined first. `--json` carries the same verdict with the expected-error fragment for
each negative fixture.

## The taxonomy, in one table

Six families, fifteen rule heads. Each head is a `$defs` entry with a fixed `params` object;
`scope` and `when` are shared by every rule in every family. The reasoning behind the
boundary — why capability is not a rule, why a processor's price is data and not policy — is
in ADR-0004 §1.

| Family | Rule head | Enforced at | It can say |
| --- | --- | --- | --- |
| `regulatory` | `regulatory.data_residency` | candidate | which acquirer regions a card's data may be processed in, and what to do when an acquirer has not declared the capability |
| | `regulatory.sca_required` | candidate | SCA required / exempt, the exemption list it accepts, what to do when the evidence is missing |
| `network` | `network.reattempt_limit` | transaction | windowed attempt ceiling with a key, per-day pace, minimum interval, and what an unknown decline category means |
| | `network.hard_stop` | transaction | the decline categories and MACs on which the attempt chain ends (Visa Cat 1, Mastercard MAC 03) |
| | `network.retry_schedule` | presentation | the issuer-prescribed wait schedule for soft declines, and its source |
| `budget` | `budget.attempt_cap` | transaction | attempts per window per key (card / BIN prefix / merchant), and whether exceeding it queues or denies |
| | `budget.chain_depth` | plan | how many acquirers one transaction may visit, optionally gated on an estimated-value test |
| | `budget.reservation` | lease | the in-flight lease: key, TTL, and what an expired lease means ([#10](https://github.com/Sehaan-1/switchback/issues/10)) |
| `mandate` | `mandate.require_3ds` | candidate | 3DS required / preferred / allowed, per route |
| | `mandate.must_process` | candidate | processors this request MUST go to, and `fail_closed` vs `fall_back` when none is available |
| | `mandate.exclude_processor` | candidate | blacklist, with an expiry |
| | `mandate.domestic_acquirer` | candidate | the acquirer must be in the card's country or region (`match`), or domiciled in one of a named set of regions (`acquirer_regions`) |
| `econ` | `econ.floor_margin` | candidate | the merchant's margin floor in bps, on a named `basis`, with `deny` vs `escalate` |
| | `econ.max_cost` | candidate | the most a transaction may cost, with fixed and attempt fees optionally included |
| `preference` | `preference.rank` | ordering | an ordering tier over processors and its fallback behaviour — an ordering rule, not a filter |

**Enforced at** is the rule's *enforcement point*, declared in the schema by the rule head
(`properties.enforcement`, checked by SV12) and read by the compiler from there rather than
from a table next to the evaluator:

- **candidate** / **transaction** — evaluated on the decision's hot path; these are the only
  scopes that can refuse an arm or empty the set, which is why they are the only ones that can
  produce `unroutable`.
- **plan** — shapes the fallback chain; **ordering** — may only reorder, never remove.
- **lease** — accounts for attempts that are in flight but not yet settled.
- **presentation** — gates the retry scheduler, outside this decision entirely.

A merchant may state `enforcement` in a rule (it is validated against the head's value) but
cannot choose it: moving a law or a scheme rule to a cheaper enforcement point is a schema
change, and `examples/invalid/invalid-scope-moved.json` is the fixture that proves the gate
notices.

A rule that cannot be expressed as one of these is a schema change, deliberately: a merchant
adds `params`, not code. `examples/merchant-default.json` is the worked example of a
low-precedence waiver ladder; `examples/marketplace-strict.json` is the worked example of a
document whose regulatory rules cannot be waived at all (checker SV7).

## Adding a rule head

1. Add `$defs/rule-<family>-<name-with-dashes>` to the schema, with `params` and
   `required: [...]` filled in and the `$comment` saying what the rule is *for*.
2. Reference it from `family<Family>.items.oneOf`.
3. Add the rule name to the checker's catalog (`catalog_def_key` maps the rule name to the
   `$defs` key) so SV3/SV4 can validate its params semantically.
4. Add a positive example to one merchant document and a negative one to `examples/invalid/`.
5. Add the evaluator in the engine, and the row to the spike that measures its effect.

If a new head is relaxable, the schema must say which scope it may be waived at. Regulatory
and network heads may not declare `relaxable` at all — precedence puts them first and the
checker refuses the document (SV7: `fixed_prefix == ["regulatory", "network"]`).

## Two things this directory is not

- **Not a production catalog.** `acquirer-catalog.example.json` is a fixture with invented
  economics and capability, pinned by every document's `policy.catalog_hash` so that a
  document and the fleet it was written for move together. Real catalogs come from the
  control plane.
- **Not the evidence.** The measurements that justify the design — the conflict census, the
  ring-counter direction, the per-arm-guard counterexample, the audit bytes — are in
  [`spikes/0005-constraints/`](../spikes/0005-constraints/), regenerated by
  `python3 spikes/0005-constraints/policy.py 100000`.
