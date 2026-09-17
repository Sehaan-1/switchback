# ADR-0012: State store — two SQLite files split by durability, the trace as the fold's log, snapshots as its checkpoints

- **Status**: Accepted
- **Date**: 2026-09-17
- **Resolves**: [#13 State store design: persistence for Beta posteriors and routing traces](https://github.com/Sehaan-1/switchback/issues/13)
- **Depends on**: [ADR-0001](0001-engine-language-go.md) (Go core, the 2 ms p99 in-engine budget the money row is measured against, `CGO_ENABLED=0` and therefore `modernc.org/sqlite`, §6's pre-commitment to a SQLite state-store binding), [ADR-0004](0004-constraint-layer.md) (the windowed counters and their 47 B/key arena, the one-record-per-decision audit claim and its hash chain, the 400-day retention window), [ADR-0005](0005-simulation-harness.md) (§5.2's trace-row contract, content-addressed scenarios, key-derived determinism, the injected virtual `Clock`), [ADR-0006](0006-thompson-sampling-implementation.md) (R39–R50: the arm key, the float64 counters, R44's dedupe-on-`(seq, attempt)`, R47's *the WAL is the only writer of learned state*, R48's one-writer-per-shard and its group-commit bound, §7's "the fsync interval is yours"), [ADR-0007](0007-drift-detection.md) (detector buckets ≤ 2 KB/processor, resets as WAL ops), [ADR-0008](0008-censored-exploration.md) (R60–R62: DECISION_LOG v1 and its run header, the score-based plug-in propensity, update-on-arrival in WAL order), [ADR-0009](0009-idempotency-double-charge.md) (R63–R71: the lease row and its persisted key, R66's `max_response_ms`/`key_lifetime_ms`, R67's route-class resolution windows, R71's *durable before dispatch*, §2's reconciliation-ledger hand-off to this ticket), [ADR-0010](0010-3ds-friction.md) (R72's AUTH_RECORD v1 field list and join key, R75's path-accurate fee incidence reading `auth_fee_minor`, the no-relabel-on-late-settlement rule), [ADR-0011](0011-module-layout.md) (the `internal/store/` package and its place in the import DAG, the row owners it fixed for this ticket, the `Catalog` struct it made `store/` serve, §7's instruction to re-pin the example catalog's contract facts *in this PR*)
- **Feeds**: #14 (safe rollout: alarm suppression reads detector buckets and counter snapshots out of the boot path), #15 (OPE: reads the trace tier and the columnar export; recomputes exact propensities from the logged posterior blobs), #16 (dashboard: reads sealed partitions or a replica, never the live file — R94), #17 (benchmarks: the Go driver, the shipped schema's absolutes, the fsync latency of the production volume), and the implementation tickets that build `internal/store/` and `analysis/`
- **Evidence**: [`spikes/0013-state-store/RESULTS.md`](../../spikes/0013-state-store/RESULTS.md) — reproduce with `python3 spikes/0013-state-store/store.py 200000` (~4 min, stdlib only, deterministic in structure). The run writes **427,998 real log rows** (200,000 decisions + 227,998 outcome ops, 1.140 attempts/decision) from `baseline-steady-v1@sha256:b49193b7e715` through ADR-0005's world generator, against `acquirer-catalog-2026.09.2@sha256:78b918123635` — this spike is the first consumer of the three contract facts ADR-0011 §7 handed here, and its lease rows carry the catalog's own per-processor `max_response_ms`. Sections `W1`–`W10` are cited inline below as `[W1]`…`[W10]`. **Sandbox gaps, stated up front because they bound every number**: no Go toolchain (driver choice and all Go absolutes belong to #17; CPython absolutes carry interpreter overhead, ratios transfer), no pyarrow/DuckDB/Arrow (the columnar *mechanism* is measured with ~150 lines of stdlib; anything said about a real format is a labelled model on cited numbers), no way to pull a power plug (power loss is emulated by truncating the WAL tail), 2 vCPU on a container overlay filesystem with no perf isolation (this box's fsync moves up to ~25% between identical configurations, so timings are quoted as bands where the band is wider than the claim).

---

## Decision

1. **Two SQLite files per shard, split by durability and not by data model**: `journal.sqlite` carries the money (`lease`, `scheduled_event`, `ledger`, `boot`) and `trace.sqlite` carries learning and audit (`boot`, `decision`, `op`, `snapshot`, `auth`). Both are WAL, both run `synchronous=FULL`, each has exactly one writer (ADR-0006 R48 preserved), `wal_autocheckpoint=1000`, `busy_timeout=5000`. Money rows never live in a file that is sealed and dropped on a schedule (§5.3, §9.5).
2. **The log is canonical; state is derived.** The Beta posteriors, the ADWIN buckets and the counter arena live in memory as the fold's accumulators and are reconstructed at boot from the newest `snapshot` row plus the ops after its `op_seq` (§1.3, §5.4). Nothing writes learned state except the fold over the op log (R47 unchanged). Snapshot cadence: every 10,000 op rows — one per second of fleet traffic, 0.091% of writer time — and once on clean shutdown.
3. **Three durability classes, and only one of them may carry money or audit.** `strict` (WAL + `synchronous=FULL`, group commit ≤ 64 rows or ≤ 5 ms on the trace; a lease convoy of K ≤ 32 or T ≤ 0.5 ms on the journal) is the production class; `batched` (NORMAL) is harness-only; `ephemeral` (OFF/MEMORY) is scratch with no claim. The guarantee is stated as an **un-fsynced window in rows and seconds** — one group commit, 64 rows, 5.6 ms of fleet traffic at `strict`, against one checkpoint interval, 4,863 rows, 0.42 s at `batched` (§5.2) — because an ack is only a durability claim if the window behind it is named.
4. **Row layout: scalars a query names are columns, fixed-layout arrays are blobs, and raw request context rides every outcome row.** The shipped layout costs 177.6 B/row and 380.1 B/decision; the blob-only alternative is 1.08× narrower and loses a windowed query by 5× (138 ms and all 227,998 rows handed over, against 27 ms index-bounded); the JSON alternative is 2.7× wider, 2.3× slower to write and worst on every query it can answer; and the narrowest layout of all — the shipped one minus six context columns — **cannot answer the re-bucketing query at all**, which is what ADR-0006 R40's promise costs (§3, `[W4]`).
5. **The columnar tier is an offline export of sealed partitions, owned by `analysis/`; the engine writes no Parquet** (ADR-0011's DAG). Retention is a partition-and-unlink decision: one file per (shard, run) in harness mode and per (shard, day) in production, expiry by `unlink` (100% of bytes back in constant time) and never by `DELETE` (0.0% back) or `VACUUM` (33.6% back, at a cost greater than the delete). Dedupe is a partial unique index with first-write-wins and a counted conflict; every decision row carries a chained `audit_hash` (§4.4, §4.5, §4.7).

---

## Context

Three facts about this project's shape change how the evidence below should be read.

**The design space was already collapsed by R47, and the ticket did not know it.** #13 was written as a choice between four model-state backends and three trace formats. ADR-0006 R47 had already decided that "the WAL is the only writer of learned state; the posterior is a fold over it", which makes the model state and the trace *one* artifact viewed from two ends: the trace is the log, the posteriors are a function of it, and a "backend for the posteriors" is really a question about how fast that function can be re-evaluated. So the four options are not four backends — three of them are answers to "where do the bytes live" and one (in-memory + checkpoint) is an answer to "what is canonical", and the measurement below separates the two questions rather than picking a column out of the ticket's table.

**The write pace is small and the retention window is enormous.** ADR-0001 budgets 5,000 decisions/s and ADR-0006 measured 1.3 attempts each, so the log takes 11,500 rows/s of decision + outcome rows — plus one AUTH_RECORD row per attempt that touched an authentication step (R72), which at the world's measured share of 0.289 makes the shipped profile **13,376 rows/s**. Against that, ADR-0004's retention window is 400 days: 1.16 G rows/day and ~84 TB across the window at the fleet pace. Throughput is therefore not the interesting question (one shard clears the pace by 8.6–10×, `[W6]`); bytes, boot time and deletion are. A design that answers "can SQLite keep up" and stops has answered the cheap half.

**One row of this store moves money, and it is not a log row.** ADR-0009 R71 requires the lease — including its CSPRNG idempotency key — to be durable *before* the dispatch it authorizes, inside ADR-0001's 2 ms p99 in-engine budget. That single row has a latency contract the trace does not have, and the trace has a throughput and volume contract the lease does not have. Every "should they share a file" question below is that asymmetry, and `[W2]` measures it three ways.

---

## 1. Model state: the backend (consideration 1)

### 1.1 What the fold actually needs, and therefore what the backend must be

R47 makes the posterior a fold over the op log, so the backend has to be good at four things and only four: **append** an op durably at the fleet pace; **re-evaluate** the fold from a checkpoint at boot; **answer** the router's own queries (an idempotency probe at ingest, a windowed counter rebuild, a re-bucketing pass); and **retain** the result for 400 days at a deletable granularity. Notice what is *not* on the list: concurrent writers, cross-shard transactions, sub-millisecond point reads from many clients. ADR-0006 R48 already made one writer per shard a rule, which removes the entire class of problems a networked store exists to solve.

The state itself is tiny and fixed: 4,320 arms × 4 float64 = **135 KiB per shard** (`[W8]`: 0.14 MB of learned state at *either* pace, against 177 GB/day of trace). That asymmetry — fixed-size state, unbounded history — is why the ticket's "in-memory + SQLite checkpoint" option is half right: the arrays *should* be in memory, and the *log* is what needs a backend.

### 1.2 The four options, measured where they can be

| The ticket's option | What it is here | Verdict | The measurement |
| --- | --- | --- | --- |
| In-memory + SQLite checkpoint | arrays in RAM, periodic snapshot, **no log** | **Rejected for the log, adopted for the arrays** | `[W3]`: loses every op since the last checkpoint — 500 of 2,000 rows — and has *no log to reconstruct them from*. Those decisions also have no audit row, so #15 cannot recompute their propensities and ADR-0004 §6's one-record-per-decision claim has a hole in it. `MEMORY` journal is the fastest writer measured (58,651 rows/s at batch 1) and is not a store |
| SQLite WAL | the log in WAL-mode SQLite, state folded from it | **Adopted** | `[W1]`: WAL + `FULL` at the shipped batch clears the fleet pace by 9× (108,006 rows/s against 11,500); `[W3]`: zero committed rows lost on a process kill at *any* pragma, and a torn WAL tail is discarded rather than believed; `[W6]`: 115,118 rows/s integrated, 1.14× first segment to last, 1M transactions in 19 s |
| Redis / Valkey | networked KV as the state backend | **Rejected** (§9.1) | Not measurable in this sandbox (no server, and a network store on 2 vCPU measures the loopback stack). Answered as a labelled model on cited numbers plus the structural argument: it does not remove the log, it adds a second persistence mechanism with the same fsync arithmetic (AOF) or the same loss window (RDB), inside a 2 ms budget |
| Event-sourced log (no database) | framed append-only file, state by replay | **Rejected — but it is what ships, plus an index** (§9.4) | The shipped design *is* an event-sourced log: `op` is append-only, `op_seq` is the total order, state is a fold. What a bare framed file cannot do is answer the router's own queries without scanning, which is `[W4]`'s layout B/D result: a window query becomes a full-file walk (138 ms and 227,998 rows handed over against 27 ms index-bounded; 600 s against 40 s at fleet pace) |

The two surviving options are the same design at different distances from the bytes: an append-only log of ops, with either a B-tree index over it (SQLite) or a hand-written scan (bare file). The B-tree is what makes the *router's* queries — not the analyst's — cheap, and SQLite supplies it with crash semantics that `[W3]` measured rather than assumed.

### 1.3 The checkpoint: what makes a 400-day log bootable

A fold from op zero is O(history), and history is 400 days. `[W7]` prices both halves:

| tail at boot | boot total | fold rate | digest == live state |
| --- | --- | --- | --- |
| cold fold, 200,000 op rows | **212 ms** | 502,928 rows/s | yes (`afd706883b5e6260…`) |
| snapshot every 100,000 rows (1 snapshot) | 157.8 ms | — | yes |
| snapshot every 50,000 rows (3) | 76.2 ms | — | yes |
| **snapshot every 10,000 rows (19)** | **15.5 ms** | — | yes |

At spike scale a cold fold is 0.2 s and looks free. At retention scale the same rate is **220 hours** (994 M rows/day × 400 days) — so snapshots are not an optimisation, they are what makes ADR-0004's window bootable. The shipped cadence (one snapshot per 10,000 op rows) costs 0.8 ms of writer p50 per snapshot, 0.76% of the run's wall time, and **0.091% of the writer's time at the fleet pace** (one snapshot per second of traffic), and it bounds a production boot's tail fold at ~20 ms.

Two properties of the checkpoint are load-bearing and both were tested rather than assumed:

- **Snapshot + tail is bit-identical to folding from zero** on the same file (`[W7]`(a)/(c): the same digest `afd706883b5e6260…`). That is what makes "the log is the source of truth" operational: any snapshot can be deleted and the answer does not change.
- **A corrupted snapshot is refused, not believed.** One flipped byte in the snapshot blob → boot REFUSED on a checksum mismatch, and the engine falls back to a cold fold (199 ms on the same file, same digest). The alternative — booting from a snapshot whose checksum does not match — is a router that has silently forgotten part of its history, and no downstream test catches it.

The spike also found the schema hole by comparing rather than reasoning: the posterior array survives a snapshot because the array *is* the snapshot, while the four derived integers (`ops folded` 106,564, `settled outcomes` 97,438, `drift resets` 3, `arms with mass` 734) report **0 / 0 / 0 / 734** when the tally blob is absent — i.e. the tail's counts silently presented as the whole history. R92 makes the payload explicit because the failure is a wrong answer, not a crash.

### 1.4 Re-bucketing, and why the log carries raw context

ADR-0006 R40 promises that changing the arm space is a re-fold, not a cold start. `[W7]`(d) prices the promise: folding the same 106,561 outcome rows into a coarser space (4 amount bands → 3, 5 regions → 3; 4,320 arms → 1,296) costs **0.19 s at 559,164 rows/s** with total α+β mass conserved exactly (97,438.0 both ways, 0 rows dropped). That is the difference between changing the arm space in an afternoon and re-running the fleet — and it is only available because every outcome row carries its raw context (`[W4]`'s layout D, the same schema minus six context columns, answers the re-bucketing query with **IMPOSSIBLE**).

---

## 2. Trace log: Parquet vs SQLite vs both (consideration 2)

### 2.1 The two consumers have incompatible query shapes

The trace has two readers and they want opposite things. The **router** reads its own log while writing it: an idempotency probe per redelivered outcome (R44), a windowed counter rebuild (ADR-0004), an analyst slice by region/bin/band, a posterior decode for OPE. Those are point and window queries against a file that is being appended to. The **analysis tier** reads sealed history: whole-window high-cardinality aggregation, OPE passes over every row, dashboards over 400 days. Those are column-oriented scans over cold data.

`[W4]`(b) prices the five shapes on four layouts, with SQLite's own plan next to each answer:

| query | A: columns + blobs (shipped) | B: blob only | C: JSON text | D: no raw context |
| --- | --- | --- | --- | --- |
| Q1 ingest idempotency probe (1,000 `(seq, attempt)` lookups) | 5.4 µs each | 5.4 µs | 9.8 µs | 5.1 µs |
| Q2 ADR-0004 counter rebuild (831 s window) | **27.4 ms**, index-bounded | 137.7 ms, 227,998 rows touched | 365.4 ms | 24.2 ms |
| Q3 analyst slice (region+bin+band) | **10.8 ms**, index-bounded | 138.9 ms | 271.8 ms | **IMPOSSIBLE** |
| Q4 R40 re-bucket (whole file, 7-key group by) | 384.4 ms | **214.8 ms** | 947.4 ms | **IMPOSSIBLE** |
| Q5 #15 OPE posterior decode (20,000 rows) | 1.80 µs each | **0.9 µs** | 7.18 µs | 1.1 µs |

At the fleet pace those per-row costs become the minutes that decide the design (`[W4]`(c): a one-hour window holds 41.4 M rows, a day 994 M): Q2 is **40 s** on the shipped layout against **600 s** on the blob layout and **1,592 s** on JSON; Q3 is 16 s against 605 s and 1,185 s. The plans are the explanation, not a mood: `SEARCH op USING INDEX op_ms (ms>? AND ms<?)` for A/D, `SCAN op` for B/C.

**Q4 is the counter-evidence and it is recorded, not argued away**: over the *whole* file, a 7-key group-by is slower in SQLite (384 ms) than decoding the same rows in CPython (215 ms), because SQLite builds a temp B-tree per group while the Python loop builds a dict — and at fleet scale the shipped layout's whole-day re-bucket projects to 226 minutes against the blob layout's 15.6. The honest response is not an index on a 7-column key; it is that whole-file high-cardinality aggregation belongs in the columnar tier. That is the measurement that justifies the export, and it comes from the layout that wins everywhere else.

### 2.2 What the columnar mechanism buys

Parquet cannot be measured in this sandbox (no pyarrow, no DuckDB, no Arrow C++), so `[W5]` measures the *mechanism* Parquet's numbers come from — row groups of ≤ 16,384 rows, typed column chunks, per-chunk zlib, and a footer carrying per-chunk min/max so a predicate can skip chunks it need not read — in ~150 lines of stdlib, on the rows the engine actually wrote:

| | measured | note |
| --- | --- | --- |
| export rate | 100,469 rows/s (5.3 µs/row) | against a write path sustaining 99,761 rows/s — export is as expensive as writing, so it is **offline only** |
| artifact size | 3.5 MB for 227,998 op rows (15 B/row) | **~21× smaller** than the SQLite row store holding the same rows; 24.6 MB uncompressed → 3.5 MB = **~7×** with zlib level 6 |
| footer | 31,024 B for 14 row groups | per-chunk min/max |
| windowed aggregate, SQLite index | 0.0201 s | competitive at this size — the row store is not bad at this query |
| windowed aggregate, columnar unpruned | 0.0252 s (1.12 M rows/s) | all 14 groups opened |
| windowed aggregate, columnar pruned | **0.0042 s (6.71 M rows/s)** | 2 of 14 groups opened, 12 skipped (86%); all three paths agree on the answer |
| whole-file scan, 3 of 22 columns | **0.011 s (21.65 M rows/s, 1.17 B/row read)** | against SQLite's 0.133 s (3.21 M rows/s, 178 B/row) |
| the same scan aggregated in CPython | 0.056 s (4.04 M rows/s) | separates the **format's** win from the **interpreter's** loss — the gap a vectorized engine closes, and the reason anything said here about DuckDB is a model |

The compression ratios above are *flattered* by the run's cycled row pool (100,000 decisions cycled 2× to reach n=200,000, so the value sequence repeats); the uncycled `--smoke` run measures ~7× zlib and ~21× against the row store, and this ADR quotes the uncycled figures. The pruning projection is arithmetic on the measured skip: a fleet day at 65,536-row groups is 15,161 groups, of which a one-hour window touches 632 — **95.8% of the file is never opened**. The skip ratio is a property of the data's time ordering, which is why it works: `trace/` appends in arrival order.

### 2.3 So: both, with duties split

The answer to consideration 2 is "both", but the interesting content is *which* both, and the boundary is a durability boundary rather than a format preference:

- **SQLite is the log and the hot queryable store**: append, dedupe, fold, boot, the router's own queries, the audit chain, retention by unlink (`W1`–`W4`, `W6`–`W10`).
- **The columnar export is the cold analytics artifact**, written once per sealed partition, after the run or after the day closes (`W5`). The engine writes no Parquet (ADR-0011); the export is a batch step owned by `analysis/`, and its column list, names and ordinals are the row store's, so a row means the same thing in both tiers.
- **Counter-evidence, recorded**: if #15's single-run OPE pass were the only consumer, the SQLite file with a covering index would be enough and the export would be pure cost — 5.3 µs/row of offline work and a second artifact to keep honest. The export is justified by the retention window (400 days of sealed partitions is where ~21× compression pays for itself: 3.26 TB against 70.6 TB) and by consumers this repo does not own yet. If neither materialises, drop the export and keep the schema (§8, trigger 4).

---

## 3. The schemas (the ticket's "specify the schema for each")

### 3.1 File map, pragmas, and who writes what

```
<state-root>/
  shard-<k>/
    journal.sqlite          money: lease, scheduled_event, ledger, boot   synchronous=FULL
    trace-<run|day>.sqlite  learning + audit: boot, decision, op,
                            snapshot, auth                               synchronous=FULL
    trace-<run|day>.sqlite-wal / -shm     (WAL sidecars; the WAL is the tail)
  sealed/                   closed partitions, read-only, expirable by unlink
  columnar/                 the offline export of sealed partitions (analysis/, never the engine)
```

Common pragmas on every connection: `journal_mode=WAL`, `synchronous=FULL` (`strict` class), `busy_timeout=5000`, `foreign_keys=OFF` (no cross-file FKs exist; the join keys are `(seq, attempt)`), `temp_store=MEMORY`, `wal_autocheckpoint=1000`. Row owners are ADR-0011 §7's, unchanged: `trace/` writes the WAL tail and snapshots, `router` writes lease rows *through* `store/` (R71), `store/` serves the catalog and owns the connections. One writer per file (R48); readers are separate connections and, per R94, separate *files*.

The DDL below is normative and is the DDL the spike executes — `[W1]`–`[W10]` are measurements of these statements, not of a similar schema.

### 3.2 `journal.sqlite` — the money file

```sql
-- The money file. synchronous=FULL: a lease row is durable BEFORE the dispatch it
-- authorizes (ADR-0009 R71), and the void/settle rows are durable before money moves
-- (R68). Small rows, low volume, one writer, no head-of-line blocking from the log.
CREATE TABLE IF NOT EXISTS lease (
  txn_seq     INTEGER NOT NULL,
  attempt     INTEGER NOT NULL,
  processor   INTEGER NOT NULL,
  key         BLOB    NOT NULL,                 -- 16 B CSPRNG nonce, persisted (R64/R71)
  dispatch_ms INTEGER NOT NULL,
  m_ms        INTEGER NOT NULL,                 -- the catalog's max_response_ms (R66)
  window_ms   INTEGER NOT NULL,                 -- the route class's window (R67)
  state       INTEGER NOT NULL,                 -- 0 inflight, 1 released, 2 gave_up
  PRIMARY KEY (txn_seq, attempt)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS lease_inflight ON lease(state, dispatch_ms);

-- The scheduled-event queue, keyed by VIRTUAL time (ADR-0005/ADR-0011): probe deadlines,
-- window expiry, late settlement, void follow-up. `tie` is a deterministic tie-break so a
-- replay of the same scenario pops the same order (ADR-0005 [M2]).
CREATE TABLE IF NOT EXISTS scheduled_event (
  due_ms   INTEGER NOT NULL,
  tie      INTEGER NOT NULL,
  kind     INTEGER NOT NULL,
  txn_seq  INTEGER NOT NULL,
  attempt  INTEGER NOT NULL,
  payload  BLOB,
  PRIMARY KEY (due_ms, tie, kind)
) WITHOUT ROWID;

-- The reconciliation ledger: keys stay queryable past their routing life (ADR-0009 §2 --
-- "reconciliation queries against an old key after the window are a #13 ledger concern").
CREATE TABLE IF NOT EXISTS ledger (
  key         BLOB    PRIMARY KEY,
  txn_seq     INTEGER NOT NULL,
  attempt     INTEGER NOT NULL,
  processor   INTEGER NOT NULL,
  opened_ms   INTEGER NOT NULL,
  closed_ms   INTEGER,
  terminal    INTEGER,
  reference   TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS boot (
  boot_id     INTEGER PRIMARY KEY,
  started_ms  INTEGER NOT NULL,
  durability  TEXT    NOT NULL,
  catalog_hash TEXT   NOT NULL,
  key_lifetime_ms INTEGER NOT NULL              -- the catalog's per-processor maximum (R66)
);
```

`m_ms` and `window_ms` are **catalog and config facts, not constants**: the spike reads `max_response_ms` per processor from the committed catalog (alpha 4,200 / bravo 5,600 / charlie 7,000 / delta 3,400 / echo 4,800 / foxtrot 10,500 ms in `acquirer-catalog-2026.09.2`) and `window_ms` from ADR-0009 R67's route-class table (30 s for `oneoff_cnp`, #12's config), and asserts R66's `key_lifetime_ms ≥ max_response_ms` at import. A lease row that carries its own M and window is self-describing under audit: an auditor can tell, from the row alone, when the ambiguity it recorded was retired and by which contract version.

### 3.3 `trace.sqlite` — the log, the decision record, the checkpoints

```sql
CREATE TABLE IF NOT EXISTS boot (
  boot_id         INTEGER PRIMARY KEY,
  started_ms      INTEGER NOT NULL,
  mode            TEXT    NOT NULL,             -- production | harness
  durability      TEXT    NOT NULL,             -- strict | batched | ephemeral
  shard           INTEGER NOT NULL,
  schema_version  INTEGER NOT NULL,
  policy_seed     INTEGER NOT NULL,
  catalog_hash    TEXT    NOT NULL,
  doc_hash        TEXT    NOT NULL,             -- the merchant ConstraintSet
  prior_hash      TEXT    NOT NULL,             -- the prior artifact (band edges ride it)
  arm_schema_hash TEXT    NOT NULL,             -- R39's key space, hashed
  config_hash     TEXT    NOT NULL,
  scenario_hash   TEXT,                         -- harness runs only: R37's world identity
  model_version   TEXT,
  harness_version TEXT,
  band_edges      BLOB                          -- the geometric ladder in force (R40)
);

-- The decision record: DECISION_LOG v1 (ADR-0008 R60). One row per decision. Scalar
-- fields a query names are columns (R89); the two fixed-layout arrays -- the per-eligible
-- posterior snapshot and the committed chain -- ride as blobs, because they are arrays,
-- and their decoder is committed code in analysis/.
CREATE TABLE IF NOT EXISTS decision (
  seq          INTEGER PRIMARY KEY,             -- the transaction's world identity
  boot_id      INTEGER NOT NULL,
  arrival_ms   INTEGER NOT NULL,
  bin_class    INTEGER NOT NULL,
  region       INTEGER NOT NULL,
  sca          INTEGER NOT NULL,
  mandate      INTEGER NOT NULL,
  band         INTEGER NOT NULL,
  amount_minor INTEGER NOT NULL,
  currency     INTEGER NOT NULL,
  merchant_cat INTEGER NOT NULL,
  route_class  INTEGER NOT NULL,
  entry_mode   INTEGER NOT NULL,
  eligible     INTEGER NOT NULL,                -- ProcessorBitmap, 8 bits/processor
  chosen       INTEGER NOT NULL,                -- attempt 0's acquirer ordinal
  chain_len    INTEGER NOT NULL,
  chain        BLOB    NOT NULL,                -- 27 B fixed layout: (u8 acquirer, i64 score)
  posteriors   BLOB    NOT NULL,                -- 80 B fixed layout, f32: k_max x (a,b,ta,tb)
  propensity   REAL    NOT NULL,                -- the score-based plug-in (R61)
  method       INTEGER NOT NULL,                -- plug-in-score-v1
  floor_state  INTEGER NOT NULL,                -- R49's onboarding state
  audit_hash   BLOB    NOT NULL                 -- 32 B, chained: ADR-0004 §6's audit record
);
CREATE INDEX IF NOT EXISTS decision_boot ON decision(boot_id, arrival_ms);

-- The op log: the WAL of learned state (ADR-0006 R47) and the outcome trace row
-- (ADR-0005 §5.2) in ONE write. op_seq is the fold's total order and is assigned by the
-- single writer; without a total order a DRIFT_RESET's decay does not commute with the
-- increments around it and replay is not bit-exact.
CREATE TABLE IF NOT EXISTS op (
  op_seq       INTEGER PRIMARY KEY,
  boot_id      INTEGER NOT NULL,
  kind         INTEGER NOT NULL,
  ms           INTEGER NOT NULL,                -- settle/arrival time in the run's clock
  seq          INTEGER NOT NULL,                -- transaction (0 for control ops)
  attempt      INTEGER NOT NULL,
  processor    INTEGER NOT NULL,
  outcome      INTEGER NOT NULL,
  code         INTEGER,
  decline_class INTEGER,
  latency_ms   INTEGER,
  settled_ms   INTEGER,
  -- raw context rides every OUTCOME row so re-bucketing is a re-fold, never a cold start
  bin_class    INTEGER, region INTEGER, sca INTEGER, mandate INTEGER,
  amount_minor INTEGER, band INTEGER, currency INTEGER,
  merchant_cat INTEGER, route_class INTEGER, entry_mode INTEGER,
  -- world identity per row (ADR-0005 R37): a row without its seed is history
  scenario_hash TEXT,
  payload      BLOB                             -- kind-specific: drift (tier,gamma,delta),
                                                -- prior-swap ref, late-settlement detail
);
CREATE UNIQUE INDEX IF NOT EXISTS op_dedupe ON op(seq, attempt) WHERE kind = 1;  -- OUTCOME
CREATE INDEX IF NOT EXISTS op_ms ON op(ms);

-- Snapshots are checkpoints of the fold (ADR-0006 §7): each names the op_seq it is a
-- checkpoint OF, so boot = newest snapshot + the ops after it. Nothing else writes state.
CREATE TABLE IF NOT EXISTS snapshot (
  op_seq       INTEGER PRIMARY KEY,
  taken_ms     INTEGER NOT NULL,
  arm_schema   TEXT    NOT NULL,
  n_arms       INTEGER NOT NULL,
  posterior    BLOB    NOT NULL,                -- 138,240 B at 4,320 arms x 4 f64
  detector     BLOB,                            -- ADWIN buckets, <= 2 KB/processor (R56)
  counters     BLOB,                            -- the windowed counter arena (ADR-0004)
  tallies      BLOB    NOT NULL,                -- the fold's derived integers (R92)
  checksum     TEXT    NOT NULL                 -- over the blobs: a mismatch refuses boot
);
```

Op kinds are one vocabulary in two files, split by the durability split and not by the data model: **learning kinds** in `trace.sqlite` — `OUTCOME=1`, `DRIFT_RESET=2`, `AUTH_RECORD=3`, `PRIOR_SWAP=4`, `LATE=5`, `SNAPSHOT_MARK=6` — and **money kinds** in `journal.sqlite` — `LEASE=16`, `RELEASE=17`, `GAVE_UP=18`, `VOID=19`, `SETTLE=20`. `op_seq` is assigned by the single writer and is the fold's total order; `LATE` exists because a correction must never be a second `OUTCOME` for the same key (R93, ADR-0008 R62).

Two schema notes that carry measured weight:

- **`propensity` is a column *and* the blob carries the inputs.** R61's logged plug-in is dashboard-grade provenance; #15 recomputes exact propensities from `posteriors` + `eligible` + `policy_seed`. Both halves are priced: the 80 B f32 blob decodes at 1.80 µs/row against 7.18 µs for the same twenty numbers as JSON text (`[W4]` Q5, 4.0×), which is R89's array half.
- **`tallies` is in the snapshot because its absence is a silent wrong answer** (§1.3). It is 4 integers; it is not optional.

### 3.4 AUTH_RECORD v1 gets a table, not a payload

ADR-0010 R72 hands this ticket the record's *layout and retention*, joined to DECISION_LOG v1 by `seq`. The spike wrote AUTH_RECORD as an op with a fixed-layout payload; the shipped schema promotes it to its own table, because R72's fourteen fields are exactly the fields #16's panels group by (`session_outcome`, `flow_version`, `exemption_requested/accepted`, `challenge_presented/completed`) and R89 says a scalar a query names is a column. `[W4]` is the evidence for that rule and it is not close: a blob-only layout turns a windowed aggregate into a full-file walk with a decode per row (137.7 ms against 27.4 ms here; 600 s against 40 s at the fleet pace).

```sql
-- AUTH_RECORD v1 (ADR-0010 R72): one row per attempt whose flow touched an authentication
-- step, joined to decision by seq and to op by (seq, attempt). Written by trace/ in the
-- SAME transaction as the attempt's OUTCOME op, so it is inside the same durability
-- boundary, the same partition and the same retention unit. Not re-labelled on late
-- settlement (ADR-0008 R62); a session with no terminal state stays `unattributed`
-- (session_outcome = 0), counted and excluded from rate denominators, never imputed.
CREATE TABLE IF NOT EXISTS auth (
  seq                 INTEGER NOT NULL,
  attempt             INTEGER NOT NULL,
  processor           INTEGER NOT NULL,
  ms                  INTEGER NOT NULL,
  flow_version        INTEGER NOT NULL,
  session_outcome     INTEGER NOT NULL,   -- closed vocabulary, R72; 0 = unattributed
  challenge_indicator INTEGER NOT NULL,
  exemption_class     INTEGER,
  exemption_requested INTEGER NOT NULL,   -- 0/1
  exemption_accepted  INTEGER NOT NULL,   -- 0/1
  challenge_presented INTEGER NOT NULL,   -- 0/1
  challenge_completed INTEGER NOT NULL,   -- 0/1
  liability_shift     INTEGER NOT NULL,   -- 0/1
  session_fee_minor   INTEGER NOT NULL,   -- incurred, path-accurate (R75)
  submission_fee_minor INTEGER NOT NULL,  -- incurred, path-accurate (R75)
  auth_fee_minor      INTEGER NOT NULL,   -- the catalog fact the fee model read
  PRIMARY KEY (seq, attempt)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS auth_ms ON auth(ms);
CREATE INDEX IF NOT EXISTS auth_session ON auth(processor, flow_version, session_outcome);
```

Retention is the trace partition's (R91): an AUTH_RECORD dies with the day it was written in, and the *counts* that outlive it (`session_unattributed`, exemption-budget burn) are fold tallies in the snapshot, not rows. Cost, stated as arithmetic on measured parts rather than as a measurement — the spike's write profile is decisions + outcome ops, and this table's rows were not written: at the world's measured auth share (0.289) the fleet profile moves 11,500 → **13,376 rows/s**, headroom per shard 10.0× → **8.6×**, and a fleet day 0.99 G → **1.16 G rows / 205 GB** (`[W6]`(d) prints all four lines). The mechanism the table exercises is one more narrow row in the same file, same transaction, same durability class, which `[W1]` and `[W4]` priced; #17 re-measures the shipped schema in Go.

### 3.5 The ticket's minimum column list, mapped

#13 named a minimum trace schema. Every field is present; two are present *by derivation* and that is a decision, not an omission:

| The ticket's column | Where it lives | Why |
| --- | --- | --- |
| `timestamp` | `decision.arrival_ms`, `op.ms`, `op.settled_ms`, `auth.ms` | the run's **virtual** clock (ADR-0005, R84); three timestamps because arrival, settlement and resolution are different events and ADR-0008 R62 folds on arrival |
| `transaction_id` | `decision.seq`, `op.seq`, `auth.seq` (+ `attempt`) | `seq` is the world identity; `(seq, attempt)` is the dedupe key R44 requires |
| `request_context` | the eleven raw columns on `decision` **and** on every `OUTCOME` op (`bin_class`, `region`, `sca`, `mandate`, `band`, `amount_minor`, `currency`, `merchant_cat`, `route_class`, `entry_mode`, `eligible`) | duplicated onto the op row on purpose: R40's re-bucketing must not need a join, and `[W4]`'s layout D shows what dropping them costs (IMPOSSIBLE on Q3/Q4) |
| `chosen_arm` | `decision.chosen` + `decision.chain` + `decision.eligible` | the arm is `(context key, processor)`; the chain is the full attempt sequence with per-step scores (27 B blob) |
| `propensity` | `decision.propensity` + `decision.method`, with `decision.posteriors` (80 B f32) as the recompute input | R61: the logged value is a tagged plug-in; #15 recomputes exact propensities from the posteriors |
| `outcome` | `op.outcome`, `op.code`, `op.decline_class`, `op.latency_ms`, `op.settled_ms` | the closed vocabulary is `core`'s (ADR-0011 §2.2); `decline_class` is what makes a hard decline non-retryable without a join |
| `reward` | **derived, not stored** — computable from `op.outcome` + `auth.session_fee_minor` + `auth.submission_fee_minor` + the catalog's fee facts + `decision.chain` | ADR-0002's reward is a *function* of the label, the priced ambiguity and the path-accurate fees; storing it freezes the reward version into every row and makes a reward change (ADR-0002's reopen triggers) a data migration instead of a re-fold. The inputs are stored; `analysis/` owns the function. Recorded as a deviation from the ticket's list, with the reason |
| `constraint_set_hash` | `boot.doc_hash` (per run) + `decision.audit_hash` (per row, chained over the row that carries the context) | the ConstraintSet cannot change inside a run — a document swap is a new boot — so a per-row copy of the hash would be 32 B of constant. ADR-0004 §6's audit record is the per-row commitment and it is chained (R95) |

### 3.6 The export's column list

`analysis/` exports `op` (and `auth`, `decision` on request) as row groups of ≤ 65,536 rows in production (16,384 in the spike, to keep the artifact small), typed column chunks, per-chunk zlib, and a footer of per-chunk min/max plus the run's `scenario_hash`. The op export's 22 columns, in ordinal order — the same names and ordinals as the row store, so a row means the same thing in both tiers:

```
op_seq i64 | kind i32 | boot_id i32 | ms i64 | seq i64 | attempt i32 | processor i32 |
outcome i32 | code i32 | decline_class i32 | latency_ms i32 | settled_ms i64 |
bin_class i32 | region i32 | sca i32 | mandate i32 | amount_minor i64 | band i32 |
currency i32 | merchant_cat i32 | route_class i32 | entry_mode i32
```

Blobs are exported **decoded** (`posteriors` → k_max × 4 f32 columns, `chain` → up to 3 × (acquirer, score) pairs, `auth` payload fields → their own columns), because the export's readers are vectorized and the decoder is committed code with a version byte. The 3-of-22-column scan that `[W5]`(c) prices at 21.65 M rows/s and 1.17 B/row read is the payoff, and the min/max footer is what makes a window query touch 632 of 15,161 groups.

---

## 4. Sharding and scale: can SQLite carry a 1M-transaction simulation? (consideration 4)

Short answer: yes, by 8.6–10× on one shard in CPython on a container overlay filesystem, with the fsync — not the B-tree, not the language, not SQLite — as the binding cost. The long answer is the four subsections below, because "can it keep up" is the cheap half of consideration 4 and the expensive half is what a day, a window and a reader cost.

### 4.1 The durability ladder

`[W1]`(a) writes the real rows (DECISION_LOG v1 plus the outcome op with its raw context — not a synthetic one-column insert) across 36 configurations. The rows that decide anything:

| journal | durability class | batch | rows/s | µs/row | commit p50 | commit p99 | B/row | vs the 11,500 rows/s profile |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| WAL | `strict` (FULL) | 1 | 7,015 | 142.6 | 126 µs | 541 µs | 178 | 0.6× |
| **WAL** | **`strict` (FULL)** | **64** | **108,006** | **9.3** | **467 µs** | **2,339 µs** | **177** | **9.4×** |
| WAL | `strict` (FULL) | 1024 | 167,092 | 6.0 | 4,385 µs | 11,088 µs | 177 | 14.5× |
| WAL | `batched` (NORMAL) | 64 | 162,938 | 6.1 | 265 µs | 4,868 µs | 177 | 14.2× |
| WAL | `ephemeral` (OFF) | 64 | 201,817 | 5.0 | 253 µs | 957 µs | 177 | 17.5× |
| DELETE | `strict` (FULL) | 1 | **1,068** | 936.3 | 889 µs | 1,512 µs | 176 | 0.1× |
| DELETE | `strict` (FULL) | 1024 | 162,413 | 6.2 | 5,369 µs | 6,750 µs | 177 | 14.1× |
| MEMORY | `ephemeral` | 1 | 58,651 | 17.1 | 15 µs | 35 µs | 176 | 5.1× |

Readings that carry into the rules:

- **An fsync is a batch decision, not a row decision.** WAL+FULL is 7,015 rows/s one-row-per-commit and 108,006 at batch 64 — **15× for the same guarantee**. `[W1]`(b) is the mechanism: commit p50 grows sub-linearly in the batch (135 µs at 1 row, 460 µs at 64, 4,181 µs at 1,024), so the amortised price of durability per row falls from 134.7 µs to 4.10 µs — two orders of magnitude across the curve.
- **The rollback journal is disqualified at the same durability**: 1,068 rows/s against WAL's 7,015 one-row-per-commit, 6.6× worse, because it fsyncs twice per commit. It reaches 157,000 rows/s only at batch 1024, i.e. only by becoming a batch store with a worse tail.
- **Once the batch exists, the guarantee is nearly free**: at batch 64 the ladder is 108,006 (FULL) / 162,938 (NORMAL) / 201,817 (OFF) — NORMAL buys 1.5× and OFF 1.9×. That is why the log can run at `FULL` and still keep ADR-0011 R83's ack a real durability claim, and why `batched` exists only for harness runs (§5.1).
- **`wal_autocheckpoint=0` is the slowest writer measured**, not the fastest: 64,628 rows/s against 112,711 at 1000 pages (43% slower), with an 87.7 MB WAL after 100,000 rows that grows with the run, because every commit walks a longer frame chain and every byte of it is a byte the next boot replays. The shipped 1000 pages costs 3,305 µs of p99.9 stall — a tail on one commit in a thousand, not a wall — and keeps the WAL under 4.2 MB. Disabling the checkpoint is a hazard dressed as an optimisation.

External corroboration, labelled as other people's boxes and cited because it agrees in shape rather than in number: published WAL benchmarks put WAL+NORMAL in the 70k–100k write-transactions/s range for typical record sizes and report `synchronous=FULL` inserts at roughly a third of that rate (deepwiki's `forwardemail/sqlite-benchmarks` collection; tenthousandmeters.com, Feb 2025), and a production FHIR ingest (Helios FHIR Server PR #1114) measured `synchronous=NORMAL` under WAL removing ~45% of per-batch commit fsync wall. This spike measures the same ordering with `FULL` kept, and pays for it with a group commit instead of a weaker pragma.

### 4.2 The integrated run, and where the wall time goes

`[W6]` runs the shipped configuration (WAL, FULL, group commit 64 rows / 5 ms, autocheckpoint 1000) over 200,000 decisions from the committed scenario and their 227,998 ops:

| | measured |
| --- | --- |
| whole run | 427,998 log rows in **3.7 s** = **115,118 rows/s**, 53,794 decisions/s |
| artifact | 76.1 MB = **177.7 B/row** = **380.3 B/decision** |
| commits | 6,689, totalling 3.30 s = **89% of wall** at 443 µs each |
| append minus commit (tuple build, `executemany`, B-tree) | 0.17 s (5%) |
| the loop feeding it | 0.25 s (7%) |
| first segment vs last | 99,670 → 113,324 rows/s = **1.14×** (no degradation as the file grows) |

The decomposition is the load-bearing part: **the store is fsync-bound, not CPU-bound**. A Go driver's per-call overhead lands in the 5% append bucket, not in the 89% commit bucket — which is why #17 can choose the driver later and why ADR-0001's `modernc.org/sqlite` pre-commitment is not a performance risk at this pace. It also bounds the device risk honestly: on a volume whose fsync is 1–5 ms instead of this box's ~0.47 ms, the commit bucket grows by roughly that factor, which at the shipped batch of 64 still leaves ~12,800 rows/s of ceiling on one shard (`[W6]`(b), `[W2]`(a)'s model).

Against the harness, the store is not free and does not hide: the world model alone costs 38.9 µs/decision (25,727/s), the store alone 18.6 µs (53,794/s), summed 57.5 µs (17,404/s) — **the store adds 48%** to per-decision cost, so a 200,000-decision run costs 11 s with the store against 8 s without. In Go the world side gets faster and the store side stays fsync-bound, so the store's share grows; the headroom below is a floor, not a forecast.

### 4.3 Shard arithmetic, and the ticket's 1M transactions

One writer per shard (R48), so the fleet's pace is met by adding shards, not by making one faster:

| quantity | value |
| --- | --- |
| fleet pace to sustain (ADR-0001 + ADR-0006) | 11,500 rows/s |
| one shard, measured here | 115,118 rows/s → **10.0× headroom, 1 shard** |
| **incl. AUTH_RECORD rows (R72, measured share 0.289)** | **13,376 rows/s → 8.6× headroom, still 1 shard** |
| 1M transactions, rows | 2.14 M |
| **1M transactions, wall time on one shard** | **19 s** |
| 1M transactions, artifact | 380 MB |
| one day of fleet traffic | 0.99 G rows (1.16 G with auth rows) |
| one day on one shard | 2.4 h of writing (2.8 h with auth rows, arithmetic) |
| one day's file | 177 GB (205 GB with auth rows) |

The ticket asked whether SQLite can handle 1M-transaction simulation write throughput. It is a **19-second job on one shard**, producing a 380 MB artifact, at a rate that does not degrade as the file grows (1.14× first segment to last). A harness run of 1M transactions with a full audit trail is therefore an interactive job, not an overnight one — which is the property #17's benchmark suite and #15's OPE loops need.

### 4.4 Retention: what 400 days costs, and how a day dies

`[W8]`(a), at both paces the repo quotes (the scenario's declared 30 TPS and ADR-0001's fleet budget):

| | at scenario pace | at fleet pace |
| --- | --- | --- |
| decisions/day | 2.60 M | 432 M |
| log rows/day | 5.6 M | 994 M |
| SQLite row store, one day | 0.99 GB | 177 GB |
| **SQLite row store, 400 days** | **395.2 GB** | **70.62 TB** |
| columnar export, one day | 0.05 GB | 8.1 GB |
| **columnar export, 400 days** | **18.2 GB** | **3.26 TB** |
| learned state (snapshots, all shards) | 0.14 MB | 0.14 MB |
| largest single **file** in the ladder | 0.99 GB | 2.04 GB |

The last row is the operational one: the fleet-pace column is the *whole fleet's* budget, and R91 makes the partition one file per (shard, day), so no single file is 177 GB — it is one shard's day, and a shard is sized by the writer. The learned state is 135 KiB per shard however long the chain gets, which is the point of the fold: history is bytes on a volume, state is a fixed-size array in memory. Retention is a storage question with a deletion mechanism, not a state question.

`[W8]`(c) measures the deletion mechanism on a real three-day file (213,321 rows, 38.4 MB):

| mechanism | seconds | file after | reclaimed |
| --- | --- | --- | --- |
| `DELETE FROM … WHERE day = 0` | 0.091 | 38.4 MB | **0.0%** |
| … then `VACUUM` | 0.112 | 25.5 MB | 33.6% |
| **drop the partition file (`unlink`)** | **0.0000** | **0.0 MB** | **100%** |

`DELETE` frees pages for reuse and gives nothing back; `VACUUM` rewrites the file, takes a lock, needs room for a second copy, and costs more than the delete it cleans up after. Expiry is therefore an `unlink` (R91), and the retention ladder needs no `DELETE` anywhere: **hot** = today's open trace file (1 day, rolled at midnight); **warm** = sealed per-day partitions (400 d, unlink); **cold** = the columnar export of sealed days (400 d, unlink); **state** = snapshots plus the tail since (forever, superseded by the next snapshot). Expiring a warm partition never changes the router's behaviour — it changes what an auditor can ask about, which is what a retention window is for. Counter-evidence recorded: day-partitioning means a query spanning days opens several files; the answer is `ATTACH` over a handful of sealed files (a few hundred µs of open cost) and the columnar tier for whole-window spans, where `[W5]`'s pruning makes the span cheap. If a deployment needs one queryable file across the window, the partition size is the knob, not the mechanism.

### 4.5 The audit chain

ADR-0004 §6's one-record-per-decision claim needs a chain, and `[W8]`(b) prices it on the write path and on the auditor's pass:

| | rows/s | B/row | file |
| --- | --- | --- | --- |
| per-decision hash, unchained | 114,207 | 177.3 | 37.9 MB |
| **chained (`sha256` over prev + row + seq)** | **91,269** | **177.3** | 37.9 MB |

The chain costs **+0.0 B/row** (32 B either way — chaining changes what is hashed, not what is stored) and 20.1% of write throughput in the committed run; four passes of this section on this box measured 9.8%, 10.7%, 17.3% and 20.1%, so the ADR quotes **10–20%** and notes that the design does not depend on which end is true (the alternative — no chain — is not available, and a per-batch chain would weaken the guarantee rather than cost much less). Verification is a scan, not a join: 100,000 decisions in 0.51 s = **194,561 rows/s** (194,561–235,198 across passes), a fleet day of 432 M decisions in **37 min**, the scenario's day in 13.4 s — and the chain head (`c4537ae58909f93b…`) is identical across runs, which is the determinism claim the timings do not get to make. The tamper test edits one column of one decision mid-file (`amount_minor + 1`) and the verifier **stops at seq 50000 after 50,001 links**: detection is a property of the chain, not of the store, since SQLite would happily return the edited row.

### 4.6 Readers against the writer

WAL promises readers do not block the writer and the writer does not block readers. `[W10]` measures the promise in forked **processes** with their own connections (what a dashboard is), against a file warmed to 427,998 rows / 76.0 MB, four seconds per phase:

| phase | writer rows/s | commit p50 | commit p99 | worst | WAL peak | Δ throughput |
| --- | --- | --- | --- | --- | --- | --- |
| writer alone | 83,997 | 519 µs | 1,014 µs | 8.1 ms | 0.5 MB | — |
| + 4 idle processes (control) | 76,436 | 569 µs | 1,087 µs | 3.0 ms | 0.9 MB | **−9%** |
| + 2 point probes | 49,635 | 571 µs | 8,391 µs | 12.1 ms | 5.7 MB | −41% |
| + 4 mixed readers (point, window, scan) | 28,358 | 968 µs | **11,117 µs** | 19.2 ms | 117.4 MB | **−66%** |
| + 1 held read transaction (1.51 s) | 43,308 | 1,291 µs | 2,181 µs | 17.8 ms | **180.2 MB** | −48% |

Readers themselves: **0 errors in every phase, no torn rows**, point probes at 12.4–12.5 µs p50, a window aggregate at 15.1 ms p50 / 42.0 p99, a full scan at 64.2 / 104.3 ms, and the writer never waited on a reader's lock. Three conclusions, in order of how much they cost:

1. **WAL does not promise free cores.** The control phase is the point: four processes that open the file and do nothing cost 9% on this 2 vCPU box, and four that actually query cost 66%. That difference is SQLite work — page cache, B-tree traversal, I/O — competing for two cores; on a box with cores to spare the reader share shrinks and never reaches zero, because a reader and a writer on one file share a page cache and a volume.
2. **A reader costs the writer's tail, not its median** (p50 +87%, p99 +996%: 1,014 µs → 11.1 ms). For the trace file that is affordable — `[W1]` sized it at 9× the pace. For the money file it would not be, and this is the second leg of §5.3's two-file argument: a dashboard query that costs 10 ms of tail must not be able to touch the lease path's 2 ms budget.
3. **The hazard is a reader that holds its transaction open**, and it is a protocol property rather than a performance one. WAL cannot reclaim frames an open reader might still need, so the WAL grows for as long as the reader holds, and a `PASSIVE` checkpoint that runs meanwhile **comes back having stopped short without reporting itself busy**: 405 of 405 checkpoints in that phase, a worst gap of **18,064 frames**, a WAL peak of **180.2 MB against 0.5 MB** — **119 MB of WAL per second of held read**, i.e. ~429 GB pinned by a reader held for one hour. The signal is the WAL peak and the frame gap, never the pragma's return code. It is a pin, not a leak: a `TRUNCATE` checkpoint at the end of every phase reclaimed the WAL to 0.0 MB. Counter-evidence recorded: the held reader cost *less* throughput (48%) than four short readers (66%), because a held read is idle CPU — so if a deployment's only readers are short and bounded, R94 reads as paranoia. It is cheap paranoia; the alternative failure mode is running out of disk on the machine that holds the money rows.

### 4.7 Exactly-once learning

R44's dedupe-on-`(seq, attempt)` is inherited as a requirement, not an option (ADR-0006 §3c measured +11.1% phantom counts with it off). `[W9]` prices the enforcement and then attacks it:

| schema | rows/s | B/row | append p50 | append p99 |
| --- | --- | --- | --- | --- |
| with `CREATE UNIQUE INDEX op_dedupe ON op(seq, attempt) WHERE kind = OUTCOME` | 101,020 | 177.3 | 0.42 µs | 475.7 µs |
| without it | 116,982 | 169.7 | 0.40 µs | 410.9 µs |

Cost: **+7.6 B/row exactly**, and 13.6% of write throughput in the committed run — five passes of this section on this box measured 3.0%, 11.8%, 13.6%, 15.6% and 25.1%, so the ADR quotes **3–25%** with the ordering as the claim (the index is not free, and it is cheaper than any read-before-write alternative at every point in that band). The index is **partial** and that is not a detail: control ops (`DRIFT_RESET`, `PRIOR_SWAP`, `SNAPSHOT_MARK`) have no `(seq, attempt)` meaning, and a full unique index would either reject them or force a fake key into the row; partial also keeps the index smaller than the table, which is why the byte cost is what it is.

The redelivery attack, on one file written through the shipped path:

| delivery | attempts | rows added | op rows now |
| --- | --- | --- | --- |
| first delivery (the truth) | 113,999 | 113,999 | 113,999 |
| full redelivery, in order | 113,999 | **0** | 113,999 |
| 30% redelivered out of order, with mutated timings | 34,199 | **0** | 113,999 |
| 200 redelivered with a **conflicting** outcome | 200 | **0** | 113,999 |

Duplicate `(seq, attempt)` outcome rows in the file: **0**. Cost of a redelivery on the ingest path: **5.84 µs per ignored insert** (113,999 of them in 0.67 s, one commit). Fold digest after the first delivery `6f7533725b1f68edf318e2f1…`; after **148,398 redeliveries**, the same — bit-identical. And the dropped-conflict counter is **200 of 200, free**: it is `changes()` on the insert.

What "ignore" means has to be written down, because the store cannot know which delivery is true: **the first outcome wins**, which is right for a retry of the same attempt (the first delivery is the one the money moved on) and wrong for a corrected outcome — so a correction arrives as a distinct op kind (`LATE`, priced in ADR-0009), never as a second `OUTCOME` for the same key, and the dropped-conflict count is a metric to alert on (R93). Counter-evidence recorded: an ingest that must *distinguish* "already learned" from "conflicting redelivery" pays a read for the distinction; this design does not pay it in the write path, it counts the conflicts and lets an offline pass decide — affordable only because the log keeps every row that *was* accepted and the conflict is visible in the ingest's own counters.

---

## 5. The checkpoint / durability guarantee, as a contract

### 5.1 Three classes, one of which may carry money

| class | pragmas | un-fsynced window | who may use it |
| --- | --- | --- | --- |
| **`strict`** | WAL, `synchronous=FULL`, `wal_autocheckpoint=1000` | one group commit: ≤ 64 rows or ≤ 5 ms on the trace; one convoy: ≤ 32 leases or ≤ 0.5 ms on the journal | production, and *anything* that carries money or audit — both files, always |
| `batched` | WAL, `synchronous=NORMAL` | one checkpoint interval: 4,863 rows / 0.42 s measured here | harness runs only, where the artifact is reproducible from its scenario hash and losing it costs a re-run rather than an audit hole |
| `ephemeral` | WAL/MEMORY, `synchronous=OFF` | everything since the last checkpoint | scratch and experiments. **Refused for `journal.sqlite` at open** — a money file with no claim is a bug, not a mode |

The class is a column in the `boot` row of both files, so an artifact says what it was written under, and a `batched` or `ephemeral` partition is distinguishable from a `strict` one by an auditor without asking anyone.

### 5.2 What an ack claims

`[W3]`(c) is the whole content of the guarantee, in rows and seconds:

| pragma | un-fsynced window | rows | s at fleet pace | s at 20 TPS |
| --- | --- | --- | --- | --- |
| WAL + FULL (`strict`) | one group commit | 64 | **0.006 s** | 2.5 s |
| WAL + NORMAL (`batched`) | 14 checkpoints seen | 4,863 | 0.423 s | 187.0 s |

So: **an ack given after a `strict` commit is a claim that survives power loss, and the exposure behind it is one group commit — 5.6 ms of fleet traffic.** `batched`'s window is 76× wider and is a housekeeping cadence rather than a promise. Both files ship `strict`; the 1.5× throughput `batched` would buy (`[W1]`(a)) is not a trade for an audit trail, and ADR-0011 R83's ack is honoured rather than approximated.

### 5.3 The money row's guarantee, and why it has its own file

ADR-0009 R71 — the lease, including its CSPRNG key, durable **before** the dispatch it authorizes — puts an fsynced write on the route path against ADR-0001's 2 ms p99 budget. `[W2]` measures it four ways:

- **Per lease, it fits.** One durable lease in its own file at `FULL` costs **467 µs p50 / 475 µs mean / 703 µs p99 = 23% / 35% of the 2 ms budget**. R71 is affordable per row on this device.
- **Per second, it does not — without a convoy.** K=1 sustains 2,104 dispatches/s against a fleet pace of 6,500 (0.3×). The convoy curve is the fix and it is steep: commit cost moves 1.8× while K moves 64× (475 µs at K=1, 763 µs at K=64), so the per-lease price falls **40×** and WAL amplification falls **20×** (9,045 → 459 B/lease). At K=64 the ceiling is 83,890 dispatches/s (12.9×).
- **The device is the risk, and it is labelled.** This box fsyncs in ~475 µs. On a network-attached volume in the 1–5 ms class the same arithmetic gives (model, constants inline — the fsync is the one number this sandbox cannot speak for): at 1 ms, K=1 serves 1,000/s and K=32 serves 32,000/s; at 5 ms, **K=1 serves 200/s (3% of the pace)** and K=32 serves 6,400/s. The convoy therefore ships **as the mechanism, not as an optimisation**, with K=1 as its degenerate low-volume case — at the 20 TPS deployment ADR-0006 sizes the WAL against, the assembly wait T fires before K fills and the convoy *is* the solo commit. K and T are config; the design must not depend on being handed a fast fsync.
- **Shipped K/T, chosen against the budget with the trace writer running in parallel**: `[W2]`(d) runs four dispatcher threads against their own file while the log writer sustains the fleet pace. K=32 / T=0.5 ms gives **4,823 leases/s at 2.0 leases per fsync, p50 670 µs and p99 1,822 µs — inside the 2 ms budget with the trace writing 20,186 rows/s beside it**. T is the binding half at low volume: T=2.0 ms at the same K gives p50 2,589 / p99 3,334 µs (over budget) and only 1,509 leases/s; K=64 / T=4.0 ms collapses to 848/s. `leases/fsync` is the metric to watch — if it sits at 1.0 at peak, the money path is paying full price for durability and reopen trigger 2 is live.

Why two files, stated honestly — **not** because of device interference. `[W2]`(b1) alternates a log batch commit and a lease commit single-threaded and measures lease p99 **721 µs with its own file against 659 µs sharing the log's**, log batch p99 328 vs 330 µs: whichever way the sign falls on a given run, the difference is inside this box's spread, and this ADR does not claim otherwise. The split is bought by three things that do not need a benchmark, plus one that does:

1. **Rotation.** The trace file is a partition: written for a day or a run, sealed, exported, eventually dropped (R91). A lease written at 23:59:58 resolves after midnight, and the ledger keeps its key for `key_lifetime_ms` and its reconciliation entry for longer. Money rows in a rotating file must be migrated forward at every rotation; money rows in their own file must not.
2. **Writer cadence.** The log's writer is the ingest fold's writer — batched, ≤ 64 rows, throughput-shaped (R48). The money writer is the route path's convoy — latency-shaped, one row per dispatch. One connection cannot be both without one inheriting the other's cadence, which is the `shared_writer` breach below.
3. **Tail.** `[W2]`(b2) models the write lock from measured parts: a batch-64 commit holds the file's single write lock ~467 µs, the fleet pace needs 180 such commits/s, so the lock is held **8.4%** of the time and a shared-file lease waits an extra 467 µs on ~8% of dispatches — p99 721 → 1,188 µs, still inside budget. A **checkpoint stall** in the same file is not: 3–10 ms of p99.9 (`[W1]`(c)), and a checkpoint is exactly what a shared file puts in the money path's tail. Add `[W10]`'s reader cost — a dashboard query moving the commit p99 to 11.1 ms — and the tail argument is decisive.
4. **`shared_writer` is rejected by arithmetic.** The lease riding the log writer's own batch and connection waits the group-commit interval (up to 5 ms) plus the batch's own fsync (467 µs) = **up to 5,467 µs, 2.7× the 2 ms budget**, before any device tail, and inherits the checkpoint stall on top. ADR-0006 R48's one-writer-per-shard rule is about who mutates the posterior arrays; reading it as a *file* rule costs the money path its latency budget.

`[W2]`(a2) closes the loop on the checkpoint from the money side: the same K=1 convoy with the shipped `wal_autocheckpoint=1000` commits at 131 µs p50 / 170 µs mean with a 4.1 MB WAL and 1,007 B/lease; with `wal_autocheckpoint=0` the *same* commit costs 450 / 472 µs (**2.8×**) with a 38.3 MB WAL and 9,349 B/lease (**9×**). Checkpoint cadence is part of the write path's cost model, not housekeeping.

### 5.4 Crash-resume protocol

What `[W3]` measured, as the procedure a restarting engine runs:

1. **Open both files.** A process crash loses nothing committed under any pragma (0 of 2,000 rows at FULL, NORMAL, OFF, under WAL *and* DELETE), because the WAL is in the OS page cache and the OS is still alive. Reopen costs 0.2–0.3 ms. This is also why FULL-vs-NORMAL cannot be settled by killing processes — it is a power-loss question.
2. **Roll back the open transaction.** Killed mid-transaction, the reopen returns exactly the committed prefix: 2,001 issued, 1,992 durable, **1,992 recovered, 0 lost**, `quick_check` ok. Atomicity measured, not assumed. The same holds killed mid-checkpoint (2,000/2,000): a checkpoint is a copy plus a WAL reset and both are recoverable — which matters because it is the one place the log and the database file are written together.
3. **Verify the snapshot, then fold the tail.** Boot = newest `snapshot` whose checksum verifies + the ops after its `op_seq` (§1.3). A checksum mismatch refuses the snapshot and falls back to a cold fold: slow (212 ms per 200,000 rows here; 220 h at retention scale, which is why the cadence exists) and correct.
4. **Re-open the money side by state, not by memory.** `lease WHERE state = inflight` gives the attempts whose ambiguity must be retired: probe them to their `m_ms`/`window_ms` (both in the row), and `ledger` gives the keys that stay queryable past their routing life. `scheduled_event` is keyed by virtual time, so a harness resume pops the same order it would have (ADR-0005 [M2]).
5. **Power loss takes exactly the un-fsynced bytes.** Emulated by truncating the WAL tail with `wal_autocheckpoint=0` (nothing in the database file yet): every cut reopens `ok` and returns a **prefix**, never a corrupt middle — 99% kept → 1,976 of 2,000 rows (1.2% lost), 50% → 1,048 (47.6%), 10% → 200 (90%), 0% → unqueryable, because losing the WAL *header* leaves no frame chain to validate. Frame checksums discard the torn tail rather than believing it. Not emulated, and not claimed: page tears in the database file and filesystem metadata reordering.

### 5.5 What is *not* guaranteed

Stated so nobody inherits a promise this ADR did not make: (i) `batched` and `ephemeral` partitions lose up to a checkpoint interval and everything since the last checkpoint respectively — the `boot` row says which class a partition was written under; (ii) an in-flight transaction at kill time is rolled back, so a dispatch whose lease committed but whose outcome did not is *ambiguous by construction* and is retired by ADR-0009's probe loop, not by the store; (iii) a held reader can grow the WAL without bound (R94) — the store bounds it by refusing long reads on the live file, not by pragma; (iv) the columnar export is a derived artifact — it can be deleted and rebuilt from a sealed partition, and it is never a source of truth; (v) nothing here survives a volume that lies about fsync, which is a device-purchase question #17 answers with the production volume.

---

## 6. Payload for dependent tickets

### 6.1 The catalog re-pin, executed in this PR (ADR-0011 §7's instruction)

ADR-0011 §7 put the example catalog's three missing contract facts in the hands of "the ticket that first consumes them — here that is #13 — with the scenario digests re-committed in the same PR. Do not split that change." This PR is that change:

- `constraints/catalog/acquirer-catalog.example.json` gains **`max_response_ms`, `key_lifetime_ms`, `auth_fee_minor`** per acquirer, in place, and moves `acquirer-catalog-2026.09.1` → **`acquirer-catalog-2026.09.2`** (`generated_at` 2026-09-17, description extended, a `contract_facts_note` key recording the derivation and provenance). Capability and economics are unchanged, so no earlier result that depends only on those moves — the *digests* move, the numbers do not.
- **`max_response_ms`** is ADR-0009 R66's M, and the fixture derives it as `clamp(10 × the declared latency p95, 3000, 15000)` — the rule `spikes/0010-idempotency/protocol.py` (`contract_max_response_ms`) already derived M with, so the catalog fact and the committed protocol spike agree instead of the fixture inventing a second rule: alpha 4,200 / bravo 5,600 / charlie 7,000 / delta 3,400 / echo 4,800 / foxtrot 10,500 ms. **`key_lifetime_ms` = 86,400,000** (24 h) for all six, which satisfies R66's `key_lifetime ≥ M` and is asserted at import by the spike. **`auth_fee_minor` = 2** for the five 3DS-capable processors — the value `spikes/0011-sca-friction` measured with — and **0 for echo**, which has no 3DS capability and can therefore never incur one. In production all three are onboarding-recorded contract facts, not derived ones.
- The catalog hash is pinned by every scenario document (`simulator/scenarios/check.py` SV3), so all nine scenario digests moved and `golden/scenario-hashes.json` was re-pinned by `--pin` in the same commit. Both gates pass (`constraints/check.py`, `simulator/scenarios/check.py`).

Accepted ADRs and older spike results cite the *old* digests and are not edited (supersede-don't-edit). This table is the record of what those citations now resolve to:

| scenario | digest before this PR | digest after |
| --- | --- | --- |
| `baseline-steady-v1` | `sha256:16661ded41dd…` | `sha256:b49193b7e715…` |
| `black-friday-degraded-v1` | `sha256:7e939d9d908a…` | `sha256:89c669ee7371…` |
| `idempotency-stress-v1` | `sha256:14273698a329…` | `sha256:984db10a2af0…` |
| `outage-recovery-v1` | `sha256:7ccf83824414…` | `sha256:1e0cd5cd764e…` |
| `replay-trace-v1` | `sha256:681e449349a7…` | `sha256:abdba21ec1e9…` |
| `long-refused-v1` (spike 0007) | `sha256:ba8b8fdd9b30…` | `sha256:21f0cffd7ad9…` |
| `quiet-improvement-v1` (spike 0009) | `sha256:43e4672b1080…` | `sha256:4cc6a620baa5…` |
| `quiet-improvement-starved-v1` (spike 0009) | `sha256:2202daa793db…` | `sha256:b58ae93262af…` |
| `sca-friction-v1` (spike 0011) | `sha256:8089760b6cd8…` | `sha256:b099068d430c…` |

Two side effects, recorded so they are not discovered as bugs: the two **valid** ConstraintSet examples (`merchant-default.json`, `marketplace-strict.json`) moved their `policy.catalog_version`/`catalog_hash` with the catalog, so the repo declares one catalog version everywhere it declares one; the six **invalid** fixtures keep `2026.09.1`, because a document compiled against an older catalog is not thereby invalid and the constraints gate does not verify the pin against the file — that wording is #5's. And the earlier spikes' RESULTS.md files still print the old digests in their headers: they are committed output of a past run, and re-running them reproduces their numbers under the new digests, since no committed world model reads the three new fields (spikes 0010 and 0011 carry their own constants for M and the auth fee).

### 6.2 `internal/store/` — the binding this ADR specifies

ADR-0011 made `store/` a package and fixed its row owners; this ADR fills in the API. The import DAG is respected: `store` imports `core` only, and `trace/` (which owns the record schemas) converts its rows into `store`'s before appending — `store` never imports `trace`.

```go
// Package store is the state-store binding: two SQLite files per shard via
// modernc.org/sqlite (ADR-0001: CGO_ENABLED=0), one writer each (ADR-0006 R48).
package store

// Durability is the class a file is opened in (§5.1). Only Strict may carry money or audit;
// Open refuses Ephemeral for the journal file.
type Durability int

const (
    Strict    Durability = iota // WAL + synchronous=FULL + wal_autocheckpoint=1000
    Batched                     // WAL + synchronous=NORMAL: harness runs only
    Ephemeral                   // OFF/MEMORY: scratch, no claim
)

// Config is the shipped configuration. Every field was measured; the comment names where.
type Config struct {
    Dir               string
    Shard             int
    Durability        Durability
    GroupCommitRows   int       // <= 64 on the trace (§4.1, [W1]a/b)
    GroupCommitMS     int64     // <= 5
    AutocheckpointPages int     // 1000: 0 is the slowest writer AND an unbounded WAL ([W1]c)
    ConvoyK           int       // <= 32 leases per fsync on the journal (§5.3, [W2]d)
    ConvoyTMS         int64     // <= 0.5 ms assembly wait; T binds at low volume
    SnapshotEveryOps  int       // 10,000: one per second of fleet traffic (§1.3, [W7]b)
    Partition         Partition // PerRun (harness) | PerDay (production) (§4.4)
}

// Store owns the two connections and the in-memory fold accumulators. One writer per file.
type Store struct{ /* unexported */ }

func Open(cfg Config, cat Catalog) (*Store, error) // asserts R66 (key_lifetime >= M), stamps boot
func (s *Store) Boot() (Boot, error)   // newest verifying snapshot + tail fold; cold-fold fallback
func (s *Store) AppendDecision(dec DecisionRow, ops []OpRow) (opSeq int64, err error)
                                       // one transaction, one group commit, chained audit_hash
func (s *Store) AppendAuth(a AuthRow) error       // same transaction as the attempt's OUTCOME op
func (s *Store) Lease(l LeaseRow) error           // synchronous + convoyed: durable BEFORE dispatch
func (s *Store) Release(txnSeq int64, attempt int, state LeaseState) error
func (s *Store) Inflight() ([]LeaseRow, error)    // crash-resume step 4 (§5.4)
func (s *Store) Schedule(e ScheduledEvent) error  // keyed by VIRTUAL time
func (s *Store) Due(nowMS int64) ([]ScheduledEvent, error)
func (s *Store) Ledger(key []byte) (LedgerEntry, bool, error)
func (s *Store) Snapshot(st FoldState) error      // posterior + detector + counters + TALLIES
func (s *Store) Seal() (Partition, error)         // close, checkpoint(TRUNCATE), mark read-only
func (s *Store) Catalog() Catalog                 // atomic value swap on onboarding (ADR-0011)
func (s *Store) VerifyChain(fromSeq int64) (int64, error) // stops at the first mismatch

// FoldState is what a snapshot must carry (§1.3, R92): the arrays AND the derived integers.
type FoldState struct {
    Posterior  []float64 // n_arms x 4, the fold's accumulator
    Detector   []byte    // ADWIN buckets, <= 2 KB/processor (ADR-0007 R56)
    Counters   []byte    // the windowed counter arena (ADR-0004)
    Tallies    Tallies   // ops_folded, settled_outcomes, drift_resets, arms_with_mass
    OpSeq      int64     // the op_seq this state is a fold OF
}

// Reader opens a SEALED partition or a replica copy, never the live file (R94).
func Reader(path string) (*Reader, error)
```

### 6.3 What each dependent ticket must design against

- **#14 (safe rollout)**: alarm suppression reads the detector buckets and counter arena that `Boot()` returns — they are in the snapshot, so a rollout gate does not need a fold. The `boot` row's `config_hash`, `prior_hash`, `arm_schema_hash` and `catalog_hash` are the proof that the fleet is running what a rollout believes it is running; compare them across shards rather than trusting config distribution.
- **#15 (OPE)**: exact-propensity recompute needs `decision.posteriors` (80 B f32, k_max × (α, β, τ_a, τ_b)), `decision.eligible`, `decision.chain` (27 B: (u8 acquirer, i64 score_minor) × ≤ 3) and `boot.policy_seed` — the logged `propensity` is R61's tagged plug-in, dashboard-grade only. Read sealed partitions or the columnar export (both are `Reader`-shaped, never the live file); the export decodes the blobs into typed columns (§3.6). `VerifyChain` is the integrity gate before a run is used as evidence, and a `batched`/`ephemeral` partition is not admissible as production evidence — the `boot` row says which.
- **#16 (dashboard)**: reads `store/` and nothing else (ADR-0011), through `Reader` against sealed partitions or a replica (R94) — a dashboard query on the live file costs the writer 66% of its throughput and 10 ms of commit tail (`[W10]`), and an interactive shell that holds a transaction pins ~119 MB of WAL per second. The panels' group-by fields are columns: `auth(session_outcome, flow_version, …)` with `auth_session` indexed, `op(ms)` with `op_ms` indexed. `session_unattributed` (R72) and the dropped-conflict count (R93) are named counters, and the WAL peak / stopped-short-checkpoint counts are metrics worth a panel of their own.
- **#17 (benchmarks)**: four things only this ticket's gaps can hand you. (i) **The driver**: ADR-0001 pre-commits `modernc.org/sqlite` for `CGO_ENABLED=0`; published Go-driver benchmarks put pure-Go insert overhead at ~1.9–5.6× C sqlite and query overhead at ~2.5–12.3× (billmill.org, Sept 2024; a Feb 2026 round has `modernc` at ~114% of `mattn`'s insert rate), and §4.2 says that overhead lands in the **5% append bucket, not the 89% commit bucket** — so benchmark it, but do not expect it to move the design. (ii) **The shipped schema's absolutes in Go**, including the `auth` table this ADR specifies but the spike priced only as arithmetic (§3.4). (iii) **The fsync latency of the production volume** — the one number this sandbox cannot speak for, and the input to §5.3's slow-device model. (iv) **`leases/fsync` at peak**: if it sits at 1.0, the convoy is not assembling and reopen trigger 2 is live.
- **#12 (routing engine)**: `Lease()` is synchronous and convoyed; call it on the dispatch path and treat the returned error as "not dispatched". `m_ms` and `window_ms` come from the catalog and the route-class config, never from a constant (§3.2). `Due()` is keyed by virtual time, so the harness's injected `Clock` drives it unchanged (R84).
- **#5 (constraint layer)**: the catalog re-pin above is landed; the `Catalog` value `store/` serves is the same document your evaluator reads, hashed into every audit record. What remains yours: the wording for a ConstraintSet pinned to an older catalog version (§6.1), and the exemption-class allow-list and liability budget ADR-0010 handed you.

---

## 7. Consequences and design rules

**Positive.** One writer per file, one artifact that is simultaneously the audit trail and the source of learned state, a boot that is a bounded fold, retention that is an `unlink`, and a 1M-transaction simulation with a full audit trail that finishes in 19 seconds. Replay is bit-exact by construction (R47's fold + R60's key-addressed draws + `op_seq` as the total order), which means the store never has to be trusted: it can be re-derived and compared, and `[W7]`/`[W9]` do exactly that.

**Costs, named and accepted.** (1) The store adds ~48% to a harness run's per-decision cost in CPython, and its share *grows* in Go as the world model gets faster — an audit-trailed simulation costs roughly twice a bare one. (2) Two files per shard means two connections, two WALs, two checkpoint cadences and no cross-file transaction: a lease and its decision row are **not** atomic together, and the resume protocol (§5.4) is what makes that safe rather than a join. (3) 177 GB/day at the fleet pace, 70.6 TB across the retention window in the row store — the columnar tier is not optional decoration, it is a 21× reduction on the cold copy. (4) The convoy adds up to T=0.5 ms of assembly wait to a solo lease at low volume, which is the price of a mechanism that also works at 6,500 dispatches/s. (5) A partial unique index costs 3–25% of write throughput for exactly-once learning.

- **R86** — Two files per shard, split by durability: `journal.sqlite` (`lease`, `scheduled_event`, `ledger`, `boot`) and `trace-<run|day>.sqlite` (`boot`, `decision`, `op`, `snapshot`, `auth`). Money rows never live in a file that is sealed and dropped on a schedule. One writer per file (R48 stands); readers are separate connections on separate files (R94).
- **R87** — Three durability classes: `strict` (WAL + `synchronous=FULL`), `batched` (NORMAL), `ephemeral` (OFF/MEMORY). Production, money and audit are `strict` only; `batched` is harness-only; `ephemeral` is refused for `journal.sqlite` at open. The class is a column in both `boot` rows, so an artifact declares what it was written under.
- **R88** — The guarantee is a **window**, stated in rows and seconds: `strict` bounds it at one group commit (≤ 64 rows or ≤ 5 ms on the trace; ≤ 32 leases or ≤ 0.5 ms on the journal). An ack given after a `strict` commit is a power-loss-survivable claim (ADR-0011 R83). No code path may widen the window without changing the class in the `boot` row.
- **R89** — Scalars a query names are **columns**; fixed-layout arrays are **blobs** carrying a version byte, with the decoder committed in `analysis/`. Both halves are measurements (`[W4]`: a windowed query on a blob-only layout costs 5× and hands over the whole file; the posterior array decodes 4.0× faster as a blob than as JSON numbers).
- **R90** — One `op` table, one writer-assigned `op_seq`, and it is the fold's **total order**. Learning kinds (1–6) live in the trace file, money kinds (16–20) in the journal. Without a total order a `DRIFT_RESET` does not commute with the increments around it and replay is not bit-exact.
- **R91** — The **partition file is the retention unit**: one file per (shard, run) in harness mode, per (shard, day) in production. Expiry is `unlink`. No `DELETE` in an expiry path, and no `VACUUM` in a write path (`[W8]`c: 0.0% / 33.6% / 100% of bytes reclaimed).
- **R92** — A snapshot's payload is explicit and checksummed: posterior array, detector buckets, counter arena, **and the fold tallies** — one row, one checksum. A snapshot whose checksum does not verify is **refused**, and boot falls back to a cold fold. Derived state that is not in the payload does not survive boot, and its absence is a silent wrong answer rather than a crash.
- **R93** — Dedupe is `CREATE UNIQUE INDEX … ON op(seq, attempt) WHERE kind = OUTCOME` with `INSERT OR IGNORE`: **first write wins**, a correction arrives as a distinct op kind (`LATE`), never as a second `OUTCOME` for the same key, and the dropped-conflict count is a **metric** (`changes()` on the insert), not a silent discard.
- **R94** — Long, interactive or unbounded reads go to a **sealed partition or a replica copy**, never to the live file. An open read transaction pins the WAL and `PASSIVE` checkpoints stop short **without reporting busy**; the signals are the WAL peak and the frame gap, never the pragma's return code. The live file is for the writer and for short bounded reads.
- **R95** — Every decision row carries `audit_hash`: `sha256` over (previous row's hash, this row's canonical bytes, `seq`). The chain is verified by a scan that stops at the first mismatch, and the chain head is part of a run's identity. Verification is admissible evidence only against a `strict` partition.
- **R96** — The columnar export is **offline**, owned by `analysis/`, written once per sealed partition; the engine writes no Parquet (ADR-0011). The export's column names and ordinals are the row store's, and blobs are exported decoded, so a row means the same thing in both tiers. Export costs as much as writing (`[W5]`a), so it is never in-path.
- **R97** — Boot = the newest verifying snapshot + the ops after its `op_seq`. Snapshot cadence is **every 10,000 op rows** and once on clean shutdown; it is a durability-adjacent parameter, not a tuning knob, because a cold fold at retention scale is ~220 hours.
- **R98** — `store/` serves the catalog: `max_response_ms`, `key_lifetime_ms` and `auth_fee_minor` are read from the served `Catalog` and stamped into rows (a lease carries its own M and window), never hardcoded. `Open` asserts R66's `key_lifetime_ms ≥ max_response_ms` per processor and refuses to start otherwise.
- **R99** — `schema_version` is a column in the `boot` row, and any field change is a **version bump, never an edit** (R60's rule applied to the store). A reader that meets a newer `schema_version` refuses the partition rather than guessing.

---

## 8. Reopen triggers

1. **Shard throughput.** If the shipped configuration sustains < 3× the fleet profile (≈ 40,000 rows/s incl. auth rows) in Go on the production volume, one shard is not enough and R48's one-writer rule starts to cost shards; reopen the shard count and the partition size. Measured here: 8.6–10.0× in CPython on a container overlay filesystem.
2. **The money row's device.** If the production volume's fsync is > 5 ms p99 such that a K ≤ 32 / T ≤ 0.5 ms convoy cannot hold 6,500 dispatches/s (§5.3's model says 6,400/s at 5 ms), or if `leases/fsync` sits at 1.0 at peak, R71's fit fails and the lease needs a different device or a replicated store. Measured here: 4,823 leases/s at 2.0 leases/fsync, p99 1,822 µs of a 2 ms budget.
3. **The reader hazard.** If R94 cannot be honoured — a consumer must read the live file — and the pinned-WAL hazard fires (WAL peak > 10× the autocheckpoint target, or checkpoints stopping short for > 60 s), reopen reader placement; the alternatives are a replicated read tier or a `wal_autocheckpoint` policy that trades writer throughput for WAL bounds. Measured here: 119 MB of WAL per second of held read.
4. **The export's consumers.** If neither #15's multi-run OPE nor #16's window panels materialise and every consumer is served by the row store with a covering index, **drop the columnar export and keep the schema** (`[W5]`'s recorded counter-evidence). The trigger is a consumer census, not a benchmark.
5. **The audit chain.** If chaining costs > 25% of write throughput in Go on the production device (measured 10–20% here) or a day's rows cannot be verified inside the audit window (37 min here for 432 M decisions), reopen the chain's granularity — per-batch instead of per-decision — and say plainly which guarantee was traded.
6. **Boot time.** If boot from snapshot + tail exceeds 1 s on a production partition (15.5 ms measured at a 10,000-row tail), the cadence or the fold is wrong: reopen R97 before touching the schema.
7. **Retention volume.** If 205 GB/day per fleet (84 TB across the window) exceeds the storage budget, reopen the *window* (ADR-0004's 400 days) or the row width (§4.4's ladder) — in that order, because the row width is what makes the queries answerable and Appendix B shows where the bytes actually are.

---

## 9. Alternatives considered

### 9.1 Redis / Valkey for model state — the strongest steelman, and why it loses

**Steelman.** The posteriors are hot, concurrent, small and read on every decision; Redis is built for exactly that, with sub-millisecond point access, native replication and failover, TTLs that map cleanly onto a lease's lifetime, and pub/sub a dashboard can subscribe to. SQLite's single-writer rule looks like a limitation next to that, and a networked store is the industry's default answer to "state that many processes need". If the fleet ever grew to multiple writers per arm space, this would be the design.

**Why not.** (i) *It does not remove the log, it adds one.* R47 makes state a fold over an append-only log; Redis would hold the accumulator and still need the log beside it, so the deployment runs two persistence mechanisms — AOF (the same fsync arithmetic as `[W1]`, on a server this repo does not control) or RDB (a checkpoint with `[W3]`'s loss window and no WAL to reconstruct from). (ii) *It puts a network hop inside a 2 ms budget.* ADR-0001 spends that budget on the whole in-engine path; `[W2]` shows a local fsynced lease already costs 23–35% of it, and a round trip to a Redis replica is not cheaper than a local page write. (iii) *It solves a problem R48 deleted.* One writer per shard means there is no concurrent-writer arbitration to buy; the failover story it offers is a story about a shard's file, which is a `Seal()` and a new boot. (iv) *It does not answer the trace*, which is 99.8% of the bytes (177 GB/day against 135 KiB of state) and needs windowed queries, a hash chain and an `unlink`-shaped retention. Not measurable in this sandbox — no server, and a loopback benchmark on 2 vCPU would measure the stack — so the rejection rests on the structural argument plus the cited fsync arithmetic, and is recorded as such.

### 9.2 LMDB / bbolt (memory-mapped B-tree, no SQL)

**Steelman.** Single file, MVCC, copy-on-write B-tree, no query planner, and append-heavy write rates that beat SQLite's in published microbenchmarks; in Go, `bbolt` is a few thousand lines with no CGo. For a log-shaped workload it is arguably the more honest data structure. **Why not.** Every query shape in `[W4]` would be hand-written against a KV API: the windowed counter rebuild, the analyst slice, the point probe and the 7-key re-bucket all become application code with application bugs, and the partial unique index that makes R93 free becomes a read-before-write on the ingest path. There is no `PRAGMA quick_check`, no `EXPLAIN QUERY PLAN` to put in an ADR, and no `ATTACH` for the warm tier. The store's job here is to make four query shapes cheap and one crash story provable; SQL is how both are audited. Reopen if #17 shows SQLite's per-call overhead dominating the append bucket in Go — which §4.2 says it will not.

### 9.3 Parquet-only trace

**Steelman.** The columnar artifact is ~21× smaller, scans at 6.7–21.7 M rows/s with pruning, and every *consumer* (#15 OPE, #16 panels, ADR-0004's counter rebuild over history) is an analytics reader. A row store is a relic: write Parquet, keep the posteriors in memory, and skip SQLite entirely. **Why not.** Parquet is immutable once written, so it cannot be the log the fold replays: no append, no dedupe key, no crash-consistent tail, no checkpoint to refuse. And two of the four query shapes are the *router's own*, at write time — the ingest idempotency probe (R44) and the windowed counter rebuild — which need point and window access to a file being appended to. Hence §2.3's split, which keeps the win (the cold artifact) and the mechanism (the hot log).

### 9.4 A bare event-sourced file (no database)

**Steelman.** The purest reading of R47: framed append-only records, state by replay, no B-tree, no planner, and the fastest append a filesystem can offer. Everything in this ADR's op log is already that. **Why not — and note how much of it ships.** The shipped design *is* an event-sourced log with an index over it: `op` is append-only, `op_seq` is the total order, state is a fold, snapshots are checkpoints. What the bare file cannot do is answer the router's own queries without scanning, and `[W4]` measures precisely that failure mode: the blob-only layout (a tape with a schema) answers the windowed rebuild in 137.7 ms by handing over all 227,998 rows, against 27.4 ms index-bounded — 600 s against 40 s at the fleet pace. The B-tree is not a departure from event sourcing; it is what makes event sourcing queryable at 994 M rows/day.

### 9.5 One file for money and learning

**Steelman.** One fsync target, one connection, one checkpoint cadence, and — the real prize — a lease and its decision row in a *single atomic transaction*, which the two-file design cannot offer. Operationally simpler: one file to back up, one to seal, one to lose. **Why not.** Rotation (money rows must not live in a file sealed and dropped on a schedule, and a lease written at 23:59:58 resolves after midnight); cadence (a throughput-shaped batch writer and a latency-shaped convoy cannot share a connection without one inheriting the other's); and tail (a checkpoint stall in the shared file is 3–10 ms of p99.9 against a 2 ms budget, and `[W10]`'s readers add 11.1 ms of commit p99 — neither may touch the lease path). Device interference, the intuitive argument, **measured inside the noise** (721 µs vs 659 µs lease p99) and is recorded as counter-evidence rather than claimed. The atomicity loss is real and is paid for by §5.4's resume protocol: `lease WHERE state = inflight` is the recovery source, and the decision row that never arrived is an ambiguity ADR-0009's probe loop retires, not a corruption.

### 9.6 In-memory + periodic checkpoint, as the ticket wrote it

**Steelman.** The state is 135 KiB; the hot path is a float64 increment; a checkpoint every N seconds costs one blob write. Nothing about a *simulation* needs a durable log, and a harness run is reproducible from its scenario hash anyway — so the log is ceremony. **Why not, for the log.** `[W3]` measures the ceremony: 500 of 2,000 rows lost with **no log to reconstruct them from**, and those decisions have no audit row, so #15 cannot recompute their propensities and ADR-0004 §6's one-record-per-decision claim has a hole in it. In production the same option loses leases, which is money. **Adopted, for the arrays**: the accumulators live in memory exactly as the option describes, and the snapshot *is* the periodic checkpoint — R97 just makes its payload explicit and its cadence bounded, downstream of a log rather than instead of one.

---

## Appendix A: reproduction, sandbox, and variance

```bash
python3 spikes/0013-state-store/store.py 200000          # RESULTS.md is this output (~4 min)
python3 spikes/0013-state-store/store.py --smoke         # every section, ~90 s, uncycled pool
python3 spikes/0013-state-store/store.py 200000 --section=W3,W7   # one or more sections
python3 simulator/scenarios/check.py && python3 constraints/check.py   # both gates, re-pinned
```

Sandbox: 2 vCPU, 3.8 GiB, container overlay filesystem, Python 3.11.2, SQLite 3.40.1, scratch on a 21 GB tmpfs-backed `/tmp`; no Go toolchain, no pyarrow/DuckDB/Arrow, no network, no way to cut power. Deterministic where determinism is meaningful: fixed scenario digest, fixed policy seed, key-addressed draws (ADR-0005/0006).

**Structural results reproduce exactly** across runs and are the load-bearing claims: rows lost and recovered per crash configuration (0 / 500 / 1,992), integrity verdicts, the torn-WAL prefix property, the fold digest (`afd706883b5e6260…` live == cold fold == snapshot+tail), the redelivery digest (`6f7533725b1f68edf318e2f1…` before and after 148,398 redeliveries), the dropped-conflict count (200/200), the audit chain head (`c4537ae58909f93b…`), bytes per row (177.6/165.0/486.9/155.7), the retention ladder, and the `IMPOSSIBLE` verdicts.

**Timings move**, and this ADR quotes bands where the band matters: five passes of `[W9]`'s index cost gave 3.0 / 11.8 / 13.6 / 15.6 / 25.1% (the `without` side is stable at 116,982–121,364 rows/s while the `with` side moves with page-cache state), four passes of `[W8]`'s chain cost gave 9.8 / 10.7 / 17.3 / 20.1%, and `[W1]`'s identical configurations moved up to ~20% between passes. Where a single number is quoted above it is the committed run's; where the spread is wider than the claim, the band is quoted and the ordering is the finding.

## Appendix B: byte accounting, from measured deltas

| component | B/row | source |
| --- | --- | --- |
| shipped layout (columns + fixed-layout blobs) | **177.6** | `[W4]`a, 427,998 real rows, 76.0 MB |
| … minus the six raw-context columns (layout D) | −21.9 (155.7) | the price of R40's re-bucketing promise — and layout D answers Q3/Q4 with IMPOSSIBLE |
| … as a blob-only row (layout B) | −12.6 (165.0) | buys 1.08× narrower, costs 5× on a windowed query |
| … as JSON text (layout C) | +309.3 (486.9) | 2.7× wider, 2.3× slower to write, worst on every query |
| partial unique index for R93 dedupe | **+7.6** | `[W9]`a, exact across every pass |
| chained `audit_hash` (R95) | **+0.0** | 32 B either way; chaining changes what is hashed, not what is stored |
| per decision (2.14 rows each) | **380.3** | `[W6]`a |
| learned state, per shard, any chain length | 138,240 B per snapshot (135 KiB) | 4,320 arms × 4 f64; `[W8]`a: 0.14 MB at either pace |
| columnar export of the same op rows | **15 B/row** | `[W5]`a: ~21× the row store, ~7× its own uncompressed bytes |

The accounting is the argument in one column: the bytes this design spends are spent on *queryability* (21.9 B/row of raw context, 7.6 B/row of dedupe) and none of them on *self-description* (JSON's +309.3 B/row buys nothing a version byte and a committed decoder do not). The cheap tier is the cold one, and it is 21× cheaper because the columns are typed and the values repeat — which is also why the export exists and why the engine never writes it.
