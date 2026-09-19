# switchback - convenience targets. The repo is design-phase: no Go toolchain is
# assumed anywhere here; every target is stdlib Python 3 and deterministic unless
# the command output itself says otherwise.

PY ?= python3

# ---- gates: fast, and they gate everything else ---------------------------------------

# the scenario gate: schema + SV1-SV10 + golden hash pins + stream vectors
gate-scenarios:
	$(PY) simulator/scenarios/check.py

# the constraint-layer gate (catalog / constraint-set fixtures)
gate-constraints:
	$(PY) constraints/check.py

gate: gate-scenarios gate-constraints

# ---- the #17 benchmark ----------------------------------------------------------------

# The committed artifact. Full profile (~15-25 min on 2 vCPU): writes BENCHMARKS.md.
# Only this target may write the file - quick/json/section runs print and leave it alone.
benchmarks:
	$(PY) scripts/run_benchmarks.py

# development smoke of the same machinery (~5-8 min, quick profile). Prints, never writes.
benchmarks-quick:
	$(PY) scripts/run_benchmarks.py --quick

# CI: the fast gates, then the quick profile as machine-readable verdicts
# (exit 1 on any FAIL row; PENDING rows are armed, not failures).
benchmarks-ci: gate
	$(PY) scripts/run_benchmarks.py --ci

# the reproducibility gate: regenerate the FULL profile and diff every non-masked
# line against the committed BENCHMARKS.md. Pre-release and on any PR touching
# scripts/run_benchmarks.py, the scenarios, or the spikes it consumes.
benchmarks-check:
	$(PY) scripts/run_benchmarks.py --check

# machine-readable verdicts without a file write (quick profile)
benchmarks-json:
	$(PY) scripts/run_benchmarks.py --quick --json

.PHONY: gate gate-scenarios gate-constraints benchmarks benchmarks-quick benchmarks-ci benchmarks-check benchmarks-json
