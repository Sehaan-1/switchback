# Spike: what a simulation harness has to be able to do, and what it costs when it can't

One file, `harness.py`, plus the scenario documents it reads from
[`simulator/scenarios/`](../../simulator/scenarios/). It is not the production harness —
that is Go, and it is #12's job — it is a working reference implementation of the design
[ADR-0005](../../docs/decisions/0005-simulation-harness.md) picks, small enough to read in
one sitting and instrumented so that every claim in the ADR is a row in `RESULTS.md` rather
than an adjective.

```bash
python3 harness.py                       # ~3 min, stdlib only, no network, fixed seed
python3 harness.py 20000 --full=1000000  # the command RESULTS.md was generated with
python3 harness.py --section=M3          # one section
python3 harness.py --stream-hash 5000    # the determinism digest, for cross-process checks
```

## Why Python, again

Same reason as [`spikes/0001-latency-budget/`](../0001-latency-budget/): there is no Go
toolchain in the environment this was written in, and the questions #6 asks are not
"how fast is Go" but "does the design have the properties we claim". CPython is the
pessimistic instrument for the speed target — if 1M transactions × 6 arms fits the budget
while interpreted, the budget is not tight — and it is a perfectly good instrument for
everything else, because determinism, interface parity and replayability are structural
properties that a language cannot improve on. Where a number is a projection rather than a
measurement, `RESULTS.md` says so and names the band.

## What each section is for

| Section | The question | The kind of answer |
| --- | --- | --- |
| `M1` | Is the scenario format executable, and is the harness interface really the processor interface? | hashes, line counts, the exact set of attributes the driver touches |
| `M2` | Does the response stream depend on anything other than the scenario? | 12 perturbation runs, 10 that must match and 2 controls that must not |
| `M3` | Shared seeded PRNG, or key-derived streams? | % of answers that move when the query order or the fleet membership changes |
| `M4` | Does the latency model matter, or is any heavy tail fine? | four shapes fitted to the same declared triple, compared where a deadline lives |
| `M5` | Is "an acquirer went down" one scenario or three? | per-10-minute outcome mix through an outage and its recovery |
| `M6` | Is the fixture fleet plausible, and is that checkable? | decline-family shares against published bands, BIN spread, 3DS funnel |
| `M7` | What does an attempt cost, and what should the target be? | µs/attempt, attempts/s, the measured 1M-transaction run, targets T1–T3 |
| `M8` | What does "replay" require that "re-run" does not? | counterfactual reconstruction with and without the seed; the cost of an unrecorded field |

Three things this is designed to catch, because they are the ones that survive review:

- **A world that depends on the run.** A seeded PRNG feels deterministic and is not: the
  answers move when the policy asks the arms in a different order, or when an acquirer is
  added to the fleet. `M3` measures both, and the second is the one that quietly invalidates
  a benchmark baseline.
- **A model that reads the environment.** `M2` riggs `time.time`, `time.monotonic`,
  `time.perf_counter`, `time.sleep`, `datetime.now`, `random.random`, `random.betavariate`
  and `os.urandom` to raise, then runs a whole scenario. That is the executable form of "no
  `time.Now()` in harness paths", which a lint rule can only approximate.
- **A benchmark that measures the model.** `M4` shows four latency shapes agreeing at the
  declared p99 and disagreeing by an order of magnitude in the rate at which a 900 ms
  deadline is breached — including the shape this repo's own spike 0004 hand-rolled, which
  cannot exceed its declared p99 at all.

## Not in scope here

`RESULTS.md` is generated output — regenerate it, do not edit it. The Go implementation, the
trace writer, the `go test -bench` targets and the committed benchmark tables are #12's and
#17's. The scenario documents in `simulator/scenarios/examples/` are the artifact this spike
reads; they are gated by `simulator/scenarios/check.py`, which is the thing to run if you
change one.
