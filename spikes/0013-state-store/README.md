# Spike: the state store — what persists, in what shape, at what cost, and what a crash takes

`store.py` is the empirical evidence for #13: the backend for the learned Beta posteriors and
the routing trace, the schema of each, and the checkpoint/durability guarantee. ADR-0006 R47
already collapsed most of the design space — the trace *is* the write-ahead log of the learned
state, and the posterior is a fold over it — and ADR-0008 R60, ADR-0009 R63–R71, ADR-0010 R72
and ADR-0011 fixed the record shapes, the money row, and the package that owns them. What is
left cannot be argued, only measured: which SQLite configuration clears the fleet's write pace
at which durability guarantee, what the row layout costs in bytes and in query time, what a
crash actually takes under each guarantee, what a restart costs, how big the retained artifact
gets, what exactly-once learning costs, and what a reader does to the writer.

It runs against the committed scenario `baseline-steady-v1` through ADR-0005's harness world
generator (the same one spikes 0005–0011 used, so the numbers are comparable across ADRs), and
against the committed acquirer catalog: this spike is the first consumer of the three contract
facts ADR-0011 handed to #13, so the lease rows it writes carry the catalog's own
`max_response_ms` per processor rather than a constant invented here, `key_lifetime_ms` is
asserted against it at import (ADR-0009 R66), and `auth_fee_minor` is read from it.

```bash
python3 store.py                        # full run at the default n=200,000, ~4 min on 2 vCPU
python3 store.py 200000                 # the same run spelled out: RESULTS.md is its output
python3 store.py 200000 --section=W3    # one section (--section=W1,W2 for a list)
python3 store.py --smoke                # every section at a size that runs in ~90 s
```

`RESULTS.md` is generated output — regenerate it, do not edit it. Everything is stdlib-only and
offline. The structural results (rows lost, rows recovered, integrity verdicts, fold digests,
dedupe counts, bytes per row, the retention ladder, the audit chain head) are reproducible
exactly and were re-verified across runs; the timings are one box and move up to ~25% between
identical configurations, so they are reported with their spread and the ADR quotes bands where
a band is wider than the claim.

## What each section is for

| Section | The question | The kind of answer |
| --- | --- | --- |
| `W1` | Which journal mode × `synchronous` × group-commit batch clears the fleet's write pace? | rows/s, µs/row, commit p50/p99, B/row for 36 configurations against 11,500 rows/s, plus the convoy curve and the `wal_autocheckpoint` stall |
| `W2` | Where does the lease append live, and what does a crash-safe dispatch cost? | per-lease durable latency against ADR-0001's 2 ms budget, the K-convoy curve, two files vs one vs `shared_writer`, and a labelled slow-device model |
| `W3` | What does each configuration lose when the process dies? | SIGKILL mid-run, mid-transaction and mid-checkpoint per config; a torn-WAL power-loss emulation; the un-fsynced window in rows and seconds |
| `W4` | What does a column cost, what does a blob cost, and who can answer the query? | four row layouts priced on bytes, insert rate, and the five query shapes #15/#16/ADR-0004/ADR-0006 actually run, each with SQLite's own plan |
| `W5` | What does a columnar export buy over the SQLite row store? | export cost, bytes and compression per column, min/max pruning, and the format win separated from the interpreter's cost |
| `W6` | Can SQLite carry a 1M-transaction simulation? | the run with the store attached, by segment, the bottleneck decomposition, the cost against the harness, and the per-shard arithmetic |
| `W7` | What does a restart cost, and what must the snapshot carry? | the tail-length fold curve, the snapshot cadence trade, snapshot-vs-fold equality per derived quantity, re-bucketing as a re-fold, and a corrupted snapshot |
| `W8` | What do 400 days cost, and how does a day die? | the retention ladder at both paces, the audit hash chain's write and verify cost with a tamper test, and DELETE vs VACUUM vs unlink |
| `W9` | What does exactly-once learning cost, and does it hold under redelivery? | the partial unique index priced with and without, then 148,398 redeliveries in four shapes against the fold digest |
| `W10` | What does a reader do to the writer? | writer throughput and commit tail with readers in separate processes, what the readers saw, and the held-read-transaction checkpoint hazard |

## Headlines

- **`W1` — the journal mode and the batch size decide it, not SQLite.** WAL + `synchronous=FULL`
  writes 7,015 rows/s one-row-per-commit and 108,006 rows/s at batch 64 — a 15× spread for the
  *same* guarantee. The rollback journal at the same durability is 1,068 rows/s, 6.6× worse. The
  shipped configuration clears the fleet's 11,500 rows/s by 9×, in CPython, with no Go driver in
  the loop: the binding constraint on this design is not throughput. `wal_autocheckpoint=0` is
  the *slowest* writer measured (64,628 rows/s against 112,711 at 1000 pages) and pins an
  87.7 MB WAL — disabling the checkpoint buys nothing and costs a tail.
- **`W2` — R71 fits the route path per lease, and only with a convoy.** One durable lease costs
  467 µs p50 / 703 µs p99: 23% / 35% of ADR-0001's 2 ms in-engine budget. But K=1 sustains
  2,104 dispatches/s against a fleet pace of 6,500, and on a labelled 5 ms-fsync volume it
  sustains 200 — so the group-commit convoy ships as the mechanism, not an optimisation, with
  K=1 as its degenerate low-volume case. Batching also buys bytes: WAL amplification falls from
  9,045 to 459 B/lease across K=1→64. The two-file split is *not* bought by device interference
  (measured inside this box's noise: lease p99 721 µs with its own file vs 659 µs sharing the
  log's); it is bought by rotation, by writer cadence, and by a tail argument — a checkpoint
  stall in a shared file costs 3–10 ms of p99.9 against a 2 ms budget. `shared_writer` (the
  lease riding the log writer's batch) models at up to 5.5 ms and is rejected.
- **`W3` — a process crash takes nothing committed, from any configuration; power loss takes
  exactly the un-fsynced bytes.** SIGKILL mid-run loses 0 of 2,000 committed rows under WAL and
  under DELETE, at `FULL`, `NORMAL` *and* `OFF`, because the OS is still alive — which is why
  FULL-vs-NORMAL cannot be settled by killing processes. Mid-transaction, the reopen returns the
  committed prefix exactly (1,992 of 2,001 issued). Truncating the WAL tail reopens `ok` at
  every cut and returns a prefix, never a corrupt middle: loss is linear in the bytes the device
  did not get. The design variable is therefore the un-fsynced window — one group commit (64
  rows, 5.6 ms of fleet traffic, 2.5 s at 20 TPS) at `FULL` against one checkpoint interval
  (4,863 rows, 0.42 s) at `NORMAL`, 76× wider. The ticket's "in-memory + periodic checkpoint"
  option loses 500 of 2,000 rows and has **no log** to reconstruct them from, which also
  punches a hole in #15's propensities and in ADR-0004's one-record-per-decision claim.
- **`W4` — scalars a query names are columns; arrays are blobs; raw context rides every row.**
  The shipped layout writes 177.6 B/row (380.1 B/decision) at 112,369 rows/s. The blob-only
  layout is 1.08× narrower and writes faster, and is rejected on queries: ADR-0004's windowed
  counter rebuild costs 27 ms index-bounded against 138 ms and all 227,998 rows handed over.
  JSON is 2.7× wider, 2.3× slower to write, and worst on every query it can answer. The
  narrowest layout of all — the shipped one minus six context columns — *cannot answer the
  re-bucketing or the analyst-slice query at all*, which is the price of ADR-0006 R40's promise,
  stated in int32s.
- **`W5` — the columnar tier is an offline export, and pruning is the mechanism that earns it.**
  Export runs at 100,469 rows/s against a write path sustaining 99,761 rows/s, so it is a batch
  step over a sealed partition and never an in-path one. The artifact is ~21× smaller than the
  row store holding the same rows (~7× over its own uncompressed bytes) — the uncycled-pool
  figures; this run's cycled pool flatters both. Min/max pruning opens 2 of 14 row groups for an
  hour-window aggregate (6.71M rows/s against 1.12M unpruned), which projects to 95.8% of a
  fleet-day file never being opened. Counter-evidence recorded: if #15's single-run OPE pass
  were the only consumer, the SQLite file with a covering index would be enough and the export
  would be pure cost.
- **`W6` — one shard is 10× the fleet pace, and 1M transactions is a 19-second job.** 427,998
  log rows in 3.7 s (115,118 rows/s, 53,794 decisions/s), 177.7 B/row, and commits are 89% of
  wall time at 443 µs each: the store is fsync-bound, not CPU-bound, which is why the batch size
  is the lever and why a Go driver's per-call overhead lands elsewhere (#17's choice, not this
  one). The rate does not degrade as the file grows (1.14× first segment to last). Including
  AUTH_RECORD rows at the world's measured share, the pace to sustain is 13,376 rows/s and the
  headroom 8.6×. Against the harness the store adds 48% to per-decision cost, so an
  audit-trailed run costs roughly twice the bare one — affordable, and not hidden.
- **`W7` — snapshots are what make 400 days bootable, and their payload is a schema
  commitment.** A cold fold of 200,000 op rows costs 212 ms (502,928 rows/s) and reproduces the
  live state's digest bit-exactly; at retention scale the same rate is 220 hours. Snapshotting
  every 10,000 op rows (one per second of fleet traffic, 0.091% of writer time) cuts boot to
  15.5 ms — 14×. Snapshot+tail is bit-identical to folding from zero, so any snapshot can be
  discarded without changing the answer; one flipped byte in a snapshot is *refused* at boot and
  the engine falls back to the cold fold. The hole the spike found by comparing: the posterior
  array survives a snapshot because it *is* the snapshot, while the four derived integers
  silently report the tail as if it were the whole history unless they are packed in.
- **`W8` — retention is a partition-and-unlink decision, not a database decision.** 177 GB/day
  and 70.6 TB over the window at the fleet pace (0.99 GB/day, 395 GB at the scenario's declared
  30 TPS) for the row store, against 8.1 GB/day and 3.26 TB columnar; learned state is 135 KiB
  per shard however long the chain gets, and the largest single file is one shard's day.
  `DELETE FROM` reclaims 0.0% of the bytes, `VACUUM` 33.6% at a cost greater than the delete,
  and `unlink` 100% in constant time. The audit hash chain costs 10–20% of write throughput
  (20.1% in the committed run) and +0.0 B/row, verifies at ~195–235k rows/s (a fleet day in
  37 min), and detects a one-column edit at exactly the row where it happened.
- **`W9` — exactly-once learning is a partial unique index, and the policy inside `OR IGNORE`
  has to be written down.** `(seq, attempt) WHERE kind = OUTCOME` costs +7.6 B/row exactly and
  3–25% of write throughput depending on the run and cache state (13.6% in the committed full
  run; 25.1 / 3.0 / 15.6 / 11.8% in four other passes) — anywhere else would mean a read before
  every write. It survives 148,398 redeliveries in four shapes, including 200 with a
  *conflicting* outcome: 0 rows added, 5.84 µs per ignored insert, and a bit-identical fold
  digest. First write wins, a correction is a distinct op kind, and the dropped-conflict count
  is free (`changes()`).
- **`W10` — WAL keeps its promise about correctness and breaks the one about free cores.** Zero
  reader errors and no torn rows in any phase, and the writer never waited on a reader's lock.
  But four *idle* reader processes cost the writer 9% of throughput while four that actually
  query cost 66%, and the writer's commit p99 moves 1,014 µs → 11.1 ms: that is affordable for
  the trace file (sized at 9× the pace) and not for the money file, which is the latency leg of
  `W2`'s two-file argument. The hazard is a reader holding its transaction open — 405 of 405
  passive checkpoints stopped short *without reporting themselves busy*, the worst gap was
  18,064 WAL frames, and the WAL peaked at 180.2 MB against 0.5 MB (119 MB per second of held
  read, ~429 GB per hour). A TRUNCATE checkpoint reclaimed it all afterwards: a pin, not a leak.

## Not in scope here

The rules are ADR-0012's; this file only measures. Stated gaps, because they bound every claim
above: **no Go toolchain** in this sandbox, so the driver question (mattn/modernc/ncruces/
zombiezen) and every absolute Go number belong to #17 — the CPython absolutes carry interpreter
overhead a Go engine would not pay, and the *ratios* are the transferable part. **No pyarrow, no
DuckDB, no Arrow C++**, so `W5` measures the columnar *mechanism* (row groups, typed chunks,
per-chunk zlib, a min/max footer) with ~150 lines of stdlib and labels anything said about a
real format as a model on cited numbers. **No plug to pull**: `W3`(b) emulates power loss by
truncating the WAL tail, which does not tear pages in the database file or reorder filesystem
metadata. **No AUTH_RECORD rows written**: the measured write profile is decisions plus outcome
ops, and R72's record is priced in `W6`(d) as arithmetic on the world's own measured auth share
rather than as a measured row — the shipped schema gives it a table of its own (ADR-0012 §3.4),
and #17 re-measures the shipped schema in Go. **2 vCPU, container overlay filesystem, no perf
isolation**, so the GIL serialises the threaded phases (`W2`(b)/(d)) and `W10` uses forked
*processes* rather than threads — a CPython thread benchmark on two cores measures the
interpreter's lock, not SQLite's.
