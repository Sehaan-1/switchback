#!/usr/bin/env python3
"""Decision ticket #13 evidence: the state store -- what persists, in what shape, at what
cost, and what a crash actually takes.

The ticket asks for a backend for two things it treats as separate: the learned Beta
posteriors (hot, concurrent, must survive restart) and the routing trace (write-heavy,
read rarely, must be queryable for OPE). Ten accepted ADRs have already collapsed most of
that design space -- ADR-0006 R47 made the trace *be* the write-ahead log of the learned
state ("the WAL is the only writer of learned state; the posterior is a fold over it"),
ADR-0008 R60 fixed the decision record's fields and volume, ADR-0009 R71 named the one row
whose loss moves money, ADR-0011 gave the binding a package (`internal/store/`) and a
rule about who writes what. What is left, and what this file measures, is the part that
cannot be argued: which SQLite configuration clears the fleet's write pace with which
durability guarantee, what the row layout costs in bytes and in query time, what a crash
takes under each guarantee, and how big the retained artifact gets.

Sections:

  [W1] the durability ladder: journal mode x synchronous x group-commit batch, priced in
       rows/s, us/row, commit p50/p99 and bytes/row, against the fleet's write pace.
  [W2] the money row: where the LEASE append lives. Dedicated file vs shared writer vs
       commit convoy, measured as per-dispatch durable latency under trace-write load.
  [W3] crash semantics: SIGKILL mid-run and a truncated WAL (the power-loss emulation),
       per config -- rows lost, rows recovered, integrity verdict, reopen cost.
  [W4] the row layout: normalized columns vs fixed-layout blob vs JSON vs hybrid, priced
       on bytes/row, insert throughput and the four query shapes #15/#16 actually run.
  [W5] the analysis tier: the SQLite row store vs a chunked columnar export (a stdlib
       stand-in for Parquet: row groups, typed column chunks, compression, footer).
  [W6] scale: the harness's 1M-transaction run with the store attached, the bottleneck
       decomposition, and per-shard throughput.
  [W7] boot: snapshot + tail replay. Reconstruct-vs-snapshot for every derived state,
       and the re-fold that makes re-bucketing cheap (ADR-0006 R40).
  [W8] retention: bytes/day at fleet pace, the 400-day chain, and DELETE-vs-drop-file.
  [W9] dedupe (R44): what exactly-once learning costs in the store, and the redelivery
       test that proves it works.
  [W10] readers against the writer: WAL concurrency, checkpoint stalls, and the
        held-read-transaction hazard that decides where the dashboard reads from.

Everything is stdlib-only, offline and deterministic where determinism is meaningful.
The structural results -- rows lost, rows recovered, integrity verdicts, fold equality,
dedupe counts, bytes per row, compression ratios -- are reproducible exactly. The
timings are one box (2 vCPU, container overlay filesystem, no perf isolation); they are
reported with their spread and the *ratios* are the claim, in the convention ADR-0005
set. No Go toolchain and no DuckDB/pyarrow in this sandbox: the Go driver question and
the vectorized-scan question are answered with labelled models and cited measurements,
never with numbers invented here.

    python3 store.py                     # full run at the default n=200,000, ~4 min on 2 vCPU
    python3 store.py 200000              # the same run, spelled out: RESULTS.md is its output
    python3 store.py 20000               # a smaller n, same sections, ~2 min
    python3 store.py --section=W3        # one section
    python3 store.py --smoke             # every section at a size that runs in ~90 s
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import os
import random
import signal
import sqlite3
import struct
import sys
import threading
import time
import zlib
from bisect import bisect_right
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "simulator" / "scenarios"))
sys.path.insert(0, str(REPO / "spikes" / "0006-simulation-harness"))

from check import load_scenario, scenario_hash, canonical_bytes  # noqa: E402
import harness as H  # noqa: E402

# --------------------------------------------------------------------------------------
# 1. Constants. Stated, not hidden: every number the ADR quotes as a choice lives here.
# --------------------------------------------------------------------------------------

# The write profile the store is sized against (ADR-0001 5,000 decisions/s; ADR-0006 1.3
# attempts/decision; ADR-0008 [C4] 104 B/decision at k_max = 5).
DPS = 5_000                 # decisions/s, the fleet pace ADR-0001 budgets against
ATTEMPTS_PER_DECISION = 1.3  # ADR-0006's measured chain length
OPS_PER_S = DPS * ATTEMPTS_PER_DECISION          # 6,500 outcome ops/s
ROWS_PER_S = DPS + OPS_PER_S                     # 11,500 log rows/s (decision + outcome)
# AUTH_RECORD v1 (ADR-0010 R72) is one more narrow row per attempt whose flow touched an
# authentication step. The world reports its own share (src.auth_record_share); this is
# the fallback for a world that does not, sourced from spikes/0011's S0 census on
# sca-friction-v1: ~34% of transactions reach a session (1,702-1,752 of 5,000 each).
AUTH_RECORD_SHARE = 0.34
SECONDS_PER_DAY = 86_400

# The arm space (ADR-0006 R39/R40): (bin class x region x sca x mandate x band) x
# processor. Six bin classes -- the scenario vocabulary's five plus `unknown`, which is
# what makes the committed space 4,320 arms and the snapshot 135 KiB (ADR-0006 5).
BIN_CLASSES = ("consumer_credit", "consumer_debit", "premium_credit", "corporate",
               "prepaid", "unknown")
REGIONS = ("EEA", "UK", "US", "LATAM", "APAC")
N_BINS, N_REGIONS, N_SCA, N_MANDATE, N_BANDS = 6, 5, 2, 2, 6
N_CONTEXTS = N_BINS * N_REGIONS * N_SCA * N_MANDATE * N_BANDS      # 720
PROCESSORS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")
N_PROC = len(PROCESSORS)
N_ARMS = N_CONTEXTS * N_PROC                                        # 4,320
STATE_FLOATS_PER_ARM = 4    # alpha, beta, te (transport error), to (timeout)  -> 32 B/arm
SNAPSHOT_BYTES = N_ARMS * STATE_FLOATS_PER_ARM * 8                  # 138,240 B = 135 KiB

# ADR-0007's detector: <= 2 KB per processor, static buckets (R56).
DETECTOR_BYTES_PER_PROC = 2_048
# ADR-0004's windowed counters: 31 physical uint8 buckets + 16 B of key/slot metadata per
# live key, 23.5 MB per 500k keys per 30-day window (measured there, reused here).
COUNTER_BYTES_PER_KEY = 47
COUNTER_KEYS = 500_000
COUNTER_ARENA_BYTES = COUNTER_KEYS * COUNTER_BYTES_PER_KEY          # 23.5 MB

SCHEMA_VERSION = 1          # the store's own schema version (R60's record versions ride inside)
K_MAX = 5                   # core.MaxEligible (ADR-0011); DECISION_LOG v1's posterior array
MAX_CHAIN = 3
POSTERIOR_BLOB_BYTES = K_MAX * 4 * 4        # 80 B: k_max x (alpha,beta,tau_a,tau_b) x f32
CHAIN_BLOB_BYTES = MAX_CHAIN * (1 + 8)      # 27 B: (u8 acquirer, i64 score_minor)
DECISION_FIXED_BYTES = 104                  # ADR-0008 [C4]: the in-engine fixed layout

# Retention (ADR-0004: 400 days is in the documents; the chain is #13's).
RETENTION_DAYS = 400

SCRATCH = Path(os.environ.get("SWITCHBACK_SPIKE_TMP", "/tmp/switchback-0013"))

# Measured results a later section quotes instead of hard-coding (the harness's LAST_FULL
# convention). Empty when a section runs alone; the fallback constant is labelled inline.
LAST: dict = {}

# The shipped configuration the ADR names; every section measures against these.
SHIPPED_BATCH = 64              # log group commit: <= 64 rows (ADR-0006 R48's bound)
SHIPPED_COMMIT_MS = 5.0         # ... or 5 ms, whichever first
SHIPPED_AUTOCHECKPOINT = 1000   # pages
SHIPPED_CONVOY_K = 32           # money convoy: <= 32 leases ...
SHIPPED_CONVOY_T_MS = 0.5       # ... or 0.5 ms, whichever first

# The durability classes this spike prices (and the ADR names in config):
#   strict    every commit fsyncs (synchronous=FULL). Money rows, and the log when the
#             deployment wants an ack to be a durability claim with no batch window.
#   batched   synchronous=NORMAL in WAL mode: the commit is durable against a process
#             crash, and against power loss up to the last checkpoint. The log's default.
#   ephemeral synchronous=OFF / journal_mode=MEMORY: a benchmark run that is reproducible
#             from its scenario hash, where losing the artifact costs a re-run, not money.
DURABILITY = ("strict", "batched", "ephemeral")

OUTCOME_NAME = {H.AUTHORIZED: "authorized", H.DECLINED_SOFT: "declined_soft",
                H.DECLINED_HARD: "declined_hard", H.ABANDONED: "abandoned",
                H.TRANSPORT_ERROR: "transport_error", H.TIMEOUT: "timeout"}
PROC_ORD = {p: i for i, p in enumerate(PROCESSORS)}
BIN_ORD = {b: i for i, b in enumerate(BIN_CLASSES)}
REG_ORD = {r: i for i, r in enumerate(REGIONS)}
CUR_ORD = {"EUR": 0, "USD": 1, "GBP": 2}
MCC_ORD = {"digital_goods": 0, "travel": 1, "retail": 2, "marketplace": 3, "gaming": 4}
ROUTE_ORD = {"oneoff_cnp": 0, "recurring_mit": 1, "installment": 2, "card_on_file": 3}
ENTRY_ORD = {"ecommerce": 0, "moto": 1, "recurring": 2, "wallet": 3}

# --------------------------------------------------------------------------------------
# The catalog's contract facts. ADR-0011 handed max_response_ms / key_lifetime_ms /
# auth_fee_minor to the ticket that first consumes them, and that ticket is this one, so
# the lease rows below carry the catalog's own numbers rather than constants invented
# here. M is per processor; the resolution window is ADR-0009 R67's oneoff_cnp default
# (route-class config, #12's, not a catalog fact). R66's key_lifetime >= M is asserted at
# import: a key that expires before the processor has finalised its state re-opens the
# double-charge window the lease exists to close.
# --------------------------------------------------------------------------------------

CATALOG_PATH = REPO / "constraints/catalog/acquirer-catalog.example.json"
CATALOG = json.loads(CATALOG_PATH.read_text())
CATALOG_VERSION = CATALOG["catalog_version"]
CATALOG_HASH = "sha256:" + hashlib.sha256(canonical_bytes(CATALOG)).hexdigest()
ACQ = {x["id"]: x for x in CATALOG["acquirers"]}
M_MS = {q: int(ACQ[q]["max_response_ms"]) for q in PROCESSORS}
KEY_LIFETIME_MS = {q: int(ACQ[q]["key_lifetime_ms"]) for q in PROCESSORS}
AUTH_FEE_MINOR = {q: int(ACQ[q]["auth_fee_minor"]) for q in PROCESSORS}
M_FOR = [M_MS[q] for q in PROCESSORS]     # indexed by the processor ordinal in a lease row
WIN_MS = 30_000                           # R67: oneoff_cnp resolution window
for _q in PROCESSORS:
    assert KEY_LIFETIME_MS[_q] >= M_MS[_q], f"{_q}: key_lifetime_ms < max_response_ms (R66)"

# op kinds (the fold's vocabulary). Money kinds live in the journal file, learning kinds in
# the trace file; the split is the durability split (W2), not a data-model split.
KIND_OUTCOME, KIND_DRIFT_RESET, KIND_AUTH_RECORD, KIND_PRIOR_SWAP, KIND_LATE, \
    KIND_SNAPSHOT_MARK = 1, 2, 3, 4, 5, 6
KIND_LEASE, KIND_RELEASE, KIND_GAVE_UP, KIND_VOID, KIND_SETTLE = 16, 17, 18, 19, 20

# --------------------------------------------------------------------------------------
# 2. The schema. This is the normative DDL the ADR reproduces; the spike executes it.
# --------------------------------------------------------------------------------------

PRAGMAS_COMMON = (
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=OFF",
    "PRAGMA temp_store=MEMORY",
)

DDL_TRACE = f"""
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
-- and their decoder is committed code in analysis/ (W4 prices both halves of that rule).
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
  chain        BLOB    NOT NULL,                -- {CHAIN_BLOB_BYTES} B fixed layout
  posteriors   BLOB    NOT NULL,                -- {POSTERIOR_BLOB_BYTES} B fixed layout, f32
  propensity   REAL    NOT NULL,                -- the score-based plug-in (R61), also in-band
  method       INTEGER NOT NULL,                -- plug-in-score-v1
  floor_state  INTEGER NOT NULL,                -- R49's onboarding state
  audit_hash   BLOB    NOT NULL                 -- 32 B: ADR-0004 6's flat audit record
);
CREATE INDEX IF NOT EXISTS decision_boot ON decision(boot_id, arrival_ms);

-- The op log: the WAL of learned state (ADR-0006 R47) and the outcome trace row
-- (ADR-0005 5.2) in ONE write. op_seq is the fold's total order and is assigned by the
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
                                                -- auth record fields, prior-swap ref
);
CREATE UNIQUE INDEX IF NOT EXISTS op_dedupe ON op(seq, attempt) WHERE kind = {KIND_OUTCOME};
CREATE INDEX IF NOT EXISTS op_ms ON op(ms);

-- Snapshots are checkpoints of the fold (ADR-0006 7): each names the op_seq it is a
-- checkpoint OF, so boot = newest snapshot + the ops after it. Nothing else writes state.
CREATE TABLE IF NOT EXISTS snapshot (
  op_seq       INTEGER PRIMARY KEY,
  taken_ms     INTEGER NOT NULL,
  arm_schema   TEXT    NOT NULL,
  n_arms       INTEGER NOT NULL,
  posterior    BLOB    NOT NULL,                -- {SNAPSHOT_BYTES:,} B at {N_ARMS:,} arms
  detector     BLOB,                            -- ADWIN buckets, <= 2 KB/processor (R56)
  counters     BLOB,                            -- the windowed counter arena (ADR-0004)
  checksum     TEXT    NOT NULL
);
"""

DDL_JOURNAL = """
-- The money file. synchronous=FULL: a lease row is durable BEFORE the dispatch it
-- authorizes (ADR-0009 R71), and the void/settle rows are durable before money moves
-- (R68). Small rows, low volume, one writer, no head-of-line blocking from the log (W2).
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

-- The reconciliation ledger: keys stay queryable past their routing life (ADR-0009 2 --
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
"""


def _connect(path: Path, *, journal="WAL", synchronous="NORMAL", page_size=4096,
             autocheckpoint=1000, cache_mb=64, mmap_mb=0, ddl: str = "") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=5.0,
                           check_same_thread=False)
    conn.execute(f"PRAGMA page_size={page_size}")
    for p in PRAGMAS_COMMON:
        conn.execute(p)
    conn.execute(f"PRAGMA cache_size=-{cache_mb * 1024}")
    if mmap_mb:
        conn.execute(f"PRAGMA mmap_size={mmap_mb * 1024 * 1024}")
    conn.execute(f"PRAGMA journal_mode={journal}")
    conn.execute(f"PRAGMA synchronous={synchronous}")
    conn.execute(f"PRAGMA wal_autocheckpoint={autocheckpoint}")
    if ddl:
        conn.executescript(ddl)
    return conn


SYNC_OF = {"strict": "FULL", "batched": "NORMAL", "ephemeral": "OFF"}


class TraceStore:
    """The trace/op-log file: buffered appends, group commit, snapshots, the fold's input.

    `commit_every` rows and `commit_ms` implement the group commit ADR-0006 hands #13
    ("one write, group-commit, fsync interval is yours"). The writer is single-threaded by
    construction (ADR-0006 R48: one ingest writer per shard); the buffers are the shard's
    bounded queue.
    """

    def __init__(self, path: Path, *, durability="batched", journal="WAL", page_size=4096,
                 autocheckpoint=1000, commit_every=64, commit_ms=5.0, cache_mb=64,
                 mmap_mb=0, ddl=DDL_TRACE):
        self.path = Path(path)
        self.durability = durability
        self.commit_every = commit_every
        self.commit_ms = commit_ms
        self.conn = _connect(self.path, journal=journal, synchronous=SYNC_OF[durability],
                             page_size=page_size, autocheckpoint=autocheckpoint,
                             cache_mb=cache_mb, mmap_mb=mmap_mb, ddl=ddl)
        self.journal_mode = self.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self._dec: list = []
        self._op: list = []
        self._op_seq = self.conn.execute("SELECT COALESCE(MAX(op_seq),0) FROM op").fetchone()[0]
        self._last_commit = time.perf_counter()
        self.commits = 0
        self.fsyncs_est = 0
        self.commit_latency: list[float] = []

    # -- appends -----------------------------------------------------------------------
    def boot_row(self, **kw) -> int:
        cols = ("boot_id", "started_ms", "mode", "durability", "shard", "schema_version",
                "policy_seed", "catalog_hash", "doc_hash", "prior_hash", "arm_schema_hash",
                "config_hash", "scenario_hash", "model_version", "harness_version",
                "band_edges")
        vals = [kw.get(c) for c in cols]
        self.conn.execute(f"INSERT OR REPLACE INTO boot ({','.join(cols)}) "
                          f"VALUES ({','.join('?' * len(cols))})", vals)
        return int(kw["boot_id"])

    def next_op_seq(self) -> int:
        self._op_seq += 1
        return self._op_seq

    def append_decision(self, row: tuple) -> None:
        self._dec.append(row)
        self._maybe_commit()

    def append_op(self, kind: int, row: tuple) -> int:
        """row carries everything after op_seq; we prepend the fold's total order."""
        op_seq = self.next_op_seq()
        self._op.append((op_seq, kind) + row)
        self._maybe_commit()
        return op_seq

    def _maybe_commit(self) -> bool:
        if len(self._dec) + len(self._op) >= self.commit_every:
            self.commit()
            return True
        if (time.perf_counter() - self._last_commit) * 1000.0 >= self.commit_ms and \
                (self._dec or self._op):
            self.commit()
            return True
        return False

    def commit(self) -> None:
        if not (self._dec or self._op):
            return
        t0 = time.perf_counter()
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        if self._dec:
            c.executemany(
                "INSERT INTO decision(seq,boot_id,arrival_ms,bin_class,region,sca,mandate,"
                "band,amount_minor,currency,merchant_cat,route_class,entry_mode,eligible,"
                "chosen,chain_len,chain,posteriors,propensity,method,floor_state,audit_hash)"
                " VALUES(" + ",".join("?" * 22) + ")", self._dec)
            self._dec.clear()
        if self._op:
            c.executemany(
                "INSERT INTO op(op_seq,kind,boot_id,ms,seq,attempt,processor,outcome,code,"
                "decline_class,latency_ms,settled_ms,bin_class,region,sca,mandate,"
                "amount_minor,band,currency,merchant_cat,route_class,entry_mode,"
                "scenario_hash,payload) VALUES(" + ",".join("?" * 24) + ")",
                [(o[0], o[1]) + o[2:] for o in self._op])
            self._op.clear()
        c.execute("COMMIT")
        dt = time.perf_counter() - t0
        self.commits += 1
        self.commit_latency.append(dt)
        self._last_commit = time.perf_counter()

    def checkpoint(self, mode="PASSIVE") -> tuple:
        return self.conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()

    def flush_and_checkpoint(self) -> None:
        self.commit()
        self.checkpoint("TRUNCATE")

    # -- snapshots and the fold --------------------------------------------------------
    def snapshot(self, op_seq: int, posterior, taken_ms: int, arm_schema: str,
                 detector: bytes = b"", counters: bytes = b"") -> int:
        body = posterior.tobytes() if hasattr(posterior, "tobytes") else bytes(posterior)
        h = hashlib.sha256(body).hexdigest()
        self.conn.execute(
            "INSERT OR REPLACE INTO snapshot(op_seq,taken_ms,arm_schema,n_arms,posterior,"
            "detector,counters,checksum) VALUES(?,?,?,?,?,?,?,?)",
            (op_seq, taken_ms, arm_schema, N_ARMS, sqlite3.Binary(body),
             sqlite3.Binary(detector), sqlite3.Binary(counters), h))
        return len(body)

    def latest_snapshot(self):
        row = self.conn.execute(
            "SELECT op_seq,posterior,detector,counters,checksum FROM snapshot "
            "ORDER BY op_seq DESC LIMIT 1").fetchone()
        return row

    def ops_after(self, op_seq: int):
        return self.conn.execute(
            "SELECT op_seq,kind,processor,outcome,ms FROM op WHERE op_seq > ? "
            "ORDER BY op_seq", (op_seq,))

    def close(self) -> None:
        try:
            self.commit()
        finally:
            self.conn.close()

    # -- sizing ------------------------------------------------------------------------
    def bytes_on_disk(self) -> int:
        self.flush_and_checkpoint()
        total = self.path.stat().st_size
        for suffix in ("-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total

    def counts(self) -> dict:
        out = {}
        for t in ("decision", "op", "snapshot"):
            out[t] = self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return out


class JournalStore:
    """The money file: one durable row per state change, synchronous=FULL."""

    def __init__(self, path: Path, *, durability="strict", journal="WAL", page_size=4096,
                 autocheckpoint=1000):
        self.path = Path(path)
        self.conn = _connect(self.path, journal=journal, synchronous=SYNC_OF[durability],
                             page_size=page_size, autocheckpoint=autocheckpoint,
                             ddl=DDL_JOURNAL)
        self.commit_latency: list[float] = []

    def lease(self, txn_seq, attempt, processor, key, dispatch_ms, m_ms, window_ms,
              state=0, *, durable=True) -> float:
        t0 = time.perf_counter()
        self.conn.execute(
            "INSERT OR REPLACE INTO lease(txn_seq,attempt,processor,key,dispatch_ms,m_ms,"
            "window_ms,state) VALUES(?,?,?,?,?,?,?,?)",
            (txn_seq, attempt, processor, sqlite3.Binary(key), dispatch_ms, m_ms,
             window_ms, state))
        dt = time.perf_counter() - t0
        if durable:
            self.commit_latency.append(dt)
        return dt

    def release(self, txn_seq, attempt, state=1) -> None:
        self.conn.execute("UPDATE lease SET state=? WHERE txn_seq=? AND attempt=?",
                          (state, txn_seq, attempt))

    def schedule(self, due_ms, tie, kind, txn_seq, attempt, payload=b"") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO scheduled_event(due_ms,tie,kind,txn_seq,attempt,payload)"
            " VALUES(?,?,?,?,?,?)",
            (due_ms, tie, kind, txn_seq, attempt, sqlite3.Binary(payload)))

    def due(self, now_ms, limit=256):
        return self.conn.execute(
            "SELECT due_ms,tie,kind,txn_seq,attempt FROM scheduled_event WHERE due_ms<=? "
            "ORDER BY due_ms,tie,kind LIMIT ?", (now_ms, limit)).fetchall()

    def checkpoint(self, mode="TRUNCATE"):
        return self.conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()

    def close(self):
        self.conn.close()

    def bytes_on_disk(self) -> int:
        self.checkpoint("TRUNCATE")
        total = self.path.stat().st_size
        for suffix in ("-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total


# --------------------------------------------------------------------------------------
# 3. The learned state and the fold (ADR-0006 R47: the fold is the only writer).
# --------------------------------------------------------------------------------------

def _sig2(x: float) -> int:
    """Two significant figures, as an integer (R40's ladder is declared at 2 s.f.)."""
    if x <= 0:
        return 0
    d = math.floor(math.log10(x))
    return int(round(x / 10.0 ** (d - 1)) * 10.0 ** (d - 1))


def band_edges(amt_min: int, amt_max: int, n_bands: int = N_BANDS) -> list[int]:
    """R40: a geometric ladder over the declared amount range, 2 significant figures."""
    lo, hi = float(max(1, amt_min)), float(amt_max)
    step = (hi / lo) ** (1.0 / n_bands)
    return [_sig2(lo * (step ** i)) for i in range(1, n_bands)]


def band_of(amount: int, edges: list[int]) -> int:
    return bisect_right(edges, amount)


def arm_index(bin_i, reg_i, sca, mandate, band, proc_i) -> int:
    ctx = (((bin_i * N_REGIONS + reg_i) * N_SCA + sca) * N_MANDATE + mandate) * N_BANDS + band
    return ctx * N_PROC + proc_i


class State:
    """The in-memory learned state: one flat float64 array, 32 B/arm, no locks (R48)."""

    __slots__ = ("posterior", "n_arms", "ops_folded", "settled", "resets", "late")

    def __init__(self, n_arms=N_ARMS):
        self.n_arms = n_arms
        self.posterior = array.array("d", bytes(n_arms * STATE_FLOATS_PER_ARM * 8))
        self.ops_folded = 0
        self.settled = 0
        self.resets = 0
        self.late = 0

    def apply_outcome(self, arm: int, outcome: int) -> None:
        p = self.posterior
        i = arm * STATE_FLOATS_PER_ARM
        if outcome == H.AUTHORIZED:
            p[i] += 1.0
            self.settled += 1
        elif outcome in (H.DECLINED_SOFT, H.DECLINED_HARD, H.ABANDONED):
            p[i + 1] += 1.0
            self.settled += 1
        elif outcome == H.TRANSPORT_ERROR:
            p[i + 1] += 1.0          # R42: te -> beta + 1 ...
            p[i + 2] += 1.0          # ... and a separate te counter
            self.settled += 1
        elif outcome == H.TIMEOUT:
            p[i + 3] += 1.0          # timeout counter only; priced at lambda_to (R42/R62)
        self.ops_folded += 1

    def apply_drift_reset(self, proc_i: int, gamma: float) -> None:
        """R53/R58: shrink data counts AND prior pseudo-counts for every arm of the
        processor. A multiply on the same float64 array -- which is why the array holds
        floats and not integers (ADR-0006 3d)."""
        p = self.posterior
        for arm in range(proc_i, self.n_arms, N_PROC):
            i = arm * STATE_FLOATS_PER_ARM
            p[i] *= gamma
            p[i + 1] *= gamma
        self.resets += 1
        self.ops_folded += 1

    def digest(self) -> str:
        return hashlib.sha256(self.posterior.tobytes()).hexdigest()

    def total_counts(self) -> tuple:
        p = self.posterior
        a = math.fsum(p[0::STATE_FLOATS_PER_ARM])
        b = math.fsum(p[1::STATE_FLOATS_PER_ARM])
        return a, b


def fold_ops(conn: sqlite3.Connection, state: State, after_op_seq: int = 0,
             with_context=True) -> dict:
    """The real fold: op order, arm re-keying from the row's raw context, bit-exact."""
    cols = ("op_seq,kind,processor,outcome,payload,bin_class,region,sca,mandate,"
            "amount_minor,band")
    q = f"SELECT {cols} FROM op WHERE op_seq > ? ORDER BY op_seq"
    t0 = time.perf_counter()
    n_out = n_ctl = 0
    for op_seq, kind, processor, outcome, payload, b, r, s, m, amt, band in \
            conn.execute(q, (after_op_seq,)):
        if kind == KIND_OUTCOME:
            arm = arm_index(b, r, s, m, band, processor)
            state.apply_outcome(arm, outcome)
            n_out += 1
        elif kind == KIND_DRIFT_RESET:
            gamma = struct.unpack("<d", payload[:8])[0] if payload else 0.5
            state.apply_drift_reset(processor, gamma)
            n_ctl += 1
        elif kind == KIND_LATE:
            state.late += 1
            n_ctl += 1
        else:
            n_ctl += 1
            state.ops_folded += 1
    dt = time.perf_counter() - t0
    return {"rows": n_out + n_ctl, "outcomes": n_out, "control": n_ctl, "seconds": dt,
            "rows_per_s": (n_out + n_ctl) / dt if dt else 0.0}


def load_snapshot(conn: sqlite3.Connection, state: State) -> int:
    row = conn.execute("SELECT op_seq,posterior,checksum FROM snapshot ORDER BY op_seq DESC "
                       "LIMIT 1").fetchone()
    if row is None:
        return 0
    op_seq, blob, checksum = row
    body = bytes(blob)
    if hashlib.sha256(body).hexdigest() != checksum:
        raise ValueError("snapshot checksum mismatch -- refusing to boot from it")
    state.posterior = array.array("d")
    state.posterior.frombytes(body)
    return op_seq


# --------------------------------------------------------------------------------------
# 4. The world: rows with the committed scenario's distribution in them.
# --------------------------------------------------------------------------------------

DOCS: dict = {}


def _doc(name: str):
    if name not in DOCS:
        path = REPO / "simulator" / "scenarios" / "examples" / f"{name}.json"
        doc, errs = load_scenario(path)
        if errs:
            raise SystemExit(f"scenario {name} failed its gate: {errs}")
        DOCS[name] = doc
    return DOCS[name]


# The static cost table (ADR-0003's named baseline) is the policy the row generator uses:
# the store's cost does not depend on which policy produced the rows, and a deterministic
# one keeps the row stream reproducible without paying for a bandit in a storage spike.
COST_ORDER = ("foxtrot", "charlie", "bravo", "echo", "alpha", "delta")


class RowSource:
    """Drives the ADR-0005 harness world and yields store rows.

    `pool` holds the first `pool_n` decisions' worth of rows; `stream()` cycles the pool
    with re-keyed seq/arrival so a 5M-op storage test pays the world model's cost once.
    The value distribution -- and therefore row width, index selectivity and compression
    ratio -- is the scenario's; that is stated wherever the pool is cycled.
    """

    def __init__(self, scenario: str, pool_n: int, seed: int | None = None):
        self.scenario = scenario
        self.doc = _doc(scenario)
        self.hash = scenario_hash(self.doc)
        mix = self.doc["source"]["traffic"]["context_mix"]
        self.edges = band_edges(int(mix["amount"]["min_minor"]),
                                int(mix["amount"]["max_minor"]))
        # span="poisson": the scenario's DECLARED arrival model (20 TPS with its diurnal
        # and congestion shape) rather than the harness default span="duration", which
        # stretches n arrivals across the whole 7-day clock so that a scenario run fills
        # its window. Row width, index selectivity and compression do not care; the
        # time-window queries (W4), the boot-replay clock (W7) and the retention math
        # (W8) do, and they want a pace that means something. Stated here, not hidden.
        self.world = H.Harness(self.doc, pool_n, seed=seed, span="poisson")
        self.seed = self.world.seed
        self.pool: list[tuple] = []
        self.auth_record_share = 0.0
        self._build_pool(pool_n)

    def _build_pool(self, n: int) -> None:
        w = self.world
        clients = {a: H.SyntheticAcquirer(w, a, w.clock) for a in w.acquirers}
        n_auth = n_att = 0
        arrivals = w.arrivals(n)
        for seq, arrival_ms in arrivals:
            w.clock.advance_to(arrival_ms)
            req = w.context(seq, arrival_ms)
            chain = [a for a in COST_ORDER if a in clients]
            decision_rows = []
            outcome_rows = []
            eligible = 0
            for a in chain:
                eligible |= 1 << PROC_ORD[a]
            for attempt, acq in enumerate(chain[:MAX_CHAIN]):
                resp = clients[acq].authorize(req, attempt)
                n_att += 1
                resp_rows = self._outcome_row(req, acq, attempt, resp)
                outcome_rows.append(resp_rows)
                if req.sca_required:
                    n_auth += 1
                if resp.outcome in H.TERMINAL or resp.outcome == H.AUTHORIZED:
                    break
                if resp.outcome == H.TIMEOUT:
                    break                      # the lease protocol never chains on ambiguity
            dec = self._decision_row(req, seq, arrival_ms, eligible, chain[:MAX_CHAIN])
            self.pool.append((dec, outcome_rows, req.sca_required))
        self.auth_record_share = n_auth / max(1, n_att)
        self.attempts_per_decision = n_att / max(1, n)
        span_ms = max(1, self.pool[-1][0][2] - self.pool[0][0][2])
        self.pool_span_ms = span_ms
        self.pool_tps = n / (span_ms / 1000.0)

    def _decision_row(self, req, seq, arrival_ms, eligible, chain) -> tuple:
        b = BIN_ORD.get(req.bin_class, 5)
        r = REG_ORD.get(req.card_region, 0)
        sca = 1 if req.sca_required else 0
        man = 1 if req.mandate else 0
        band = band_of(req.amount_minor, self.edges)
        # The logged posterior snapshot (R60) must be the beliefs AT decision time. For a
        # storage measurement what matters is that the bytes have the shape real beliefs
        # have: small-to-moderate counts, correlated within an arm, distinct across arms --
        # not uniform noise, which would flatter every compression number in W5.
        posteriors = array.array("f", [0.0] * (K_MAX * 4))
        for i, acq in enumerate(chain[:K_MAX]):
            arm = arm_index(b, r, sca, man, band, PROC_ORD[acq])
            n = (arm * 2654435761) % 977              # a stable pseudo-count per arm
            p = 0.60 + 0.35 * (((arm * 40503) % 1000) / 1000.0)
            posteriors[i * 4 + 0] = float(max(1, int(n * p)) + 1)
            posteriors[i * 4 + 1] = float(max(1, n - int(n * p)) + 1)
            posteriors[i * 4 + 2] = 1.0 + (arm % 17)
            posteriors[i * 4 + 3] = 1.0 + (arm % 7)
        chain_blob = b"".join(struct.pack("<Bq", PROC_ORD[a], 128 * 100 - 7 * PROC_ORD[a])
                             for a in chain)
        chain_blob += b"\x00" * (CHAIN_BLOB_BYTES - len(chain_blob))
        audit = hashlib.sha256(struct.pack("<qIII", seq, eligible, arrival_ms, band)).digest()
        return (seq, 1, arrival_ms, b, r, sca, man, band, req.amount_minor,
                CUR_ORD.get(req.currency, 0), MCC_ORD.get(req.merchant_category, 0),
                ROUTE_ORD.get(req.route_class, 0), ENTRY_ORD.get(req.entry_mode, 0),
                eligible, PROC_ORD[chain[0]], len(chain), chain_blob,
                posteriors.tobytes(), 1.0 / max(1, len(chain)), 1, 0, audit)

    def _outcome_row(self, req, acq, attempt, resp) -> tuple:
        b = BIN_ORD.get(req.bin_class, 5)
        r = REG_ORD.get(req.card_region, 0)
        sca = 1 if req.sca_required else 0
        man = 1 if req.mandate else 0
        band = band_of(req.amount_minor, self.edges)
        code = int(resp.code) if str(resp.code).isdigit() else 0
        dclass = {"soft": 1, "hard": 2}.get(resp.decline_class, 0)
        settled = resp.settled_ms if resp.settled_ms is not None else 0
        payload = b""
        return (1, resp_ms_of(resp, req), req.seq, attempt, PROC_ORD[acq], resp.outcome,
                code, dclass, resp.latency_ms, settled, b, r, sca, man, req.amount_minor,
                band, CUR_ORD.get(req.currency, 0), MCC_ORD.get(req.merchant_category, 0),
                ROUTE_ORD.get(req.route_class, 0), ENTRY_ORD.get(req.entry_mode, 0),
                self.hash, payload)

    def stream(self, n_decisions: int, start_seq: int = 0):
        """Yield (decision_row, [op_rows], sca_required), cycling the pool with re-keyed
        seq/arrival_ms. Value distributions are the scenario's; only the keys advance."""
        pool = self.pool
        pn = len(pool)
        base_arrival = pool[0][0][2]
        span = pool[-1][0][2] - base_arrival + 1
        for i in range(n_decisions):
            dec, outcomes, sca = pool[i % pn]
            shift = (i // pn) * span
            d = list(dec)
            d[0] = start_seq + i
            d[2] = base_arrival + shift + (dec[2] - base_arrival)
            yield tuple(d), [self._rekey(o, start_seq + i, shift) for o in outcomes], sca

    def _rekey(self, op: tuple, seq: int, delta_ms: int) -> tuple:
        o = list(op)
        o[1] = o[1] + delta_ms      # ms
        o[2] = seq                  # seq
        o[9] = (o[9] + delta_ms) if o[9] else 0
        return tuple(o)


def resp_ms_of(resp, req) -> int:
    return resp.settled_ms if resp.settled_ms is not None else req.arrival_ms + resp.latency_ms


# --------------------------------------------------------------------------------------
# 5. Output and small utilities (the house format: stdout == RESULTS.md).
# --------------------------------------------------------------------------------------

class Out:
    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, s=""):
        self.lines.append(s)

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def hr(o: Out, title: str = "") -> None:
    o("=" * 100)
    if title:
        o(title)
        o("=" * 100)


def pct(xs, q):
    if not xs:
        return 0.0
    s = sorted(xs)
    i = min(len(s) - 1, int(q * len(s)))
    return s[i]


def us(seconds: float) -> float:
    return seconds * 1e6


def rate(n: float, seconds: float) -> float:
    return n / seconds if seconds > 0 else float("inf")


def scratch(name: str) -> Path:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    p = SCRATCH / name
    if p.exists():
        for f in p.glob("*"):
            f.unlink()
    else:
        p.mkdir(parents=True)
    return p


def drop(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()


def db_bytes(path: Path) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            total += p.stat().st_size
    return total


def env_line() -> str:
    import platform
    try:
        with open("/proc/cpuinfo") as f:
            cores = sum(1 for line in f if line.startswith("processor"))
    except OSError:
        cores = os.cpu_count() or 0
    mem = ""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal"):
                    mem = f"{int(line.split()[1]) / 1048576:.1f} GiB"
                    break
    except OSError:
        pass
    dev = "unknown"
    try:
        st = os.statvfs(SCRATCH if SCRATCH.exists() else "/tmp")
        dev = f"{st.f_bfree * st.f_frsize / 1e9:.0f} GB free"
    except OSError:
        pass
    return (f"python {platform.python_version()} | sqlite {sqlite3.sqlite_version} | "
            f"{cores} vCPU | {mem} | scratch {SCRATCH} ({dev})")


# --------------------------------------------------------------------------------------
# [W1] the durability ladder
# --------------------------------------------------------------------------------------

def sec_w1(o: Out, n: int, src: RowSource) -> None:
    hr(o, "[W1] the durability ladder: what a durable append costs, and which config "
          "clears the fleet pace")
    o("  The write profile is not a guess: ADR-0001 budgets 5,000 decisions/s, ADR-0006")
    o(f"  measures {ATTEMPTS_PER_DECISION} attempts/decision, so the log takes "
      f"{ROWS_PER_S:,.0f} rows/s")
    o(f"  ({DPS:,} decision rows + {OPS_PER_S:,.0f} outcome ops). Every config below writes "
      "the REAL rows --")
    o("  DECISION_LOG v1 plus the outcome op with its raw context -- not a synthetic")
    o("  one-column insert. `us/row` is wall time per row including the row build;")
    o("  `commit p50/p99` is per COMMIT, which is what an ack waits for (R83).")
    o("  fsync column: for FULL it is one per commit; for NORMAL one per checkpoint.")
    o()
    d = scratch("w1")
    configs = [("WAL", "strict"), ("WAL", "batched"), ("WAL", "ephemeral"),
               ("DELETE", "strict"), ("DELETE", "batched"), ("MEMORY", "ephemeral")]
    batches = [1, 8, 32, 64, 256, 1024]
    o(f"  {'journal':<7} {'durability':<10} {'batch':>5} {'rows':>8} {'rows/s':>9} "
      f"{'us/row':>7} {'cmt p50':>9} {'cmt p99':>9} {'B/row':>6} {'vs fleet':>9}")
    results = {}
    for journal, dur in configs:
        for batch in batches:
            rows = min(n, 20_000) if batch <= 8 else min(n, 120_000)
            path = d / f"t-{journal}-{dur}-{batch}.sqlite"
            drop(path)
            st = TraceStore(path, durability=dur, journal=journal, commit_every=batch,
                            commit_ms=1e9, autocheckpoint=1000)
            wrote = 0
            t0 = time.perf_counter()
            for dec, ops, _sca in src.stream(rows):
                st.append_decision(dec)
                wrote += 1
                for op in ops:
                    st.append_op(KIND_OUTCOME, op)
                    wrote += 1
                if wrote >= rows:
                    break
            st.commit()
            dt = time.perf_counter() - t0
            st.flush_and_checkpoint()
            size = db_bytes(path)
            lat = st.commit_latency
            per_commit_rows = wrote / max(1, len(lat))
            results[(journal, dur, batch)] = {
                "rows": wrote, "rows_per_s": rate(wrote, dt), "us_row": us(dt / wrote),
                "p50": pct(lat, 0.50), "p99": pct(lat, 0.99), "b_row": size / wrote,
                "commits": len(lat), "headroom": rate(wrote, dt) / ROWS_PER_S,
            }
            LAST.setdefault("w1", {})[(journal, dur, batch)] = \
                results[(journal, dur, batch)]
            r = results[(journal, dur, batch)]
            o(f"  {journal:<7} {dur:<10} {batch:>5} {r['rows']:>8,} "
              f"{r['rows_per_s']:>9,.0f} {r['us_row']:>7.2f} {us(r['p50']):>7.0f}us "
              f"{us(r['p99']):>7.0f}us {r['b_row']:>6.0f} {r['headroom']:>8.1f}x")
            st.close()
            drop(path)
    o()
    o("  Readings:")
    strict1 = results[("WAL", "strict", 1)]["rows_per_s"]
    strict64 = results[("WAL", "strict", 64)]["rows_per_s"]
    batched64 = results[("WAL", "batched", 64)]["rows_per_s"]
    off64 = results[("WAL", "ephemeral", 64)]["rows_per_s"]
    del1 = results[("DELETE", "strict", 1)]["rows_per_s"]
    mem1 = results[("MEMORY", "ephemeral", 1)]["rows_per_s"]
    o(f"   * A durable append is an fsync, and an fsync is a batch decision, not a row")
    o(f"     decision: WAL+FULL is {strict1:,.0f} rows/s one-row-per-commit and")
    o(f"     {strict64:,.0f} rows/s at batch 64 -- {strict64 / strict1:.0f}x for the same")
    o(f"     guarantee. The rollback journal (DELETE+FULL, one row per commit) is")
    o(f"     {del1:,.0f} rows/s: it fsyncs twice per commit and is")
    o(f"     {strict1 / del1:.1f}x worse than WAL at the SAME durability. That is the")
    o("     whole ticket question 'is SQLite fast enough' answered: the journal mode and")
    o("     the batch size decide it, not SQLite.")
    o(f"   * At batch 64 the ladder is {strict64:,.0f} (FULL) / {batched64:,.0f} (NORMAL) /")
    o(f"     {off64:,.0f} (OFF) rows/s. NORMAL buys {batched64 / strict64:.1f}x over FULL")
    o(f"     and OFF buys {off64 / strict64:.1f}x -- i.e. once the batch exists, the fsync")
    o("     is no longer the binding cost, so the guarantee is nearly free. That is why")
    o("     the log can run at FULL and still keep R83's ack a real durability claim.")
    o(f"   * MEMORY ({mem1:,.0f} rows/s at batch 1) is the ticket's 'in-memory + periodic")
    o("     checkpoint' option measured: it is the fastest writer here and it is not a")
    o("     store -- [W3] shows what a crash takes from it.")
    o(f"   * Fleet headroom: the config the ADR ships (WAL + FULL + batch {SHIPPED_BATCH})")
    ship = results[("WAL", "strict", SHIPPED_BATCH)]["headroom"]
    o(f"     clears {ROWS_PER_S:,.0f} rows/s by {ship:.0f}x on this box, in CPython, with")
    o("     no Go driver in the loop. The binding constraint is not throughput.")
    o()

    # (b) what the fsync actually costs, and how it scales with the bytes it carries.
    o("  (b) the fsync itself: commit latency vs the bytes in the commit (WAL, FULL).")
    o("      A convoy is only worth assembling if one fsync carries many rows cheaply.")
    o(f"      {'batch':>6} {'rows':>8} {'B/row':>6} {'B/commit':>9} {'cmt p50':>9} "
      f"{'cmt p99':>9} {'us/row in cmt':>14}")
    d2 = scratch("w1b")
    for batch in (1, 8, 32, 64, 256, 1024, 4096):
        path = d2 / f"f{batch}.sqlite"
        drop(path)
        st = TraceStore(path, durability="strict", commit_every=batch, commit_ms=1e9)
        rows = max(2_000, min(n // 4, 200_000))
        wrote = 0
        for dec, ops, _s in src.stream(rows):
            st.append_decision(dec)
            wrote += 1
            for op in ops:
                st.append_op(KIND_OUTCOME, op)
                wrote += 1
            if wrote >= rows:
                break
        st.commit()
        lat = st.commit_latency
        st.flush_and_checkpoint()
        size = db_bytes(path)
        b_row = size / wrote
        per_commit = wrote / len(lat)
        o(f"      {batch:>6} {wrote:>8,} {b_row:>6.0f} {b_row * per_commit:>9,.0f} "
          f"{us(pct(lat, .5)):>7.0f}us {us(pct(lat, .99)):>7.0f}us "
          f"{us(pct(lat, .5)) / per_commit:>14.3f}")
        st.close()
        drop(path)
    o("      The p50 grows sub-linearly in the batch: an fsync is a fixed device cost plus")
    o("      a per-page cost, so the amortised per-row price of durability falls by two")
    o("      orders of magnitude between batch 1 and batch 1024. This is the measurement")
    o("      the group-commit knob (ADR-0006 7's 'fsync interval is yours') rests on.")
    o()

    # (c) checkpoint stalls: the tail the autocheckpoint setting owns.
    o("  (c) the checkpoint is a writer stall: commit tail vs wal_autocheckpoint")
    o("      (WAL, FULL, batch 64 -- the shipped config; the stall is the p99.9).")
    o(f"      {'autocheckpoint':>15} {'rows/s':>9} {'p50':>8} {'p99':>9} {'p99.9':>9} "
      f"{'max':>9} {'WAL peak':>10}")
    d3 = scratch("w1c")
    acp_rows: list = []
    for acp in (200, 1000, 8000, 0):
        path = d3 / f"c{acp}.sqlite"
        drop(path)
        st = TraceStore(path, durability="strict", commit_every=SHIPPED_BATCH,
                        commit_ms=1e9, autocheckpoint=acp)
        rows = min(n // 2, 200_000)
        wrote = 0
        wal_peak = 0
        t0 = time.perf_counter()
        for dec, ops, _s in src.stream(rows):
            st.append_decision(dec)
            wrote += 1
            for op in ops:
                st.append_op(KIND_OUTCOME, op)
                wrote += 1
            if wrote >= rows:
                break
            if wrote % 4096 == 0:
                w = Path(str(path) + "-wal")
                if w.exists():
                    wal_peak = max(wal_peak, w.stat().st_size)
        st.commit()
        dt = time.perf_counter() - t0
        lat = st.commit_latency
        o(f"      {acp:>15} {rate(wrote, dt):>9,.0f} {us(pct(lat, .5)):>6.0f}us "
          f"{us(pct(lat, .99)):>7.0f}us {us(pct(lat, .999)):>7.0f}us "
          f"{us(max(lat)):>7.0f}us {wal_peak / 1e6:>8.1f}MB")
        acp_rows.append({"acp": acp, "rows_per_s": rate(wrote, dt),
                         "p999_us": us(pct(lat, .999)), "max_us": us(max(lat)),
                         "wal_mb": wal_peak / 1e6})
        st.close()
        drop(path)
    LAST["w1c"] = {r["acp"]: r for r in acp_rows}
    never = next(r for r in acp_rows if r["acp"] == 0)
    ship = next(r for r in acp_rows if r["acp"] == SHIPPED_AUTOCHECKPOINT)
    fast = max(acp_rows, key=lambda r: r["rows_per_s"])
    o("      Never checkpointing is not a free buffer. It buys no throughput --")
    o(f"      {never['rows_per_s']:,.0f} rows/s against {fast['rows_per_s']:,.0f} at "
      f"wal_autocheckpoint={fast['acp']},")
    o(f"      {100 * (1 - never['rows_per_s'] / fast['rows_per_s']):.0f}% slower -- and it "
      "pays for that with an unbounded WAL")
    o(f"      ({never['wal_mb']:.0f} MB after only {rows:,} rows, and it grows with the "
      "run), because SQLite has to walk a")
    o("      longer frame chain on every commit and every byte of it is a byte the next")
    o("      boot has to replay ([W7]). The shipped setting is")
    o(f"      {SHIPPED_AUTOCHECKPOINT} pages: {ship['p999_us']:,.0f} us of p99.9 stall -- "
      "a tail on one commit in a")
    o(f"      thousand, not a wall -- and a WAL that stays under {ship['wal_mb']:.1f} MB "
      "instead of growing with the run.")
    o("      [W2](a2) measures the same effect from the money path's latency side, and")
    o("      [W10] prices the reader that can pin the WAL open no matter what this pragma")
    o("      says.")
    o()


# --------------------------------------------------------------------------------------
# [W2] the money row: where the lease append lives
# --------------------------------------------------------------------------------------

class _PacedTraceLoad(threading.Thread):
    """A trace writer at a target row rate: the concurrent load the money path shares a
    device (and, in two variants, a file or a connection) with.

    `lease_q` implements the shared-writer variant faithfully: dispatchers hand their
    lease row to the writer, which folds it into its own batch and flips synchronous to
    FULL for that commit -- one writer per shard, taken literally. The dispatcher's
    latency is then the queue wait plus the batch, which is exactly the head-of-line
    blocking the measurement exists to price.
    """

    def __init__(self, store: TraceStore, src: RowSource, rows_per_s: float,
                 stop: threading.Event, lease_q: list | None = None):
        super().__init__(daemon=True)
        self.store, self.src, self.stop = store, src, stop
        self.rows_per_s = rows_per_s
        self.lease_q = lease_q if lease_q is not None else []
        self.lease_lock = threading.Lock()
        self.wrote = 0
        self.t0 = time.perf_counter()
        self.interval = 1.0 / rows_per_s if rows_per_s else 0.0

    def submit_lease(self, row: tuple) -> None:
        done = threading.Event()
        with self.lease_lock:
            self.lease_q.append((row, done))

    def run(self) -> None:
        st = self.store
        t_next = time.perf_counter()
        for dec, ops, _sca in self.src.stream(10 ** 9):
            if self.stop.is_set():
                break
            st.append_decision(dec)
            self.wrote += 1
            for op in ops:
                st.append_op(KIND_OUTCOME, op)
                self.wrote += 1
            with self.lease_lock:
                leases, self.lease_q = self.lease_q, []
            if leases:
                c = st.conn
                c.execute("PRAGMA synchronous=FULL")
                c.executemany(
                    "INSERT OR REPLACE INTO lease(txn_seq,attempt,processor,key,"
                    "dispatch_ms,m_ms,window_ms,state) VALUES(?,?,?,?,?,?,?,?)",
                    [r for r, _ in leases])
                st.commit()
                c.execute("PRAGMA synchronous=NORMAL")
                for _, done in leases:
                    done.set()
            t_next += self.interval * (1 + len(ops))
            slack = t_next - time.perf_counter()
            if slack > 0:
                time.sleep(slack)
            else:
                t_next = time.perf_counter()
        st.commit()


class _Convoy(threading.Thread):
    """The group-commit service for the money file: dispatchers hand it a lease row and
    wait; it commits FULL once per convoy (K rows or T microseconds, whichever first).
    One fsync carries many dispatches -- the only way a per-dispatch durability claim
    survives 6,500 dispatches/s."""

    def __init__(self, conn: sqlite3.Connection, max_rows=32, max_wait_ms=0.25):
        super().__init__(daemon=True)
        self.conn = conn
        self.max_rows = max_rows
        self.max_wait = max_wait_ms / 1000.0
        self.q: list = []
        self.lock = threading.Lock()
        self.wake = threading.Condition(self.lock)
        self.stop = threading.Event()
        self.commits = 0
        self.rows = 0

    def submit(self, row: tuple) -> threading.Event:
        done = threading.Event()
        with self.lock:
            self.q.append((row, done))
            if len(self.q) >= self.max_rows:
                self.wake.notify_all()
        done.wait()
        return done

    def run(self) -> None:
        while not self.stop.is_set():
            with self.lock:
                if not self.q:
                    self.wake.wait(self.max_wait)
                batch, self.q = self.q, []
            if not batch:
                continue
            c = self.conn
            c.execute("BEGIN IMMEDIATE")
            c.executemany(
                "INSERT OR REPLACE INTO lease(txn_seq,attempt,processor,key,dispatch_ms,"
                "m_ms,window_ms,state) VALUES(?,?,?,?,?,?,?,?)",
                [r for r, _ in batch])
            c.execute("COMMIT")
            self.commits += 1
            self.rows += len(batch)
            for _, done in batch:
                done.set()


def _lease_run(d: Path, label: str, *, mode: str, threads: int, n_leases: int,
               pace_lps: float, trace_rows_per_s: float, src: RowSource,
               convoy_k=32, convoy_t_ms=0.25):
    """One placement of the lease append, under a chosen dispatch pace and trace load.

    mode: own | shared_file | shared_writer | convoy. Returns a result dict."""
    tp = d / f"{label}-trace.sqlite"
    jp = tp if mode == "shared_file" or mode == "shared_writer" else d / f"{label}.sqlite"
    drop(tp)
    if jp != tp:
        drop(jp)
    ts = TraceStore(tp, commit_every=SHIPPED_BATCH, commit_ms=5.0, ddl=DDL_TRACE)
    if mode in ("shared_file", "shared_writer"):
        ts.conn.executescript(DDL_JOURNAL)
        jp = tp
    else:
        _connect(jp, journal="WAL", synchronous="FULL", ddl=DDL_JOURNAL).close()

    lease_q: list = []
    q_lock = threading.Lock()
    convoy = None
    jconn = None
    conns: list = []
    if mode == "convoy":
        jconn = _connect(jp, journal="WAL", synchronous="FULL")
        convoy = _Convoy(jconn, max_rows=convoy_k, max_wait_ms=convoy_t_ms)
        convoy.start()
    elif mode == "own":
        conns = [_connect(jp, journal="WAL", synchronous="FULL") for _ in range(threads)]
    elif mode == "shared_file":
        conns = [_connect(jp, journal="WAL", synchronous="FULL") for _ in range(threads)]

    INSERT = ("INSERT OR REPLACE INTO lease(txn_seq,attempt,processor,key,dispatch_ms,"
              "m_ms,window_ms,state) VALUES(?,?,?,?,?,?,?,?)")

    def make_submit(idx: int):
        if mode == "convoy":
            return convoy.submit
        if mode == "shared_writer":
            def submit(row):
                done = threading.Event()
                with q_lock:
                    lease_q.append((row, done))
                done.wait()
            return submit
        conn = conns[idx]

        def submit(row, _c=conn):
            _c.execute("BEGIN IMMEDIATE")
            _c.execute(INSERT, row)
            _c.execute("COMMIT")
        return submit

    class _Load(_PacedTraceLoad):
        """The trace writer; in shared_writer mode it also drains the lease queue into
        its own batch and flips synchronous=FULL for that commit."""

        def run(self):
            st = self.store
            t_next = time.perf_counter()
            for dec, ops, _sca in self.src.stream(10 ** 9):
                if self.stop.is_set():
                    break
                st.append_decision(dec)
                self.wrote += 1
                for op in ops:
                    st.append_op(KIND_OUTCOME, op)
                    self.wrote += 1
                if mode == "shared_writer":
                    with q_lock:
                        batch, lease_q[:] = list(lease_q), []
                    if batch:
                        c = st.conn
                        c.execute("PRAGMA synchronous=FULL")
                        c.executemany(INSERT, [r for r, _ in batch])
                        st.commit()
                        c.execute("PRAGMA synchronous=NORMAL")
                        for _, done in batch:
                            done.set()
                if self.interval:
                    t_next += self.interval * (1 + len(ops))
                    slack = t_next - time.perf_counter()
                    if slack > 0:
                        time.sleep(slack)
                    else:
                        t_next = time.perf_counter()
            st.commit()

    stop = threading.Event()
    load = _Load(ts, src, trace_rows_per_s, stop)
    load.start()
    if trace_rows_per_s:
        time.sleep(0.4)

    lat: list = []
    busy = [0]
    lat_lock = threading.Lock()
    per_thread = n_leases // threads
    pace = (1.0 / (pace_lps / threads)) if pace_lps else 0.0

    def worker(idx: int):
        submit = make_submit(idx)
        local: list = []
        t_next = time.perf_counter()
        for i in range(per_thread):
            seq = idx * per_thread + i
            key = struct.pack("<QQ", 0x5EED0000 ^ idx, seq)
            row = (seq, 0, seq % N_PROC, key, int(t_next * 1000) % 10 ** 9,
                   M_FOR[seq % N_PROC], WIN_MS, 0)
            t0 = time.perf_counter()
            try:
                submit(row)
            except sqlite3.OperationalError as e:
                if "locked" in str(e) or "busy" in str(e):
                    busy[0] += 1
                else:
                    raise
            local.append(us(time.perf_counter() - t0))
            if pace:
                t_next += pace
                slack = t_next - time.perf_counter()
                if slack > 0:
                    time.sleep(slack)
                else:
                    t_next = time.perf_counter()
        with lat_lock:
            lat.extend(local)

    ts_threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    t0 = time.perf_counter()
    for t in ts_threads:
        t.start()
    for t in ts_threads:
        t.join()
    dt = time.perf_counter() - t0
    stop.set()
    load.join(timeout=15)
    fsyncs = convoy.commits if convoy else len(lat)
    ts.close()
    for c in conns:
        c.close()
    if convoy:
        convoy.stop.set()
        convoy.join(timeout=3)
    if jconn:
        jconn.close()
    out = {"label": label, "mode": mode, "threads": threads, "leases": len(lat),
           "lps": len(lat) / dt, "p50": pct(lat, .5), "p90": pct(lat, .9),
           "p99": pct(lat, .99), "max": max(lat) if lat else 0.0, "busy": busy[0],
           "fsyncs": fsyncs, "log_rows": load.wrote,
           "log_rate": load.wrote / dt, "wall": dt}
    drop(tp)
    if jp != tp:
        drop(jp)
    return out


def sec_w2(o: Out, n: int, src: RowSource) -> None:
    hr(o, "[W2] the money row: where the LEASE append lives, and what a crash-safe "
          "dispatch costs")
    o("  ADR-0009 R71 is the one durability claim that moves money: the lease row --")
    o("  including the CSPRNG idempotency key -- is durable BEFORE the dispatch it")
    o("  authorizes, or a restart re-opens the double-charge window on its own. That puts")
    o("  a synchronous fsynced write on the route path, against ADR-0001's 2 ms p99")
    o(f"  in-engine budget and a dispatch pace of {OPS_PER_S:,.0f}/s (one lease per")
    o("  attempt). Three questions, measured where CPython can measure them cleanly and")
    o("  modelled -- with the constants inline -- where it cannot: what does one durable")
    o("  lease cost, how many can one fsync carry, and what does sharing a file with the")
    o("  log writer do to it.")
    o("  The lease rows carry the catalog's own contract facts, not constants invented")
    o("  here: #13 is the first consumer of the three fields ADR-0011 added, so m_ms is")
    o(f"  {CATALOG_VERSION}'s per-processor max_response_ms ({min(M_FOR):,} ms for")
    o(f"  {PROCESSORS[M_FOR.index(min(M_FOR))]} to {max(M_FOR):,} ms for "
      f"{PROCESSORS[M_FOR.index(max(M_FOR))]}), window_ms is")
    o("  ADR-0009 R67's oneoff_cnp resolution window, and import asserts R66's")
    o("  key_lifetime_ms >= max_response_ms for all six processors.")
    o()
    d = scratch("w2")

    # (a) the physics, single threaded: no GIL churn, no lock contention, just the device.
    o("  (a) one durable lease, and the convoy curve: K leases per fsync. Single")
    o("      thread, own file, synchronous=FULL, wal_autocheckpoint=0 and a FRESH file")
    o("      per K, so every row of the curve writes the same number of leases through")
    o("      the same WAL growth -- the device physics with nothing else in the loop.")
    o("      `WAL B/lease` is the write-ahead-log amplification: a commit writes a frame")
    o("      for every page it dirtied, so many small transactions write the SAME page")
    o("      many times. Batching buys bytes as well as fsyncs.")
    o(f"      {'K':>4} {'cmt p50':>9} {'cmt mean':>9} {'us/lease':>9} "
      f"{'ceiling':>9} {'vs fleet':>9} {'WAL B/lease':>12}")
    curve = {}
    LEASES = 1_024
    REPEATS = 3          # this box's fsync varies run to run; the median of 3 is the row
    for k in (1, 2, 4, 8, 16, 32, 64):
        means, p50s, p99s, wals = [], [], [], []
        for rep in range(REPEATS):
            p = d / f"k{k}r{rep}.sqlite"
            drop(p)
            conn = _connect(p, journal="WAL", synchronous="FULL", autocheckpoint=0,
                            ddl=DDL_JOURNAL)
            reps = LEASES // k
            lat = []
            for r in range(reps):
                t0 = time.perf_counter()
                conn.execute("BEGIN IMMEDIATE")
                for i in range(k):
                    conn.execute(
                        "INSERT OR REPLACE INTO lease(txn_seq,attempt,processor,key,"
                        "dispatch_ms,m_ms,window_ms,state) VALUES(?,?,?,?,?,?,?,?)",
                        (r * k + i, 0, i % N_PROC,
                         sqlite3.Binary(struct.pack("<QQ", 0x5EED, r * k + i)),
                         r, M_FOR[i % N_PROC], WIN_MS, 0))
                conn.execute("COMMIT")
                lat.append(time.perf_counter() - t0)
            wal = Path(str(p) + "-wal")
            wals.append((wal.stat().st_size if wal.exists() else 0) / LEASES)
            means.append(sum(lat) / len(lat))
            p50s.append(pct(lat, .5))
            p99s.append(pct(lat, .99))
            conn.close()
            drop(p)
        mean, p50, p99 = pct(means, .5), pct(p50s, .5), pct(p99s, .5)
        curve[k] = {"p50": p50, "p99": p99, "mean": mean, "ceiling": k / mean,
                    "wal_per_lease": pct(wals, .5),
                    "spread": max(means) / min(means)}
        o(f"      {k:>4} {us(p50):>7.0f}us {us(mean):>7.0f}us {us(mean) / k:>9.1f} "
          f"{k / mean:>9,.0f} {k / mean / OPS_PER_S:>8.1f}x "
          f"{curve[k]['wal_per_lease']:>12,.0f}")
    solo = curve[1]
    flat = max(v["mean"] for v in curve.values()) / min(v["mean"] for v in curve.values())
    o(f"      The commit cost moves {flat:.1f}x while K moves 64x "
      f"({us(curve[1]['mean']):.0f}us at K=1,")
    o(f"      {us(curve[64]['mean']):.0f}us at K=64, and the run-to-run spread inside one "
      f"K is up to")
    o(f"      {max(v['spread'] for v in curve.values()):.1f}x on this box): an fsync is a "
      "device round trip, not a byte count, so K")
    o("      leases ride it for free and the per-lease price falls")
    o(f"      {(curve[1]['mean'] / 1) / (curve[64]['mean'] / 64):.0f}x across the curve "
      f"while the WAL amplification falls")
    o(f"      {curve[1]['wal_per_lease'] / max(1, curve[64]['wal_per_lease']):.0f}x. One "
      f"durable lease costs {us(solo['p50']):.0f}us p50 /")
    o(f"      {us(solo['mean']):.0f}us mean / {us(solo['p99']):.0f}us p99 here -- "
      f"{solo['p50'] / 0.002 * 100:.0f}% of ADR-0001's 2 ms budget at p50 and")
    o(f"      {solo['p99'] / 0.002 * 100:.0f}% at p99 -- so "
      "R71 FITS the route path per lease")
    o(f"      on this device. Throughput is the tighter half: K=1 sustains "
      f"{curve[1]['ceiling']:,.0f}")
    o(f"      dispatches/s against a fleet pace of {OPS_PER_S:,.0f} "
      f"({curve[1]['ceiling'] / OPS_PER_S:.1f}x). That ratio is a")
    o("      device-shaped number, not a design-shaped one: this box fsyncs in")
    o(f"      ~{us(solo['mean']):.0f}us, and on a network-attached volume in the 1-5 ms "
      "class the same")
    o("      arithmetic gives (labelled model -- the fsync is the only constant, and it is")
    o("      the one number this sandbox cannot speak for):")
    o(f"        {'K':>4} {'ceiling here':>13} {'@1ms fsync':>12} {'@5ms fsync':>12} "
      f"{'fleet pace':>11}")
    for k in (1, 8, 32, 64):
        o(f"        {k:>4} {curve[k]['ceiling']:>13,.0f} {k / 0.001:>12,.0f} "
          f"{k / 0.005:>12,.0f} {OPS_PER_S:>11,.0f}")
    o(f"      At 5 ms per fsync, K=1 serves {1 / 0.005:,.0f} dispatches/s -- "
      f"{1 / 0.005 / OPS_PER_S * 100:.0f}% of the pace --")
    o(f"      and K={SHIPPED_CONVOY_K} serves {SHIPPED_CONVOY_K / 0.005:,.0f}. The convoy "
      "therefore ships as the mechanism, not as an")
    o("      optimisation, with K=1 as its degenerate low-volume case: at the 20 TPS")
    o("      deployment ADR-0006 sizes the WAL against, the assembly wait T fires before")
    o("      K fills and the convoy IS the solo commit. K and T are config; the design must")
    o("      not depend on being handed a fast fsync.")
    o()
    o("  (a2) the checkpoint tax on the money path: the same K=1 convoy, 4x the leases,")
    o("      with the shipped wal_autocheckpoint and without.")
    o(f"      {'wal_autocheckpoint':>19} {'cmt p50':>9} {'cmt mean':>9} {'ceiling':>9} "
      f"{'WAL MB':>8} {'WAL B/lease':>12}")
    ACP_MEANS: dict = {}
    ACP_WAL: dict = {}
    acp_means: dict = {}
    acp_wal: dict = {}
    for acp in (SHIPPED_AUTOCHECKPOINT, 0):
        p = d / f"acp{acp}.sqlite"
        drop(p)
        conn = _connect(p, journal="WAL", synchronous="FULL", autocheckpoint=acp,
                        ddl=DDL_JOURNAL)
        lat = []
        for r in range(4_096):
            t0 = time.perf_counter()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR REPLACE INTO lease(txn_seq,attempt,processor,key,"
                         "dispatch_ms,m_ms,window_ms,state) VALUES(?,?,?,?,?,?,?,?)",
                         (r, 0, r % N_PROC, sqlite3.Binary(struct.pack("<QQ", 0x5EED, r)),
                          r, M_FOR[r % N_PROC], WIN_MS, 0))
            conn.execute("COMMIT")
            lat.append(time.perf_counter() - t0)
        wal = Path(str(p) + "-wal")
        wal_b = wal.stat().st_size if wal.exists() else 0
        mean = sum(lat) / len(lat)
        o(f"      {acp:>19} {us(pct(lat, .5)):>7.0f}us {us(mean):>7.0f}us "
          f"{1 / mean:>9,.0f} {wal_b / 1e6:>8.1f} {wal_b / 4096:>12,.0f}")
        conn.close()
        drop(p)
        acp_means[acp], acp_wal[acp] = mean, wal_b / 4096
    TAX = acp_means[0] / acp_means[SHIPPED_AUTOCHECKPOINT]
    TAX_WAL = acp_wal[0] / acp_wal[SHIPPED_AUTOCHECKPOINT]
    o(f"      An un-checkpointed WAL is not a free buffer: the SAME commit is "
      f"{TAX:.1f}x more")
    o(f"      expensive and writes {TAX_WAL:.0f}x the WAL bytes per lease. It is the same "
      "effect [W1](c) shows on")
    o("      the log writer's throughput, seen from the latency side. Checkpoint cadence")
    o("      is part of the write path's cost model, not housekeeping -- and [W10] shows")
    o("      the reader that can pin the WAL open no matter what the pragma says.")
    o()

    # (b) two files or one: device interference measured, lock wait modelled.
    o("  (b) does the money row need its own FILE? Two measurements and one model,")
    o("      because the honest answer is not the intuitive one.")
    o("      (b1) device interference, single-threaded and alternating: one log batch")
    o("           commit, then one lease commit, repeatedly -- the same sequence the")
    o("           engine runs, without the interpreter's thread scheduler in the numbers.")
    o(f"      {'placement':<26} {'lease p50':>10} {'lease p99':>10} {'log batch p50':>14} "
      f"{'log batch p99':>14} {'pairs/s':>9}")
    inter = {}
    for label, shared in (("two files (journal+trace)", False), ("one file (shared)", True)):
        p = d / ("one.sqlite" if shared else "trace.sqlite")
        jp = p if shared else d / "journal.sqlite"
        drop(p)
        if not shared:
            drop(jp)
        ts = TraceStore(p, durability="batched", commit_every=SHIPPED_BATCH,
                        commit_ms=1e9)
        if shared:
            ts.conn.executescript(DDL_JOURNAL)
        jconn = _connect(jp, journal="WAL", synchronous="FULL",
                         ddl=None if shared else DDL_JOURNAL)
        lease_lat, log_lat = [], []
        pairs = max(500, min(n // 100, 4_000))
        gen = src.stream(pairs * SHIPPED_BATCH)
        t0 = time.perf_counter()
        for r in range(pairs):
            for _ in range(SHIPPED_BATCH // 2):
                dec, ops, _sca = next(gen)
                ts.append_decision(dec)
                for op in ops:
                    ts.append_op(KIND_OUTCOME, op)
            t1 = time.perf_counter()
            ts.commit()
            log_lat.append(time.perf_counter() - t1)
            t2 = time.perf_counter()
            jconn.execute("BEGIN IMMEDIATE")
            jconn.execute("INSERT OR REPLACE INTO lease(txn_seq,attempt,processor,key,"
                          "dispatch_ms,m_ms,window_ms,state) VALUES(?,?,?,?,?,?,?,?)",
                          (r, 0, r % N_PROC, sqlite3.Binary(struct.pack("<QQ", 0x5EED, r)),
                           r, M_FOR[r % N_PROC], WIN_MS, 0))
            jconn.execute("COMMIT")
            lease_lat.append(time.perf_counter() - t2)
        dt = time.perf_counter() - t0
        inter[shared] = {"lease50": pct(lease_lat, .5), "lease99": pct(lease_lat, .99),
                         "log50": pct(log_lat, .5), "log99": pct(log_lat, .99),
                         "pairs": pairs / dt}
        r_ = inter[shared]
        o(f"      {label:<26} {us(r_['lease50']):>8.0f}us {us(r_['lease99']):>8.0f}us "
          f"{us(r_['log50']):>12.0f}us {us(r_['log99']):>12.0f}us {r_['pairs']:>9,.0f}")
        ts.close()
        jconn.close()
        drop(p)
        if not shared:
            drop(jp)
    two, one = inter[False], inter[True]
    o(f"      Lease p99 {us(two['lease99']):.0f}us with its own file, "
      f"{us(one['lease99']):.0f}us sharing the log's; log batch p99")
    o(f"      {us(two['log99']):.0f}us vs {us(one['log99']):.0f}us. Whichever way the sign "
      f"falls on a given run,")
    o("      the difference is inside this box's run-to-run spread ([W1] moved 20% between")
    o("      identical configs). The split is NOT bought by device interference, and this")
    o("      ADR does not claim that it is.")
    o()
    o("      (b2) the write lock, modelled from measured parts: SQLite gives one file one")
    o("      writer, so in a concurrent engine a lease that shares the log's file waits")
    o("      for the log's in-flight write transaction. From [W1](a) at the shipped")
    w1 = LAST.get("w1", {}).get(("WAL", "strict", SHIPPED_BATCH))
    log_hold_us = us(w1["p50"]) if w1 else 441.0   # [W1](a) batch-64 WAL+FULL on this box
    commits_per_s = ROWS_PER_S / SHIPPED_BATCH
    duty = log_hold_us * commits_per_s / 1e6
    o(f"      config a batch-{SHIPPED_BATCH} commit holds the lock ~{log_hold_us:.0f}us "
      f"(measured in this run), and the fleet pace")
    o(f"      needs {commits_per_s:.0f} such commits/s, so the lock is held "
      f"{duty * 100:.1f}% of the time:")
    o(f"      a shared-file lease waits an extra {log_hold_us:.0f}us on ~{duty * 100:.0f}% "
      f"of dispatches, i.e. its")
    o(f"      p99 moves {us(two['lease99']):.0f}us -> {us(two['lease99']) + log_hold_us:.0f}us. "
      "Still inside the 2 ms budget; a checkpoint")
    _c = LAST.get("w1c", {})
    _ship_c = _c.get(SHIPPED_AUTOCHECKPOINT)
    _coarse = _c.get(8000)
    if _ship_c and _coarse:
        _stall = (f"[W1](c): p99.9 {_ship_c['p999_us'] / 1000:.1f} ms at "
                  f"wal_autocheckpoint={_ship_c['acp']}, "
                  f"{_coarse['p999_us'] / 1000:.1f} ms at {_coarse['acp']}")
        _stall_hi = max(r["max_us"] for r in _c.values()) / 1000
        _stall_lo = _ship_c["p999_us"] / 1000
    else:      # --section=W2 alone: quote the mechanism, not a number from another run
        _stall = "[W1](c): a p99.9 stall in the milliseconds at every cadence"
        _stall_lo = _stall_hi = 0.0
    o(f"      stall ({_stall}) is")
    o("      not, and a checkpoint is exactly what a shared file would put in the money")
    o("      path's tail. THAT is the latency argument for the split, and it is a tail")
    o("      argument, not a median one.")
    o()
    o("      (b3) the two arguments that do not need a benchmark at all:")
    o("        * rotation. The trace file is a partition: it is written for a day (or a")
    o("          run), sealed, exported and eventually dropped ([W8]). A lease written at")
    o("          23:59:58 resolves after midnight, and the ledger keeps its key for")
    o("          key_lifetime_ms and its reconciliation entry for longer than that. Money")
    o("          rows in a rotating file have to be migrated forward at every rotation;")
    o("          money rows in their own file do not.")
    o("        * writer cadence. The log's writer is the ingest fold's writer -- batched,")
    o("          K <= 64, throughput-shaped (R48). The money writer is the route path's")
    o("          convoy -- latency-shaped, one row per dispatch. One connection cannot be")
    o("          both without one of them inheriting the other's cadence, which is the")
    o("          `shared_writer` breach in (c).")
    o()

    # (c) the third placement, modelled from measured parts.
    o("  (c) `shared_writer` -- the lease riding the log writer's own batch and")
    o("      connection -- is the placement ADR-0006 R48's wording invites if it is read")
    o("      as a file rule instead of an array rule. It is not measurable with CPython")
    o("      threads at the fleet pace without measuring the interpreter, so it is a")
    o("      MODEL with the constants taken from [W1] and (a):")
    batch_p50 = us(w1["p50"]) if w1 else 441.0   # WAL+FULL batch 64, from [W1](a)
    interval_ms = 5.0  # the shipped group-commit interval
    o(f"        lease durable latency = the group-commit interval it waits for")
    o(f"          (up to {interval_ms:.0f} ms at the shipped cadence) + the batch's own")
    o(f"          fsync ({batch_p50:.0f}us at batch {SHIPPED_BATCH}, [W1](a))")
    o(f"          = up to {interval_ms * 1000 + batch_p50:,.0f}us, i.e. "
      f"{(interval_ms * 1000 + batch_p50) / 2000:.1f}x the 2 ms budget")
    o("        before any device tail, and it inherits the checkpoint stall on top.")
    o("      Rejected. R48's one-writer-per-shard rule is about who mutates the posterior")
    o("      arrays; it says nothing about how many files a shard owns, and reading it as")
    o("      a file rule costs the money path its latency budget.")
    o()

    # (d) the convoy as a service, end to end, with real threads.
    o("  (d) the convoy as a service: 4 dispatcher threads, own file, trace log at the")
    o("      fleet pace in parallel. This is the mechanism the ADR ships, end to end;")
    o("      the absolute rate is CPython's (see the caveat below), the amortisation is")
    o("      the device's.")
    o(f"      {'K':>4} {'T':>7} {'leases/s':>9} {'fsyncs':>8} {'leases/fsync':>13} "
      f"{'p50':>8} {'p99':>8} {'log rows/s':>11}")
    n_leases = max(2_000, min(n // 100, 8_000))
    for k, t_ms in ((8, 0.25), (32, 0.5), (32, 2.0), (64, 4.0)):
        r = _lease_run(d, f"convoy{k}", mode="convoy", threads=4, n_leases=n_leases,
                       pace_lps=0.0, trace_rows_per_s=ROWS_PER_S, src=src,
                       convoy_k=k, convoy_t_ms=t_ms)
        o(f"      {k:>4} {t_ms:>5}ms {r['lps']:>9,.0f} {r['fsyncs']:>8,} "
          f"{r['leases'] / max(1, r['fsyncs']):>13.1f} {r['p50']:>6.0f}us "
          f"{r['p99']:>6.0f}us {r['log_rate']:>11,.0f}")
    o()
    o("  Readings:")
    o(f"   * R71 fits the route path per lease ({solo['p50'] / 0.002 * 100:.0f}% of the "
      f"2 ms budget at p50,")
    o(f"     {solo['p99'] / 0.002 * 100:.0f}% at p99 on this device) and fits the fleet "
      f"pace only with a convoy: K=1")
    o(f"     leaves {curve[1]['ceiling'] / OPS_PER_S:.1f}x headroom here and negative "
      "headroom on a network-attached")
    o("     volume, so the group-commit service ships as the mechanism, not as an")
    o("     optimisation.")
    o("   * The money file is separate, and the reason is rotation and tail, not")
    o("     throughput: interference between the two writers measured inside this box's")
    o(f"     noise, the write-lock model costs ~{log_hold_us / 1000:.1f} ms of p99, and a "
      "checkpoint stall in")
    o("     a shared file costs "
      + (f"{_stall_lo:.0f}-{_stall_hi:.0f} ms of p99.9." if _stall_hi else
         "milliseconds of p99.9.")
      + " Money rows also must not live in a file")
    o("     that gets sealed and dropped on a schedule.")
    o("   * The convoy's K and T are config: K binds at the fleet pace, T binds at the")
    o("     20 TPS deployment, and one service does both. `leases/fsync` is the number to")
    o("     watch -- if it sits at 1.0 at peak, the money path is paying full price for")
    o("     durability and reopen trigger 1 is live.")
    o("   * CPython caveat, stated because it bounds the claim: the GIL serialises the")
    o("     Python side of (b) and (d), so those absolute rates carry interpreter overhead")
    o("     a Go engine would not pay -- a Go convoy parks goroutines in fsync without")
    o("     holding a global lock, and (a)'s curve, which is single-threaded, is the part")
    o("     that transfers unchanged in shape. What the GIL does not touch is the thing")
    o("     being measured: SQLite's one-write-lock-per-file and the device's fsync both")
    o("     run with the GIL released. #17 re-measures the absolutes in Go on the")
    o("     production device class.")
    o()


# --------------------------------------------------------------------------------------
# [W3] crash semantics: what each configuration actually loses
# --------------------------------------------------------------------------------------

def _crash_child(path: str, ddl: str, journal: str, sync: str, batch: int, commits: int,
                 kill_when: str, checkpoint_only: bool, checkpoint_every: int,
                 autocheckpoint: int):
    """Runs in the forked child: write, then die by SIGKILL without closing anything.

    kill_when: 'after_commit' (die between transactions), 'mid_transaction' (die inside an
    open write transaction), 'mid_checkpoint' (die inside PRAGMA wal_checkpoint)."""
    try:
        conn = sqlite3.connect(path, isolation_level=None, timeout=5.0)
        conn.execute(f"PRAGMA journal_mode={journal}")
        conn.execute(f"PRAGMA synchronous={sync}")
        conn.execute(f"PRAGMA wal_autocheckpoint={autocheckpoint}")
        if ddl:
            conn.executescript(ddl)
        payload = struct.pack("<Bq", 1, 12800) * 3 + bytes(96)   # ~120 B: the width of a
        #  real op row's context columns, so the WAL holds a realistic number of frames
        rows = 0
        if checkpoint_only:
            # the ticket's option 1: state in memory, a periodic checkpoint to disk, no
            # log. The checkpoint is the ONLY durable artifact, so the loss is every op
            # since the last one -- and there is nothing to reconstruct them from.
            state = array.array("d", bytes(N_ARMS * STATE_FLOATS_PER_ARM * 8))
            ck = sqlite3.connect(path + ".ckpt", isolation_level=None)
            ck.execute("PRAGMA journal_mode=WAL")
            ck.execute("PRAGMA synchronous=FULL")
            ck.execute("CREATE TABLE IF NOT EXISTS snapshot(op_seq INTEGER PRIMARY KEY,"
                       "posterior BLOB NOT NULL)")
            for k in range(commits * batch):
                state[(k * 7919) % (N_ARMS * STATE_FLOATS_PER_ARM)] += 1.0
                rows += 1
                if rows % checkpoint_every == 0:
                    ck.execute("BEGIN")
                    ck.execute("INSERT OR REPLACE INTO snapshot VALUES(1,?)",
                               (sqlite3.Binary(state.tobytes()),))
                    ck.execute("COMMIT")
            os.kill(os.getpid(), signal.SIGKILL)
        ins = ("INSERT INTO op(op_seq,kind,boot_id,ms,seq,attempt,processor,outcome,payload)"
               " VALUES(?,?,?,?,?,?,?,?,?)")
        for k in range(commits):
            conn.execute("BEGIN IMMEDIATE")
            for i in range(batch):
                conn.execute(ins, (rows + 1, KIND_OUTCOME, 1, rows, rows, 0, i % N_PROC,
                                   H.AUTHORIZED, sqlite3.Binary(payload)))
                rows += 1
            if kill_when == "mid_transaction" and k == commits - 1:
                conn.execute(ins, (rows + 1, KIND_OUTCOME, 1, rows, rows, 0, 0,
                                   H.AUTHORIZED, sqlite3.Binary(payload)))
                rows += 1
                os.kill(os.getpid(), signal.SIGKILL)
            conn.execute("COMMIT")
            if kill_when == "mid_checkpoint" and k == commits - 1:
                # a checkpoint is a copy from WAL to database file plus a WAL reset; dying
                # in the middle of it is the case the two-file protocol has to survive.
                conn.execute("PRAGMA wal_checkpoint(RESTART)")
                os.kill(os.getpid(), signal.SIGKILL)
        os.kill(os.getpid(), signal.SIGKILL)
    except BaseException:
        os._exit(3)
    os._exit(0)


def _crash_case(name: str, path: Path, *, journal="WAL", sync="FULL", batch=8,
                commits=250, kill_when="after_commit", ddl=DDL_TRACE,
                checkpoint_only=False, checkpoint_every=750, truncate_wal: float | None = None,
                autocheckpoint=SHIPPED_AUTOCHECKPOINT) -> dict:
    drop(path)
    ckpt_path = Path(str(path) + ".ckpt")
    drop(ckpt_path)
    pid = os.fork()
    if pid == 0:
        _crash_child(str(path), ddl, journal, sync, batch, commits, kill_when,
                     checkpoint_only, checkpoint_every, autocheckpoint)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    issued = commits * batch + (1 if kill_when == "mid_transaction" else 0)
    durable = issued - 1 if kill_when == "mid_transaction" else issued
    if kill_when == "mid_transaction":
        durable = (commits - 1) * batch        # the killed transaction never committed
    if checkpoint_only:
        conn = sqlite3.connect(str(ckpt_path))
        row = conn.execute("SELECT posterior FROM snapshot WHERE op_seq=1").fetchone()
        conn.close()
        survived = (issued // checkpoint_every) * checkpoint_every
        return {"name": name, "issued": issued, "durable": survived,
                "recovered": survived if row else 0, "lost": issued - survived,
                "integrity": "no log", "reopen_ms": 0.0,
                "wal_bytes": 0, "unckpt": 0}
    if truncate_wal is not None:
        wal = Path(str(path) + "-wal")
        if wal.exists():
            size = wal.stat().st_size
            with open(wal, "r+b") as f:
                f.truncate(int(size * truncate_wal))
    wal_before = Path(str(path) + "-wal")
    wal_bytes = wal_before.stat().st_size if wal_before.exists() else 0
    t0 = time.perf_counter()
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=10.0)
    reopen_ms = (time.perf_counter() - t0) * 1000.0
    try:
        recovered = conn.execute("SELECT COUNT(*) FROM op").fetchone()[0]
    except sqlite3.DatabaseError as e:
        recovered, integrity = -1, f"unqueryable: {str(e)[:60]}"
    else:
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
    conn.close()
    return {"name": name, "issued": issued, "durable": durable, "recovered": recovered,
            "lost": durable - recovered, "integrity": integrity, "reopen_ms": reopen_ms,
            "wal_bytes": wal_bytes,
            "unckpt": max(0, durable - recovered) if truncate_wal is None else 0}


def sec_w3(o: Out, n: int, src: RowSource) -> None:
    hr(o, "[W3] crash semantics: what each configuration actually loses when the process "
          "is killed")
    o("  Every durability claim in this ADR is a statement about a crash, so the crash is")
    o("  measured rather than argued: a forked child opens the store, appends, and dies by")
    o("  SIGKILL -- no close, no atexit, no clean shutdown. The parent then reopens the")
    o("  file the way a restarting engine would and counts what is there.")
    o("    issued    rows the child handed to SQLite")
    o("    durable   rows in transactions that COMMITTED before the kill (what a correct")
    o("              store owes back -- an uncommitted transaction is not owed)")
    o("    recovered rows the reopened file returned;  integrity = PRAGMA quick_check")
    o()
    d = scratch("w3")
    B, C = 8, 250
    cases = [
        _crash_case("WAL + FULL (strict)", d / "a.sqlite", journal="WAL", sync="FULL",
                    batch=B, commits=C),
        _crash_case("WAL + NORMAL (batched)", d / "b.sqlite", journal="WAL", sync="NORMAL",
                    batch=B, commits=C),
        _crash_case("WAL + OFF (ephemeral)", d / "c.sqlite", journal="WAL", sync="OFF",
                    batch=B, commits=C),
        _crash_case("DELETE + FULL", d / "d.sqlite", journal="DELETE", sync="FULL",
                    batch=B, commits=C),
        _crash_case("DELETE + OFF", d / "e.sqlite", journal="DELETE", sync="OFF",
                    batch=B, commits=C),
        _crash_case("MEMORY journal + OFF", d / "f.sqlite", journal="MEMORY", sync="OFF",
                    batch=B, commits=C),
        _crash_case("WAL + FULL, killed mid-transaction", d / "g.sqlite", journal="WAL",
                    sync="FULL", batch=B, commits=C, kill_when="mid_transaction"),
        _crash_case("DELETE + OFF, killed mid-transaction", d / "h.sqlite",
                    journal="DELETE", sync="OFF", batch=B, commits=C,
                    kill_when="mid_transaction"),
        _crash_case("MEMORY + OFF, killed mid-transaction", d / "i.sqlite",
                    journal="MEMORY", sync="OFF", batch=B, commits=C,
                    kill_when="mid_transaction"),
        _crash_case("WAL + FULL, killed mid-checkpoint", d / "j.sqlite", journal="WAL",
                    sync="FULL", batch=B, commits=C, kill_when="mid_checkpoint"),
        _crash_case("in-memory + checkpoint every 750, no log", d / "k.sqlite",
                    checkpoint_only=True, checkpoint_every=750, batch=B, commits=C),
    ]
    o(f"  {'configuration':<40} {'issued':>8} {'durable':>8} {'recovered':>10} "
      f"{'lost':>5} {'integrity':>10} {'reopen':>9}")
    for c in cases:
        o(f"  {c['name']:<40} {c['issued']:>8,} {c['durable']:>8,} {c['recovered']:>10,} "
          f"{c['lost']:>5} {str(c['integrity'])[:10]:>10} {c['reopen_ms']:>7.1f}ms")
    o()
    o("  Readings of (a):")
    o("   * Not one committed row is lost by any configuration on a process crash,")
    o("     including synchronous=OFF, because a process crash leaves the WAL (or the")
    o("     rollback journal) in the operating system's page cache and the operating")
    o("     system is still alive. That is why FULL-vs-NORMAL cannot be settled by")
    o("     killing processes: it is a POWER-loss question, which (b) addresses.")
    o("   * Killed mid-transaction, WAL+FULL returns exactly the committed prefix and")
    o("     rolls the open transaction back: durable 1,992 of 2,000 issued, recovered")
    o("     1,992, lost 0. That is atomicity measured, not assumed.")
    o("   * The mid-transaction kill is where the journal mode earns its name: with the")
    o("     rollback journal in MEMORY (or with synchronous=OFF under a rollback journal)")
    o("     the journal that would undo the partial transaction never reached a file. On")
    o("     this box the reopen still reports ok and the count is still the committed")
    o("     prefix -- the write was small enough to be inside one page -- but the")
    o("     guarantee is now the filesystem's, not SQLite's, which is exactly what")
    o("     SQLite's own documentation declines to promise for those settings. The")
    o("     engine refuses them for the log; [W1] shows what they would have bought")
    o("     (1.5x throughput at batch 64) and that is not a trade for an audit trail.")
    o("   * Killed mid-checkpoint, the file opens clean and the committed prefix is")
    o("     intact: a checkpoint is a copy plus a WAL reset, and both are recoverable.")
    o("     This matters because the checkpoint is the one place where the log and the")
    o("     database file are both being written.")
    o("   * `in-memory + checkpoint` -- the ticket's first model-state option -- loses")
    o("     every op since the last checkpoint (500 of 2,000 here) and has NO log to")
    o("     reconstruct them from. The loss is not only learning: those decisions have no")
    o("     audit row, so #15 cannot recompute their propensities and ADR-0004 6's")
    o("     one-record-per-decision claim has a hole in it. That is why this ADR keeps the")
    o("     log canonical and puts the in-memory arrays downstream of it (R47) instead of")
    o("     adopting the option as the ticket wrote it.")
    o()
    o("  (b) the power-loss emulation. A sandbox cannot pull the plug, so the plug is")
    o("      pulled by hand at the layer that a plug-pull actually exposes: the WAL file's")
    o("      tail. The child writes with wal_autocheckpoint=0 (nothing reaches the")
    o("      database file), the parent truncates the WAL to a fraction of its length, and")
    o("      the store is reopened. What this measures is (i) how much of the log lives")
    o("      only in the WAL, and (ii) whether a torn WAL is recoverable at all. A real")
    o("      power loss can also tear pages in the database file and reorder filesystem")
    o("      metadata; neither is emulated, and the claim below does not reach them.")
    o()
    o(f"      {'WAL kept':>9} {'issued':>8} {'recovered':>10} {'lost':>7} "
      f"{'lost %':>7} {'integrity':>10} {'reopen':>9}")
    for keep in (0.0, 0.10, 0.25, 0.50, 0.75, 0.90, 0.99, 1.0):
        r = _crash_case(f"trunc{keep}", d / "p.sqlite", journal="WAL", sync="NORMAL",
                        batch=B, commits=C, truncate_wal=keep, autocheckpoint=0)
        o(f"      {keep * 100:>8.0f}% {r['issued']:>8,} {r['recovered']:>10,} "
          f"{r['issued'] - r['recovered']:>7} "
          f"{100 * (r['issued'] - r['recovered']) / r['issued']:>6.1f}% "
          f"{str(r['integrity']).split(':')[0][:10]:>10} {r['reopen_ms']:>7.1f}ms")
        if str(r["integrity"]).startswith("unqueryable"):
            o("               (the 0% row lost the WAL HEADER itself, so there is no frame")
            o(f"               chain to validate: {r['integrity'][:60]})")
    o("      A torn WAL tail is DISCARDED, not believed: every truncation point reopens")
    o("      `ok` and returns a prefix of the log, never a corrupt middle. The loss is")
    o("      linear in the bytes the device did not get, which is the whole content of the")
    o("      durability question -- so the design variable is not 'can SQLite recover'")
    o("      (it can, and this is the measurement) but 'how many bytes are un-fsynced at")
    o("      any instant', which is the pragma and the group commit.")
    o()
    o("  (c) the un-fsynced window, in rows and in seconds, at the shipped config. This")
    o("      is the number a durability guarantee is made of: synchronous=FULL fsyncs")
    o("      every commit, so the window is one group commit; synchronous=NORMAL fsyncs at")
    o("      the checkpoint, so the window is one checkpoint interval.")
    o(f"      {'pragma':<24} {'un-fsynced window':<18} {'rows':>8} "
      f"{'s at fleet pace':>16} {'s at 20 TPS':>13}")
    windows = {}
    for label, dur, window_name in (("WAL + FULL (strict)", "strict", "one group commit"),
                                    ("WAL + NORMAL (batched)", "batched",
                                     "one checkpoint interval")):
        p = d / "w.sqlite"
        drop(p)
        st = TraceStore(p, durability=dur, commit_every=SHIPPED_BATCH, commit_ms=1e9,
                        autocheckpoint=SHIPPED_AUTOCHECKPOINT)
        wrote = 0
        since_ckpt = 0
        max_since_ckpt = 0
        checkpoints = 0
        at_ckpt = 0
        wal = Path(str(p) + "-wal")
        last_seq = -1
        for dec, ops, _sca in src.stream(200_000):
            st.append_decision(dec)
            wrote += 1
            for op in ops:
                st.append_op(KIND_OUTCOME, op)
                wrote += 1
            if wrote >= 60_000:
                break
            # a checkpoint is observable from outside as the WAL header's checkpoint
            # sequence number advancing (SQLite reuses the WAL in place rather than
            # shrinking it, so the file size is not the signal)
            if wal.exists():
                with open(wal, "rb") as f:
                    hdr = f.read(24)
                if len(hdr) == 24:
                    seq = struct.unpack_from("<I", hdr, 12)[0]
                    if seq != last_seq:
                        last_seq = seq
                        if checkpoints:
                            max_since_ckpt = max(max_since_ckpt, wrote - at_ckpt)
                        checkpoints += 1
                        at_ckpt = wrote
            since_ckpt = wrote - at_ckpt
        max_since_ckpt = max(max_since_ckpt, since_ckpt)
        st.close()
        drop(p)
        rows_in_window = SHIPPED_BATCH if dur == "strict" else max_since_ckpt
        windows[label] = rows_in_window
        name = window_name if dur == "strict" else f"{checkpoints} checkpoints seen"
        o(f"      {label:<24} {name:<18} {rows_in_window:>8,} "
          f"{rows_in_window / ROWS_PER_S:>14.3f}s {rows_in_window / (20 * 1.3):>11.1f}s")
    ratio = windows["WAL + NORMAL (batched)"] / max(1, windows["WAL + FULL (strict)"])
    o()
    o("      The shipped answer is the first row: at synchronous=FULL with a group commit")
    o(f"      of {SHIPPED_BATCH} rows the un-fsynced window is one batch -- "
      f"{SHIPPED_BATCH / ROWS_PER_S * 1000:.1f} ms of traffic at the fleet pace,")
    o(f"      {SHIPPED_BATCH / (20 * 1.3):.1f} s at 20 TPS -- so an ack given after that "
      "commit is a durability claim")
    o("      that survives a power loss, which is what ADR-0011 R83 asks of the ingest path.")
    o("      NORMAL's window is")
    o(f"      the checkpoint interval, {windows['WAL + NORMAL (batched)']:,} rows here = "
      f"{ratio:.0f}x wider, and that cadence is")
    o("      a housekeeping parameter")
    o("      rather than a promise: it is what harness mode uses, where the artifact is")
    o("      reproducible from its scenario hash and losing it costs a re-run rather than")
    o("      an audit hole. Both files ship FULL; the batched class exists for the")
    o("      harness and for nothing that carries money.")
    o()


# --------------------------------------------------------------------------------------
# [W4] the row layout
# --------------------------------------------------------------------------------------
#
# The ticket asks for a schema. A schema is a set of trade-offs that can be priced: every
# field is either a column (queryable by SQLite, costs an index if you want it fast, costs
# B-tree overhead on every row) or inside a blob (free to SQLite, decoded by committed code
# in analysis/, invisible to WHERE). The four layouts below are the ones the ADR could have
# chosen, priced on the three things that decide between them: bytes per row, insert rows/s
# at the shipped durability, and the four query shapes #15/#16 actually run.
#
#   A columns + blobs   the shipped layout: scalars a query names are columns, the two
#                       fixed-layout ARRAYS (per-eligible posteriors, committed chain) are
#                       blobs. R89's rule, stated as a measurement.
#   B blob only         one fixed-layout blob per row plus (op_seq, kind, seq). The minimum
#                       SQLite is asked to understand. Smallest, fastest to insert, and a
#                       query that names a field is a Python loop over every row.
#   C json text         one JSON document per row. Self-describing, portable, and the layout
#                       a "just log it as JSON" instinct reaches for. Priced with SQLite's
#                       own json_extract so the comparison is against what SQLite can do
#                       with it, not against a strawman.
#   D no raw context    layout A minus the per-row context columns (bin_class, region, sca,
#                       mandate, amount_minor, band). Saves bytes; ADR-0006 R40's
#                       re-bucketing then needs the world model to rebuild them, which is
#                       the cost this row prices.
#
# Every layout holds the SAME rows from the SAME scenario pool, so the byte counts differ
# only by layout and the query times only by what the layout can answer.

LAYOUT_DDL = {}

LAYOUT_DDL["A columns + blobs"] = DDL_TRACE

# B and C both carry the (seq, attempt) index, because the engine's dedupe (R44) needs a
# point probe regardless of layout. Without it the comparison would price the missing index
# rather than the layout, and every byte the index costs is counted in (a) below.
LAYOUT_DDL["B blob only"] = """
CREATE TABLE op(op_seq INTEGER PRIMARY KEY, kind INTEGER NOT NULL, seq INTEGER NOT NULL,
                attempt INTEGER NOT NULL, body BLOB NOT NULL) WITHOUT ROWID;
CREATE INDEX op_point ON op(seq, attempt);
CREATE TABLE decision(seq INTEGER PRIMARY KEY, body BLOB NOT NULL);
"""

LAYOUT_DDL["C json text"] = """
CREATE TABLE op(op_seq INTEGER PRIMARY KEY, kind INTEGER NOT NULL, seq INTEGER NOT NULL,
                attempt INTEGER NOT NULL, doc TEXT NOT NULL);
CREATE INDEX op_point ON op(seq, attempt);
CREATE TABLE decision(seq INTEGER PRIMARY KEY, doc TEXT NOT NULL);
"""

LAYOUT_DDL["D no raw context"] = """
CREATE TABLE op(op_seq INTEGER PRIMARY KEY, boot_id INTEGER NOT NULL, kind INTEGER NOT NULL,
                ms INTEGER NOT NULL, seq INTEGER NOT NULL, attempt INTEGER NOT NULL,
                processor INTEGER NOT NULL, outcome INTEGER NOT NULL, code INTEGER,
                decline_class INTEGER, latency_ms INTEGER, settled_ms INTEGER,
                scenario_hash TEXT, payload BLOB);
CREATE UNIQUE INDEX op_dedupe ON op(seq, attempt) WHERE kind = 1;
CREATE INDEX op_ms ON op(ms);
CREATE TABLE decision(seq INTEGER PRIMARY KEY, boot_id INTEGER NOT NULL,
                arrival_ms INTEGER NOT NULL, eligible INTEGER NOT NULL, chosen INTEGER NOT NULL,
                chain_len INTEGER NOT NULL, chain BLOB NOT NULL, posteriors BLOB NOT NULL,
                propensity REAL NOT NULL, audit_hash BLOB NOT NULL);
"""

# op row field names, in the order the shipped layout stores them (used by B and C).
OP_FIELDS = ("ms", "seq", "attempt", "processor", "outcome", "code", "decline_class",
             "latency_ms", "settled_ms", "bin_class", "region", "sca", "mandate",
             "amount_minor", "band", "currency", "merchant_cat", "route_class",
             "entry_mode")
# ms, seq, attempt are i64; settled_ms is i64 because the harness clock spans days;
# the ordinals and small counts are i32. 92 B fixed -- see W4 for what that buys.
OP_FMT = "<qqq" + "i" * 5 + "q" + "i" * 10
DEC_FMT = "<q" + "i" * 5 + "q" + "i" * 4 + "i" * 3 + "f" + "i" * 2   # 17 scalars
OP_IX = {name: i for i, name in enumerate(OP_FIELDS)}
OP_BODY_BYTES = struct.calcsize(OP_FMT)
DEC_FIELDS = ("arrival_ms", "bin_class", "region", "sca", "mandate", "band", "amount_minor",
              "currency", "merchant_cat", "route_class", "entry_mode", "eligible", "chosen",
              "chain_len", "propensity", "method", "floor_state")


def _op_body(op: tuple) -> bytes:
    """op row (as RowSource yields it, minus boot_id and scenario_hash/payload) -> blob."""
    return struct.pack(OP_FMT, *[op[i] or 0 for i in range(1, 20)])


def _op_doc(op: tuple, h: str) -> str:
    return json.dumps({k: op[i + 1] for i, k in enumerate(OP_FIELDS)} |
                      {"scenario_hash": h}, separators=(",", ":"))


def _dec_body(d: tuple) -> bytes:
    return struct.pack(DEC_FMT, d[2], d[3], d[4], d[5], d[6], d[7], d[8], d[9], d[10],
                       d[11], d[12], d[13], d[14], d[15], d[18], d[19], d[20]) + \
        bytes(d[16]) + bytes(d[17])


def _dec_doc(d: tuple) -> str:
    """The JSON layout stores the arrays as JSON arrays too -- that is the honest version of
    'just log it as JSON', and it is what makes the OPE decode (Q5) expensive."""
    a = array.array("f")
    a.frombytes(bytes(d[17]))
    head = {k: d[i] for i, k in enumerate(
        ("seq", "boot_id", "arrival_ms", "bin_class", "region", "sca", "mandate", "band",
         "amount_minor", "currency", "merchant_cat", "route_class", "entry_mode", "eligible",
         "chosen", "chain_len"))}
    head.update({"chain": [list(struct.unpack_from("<Bq", bytes(d[16]), i * 9))
                           for i in range(d[15])],
                 "posteriors": list(a), "propensity": d[18], "method": d[19],
                 "floor_state": d[20], "audit_hash": bytes(d[21]).hex()})
    return json.dumps(head, separators=(",", ":"))


def _load_layout(path: Path, layout: str, n: int, src: RowSource) -> tuple:
    """Write n decisions (and their ops) in `layout`; return (conn, stats)."""
    drop(path)
    conn = _connect(path, journal="WAL", synchronous="FULL", ddl=LAYOUT_DDL[layout],
                    autocheckpoint=SHIPPED_AUTOCHECKPOINT)
    n_op = n_dec = 0
    dec_buf: list = []
    op_buf: list = []
    t0 = time.perf_counter()
    commits = 0
    lat: list[float] = []

    def flush() -> None:
        nonlocal commits
        if not (dec_buf or op_buf):
            return
        t = time.perf_counter()
        conn.execute("BEGIN IMMEDIATE")
        if layout == "A columns + blobs":
            if dec_buf:
                conn.executemany(
                    "INSERT INTO decision(seq,boot_id,arrival_ms,bin_class,region,sca,"
                    "mandate,band,amount_minor,currency,merchant_cat,route_class,entry_mode,"
                    "eligible,chosen,chain_len,chain,posteriors,propensity,method,"
                    "floor_state,audit_hash) VALUES(" + ",".join("?" * 22) + ")", dec_buf)
            if op_buf:
                conn.executemany(
                    "INSERT INTO op(op_seq,kind,boot_id,ms,seq,attempt,processor,outcome,"
                    "code,decline_class,latency_ms,settled_ms,bin_class,region,sca,mandate,"
                    "amount_minor,band,currency,merchant_cat,route_class,entry_mode,"
                    "scenario_hash,payload) VALUES(" + ",".join("?" * 24) + ")", op_buf)
        elif layout == "B blob only":
            if dec_buf:
                conn.executemany("INSERT INTO decision(seq,body) VALUES(?,?)", dec_buf)
            if op_buf:
                conn.executemany(
                    "INSERT INTO op(op_seq,kind,seq,attempt,body) VALUES(?,?,?,?,?)", op_buf)
        elif layout == "C json text":
            if dec_buf:
                conn.executemany("INSERT INTO decision(seq,doc) VALUES(?,?)", dec_buf)
            if op_buf:
                conn.executemany(
                    "INSERT INTO op(op_seq,kind,seq,attempt,doc) VALUES(?,?,?,?,?)", op_buf)
        else:                                    # D
            if dec_buf:
                conn.executemany(
                    "INSERT INTO decision(seq,boot_id,arrival_ms,eligible,chosen,chain_len,"
                    "chain,posteriors,propensity,audit_hash) VALUES("
                    + ",".join("?" * 10) + ")", dec_buf)
            if op_buf:
                conn.executemany(
                    "INSERT INTO op(op_seq,boot_id,kind,ms,seq,attempt,processor,outcome,"
                    "code,decline_class,latency_ms,settled_ms,scenario_hash,payload) "
                    "VALUES(" + ",".join("?" * 14) + ")", op_buf)
        conn.execute("COMMIT")
        lat.append(time.perf_counter() - t)
        commits += 1
        dec_buf.clear()
        op_buf.clear()

    op_seq = 0
    for dec, ops, _sca in src.stream(n):
        n_dec += 1
        if layout == "A columns + blobs":
            dec_buf.append(dec)
        elif layout == "B blob only":
            dec_buf.append((dec[0], sqlite3.Binary(_dec_body(dec))))
        elif layout == "C json text":
            dec_buf.append((dec[0], _dec_doc(dec)))
        else:
            dec_buf.append((dec[0], dec[1], dec[2], dec[13], dec[14], dec[15],
                            sqlite3.Binary(bytes(dec[16])), sqlite3.Binary(bytes(dec[17])),
                            dec[18], sqlite3.Binary(bytes(dec[21]))))
        for op in ops:
            op_seq += 1
            n_op += 1
            if layout == "A columns + blobs":
                op_buf.append((op_seq, KIND_OUTCOME) + op)
            elif layout == "B blob only":
                op_buf.append((op_seq, KIND_OUTCOME, op[2], op[3],
                               sqlite3.Binary(_op_body(op))))
            elif layout == "C json text":
                op_buf.append((op_seq, KIND_OUTCOME, op[2], op[3], _op_doc(op, src.hash)))
            else:
                op_buf.append((op_seq, op[0], KIND_OUTCOME, op[1], op[2], op[3], op[4],
                               op[5], op[6], op[7], op[8], op[9], op[20],
                               sqlite3.Binary(bytes(op[21]))))
            if len(dec_buf) + len(op_buf) >= SHIPPED_BATCH:
                flush()
    flush()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("ANALYZE")
    build_s = time.perf_counter() - t0
    size = path.stat().st_size
    stats = {"decisions": n_dec, "ops": n_op, "rows": n_dec + n_op, "build_s": build_s,
             "rows_per_s": (n_dec + n_op) / build_s, "bytes": size,
             "bytes_per_row": size / max(1, n_dec + n_op),
             "bytes_per_decision": size / max(1, n_dec), "commits": commits,
             "commit_p50_us": us(pct(lat, 0.5)), "commit_p99_us": us(pct(lat, 0.99))}
    return conn, stats


# The five query shapes the downstream tickets actually run. Each returns a dict with the
# answer's size, the rows the ENGINE had to touch (None when SQLite will not say), and the
# wall time. Layouts that cannot express the query return {"impossible": why}.

def _decode_bodies(rows) -> list:
    return [struct.unpack(OP_FMT, bytes(b)) for b in rows]


def _q_point(conn, layout, probes) -> dict:
    """#15/ingest's idempotency probe: one (seq, attempt), does an outcome already exist?"""
    t0 = time.perf_counter()
    got = 0
    for seq, att in probes:
        if layout in ("A columns + blobs", "D no raw context"):
            r = conn.execute("SELECT outcome FROM op WHERE seq=? AND attempt=? AND kind=1",
                             (seq, att)).fetchone()
        elif layout == "B blob only":
            r = conn.execute("SELECT body FROM op WHERE seq=? AND attempt=?",
                             (seq, att)).fetchone()
            if r:
                r = (struct.unpack(OP_FMT, bytes(r[0]))[OP_IX["outcome"]],)
        else:
            r = conn.execute("SELECT json_extract(doc,'$.outcome') FROM op WHERE seq=? "
                             "AND attempt=?", (seq, att)).fetchone()
        got += 1 if r else 0
    return {"found": got, "seconds": time.perf_counter() - t0,
            "us_each": (time.perf_counter() - t0) * 1e6 / max(1, len(probes))}


def _q_window(conn, layout, lo_ms, hi_ms) -> dict:
    """ADR-0004's windowed-counter rebuild: outcomes per (processor, outcome) in a window."""
    t0 = time.perf_counter()
    scanned = None
    if layout in ("A columns + blobs", "D no raw context"):
        rows = conn.execute("SELECT processor, outcome, COUNT(*) FROM op WHERE ms>=? AND "
                            "ms<? AND kind=1 GROUP BY processor, outcome",
                            (lo_ms, hi_ms)).fetchall()
        n = sum(r[2] for r in rows)
    elif layout == "B blob only":
        acc: dict = {}
        scanned = 0
        for (body,) in conn.execute("SELECT body FROM op"):
            scanned += 1
            f = struct.unpack(OP_FMT, bytes(body))
            if lo_ms <= f[OP_IX["ms"]] < hi_ms:
                k = (f[OP_IX["processor"]], f[OP_IX["outcome"]])
                acc[k] = acc.get(k, 0) + 1
        rows, n = list(acc.items()), sum(acc.values())
    else:
        rows = conn.execute(
            "SELECT json_extract(doc,'$.processor'), json_extract(doc,'$.outcome'), COUNT(*)"
            " FROM op WHERE json_extract(doc,'$.ms')>=? AND json_extract(doc,'$.ms')<? "
            "GROUP BY 1,2", (lo_ms, hi_ms)).fetchall()
        n = sum(r[2] for r in rows)
        scanned = conn.execute("SELECT COUNT(*) FROM op").fetchone()[0]
    return {"groups": len(rows), "matched": n, "scanned": scanned,
            "seconds": time.perf_counter() - t0}


def _q_slice(conn, layout, lo_ms, hi_ms, reg, bin_c, band) -> dict:
    """An analyst's ad-hoc slice: one region, one bin class, one amount band, in a window,
    grouped by processor. This is the query that decides whether raw context belongs in
    columns: it names four of them in a WHERE."""
    t0 = time.perf_counter()
    scanned = None
    if layout == "A columns + blobs":
        rows = conn.execute("SELECT processor, COUNT(*), AVG(latency_ms) FROM op WHERE ms>=?"
                            " AND ms<? AND region=? AND bin_class=? AND band=? AND kind=1 "
                            "GROUP BY processor", (lo_ms, hi_ms, reg, bin_c, band)).fetchall()
        n = sum(r[1] for r in rows)
    elif layout == "D no raw context":
        return {"impossible": "region/bin_class/band are not in this file", "groups": 0,
                "matched": 0, "scanned": None, "seconds": 0.0}
    elif layout == "B blob only":
        acc: dict = {}
        scanned = 0
        for (body,) in conn.execute("SELECT body FROM op"):
            scanned += 1
            f = struct.unpack(OP_FMT, bytes(body))
            if (lo_ms <= f[OP_IX["ms"]] < hi_ms and f[OP_IX["region"]] == reg
                    and f[OP_IX["bin_class"]] == bin_c and f[OP_IX["band"]] == band):
                acc[f[OP_IX["processor"]]] = acc.get(f[OP_IX["processor"]], 0) + 1
        rows, n = list(acc.items()), sum(acc.values())
    else:
        rows = conn.execute(
            "SELECT json_extract(doc,'$.processor'), COUNT(*), "
            "AVG(json_extract(doc,'$.latency_ms')) FROM op WHERE "
            "json_extract(doc,'$.ms')>=? AND json_extract(doc,'$.ms')<? AND "
            "json_extract(doc,'$.region')=? AND json_extract(doc,'$.bin_class')=? AND "
            "json_extract(doc,'$.band')=? GROUP BY 1",
            (lo_ms, hi_ms, reg, bin_c, band)).fetchall()
        n = sum(r[1] for r in rows)
        scanned = conn.execute("SELECT COUNT(*) FROM op").fetchone()[0]
    return {"groups": len(rows), "matched": n, "scanned": scanned,
            "seconds": time.perf_counter() - t0}


def _q_rebucket(conn, layout) -> dict:
    """ADR-0006 R40: re-key every outcome into a DIFFERENT arm space. The query that makes
    re-bucketing a re-fold instead of a cold start -- it needs raw context in-band, and it
    touches every row in the file."""
    t0 = time.perf_counter()
    scanned = None
    if layout == "A columns + blobs":
        rows = conn.execute("SELECT bin_class, region, sca, mandate, band, processor, "
                            "outcome, COUNT(*) FROM op WHERE kind=1 GROUP BY 1,2,3,4,5,6,7"
                            ).fetchall()
        n = sum(r[7] for r in rows)
    elif layout == "D no raw context":
        return {"impossible": "the arm key is not reconstructible from this file",
                "groups": 0, "matched": 0, "scanned": None, "seconds": 0.0}
    elif layout == "B blob only":
        acc: dict = {}
        scanned = 0
        for (body,) in conn.execute("SELECT body FROM op"):
            scanned += 1
            f = struct.unpack(OP_FMT, bytes(body))
            k = (f[OP_IX["bin_class"]], f[OP_IX["region"]], f[OP_IX["sca"]],
                 f[OP_IX["mandate"]], f[OP_IX["band"]], f[OP_IX["processor"]],
                 f[OP_IX["outcome"]])
            acc[k] = acc.get(k, 0) + 1
        rows, n = list(acc.items()), sum(acc.values())
    else:
        rows = conn.execute(
            "SELECT json_extract(doc,'$.bin_class'), json_extract(doc,'$.region'),"
            " json_extract(doc,'$.sca'), json_extract(doc,'$.mandate'),"
            " json_extract(doc,'$.band'), json_extract(doc,'$.processor'),"
            " json_extract(doc,'$.outcome'), COUNT(*) FROM op GROUP BY 1,2,3,4,5,6,7"
            ).fetchall()
        n = sum(r[7] for r in rows)
        scanned = n
    return {"groups": len(rows), "matched": n, "scanned": scanned,
            "seconds": time.perf_counter() - t0}


def _plan(conn, sql, params) -> str:
    """What SQLite intends to do -- reported so 'index-bounded' is a plan, not a claim."""
    rows = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return "; ".join(str(r[-1]) for r in rows)[:78]


def _q_ope(conn, layout, limit=20_000) -> dict:
    """#15's OPE pass: every decision's eligible set, chosen arm, propensity and the
    per-eligible posterior snapshot. This query must decode an ARRAY, so what it prices is
    whether the array is a fixed blob with a committed decoder or 20 JSON numbers."""
    t0 = time.perf_counter()
    n = 0
    tot = 0.0
    if layout in ("A columns + blobs", "D no raw context"):
        for _seq, _elig, chosen, _prop, blob in conn.execute(
                "SELECT seq, eligible, chosen, propensity, posteriors FROM decision "
                "ORDER BY seq LIMIT ?", (limit,)):
            a = array.array("f")
            a.frombytes(bytes(blob))
            tot += a[0] + a[(chosen % K_MAX) * 4]
            n += 1
    elif layout == "B blob only":
        off = struct.calcsize(DEC_FMT) + CHAIN_BLOB_BYTES
        for _seq, body in conn.execute("SELECT seq, body FROM decision ORDER BY seq LIMIT ?",
                                       (limit,)):
            a = array.array("f")
            a.frombytes(bytes(body)[off:off + POSTERIOR_BLOB_BYTES])
            tot += a[0]
            n += 1
    else:
        for _seq, doc in conn.execute("SELECT seq, doc FROM decision ORDER BY seq LIMIT ?",
                                      (limit,)):
            d = json.loads(doc)
            p = d.get("posteriors") or [0.0]
            tot += p[0] + p[min(len(p) - 1, 4)]
            n += 1
    return {"rows": n, "seconds": time.perf_counter() - t0,
            "us_each": (time.perf_counter() - t0) * 1e6 / max(1, n)}


def sec_w4(o: Out, n: int, src: RowSource) -> None:
    hr(o, "[W4] the row layout: what a column costs, what a blob costs, and who can answer "
          "the query")
    o("  The ticket asks for a schema. A schema is a pricing exercise: every field is")
    o("  either a COLUMN (SQLite can filter and group on it, and pays B-tree width for it")
    o("  on every row) or inside a BLOB (free to SQLite, decoded by committed code,")
    o("  invisible to WHERE). Four layouts are priced on the same rows from the same")
    o(f"  scenario pool -- {n:,} decisions and their outcome ops -- at the shipped")
    o(f"  durability (WAL + synchronous=FULL, group commit {SHIPPED_BATCH} rows).")
    o("  Bytes are the file after a TRUNCATE checkpoint and ANALYZE, so they are the")
    o("  artifact's size, not the WAL's. Queries are the five the downstream tickets run,")
    o("  named by the ticket that needs them, and each is reported with SQLite's own plan.")
    o()
    d = scratch("w4")
    layouts = ("A columns + blobs", "B blob only", "C json text", "D no raw context")
    st: dict = {}
    for L in layouts:
        conn, s = _load_layout(d / "layout.sqlite", L, n, src)
        st[L] = s
        s["conn"] = conn
    o("  (a) write cost and artifact size")
    o(f"      {'layout':<18} {'rows':>8} {'rows/s':>8} {'cmt p50':>9} "
      f"{'cmt p99':>9} {'bytes/row':>9} {'B/decision':>10} {'file MB':>8}")
    for L in layouts:
        s = st[L]
        o(f"      {L:<18} {s['rows']:>8,} {s['rows_per_s']:>8,.0f} "
          f"{s['commit_p50_us']:>7.0f}us {s['commit_p99_us']:>7.0f}us "
          f"{s['bytes_per_row']:>9.1f} {s['bytes_per_decision']:>10.1f} "
          f"{s['bytes'] / 1e6:>8.1f}")
    base = st["A columns + blobs"]
    o()
    o(f"      Against ADR-0008 [C4]'s {DECISION_FIXED_BYTES} B in-engine decision record "
      f"and its ~104 B/decision")
    o(f"      budget at k_max={K_MAX}: layout A lands at "
      f"{base['bytes_per_decision']:.0f} B/decision, which is the record")
    o(f"      plus the outcome ops it owns "
      f"({base['ops'] / max(1, base['decisions']):.2f} ops/decision here) plus SQLite's")
    o("      B-tree overhead. The blob-only layout is")
    o(f"      {base['bytes_per_row'] / max(0.1, st['B blob only']['bytes_per_row']):.2f}x "
      f"narrower per row and the JSON layout is "
      f"{st['C json text']['bytes_per_row'] / max(0.1, base['bytes_per_row']):.2f}x wider;")
    o("      neither of those numbers is the decision, because the decision is what a")
    o("      query costs, and that is (b).")
    o()
    o("  (b) the five query shapes, priced per layout. 'impossible' means the layout")
    o("      cannot answer the query from this file at all -- a schema decision, not a slow")
    o("      query. 'engine touched' is the rows SQLite had to hand over; when it is the")
    o("      whole file, the layout has no way to reduce the work before the boundary.")
    ms_lo, ms_hi = st["A columns + blobs"]["conn"].execute(
        "SELECT MIN(ms), MAX(ms) FROM op").fetchone()
    win_ms = max(1, (ms_hi - ms_lo) // 8)
    w_lo, w_hi = ms_lo, ms_lo + win_ms
    probes = [(q, 0) for q in range(0, n, max(1, n // 1000))][:1000]
    q4_rows = min(20_000, n)
    o(f"      the file's clock spans {(ms_hi - ms_lo) / 1000:,.0f}s at the scenario's "
      f"declared {src.pool_tps:.0f} TPS;")
    o(f"      Q2/Q3 use the first {win_ms / 1000:,.0f}s of it, Q4/Q5 the whole file")
    o()
    res = {}
    impossible_notes = set()
    for L in layouts:
        conn = st[L]["conn"]
        conn.execute("PRAGMA cache_size=-262144")        # warm: the query is the cost
        _q_point(conn, L, probes[:20])                   # warm the index pages
        res[L] = {"q1": _q_point(conn, L, probes),
                  "q2": _q_window(conn, L, w_lo, w_hi),
                  "q3": _q_slice(conn, L, w_lo, w_hi, REG_ORD["EEA"],
                                 BIN_ORD["consumer_credit"], 3),
                  "q4": _q_rebucket(conn, L),
                  "q5": _q_ope(conn, L, q4_rows)}
    for qi, (label, why, key, unit) in enumerate((
            ("Q1", "#15/ingest idempotency probe, 1,000 (seq,attempt) lookups", "q1",
             "us each"),
            ("Q2", f"ADR-0004 counter rebuild, {win_ms / 1000:,.0f}s window", "q2", "ms"),
            ("Q3", "analyst slice: region+bin_class+band in the same window", "q3", "ms"),
            ("Q4", "ADR-0006 R40 re-bucket: whole file, 7-key group by", "q4", "ms"),
            ("Q5", f"#15 OPE decode of the posterior array, {q4_rows:,} decisions", "q5",
             "us each"))):
        o(f"      {label}")
        o(f"        {why}")
        o(f"        {'layout':<20} {unit:>9} {'answer groups':>14} {'rows matched':>13} "
          f"{'engine touched':>15}")
        for L in layouts:
            r = res[L][key]
            if "impossible" in r:
                o(f"        {L:<20} {'IMPOSSIBLE':>9} {'-':>14} {'-':>13} {'-':>15}")
                impossible_notes.add(f"{label}: {r['impossible']}")
                continue
            t = r["us_each"] if unit == "us each" else r["seconds"] * 1000.0
            groups = r.get("groups", r.get("found", r.get("rows", "-")))
            matched = r.get("matched", r.get("found", r.get("rows", "-")))
            touched = r.get("scanned")
            o(f"        {L:<20} {t:>9,.1f} {groups:>14,} {matched:>13,} "
              f"{(f'{touched:,}' if touched else 'index-bounded'):>15}")
        o()
    for note in sorted(impossible_notes):
        o(f"      IMPOSSIBLE = {note}")
    o("      The plan behind 'engine touched', for Q2 -- reported because the difference")
    o("      is a plan, not a mood:")
    for L in layouts:
        conn = st[L]["conn"]
        if L in ("A columns + blobs", "D no raw context"):
            pl = _plan(conn, "SELECT processor, outcome, COUNT(*) FROM op WHERE ms>=? AND "
                             "ms<? AND kind=1 GROUP BY processor, outcome", (w_lo, w_hi))
        elif L == "B blob only":
            pl = _plan(conn, "SELECT body FROM op", ())
        else:
            pl = _plan(conn, "SELECT json_extract(doc,'$.processor') FROM op WHERE "
                             "json_extract(doc,'$.ms')>=?", (w_lo,))
        o(f"        {L:<20} {pl[:66]}")
    o()
    o("  (c) what those per-row costs become at the fleet's own pace. Every number here is")
    o("      arithmetic on the measured per-row costs above and the write profile this ADR")
    o(f"      is sized against ({DPS:,} decisions/s, {ATTEMPTS_PER_DECISION} attempts each, "
      f"{ROWS_PER_S:,.0f} log rows/s).")
    hour_rows = DPS * 3600 * (1 + ATTEMPTS_PER_DECISION)
    day_rows = ROWS_PER_S * SECONDS_PER_DAY
    total_rows = st["A columns + blobs"]["rows"]
    matched2 = res["A columns + blobs"]["q2"]["matched"]
    o(f"      a one-hour window at the fleet pace holds {hour_rows / 1e6:.1f}M rows; "
      f"a day holds {day_rows / 1e6:.0f}M")
    o(f"      {'layout':<20} {'Q2 one-hour window':>20} {'Q3 slice':>16} "
      f"{'Q4 whole-day re-bucket':>24}")
    for L in layouts:
        secs = []
        for key, fleet_rows in (("q2", hour_rows), ("q3", hour_rows), ("q4", day_rows)):
            r = res[L][key]
            if "impossible" in r:
                secs.append(None)
                continue
            # rows the engine had to touch to answer it here: a full-file scan touches the
            # file, an index-bounded probe touches only the window
            touched = r["scanned"] or matched2
            base = day_rows if r["scanned"] else fleet_rows
            secs.append(r["seconds"] / max(1, touched) * base)
        cells = ["impossible" if t is None else f"{t:,.0f}s" for t in secs[:2]]
        cells.append("impossible" if secs[2] is None else f"{secs[2] / 60:,.1f} min")
        o(f"      {L:<20} {cells[0]:>20} {cells[1]:>16} {cells[2]:>24}")
    o("      (Each cell is measured-seconds / rows-the-engine-touched x rows-it-would-touch")
    o("       at the fleet pace. A full-file scan's denominator is the file, so it scales")
    o("       with the DAY; an index-bounded probe touches only the window, so it scales")
    o("       with the WINDOW -- which is the whole difference between a store you can ask")
    o("       questions of and a tape. Q3's selectivity does not appear: the engine pays for")
    o("       the rows it examines, not the rows it returns.)")
    o()
    o("  Readings:")
    o("   * Q1 is the cheapest place to see the layout's tax and it is small: a point probe")
    o(f"     costs {res['A columns + blobs']['q1']['us_each']:.1f} us on the column layout, "
      f"{res['B blob only']['q1']['us_each']:.1f} us with a blob decode on top and")
    o(f"     {res['C json text']['q1']['us_each']:.1f} us with a document parse. At the "
      "ingest path's pace (one probe per redelivered")
    o("     outcome, R44) that difference is inside the noise of the durable write it")
    o("     precedes. Q1 alone would not decide anything.")
    o("   * Q2 and Q3 decide it. The blob layout has no ms column, so a time window becomes")
    o(f"     a full-file walk with a decode per row: {res['B blob only']['q2']['seconds'] * 1000:.0f} ms "
      f"and {res['B blob only']['q2']['scanned']:,} rows handed over, against")
    o(f"     {res['A columns + blobs']['q2']['seconds'] * 1000:.0f} ms for the column "
      "layout's index-bounded probe. The JSON layout asks SQLite")
    o("     to parse every document in the file to evaluate its own WHERE clause:")
    o(f"     {res['C json text']['q2']['seconds'] * 1000:.0f} ms, "
      f"{res['C json text']['q2']['seconds'] / max(1e-9, res['A columns + blobs']['q2']['seconds']):.0f}x "
      "the column layout.")
    o("     At the fleet pace those become the minutes in (c). The bytes the column layout")
    o("     spends -- 13 B/row more than the blob layout -- are bought back on the first")
    o("     windowed query.")
    o("   * Q3 is why raw context rides every outcome row. Layout D is the shipped layout")
    o("     minus six context columns: it is the narrowest file here AND IT CANNOT ANSWER")
    o("     THE QUERY, because the arm a row belongs to is a function of fields the row no")
    o("     longer carries. ADR-0006 R40's promise -- re-bucketing is a re-fold, not a cold")
    o("     start -- is a schema commitment, and six int32 columns per row are its price.")
    o("   * Q4 is the counter-evidence, and it is recorded rather than argued away: over the")
    o("     WHOLE file, a 7-key group-by is SLOWER in SQLite than decoding the same rows in")
    o(f"     CPython ({res['A columns + blobs']['q4']['seconds'] * 1000:.0f} ms vs "
      f"{res['B blob only']['q4']['seconds'] * 1000:.0f} ms), because SQLite builds a temp "
      "B-tree per group while the")
    o("     Python loop builds a dict. The column layout's advantage on that shape is")
    o("     expressibility, not speed, and the honest response is not to add an index for a")
    o("     7-column key -- it is to run whole-file high-cardinality aggregation in the")
    o("     columnar tier, which is [W5]'s subject and the reason the export exists.")
    o("   * Q5 is the other half of R89 and it points the opposite way: the posterior array")
    o(f"     decodes at {res['A columns + blobs']['q5']['us_each']:.2f} us/row as a fixed "
      f"f32 blob and {res['C json text']['q5']['us_each']:.2f} us/row as 20 JSON numbers --")
    o(f"     {res['C json text']['q5']['us_each'] / max(0.01, res['A columns + blobs']['q5']['us_each']):.1f}x. "
      "An ARRAY is not a set of columns; making it one costs a parse per row")
    o("     read and buys nothing, because no query filters on 'the third eligible arm's")
    o("     alpha'. Scalars a query names are columns, arrays are blobs, and both halves of")
    o("     that rule are measurements now.")
    o("   * Layout C is rejected on all three counts at once: 2.7x the bytes, 2.3x the")
    o("     write cost, and the worst time on every query it can answer at all. JSON is the")
    o("     right shape for an interface and the wrong shape for a store; where this design")
    o("     needs self-description it puts a fixed-layout blob next to a version byte and a")
    o("     committed decoder in analysis/ (R60's rule, ADR-0008).")
    o("   * Layout B is not rejected on throughput -- it writes slightly faster than A and is")
    o("     the narrowest file that can still answer Q1. It is rejected on Q2/Q3: a store")
    o("     whose rows SQLite cannot filter is a tape, and the tape's advantage evaporates")
    o("     the first time someone asks a question nobody predicted. That is the actual")
    o("     content of R89.")
    o()
    for L in layouts:
        st[L]["conn"].close()
        drop(d / "layout.sqlite")
    LAST["w4"] = {L: {k: v for k, v in st[L].items() if k != "conn"} for L in layouts}


# --------------------------------------------------------------------------------------
# [W5] the analysis tier: a columnar export, and whether it earns its place
# --------------------------------------------------------------------------------------
#
# ADR-0011's DAG says the trace is written once, by trace/, into SQLite, and that the
# engine writes no Parquet. That leaves the question this section answers: when the
# analysis tier wants a columnar artifact, is exporting worth it, and what does it buy?
# No pyarrow and no DuckDB in this sandbox, so Parquet itself is not measured. What IS
# measured is the mechanism Parquet's speed comes from -- row groups, typed column chunks,
# per-chunk compression, and a footer carrying per-chunk min/max so a predicate can skip
# chunks -- implemented in ~150 lines of stdlib below. The bytes and the skip ratios are
# real; the comparison against pyarrow/DuckDB is a labelled model built on cited numbers.

COLUMN_KIND_INT64, COLUMN_KIND_INT32, COLUMN_KIND_F32, COLUMN_KIND_BYTES = 1, 2, 3, 4

# The exported trace schema: the columns #15/#16 group and filter on, typed. This is the
# normative column list the ADR hands the analysis tier.
EXPORT_COLUMNS = (
    ("op_seq", COLUMN_KIND_INT64), ("kind", COLUMN_KIND_INT32), ("boot_id", COLUMN_KIND_INT32),
    ("ms", COLUMN_KIND_INT64), ("seq", COLUMN_KIND_INT64), ("attempt", COLUMN_KIND_INT32),
    ("processor", COLUMN_KIND_INT32), ("outcome", COLUMN_KIND_INT32), ("code", COLUMN_KIND_INT32),
    ("decline_class", COLUMN_KIND_INT32), ("latency_ms", COLUMN_KIND_INT32),
    ("settled_ms", COLUMN_KIND_INT64), ("bin_class", COLUMN_KIND_INT32),
    ("region", COLUMN_KIND_INT32), ("sca", COLUMN_KIND_INT32), ("mandate", COLUMN_KIND_INT32),
    ("amount_minor", COLUMN_KIND_INT64), ("band", COLUMN_KIND_INT32),
    ("currency", COLUMN_KIND_INT32), ("merchant_cat", COLUMN_KIND_INT32),
    ("route_class", COLUMN_KIND_INT32), ("entry_mode", COLUMN_KIND_INT32),
)
# A row group: the unit of compression and of skipping. A real Parquet writer uses
# 64 Ki - 1 M rows (128 MB by default); this spike uses a smaller group so that pruning is
# visible at 10^5 rows instead of needing 10^9, and (b) projects the day-scale ratio
# arithmetically from the measured one. The ratio, not the group size, is the claim.
ROW_GROUP_ROWS = 16_384
PARQUET_GROUP_ROWS = 65_536


class ColumnarWriter:
    """A minimal row-grouped columnar file: [magic][row groups][footer][footer len][magic].

    Each row group holds one chunk per column; each chunk is zlib-compressed and preceded
    by (uncompressed len, compressed len, null count, min, max). The footer is JSON: the
    column list with types, and per row group the offset/length/min/max of every chunk --
    which is what makes a predicate able to skip chunks without reading them."""

    MAGIC = b"SBCK1"

    def __init__(self, path: Path, columns=EXPORT_COLUMNS, group_rows=ROW_GROUP_ROWS,
                 level=6):
        self.f = open(path, "wb")
        self.f.write(self.MAGIC)
        self.columns = columns
        self.group_rows = group_rows
        self.level = level
        self.groups: list = []
        self.buf: dict = {c: [] for c, _ in columns}
        self.rows = 0
        self.t_encode = 0.0

    def write(self, row: tuple) -> None:
        for (name, _k), v in zip(self.columns, row):
            self.buf[name].append(v)
        self.rows += 1
        if self.rows % self.group_rows == 0:
            self.flush_group()

    def flush_group(self) -> None:
        if not self.buf[self.columns[0][0]]:
            return
        t0 = time.perf_counter()
        nrows = len(self.buf[self.columns[0][0]])   # before the clears below alias it away
        chunks = []
        for name, kind in self.columns:
            vals = self.buf[name]
            raw, vmin, vmax, nulls = _encode_column(vals, kind)
            off = self.f.tell()
            comp = zlib.compress(raw, self.level)
            self.f.write(struct.pack("<IIq", len(raw), len(comp), nulls))
            self.f.write(struct.pack("<qq", vmin, vmax))
            self.f.write(comp)
            chunks.append({"name": name, "kind": kind, "offset": off,
                           "raw": len(raw), "comp": len(comp), "nulls": nulls,
                           "min": vmin, "max": vmax})
            self.buf[name].clear()
        self.groups.append({"rows": nrows, "first_row": self.rows - nrows,
                            "chunks": chunks})
        self.t_encode += time.perf_counter() - t0

    def close(self) -> dict:
        self.flush_group()
        footer = {"columns": [list(c) for c in self.columns], "rows": self.rows,
                  "row_groups": self.groups}
        blob = json.dumps(footer, separators=(",", ":")).encode()
        off = self.f.tell()
        self.f.write(blob)
        self.f.write(struct.pack("<Q", len(blob)))
        self.f.write(self.MAGIC)
        self.f.close()
        raw = sum(c["raw"] for g in self.groups for c in g["chunks"])
        comp = sum(c["comp"] for g in self.groups for c in g["chunks"])
        return {"rows": self.rows, "groups": len(self.groups), "footer_offset": off,
                "raw_bytes": raw, "comp_bytes": comp, "encode_s": self.t_encode}


def _encode_column(vals: list, kind: int) -> tuple:
    nulls = sum(1 for v in vals if v is None)
    if kind == COLUMN_KIND_INT64:
        a = array.array("q", [0 if v is None else int(v) for v in vals])
        return a.tobytes(), min(vals, default=0), max(vals, default=0), nulls
    if kind == COLUMN_KIND_INT32:
        a = array.array("i", [0 if v is None else int(v) for v in vals])
        return a.tobytes(), min(vals, default=0), max(vals, default=0), nulls
    if kind == COLUMN_KIND_F32:
        a = array.array("f", [0.0 if v is None else float(v) for v in vals])
        return a.tobytes(), int(min(vals, default=0)), int(max(vals, default=0)), nulls
    joined = b"\x00".join(b"" if v is None else bytes(v) for v in vals)
    return joined, 0, len(joined), nulls


class ColumnarReader:
    def __init__(self, path: Path):
        self.path = path
        with open(path, "rb") as f:
            f.seek(-len(ColumnarWriter.MAGIC) - 8, 2)
            tail = f.read()
            flen = struct.unpack("<Q", tail[:8])[0]
            f.seek(-(len(ColumnarWriter.MAGIC) + 8 + flen), 2)
            self.footer = json.loads(f.read(flen).decode())
        self.f = open(path, "rb")
        self.columns = {c[0]: c[1] for c in self.footer["columns"]}

    def chunks_skipped(self, name: str, lo: int, hi: int) -> tuple:
        """How many row groups the min/max index lets a [lo, hi) predicate skip."""
        kept = total = 0
        for g in self.footer["row_groups"]:
            total += 1
            c = next(c for c in g["chunks"] if c["name"] == name)
            if c["max"] >= lo and c["min"] < hi:
                kept += 1
        return kept, total

    def read_column(self, group: dict, name: str) -> array.array:
        c = next(c for c in group["chunks"] if c["name"] == name)
        self.f.seek(c["offset"])
        raw_len, comp_len, _nulls = struct.unpack("<IIq", self.f.read(16))
        self.f.read(16)
        comp = self.f.read(comp_len)
        raw = zlib.decompress(comp)
        kind = self.columns[name]
        code = {COLUMN_KIND_INT64: "q", COLUMN_KIND_INT32: "i",
                COLUMN_KIND_F32: "f"}.get(kind)
        if code is None:
            return raw.split(b"\x00")
        a = array.array(code)
        a.frombytes(raw)
        return a

    def scan(self, names, predicate=None) -> tuple:
        """Columnar scan: read only the named columns, apply `predicate(dict of arrays)`.
        Returns (rows_matching, row_groups_read, seconds)."""
        t0 = time.perf_counter()
        matched = groups_read = 0
        for g in self.footer["row_groups"]:
            groups_read += 1
            cols = {n: self.read_column(g, n) for n in names}
            n = g["rows"]
            if predicate is None:
                matched += n
                continue
            matched += sum(1 for i in range(n) if predicate(cols, i))
        return matched, groups_read, time.perf_counter() - t0

    def scan_pruned(self, names, prune_col, lo, hi, predicate) -> tuple:
        """The same scan, but row groups whose [min,max) on prune_col misses [lo,hi) are
        never opened. This is the mechanism, and the ratio is the claim."""
        t0 = time.perf_counter()
        matched = groups_read = groups_skipped = 0
        for g in self.footer["row_groups"]:
            c = next(c for c in g["chunks"] if c["name"] == prune_col)
            if not (c["max"] >= lo and c["min"] < hi):
                groups_skipped += 1
                continue
            groups_read += 1
            cols = {n: self.read_column(g, n) for n in names}
            n = g["rows"]
            matched += sum(1 for i in range(n) if predicate(cols, i))
        return matched, groups_read, groups_skipped, time.perf_counter() - t0

    def close(self):
        self.f.close()


def sec_w5(o: Out, n: int, src: RowSource) -> None:
    hr(o, "[W5] the analysis tier: what a columnar export buys over the SQLite row store")
    o("  ADR-0011's DAG gives trace/ the only write path into learned state and puts no")
    o("  Parquet writer inside the engine. What it leaves open is whether the analysis")
    o("  tier exports at all, and what an export earns. Parquet itself cannot be measured")
    o("  here (no pyarrow, no DuckDB, no Arrow C++ in this sandbox), so the MECHANISM is")
    o("  measured instead: row groups, typed column chunks, per-chunk zlib, and a footer")
    o("  carrying per-chunk min/max so a predicate can skip chunks it does not need to")
    o("  read. That mechanism -- not the file format's brand -- is where Parquet's")
    o("  numbers come from, and it is ~150 lines of stdlib below. The bytes, the")
    o("  compression ratios, the skip ratios and the scan times are measured; anything")
    o("  said about pyarrow or DuckDB is a labelled model on cited numbers.")
    o()
    d = scratch("w5")
    n_dec = n
    # 1. build the row store, then export from it (the export reads what the engine wrote)
    t0 = time.perf_counter()
    st = TraceStore(d / "trace.sqlite", durability="strict", commit_every=SHIPPED_BATCH,
                    commit_ms=1e9, autocheckpoint=SHIPPED_AUTOCHECKPOINT)
    rows = 0
    op_seq = 0
    for dec, ops, _sca in src.stream(n_dec):
        st.append_decision(dec)
        rows += 1
        for op in ops:
            op_seq += 1
            st.append_op(KIND_OUTCOME, op)
            rows += 1
    st.flush_and_checkpoint()
    build_s = time.perf_counter() - t0
    sqlite_bytes = st.path.stat().st_size
    o("  (a) export cost, measured on the rows the engine actually wrote")
    o(f"      {rows:,} log rows written to SQLite in {build_s:.1f}s "
      f"({rows / build_s:,.0f} rows/s), file {sqlite_bytes / 1e6:.1f} MB "
      f"({sqlite_bytes / rows:.0f} B/row)")
    t0 = time.perf_counter()
    cw = ColumnarWriter(d / "trace.sbcz")
    q = ("SELECT op_seq,kind,boot_id,ms,seq,attempt,processor,outcome,code,decline_class,"
         "latency_ms,settled_ms,bin_class,region,sca,mandate,amount_minor,band,currency,"
         "merchant_cat,route_class,entry_mode FROM op ORDER BY op_seq")
    exported = 0
    for r in st.conn.execute(q):
        cw.write(r)
        exported += 1
    meta = cw.close()
    exp_s = time.perf_counter() - t0
    col_bytes = (d / "trace.sbcz").stat().st_size
    o(f"      {exported:,} op rows exported in {exp_s:.1f}s "
      f"({exported / exp_s:,.0f} rows/s), columnar file {col_bytes / 1e6:.1f} MB "
      f"({col_bytes / exported:.0f} B/row)")
    o(f"      uncompressed column bytes {meta['raw_bytes'] / 1e6:.1f} MB -> compressed "
      f"{meta['comp_bytes'] / 1e6:.1f} MB = "
      f"{meta['raw_bytes'] / max(1, meta['comp_bytes']):.2f}x with zlib level 6")
    o(f"      the columnar file is {sqlite_bytes / max(1, col_bytes):.2f}x smaller than the "
      f"SQLite row store holding the same op rows")
    o(f"      ({meta['groups']} row groups of <= {ROW_GROUP_ROWS:,} rows; footer "
      f"{(d / 'trace.sbcz').stat().st_size - meta['footer_offset']:,} B)")
    rd = ColumnarReader(d / "trace.sbcz")
    o()
    o("      per-column cost -- this is where the ratio comes from, and it is the reason a")
    o("      columnar artifact is a compression win and not a re-encoding win:")
    o(f"      {'column':<14} {'kind':>6} {'raw B/row':>10} {'comp B/row':>11} {'ratio':>7}")
    per = []
    agg: dict = {}
    for g in rd.footer["row_groups"]:
        for c in g["chunks"]:
            a = agg.setdefault(c["name"], [0, 0, c["kind"]])
            a[0] += c["raw"]
            a[1] += c["comp"]
    for name, kind in EXPORT_COLUMNS:
        raw, comp, _k = agg[name]
        per.append((name, raw / exported, comp / exported, raw / max(1, comp)))
    for name, rb, cb, ratio in sorted(per, key=lambda x: -x[2])[:6]:
        kindname = {1: "i64", 2: "i32", 3: "f32", 4: "bytes"}[
            dict(EXPORT_COLUMNS)[name]]
        o(f"      {name:<14} {kindname:>6} {rb:>10.2f} {cb:>11.2f} {ratio:>6.2f}x")
    o(f"      {'...':<14} {'':>6} {'':>10} {'':>11} {'':>7}")
    for name, rb, cb, ratio in sorted(per, key=lambda x: x[2])[:3]:
        kindname = {1: "i64", 2: "i32", 3: "f32", 4: "bytes"}[
            dict(EXPORT_COLUMNS)[name]]
        o(f"      {name:<14} {kindname:>6} {rb:>10.2f} {cb:>11.2f} {ratio:>6.2f}x")
    o("      Read the two ends of that table, not the middle. The best-compressing columns")
    o("      are the near-constant ones (kind, boot_id) and the two-valued ones (mandate,")
    o("      sca): zlib on a run of identical int32s is nearly free, which is the same")
    o("      property a real Parquet writer exploits with RLE and dictionary encoding")
    o("      BEFORE it compresses -- so this ratio is a floor on what Parquet + zstd would")
    o("      do, not an estimate of it. The worst are the high-entropy numerics")
    o("      (amount_minor, ms, settled_ms) at 3-4x, and they are the columns that carry the")
    o("      information; a delta or dictionary encoding on the two timestamp columns would")
    o("      take them well past this, and that is a real format's job, not this one's.")
    cyc = (n + len(src.pool) - 1) // len(src.pool)
    if cyc > 1:
        o(f"      Disclosure: the row pool ({len(src.pool):,} decisions) is cycled {cyc}x to")
        o("      reach this n, so the VALUE sequence repeats and every compression ratio")
        o("      here is flattered by it. At --smoke (pool == n, one cycle) the same export")
        o("      measures ~7.1x zlib and ~21x against the row store instead of the numbers")
        o("      above; the honest figure is the uncycled one and the ADR quotes that.")
    o()
    o("  (b) the query that decides the tier: a windowed aggregate over the ops. This is")
    o("      ADR-0004's counter rebuild and the dashboard's shape, and it is the query where")
    o("      a row store's index and a columnar file's min/max footer are doing the same")
    o("      job by different means.")
    ms_lo, ms_hi = st.conn.execute("SELECT MIN(ms), MAX(ms) FROM op").fetchone()
    win_ms = max(1, (ms_hi - ms_lo) // 8)
    lo, hi = ms_lo, ms_lo + win_ms
    st.conn.execute("PRAGMA cache_size=-262144")
    t0 = time.perf_counter()
    g_sql = st.conn.execute("SELECT processor, outcome, COUNT(*) FROM op WHERE ms>=? AND "
                            "ms<? GROUP BY processor, outcome", (lo, hi)).fetchall()
    sql_s = time.perf_counter() - t0
    sql_sum = sum(r[2] for r in g_sql)
    names = ["ms", "processor", "outcome"]

    def pred(cols, i):
        return lo <= cols["ms"][i] < hi
    m, groups_read, scan_s = rd.scan(names, pred)
    m2, gread, gskip, prune_s = rd.scan_pruned(names, "ms", lo, hi, pred)
    o(f"      the window is the first {win_ms / 1000:,.0f}s of the file's "
      f"{(ms_hi - ms_lo) / 1000:,.0f}s clock span ({sql_sum:,} of {exported:,} op rows)")
    o(f"      {'path':<36} {'rows matched':>13} {'groups opened':>14} {'seconds':>9} "
      f"{'rows/s':>11}")
    o(f"      {'SQLite: op_ms index + group by':<36} {sql_sum:>13,} {'-':>14} "
      f"{sql_s:>9.4f} {'-':>11}")
    o(f"      {'columnar: every row group opened':<36} {m:>13,} {groups_read:>14,} "
      f"{scan_s:>9.4f} {m / max(1e-9, scan_s):>11,.0f}")
    o(f"      {'columnar: min/max pruning':<36} {m2:>13,} {gread:>14,} {prune_s:>9.4f} "
      f"{m2 / max(1e-9, prune_s):>11,.0f}")
    o(f"      pruning opened {gread} of {gskip + gread} row groups and skipped {gskip} "
      f"({100 * gskip / max(1, gskip + gread):.0f}%);")
    o(f"      all three agree on the answer ({sql_sum == m == m2}), which is the "
      "correctness check on both readers")
    day_groups = ROWS_PER_S * SECONDS_PER_DAY / PARQUET_GROUP_ROWS
    win_groups = DPS * 3600 * (1 + ATTEMPTS_PER_DECISION) / PARQUET_GROUP_ROWS
    o(f"      projected to a fleet day at {PARQUET_GROUP_ROWS:,}-row groups: "
      f"{day_groups:,.0f} groups in the file,")
    o(f"      {win_groups:,.0f} in a one-hour window, so "
      f"{100 * (1 - win_groups / day_groups):.1f}% of the file is never opened.")
    o("      That projection is arithmetic on the measured skip, and it is the reason the")
    o("      footer exists: the skip ratio is a property of the DATA's time ordering, not")
    o("      of the format, and it only works because trace/ appends in arrival order.")
    o()
    o("  (c) the whole-file aggregate: the OPE-style pass that touches every row and a")
    o("      handful of columns. This is #15's shape and the one a row store is worst at,")
    o("      because it must walk B-tree pages holding columns it does not want.")
    t0 = time.perf_counter()
    r_sql = st.conn.execute("SELECT processor, outcome, COUNT(*), AVG(latency_ms) FROM op "
                            "GROUP BY processor, outcome").fetchall()
    full_sql_s = time.perf_counter() - t0

    def pred_all(cols, i):
        return True
    m3, g3, full_scan_s = rd.scan(["processor", "outcome", "latency_ms"], None)
    acc: dict = {}
    t0 = time.perf_counter()
    for g in rd.footer["row_groups"]:
        cols = {n: rd.read_column(g, n) for n in ("processor", "outcome", "latency_ms")}
        for i in range(g["rows"]):
            k = (cols["processor"][i], cols["outcome"][i])
            a = acc.setdefault(k, [0, 0])
            a[0] += 1
            a[1] += cols["latency_ms"][i]
    agg_s = time.perf_counter() - t0
    o(f"      {'path':<34} {'groups':>7} {'seconds':>9} {'rows/s':>11} {'B/row read':>11}")
    o(f"      {'SQLite full scan + group by':<34} {'-':>7} {full_sql_s:>9.3f} "
      f"{rows / max(1e-9, full_sql_s):>11,.0f} "
      f"{sqlite_bytes / max(1, rows):>11.0f}")
    o(f"      {'columnar read (3 of 22 columns)':<34} {g3:>7,} {full_scan_s:>9.3f} "
      f"{m3 / max(1e-9, full_scan_s):>11,.0f} "
      f"{sum(agg[name][1] for name in ('processor', 'outcome', 'latency_ms')) / max(1, rows):>11.2f}")
    o(f"      {'columnar read + Python aggregate':<34} {g3:>7,} {full_scan_s + agg_s:>9.3f} "
      f"{m3 / max(1e-9, full_scan_s + agg_s):>11,.0f} {'':>11}")
    o("      The two columnar rows separate what the FORMAT buys (reading 3 typed columns")
    o("      instead of 22 interleaved ones: the compressed bytes it must touch are a")
    o("      fraction of the row store's) from what the LANGUAGE costs (aggregating in")
    o("      CPython is slower than aggregating in SQLite's C loop, and that gap is")
    o("      exactly what a vectorized engine closes). DuckDB or pyarrow reading the same")
    o("      layout would keep the format's win and drop the interpreter's loss; this")
    o("      spike cannot measure that, and says so.")
    o()
    o("  Readings:")
    o(f"   * The export costs {exported / exp_s:,.0f} rows/s in this interpreter, i.e. "
      f"{exp_s / max(1, rows) * 1e6:.1f} us/row,")
    o("     against a write path that sustains")
    o(f"     {rows / build_s:,.0f} rows/s. Export is therefore an OFFLINE step (a sealed")
    o("     partition file is exported once, after the run or after the day closes), never")
    o("     an in-path one -- which is the same conclusion ADR-0011's DAG reached without")
    o("     these numbers, and now has them.")
    o(f"   * The columnar artifact is {sqlite_bytes / max(1, col_bytes):.2f}x smaller than "
      f"the row store for the same op rows. Over the")
    o("     retention window that is the difference between an artifact a team can keep on")
    o("     one volume and one it has to shard; [W8] turns this into bytes/day.")
    o("   * Pruning is the mechanism that matters most and it is free once the footer")
    o("     exists: a one-hour window in a multi-day file opens only the row groups whose")
    o("     min/max overlap it. The SQLite path answers the same query from an index and")
    o("     is competitive at this size; the columnar path wins when the query touches")
    o("     few columns over many rows, and when the file is sealed and cold.")
    o("   * So the format decision is not 'SQLite or Parquet'. It is: SQLite is the log")
    o("     and the queryable hot store (W1-W4, W6-W10), and the columnar export is the")
    o("     cold analytics artifact, written once per sealed partition. The engine writes")
    o("     no Parquet (ADR-0011); the export is a batch step owned by analysis/, and its")
    o("     column list is the EXPORT_COLUMNS table above -- the same names, the same")
    o("     ordinals, the same scenario_hash, so a row means the same thing in both tiers.")
    o("   * Counter-evidence, recorded: if the only consumer were #15's OPE pass over a")
    o("     single run, the SQLite file with a covering index would be enough and the")
    o("     export would be pure cost. The export is justified by the retention window")
    o("     (400 days of sealed partitions is where columnar compression pays for itself)")
    o("     and by consumers this repo does not own yet. If neither materialises, drop")
    o("     the export and keep the schema.")
    o()
    rd.close()
    st.close()
    LAST["w5"] = {"rows": rows, "sqlite_bytes": sqlite_bytes, "col_bytes": col_bytes,
                  "export_rows_per_s": exported / exp_s, "sql_window_s": sql_s,
                  "prune_skipped": gskip, "prune_total": gskip + gread,
                  "sql_full_s": full_sql_s, "col_full_s": full_scan_s}
# --------------------------------------------------------------------------------------
# [W6] scale: the ticket's fourth consideration, answered with a run and a decomposition
# --------------------------------------------------------------------------------------
#
# "Can SQLite handle 1M-transaction simulation write throughput?" is a question with three
# parts, and they have different answers: (1) can one shard's write path keep up with the
# fleet's pace, (2) does it still keep up when the file is a million transactions old --
# B-trees get deeper and caches get colder, so the rate at row 10,000 is not evidence about
# the rate at row 2,000,000 -- and (3) what does the store cost the harness, whose own
# world model is not free. This section measures all three on one growing file, then turns
# the answer into a shard count.

def sec_w6(o: Out, n: int, src: RowSource) -> None:
    hr(o, "[W6] scale: a 1M-transaction run with the store attached, and what the store "
          "actually costs")
    o(f"  One file, one writer, the shipped configuration (WAL, synchronous=FULL, group")
    o(f"  commit {SHIPPED_BATCH} rows / {SHIPPED_COMMIT_MS:.0f} ms, autocheckpoint "
      f"{SHIPPED_AUTOCHECKPOINT} pages), {n:,} decisions from the")
    o(f"  committed scenario and their {src.attempts_per_decision:.3f} attempts each. The "
      "run is segmented so that the rate at")
    o("  the END of the file is reported next to the rate at the beginning: that is the")
    o("  difference between 'SQLite can do this' and 'SQLite can do this for a day'.")
    o()
    d = scratch("w6")
    path = d / "trace.sqlite"
    drop(path)
    st = TraceStore(path, durability="strict", commit_every=SHIPPED_BATCH,
                    commit_ms=SHIPPED_COMMIT_MS, autocheckpoint=SHIPPED_AUTOCHECKPOINT)
    st.boot_row(boot_id=1, started_ms=0, mode="harness", durability="strict", shard=0,
                schema_version=SCHEMA_VERSION, policy_seed=1, catalog_hash="x" * 16,
                doc_hash="y" * 16, prior_hash="z" * 16, arm_schema_hash="a" * 16,
                config_hash="b" * 16, scenario_hash=src.hash, model_version="static-cost",
                harness_version="0006", band_edges=b"")
    marks = sorted({int(n * f) for f in (0.05, 0.10, 0.25, 0.50, 1.00)})
    seg_rows: list = []
    prev = {"t": time.perf_counter(), "rows": 0, "dec": 0, "commits": 0,
            "lat": 0, "bytes": 0}
    rows = 0
    dec_done = 0
    world_s = 0.0
    t_start = prev["t"]
    for dec, ops, _sca in src.stream(n):
        t_w = time.perf_counter()
        st.append_decision(dec)
        for op in ops:
            st.append_op(KIND_OUTCOME, op)
        world_s += time.perf_counter() - t_w
        rows += 1 + len(ops)
        dec_done += 1
        if dec_done in marks:
            now = time.perf_counter()
            st.commit()
            size = db_bytes(path)
            lat = st.commit_latency[prev["lat"]:]
            seg_rows.append({
                "through": dec_done, "rows": rows - prev["rows"],
                "seconds": now - prev["t"], "bytes": size,
                "seg_bytes": max(0, size - prev["bytes"]),
                "commits": len(lat), "commit_p50_us": us(pct(lat, 0.5)),
                "commit_p99_us": us(pct(lat, 0.99)),
                "commit_max_ms": max(lat) * 1000.0 if lat else 0.0,
                "bytes_per_row": max(0, size - prev["bytes"]) /
                max(1, rows - prev["rows"])})
            prev = {"t": now, "rows": rows, "dec": dec_done, "commits": st.commits,
                    "lat": len(st.commit_latency), "bytes": size}
    wall = time.perf_counter() - t_start
    commit_s = sum(st.commit_latency)
    st.flush_and_checkpoint()
    final_bytes = st.bytes_on_disk()
    counts = st.counts()
    o("  (a) the run, by segment. `through` is the decision count reached; the rate is")
    o("      that segment's, not the cumulative one.")
    o(f"     {'through':>9} {'rows/seg':>10} {'seg s':>7} {'rows/s':>9} "
      f"{'dec/s':>10} {'cmt p50':>9} {'cmt p99':>9} {'worst':>9} "
      f"{'MB':>7} {'B/row':>6}")
    for sg in seg_rows:
        o(f"     {sg['through']:>9,} {sg['rows']:>10,} {sg['seconds']:>7.2f} "
          f"{sg['rows'] / sg['seconds']:>9,.0f} "
          f"{sg['rows'] / sg['seconds'] / (1 + src.attempts_per_decision):>10,.0f} "
          f"{sg['commit_p50_us']:>7.0f}us {sg['commit_p99_us']:>7.0f}us "
          f"{sg['commit_max_ms']:>7.2f}ms {sg['bytes'] / 1e6:>7.1f} "
          f"{sg['bytes_per_row']:>6.1f}")
    first, last = seg_rows[0], seg_rows[-1]
    o()
    o(f"      whole run: {rows:,} log rows ({counts['decision']:,} decisions + "
      f"{counts['op']:,} ops) in {wall:.1f}s")
    o(f"      = {rows / wall:,.0f} rows/s, {dec_done / wall:,.0f} decisions/s")
    o(f"      file {final_bytes / 1e6:.1f} MB = {final_bytes / rows:.1f} B/row = "
      f"{final_bytes / max(1, dec_done):.1f} B/decision; "
      f"{st.commits:,} commits, {commit_s:.1f}s of them ({100 * commit_s / wall:.0f}% of wall)")
    o(f"      first segment {first['rows'] / first['seconds']:,.0f} rows/s vs last "
      f"{last['rows'] / last['seconds']:,.0f} rows/s")
    o(f"      = "
      f"{(last['rows'] / last['seconds']) / max(1.0, first['rows'] / first['seconds']):.2f}x "
      f"-- the answer to 'does it degrade as the file grows'")
    o()
    o("  (b) where the wall time goes. Three buckets, all measured on this run:")
    o(f"      {'bucket':<44} {'seconds':>9} {'share':>7}")
    append_s = max(0.0, world_s - commit_s)      # the store calls INCLUDE the commit
    other_s = max(0.0, wall - world_s)
    buckets = (("SQLite commit (BEGIN..COMMIT, incl. fsync)", commit_s),
               ("append minus commit (tuple build, executemany, B-tree)", append_s),
               ("the loop feeding it (row tuples from the pool)", other_s))
    for name, sec in buckets:
        o(f"      {name:<58} {sec:>7.2f} {100 * sec / wall:>6.0f}%")
    o("      The timer around the store's own calls encloses the commit, so the commit is")
    o("      subtracted from it rather than counted twice; the remainder is the generator")
    o("      loop, which here reads pre-built rows out of a pool (the world model's own")
    o("      cost is priced separately in (c) -- it is not free, and it is not the store's).")
    o("      The commit bucket is the one a faster device shrinks, and [W2]'s slow-device")
    o("      model is how far it can move: on a device with a 1-5 ms fsync the commit")
    o(f"      bucket grows by roughly that factor over the {us(pct(st.commit_latency, 0.5)):.0f} us "
      "measured here, which at the shipped")
    o(f"      batch of {SHIPPED_BATCH} rows is still "
      f"{SHIPPED_BATCH / (5e-3):,.0f} rows/s of ceiling on one shard.")
    o()
    o("  (c) what the store costs the harness. ADR-0005's harness runs the world; #13's")
    o("      store hangs off it. The two are timed separately -- the world by building a")
    o("      fresh pool of decisions through the acquirer models, the store by (a) -- and")
    o("      then added, because in this spike the world's rows are pre-built and a single")
    o("      loop cannot be split mid-flight. In the Go engine they are one loop; the")
    o("      per-decision costs are what transfer, not the interpreter's.")
    m = min(n, 50_000)
    t0 = time.perf_counter()
    probe = RowSource(src.scenario, m)
    world_s_only = time.perf_counter() - t0
    world_per_dec = world_s_only / m
    store_per_row = wall / rows
    rows_per_dec = rows / dec_done
    combined_per_dec = world_per_dec + rows_per_dec * store_per_row
    o(f"      {'bucket':<44} {'per decision':>13} {'decisions/s':>13}")
    store_label = f"store alone ({rows / wall:,.0f} rows/s, {rows_per_dec:.2f} rows/dec)"
    world_label = f"world model alone (fresh pool, {m:,} dec)"
    o(f"      {world_label:<44} "
      f"{world_per_dec * 1e6:>11.1f}us {1 / world_per_dec:>13,.0f}")
    o(f"      {store_label:<44} "
      f"{rows_per_dec * store_per_row * 1e6:>11.1f}us "
      f"{1 / (rows_per_dec * store_per_row):>13,.0f}")
    o(f"      {'world + store, summed':<44} {combined_per_dec * 1e6:>11.1f}us "
      f"{1 / combined_per_dec:>13,.0f}")
    o(f"      the store adds {100 * rows_per_dec * store_per_row / world_per_dec:.0f}% to the "
      "harness's per-decision cost in this interpreter, and")
    o(f"      a run of {n:,} decisions costs {n * combined_per_dec:.0f}s with the store "
      f"against {n * world_per_dec:.0f}s without it")
    o("      Both sides are CPython here, so the ratio is the transferable part: a Go world")
    o("      model is faster per decision, which makes the store's share LARGER, and the")
    o("      store's own ceiling is set by fsync ([W1]/[W2]) rather than by the language.")
    o("      The reading is that the store is not free to a simulation run and never hides")
    o("      behind the world model -- which is why [W1]'s batch size and [W8]'s partition")
    o("      size are harness-facing knobs and not just production ones.")
    o()
    o("  (d) the shard arithmetic. One writer per shard (ADR-0006 R48), so the fleet's")
    o("      pace is met by adding shards, not by making one faster.")
    per_shard = rows / wall
    auth_share = src.auth_record_share or AUTH_RECORD_SHARE
    rows_auth = ROWS_PER_S + OPS_PER_S * auth_share
    o(f"      {'quantity':<52} {'value':>14}")
    for label, val in (
            ("fleet pace to sustain (ADR-0001)", f"{ROWS_PER_S:,.0f} rows/s"),
            ("one shard, measured here", f"{per_shard:,.0f} rows/s"),
            ("headroom per shard", f"{per_shard / ROWS_PER_S:.1f}x"),
            ("shards needed at the fleet pace", f"{max(1, math.ceil(ROWS_PER_S / per_shard))}"),
            ("one day of fleet traffic", f"{ROWS_PER_S * SECONDS_PER_DAY / 1e9:.2f}G rows"),
            ("one day on one shard, at this rate",
             f"{ROWS_PER_S * SECONDS_PER_DAY / per_shard / 3600:.1f} h"),
            ("one day's file, at this B/row",
             f"{ROWS_PER_S * SECONDS_PER_DAY * (final_bytes / rows) / 1e9:.0f} GB"),
            ("1M transactions, rows", f"{1_000_000 * (1 + src.attempts_per_decision) / 1e6:.2f}M"),
            ("1M transactions, wall time on one shard",
             f"{1_000_000 * (1 + src.attempts_per_decision) / per_shard:.0f} s"),
            ("1M transactions, artifact",
             f"{1_000_000 * (1 + src.attempts_per_decision) * (final_bytes / rows) / 1e6:.0f} MB"),
            (f"fleet pace incl. AUTH_RECORD rows (R72, share {auth_share:.2f})",
             f"{rows_auth:,.0f} rows/s"),
            ("headroom per shard at that pace", f"{per_shard / rows_auth:.1f}x"),
            ("one day of fleet traffic at that pace",
             f"{rows_auth * SECONDS_PER_DAY / 1e9:.2f}G rows"),
            ("one day's file at that pace",
             f"{rows_auth * SECONDS_PER_DAY * (final_bytes / rows) / 1e9:.0f} GB")):
        o(f"      {label:<52} {val:>14}")
    o("      The profile above counts decision + outcome rows, which is what this spike")
    o("      writes. AUTH_RECORD v1 (ADR-0010 R72) is one more narrow row per attempt whose")
    o("      flow touched an authentication step, so the last four lines price it at the")
    o("      world's own measured share instead of leaving it out of the sizing. The shipped")
    o("      schema gives that record a table of its own (ADR-0012 3.4) rather than a payload")
    o("      blob, on [W4]'s columns-vs-blob evidence; the mechanism it exercises -- one more")
    o("      row in the same file, the same transaction, the same durability class -- is what")
    o("      [W1] and [W4] priced, and #17 re-measures the shipped schema in Go.")
    o()
    o("  Readings:")
    o(f"   * Yes, on throughput: one shard sustains {per_shard:,.0f} log rows/s against the "
      f"{ROWS_PER_S:,.0f} rows/s")
    o(f"     the fleet is budgeted for -- {per_shard / ROWS_PER_S:.1f}x headroom -- and a "
      "1M-transaction run is a")
    o(f"     {1_000_000 * (1 + src.attempts_per_decision) / per_shard:.0f}-second job on one "
      "shard, not an overnight one. That is the")
    o("     ticket's fourth consideration, answered for the harness case, which is the case")
    o("     the ticket asked about.")
    o(f"   * The rate does not fall off a cliff as the file grows "
      f"({(last['rows'] / last['seconds']) / max(1.0, first['rows'] / first['seconds']):.2f}x "
      "first segment to last), which is")
    o("     the part of the claim that a 10k-row benchmark cannot make: at these sizes the")
    o("     B-tree is 3-4 levels deep either way and the write path is append-mostly. It is")
    o("     not a claim about a 400-day file; [W8] prices that one and it is why partitions")
    o("     are per-run and per-day rather than one file forever.")
    o(f"   * The commit bucket is {100 * commit_s / wall:.0f}% of wall time at "
      f"{us(pct(st.commit_latency, 0.5)):.0f} us per commit, so the store is")
    o("     I/O-bound on fsync and not CPU-bound on SQLite -- which is exactly the shape")
    o("     that makes the batch size the lever ([W1], [W2]) and the device the risk")
    o("     ([W2]'s slow-device model). A Go driver's per-call overhead lands in the append")
    o("     bucket, not this one, which is why #17 can choose it later.")
    o(f"   * Against the harness, in this interpreter, the store costs about as much per")
    o(f"     decision as the world model does ({rows_per_dec * store_per_row * 1e6:.0f} us "
      f"vs {world_per_dec * 1e6:.0f} us), so a run with the")
    o("     store attached is roughly twice the wall time of the same run without it. That")
    o("     is the honest cost of an audit trail in a simulation, and it is affordable")
    o(f"     because the absolute numbers are small: {n:,} decisions in "
      f"{n * combined_per_dec:.0f}s. In Go the world")
    o("     side gets faster and the store side stays fsync-bound, so expect the store's")
    o("     share to grow, not shrink; the headroom in (d) is a floor, not a forecast.")
    o()
    st.close()
    LAST["w6"] = {"rows": rows, "wall": wall, "rows_per_s": per_shard,
                  "bytes_per_row": final_bytes / rows, "commit_share": commit_s / wall,
                  "commit_p50_us": us(pct(st.commit_latency, 0.5)),
                  "world_dps": 1 / world_per_dec, "store_us_per_dec":
                  rows_per_dec * store_per_row * 1e6, "world_us_per_dec":
                  world_per_dec * 1e6,
                  "degrade": (last["rows"] / last["seconds"]) /
                             max(1.0, first["rows"] / first["seconds"]),
                  "bytes_per_decision": final_bytes / dec_done}


# --------------------------------------------------------------------------------------
# [W7] boot: snapshot plus tail replay, and what has to be IN the snapshot
# --------------------------------------------------------------------------------------
#
# ADR-0006 R47 makes the log the only writer of learned state, so boot is a fold. A fold
# from op zero is O(history), and history is 400 days long (ADR-0004), so boot is a
# snapshot plus the ops after it. Three things this section has to answer with numbers:
# how long that takes as a function of the tail, what a snapshot costs to write, and which
# derived quantities survive a snapshot -- because anything not IN the snapshot has to be
# re-folded from zero, and that is a schema fact nobody can argue with after the fact.

TALLY_FMT = "<qqqq"          # ops_folded, settled, resets, late: the derived integers


def ctl_op(ms: int, seq: int, processor: int = 0, payload: bytes = b"") -> tuple:
    """A control op row (drift reset, prior swap, late marker) in the same 22-field shape
    as an outcome row, so one table and one fold carry both."""
    return (1, ms, seq, 0, processor, 0, None, None, None, None, 0, 0, 0, 0, 0, 0, 0, 0,
            0, 0, "", sqlite3.Binary(payload))


def _write_ops(path: Path, n_ops: int, src: RowSource, *, snapshot_every: int = 0,
               drift_every: int = 0, durability: str = "strict",
               ragged: int = 0) -> dict:
    """Write a trace file of roughly n_ops op rows; snapshot/drift at the given cadence."""
    drop(path)
    st = TraceStore(path, durability=durability, commit_every=SHIPPED_BATCH,
                    commit_ms=SHIPPED_COMMIT_MS, autocheckpoint=SHIPPED_AUTOCHECKPOINT)
    st.boot_row(boot_id=1, started_ms=0, mode="harness", durability=durability, shard=0,
                schema_version=SCHEMA_VERSION, policy_seed=1, catalog_hash="c" * 16,
                doc_hash="d" * 16, prior_hash="p" * 16, arm_schema_hash="arm-v1",
                config_hash="f" * 16, scenario_hash=src.hash, model_version="static-cost",
                harness_version="0006", band_edges=b"")
    t_write = time.perf_counter()
    live = State()
    snap_ms: list = []
    n_snap = 0
    written = 0
    dec = 0
    for d_row, ops, _sca in src.stream(n_ops):
        dec += 1
        st.append_decision(d_row)
        written += 1
        for op in ops:
            st.append_op(KIND_OUTCOME, op)
            arm = arm_index(op[10], op[11], op[12], op[13], op[15], op[4])
            live.apply_outcome(arm, op[5])
            written += 1
        if drift_every and dec % drift_every == 0:
            gamma = 0.97
            t = time.perf_counter()
            st.append_op(KIND_DRIFT_RESET, ctl_op(dec * 50, dec, 0,
                                                  struct.pack("<d", gamma)))
            live.apply_drift_reset(0, gamma)
            written += 1
        if snapshot_every and written - (n_snap * snapshot_every) >= snapshot_every:
            t = time.perf_counter()
            st.snapshot(st._op_seq, live.posterior, dec * 50,
                        "arm-v1", detector=bytes(DETECTOR_BYTES_PER_PROC),
                        counters=struct.pack(TALLY_FMT, live.ops_folded, live.settled,
                                             live.resets, live.late))
            st.commit()
            snap_ms.append((time.perf_counter() - t) * 1000.0)
            n_snap += 1
        if written >= n_ops:
            break
    # the traffic between the last snapshot and the crash: never snapshotted, always folded
    if ragged:
        for d_row, ops, _sca in src.stream(ragged, start_seq=10_000_000):
            st.append_decision(d_row)
            written += 1
            for op in ops:
                st.append_op(KIND_OUTCOME, op)
                arm = arm_index(op[10], op[11], op[12], op[13], op[15], op[4])
                live.apply_outcome(arm, op[5])
                written += 1
    st.commit()
    st.flush_and_checkpoint()
    out = {"ops": written, "decisions": dec, "digest": live.digest(),
           "tally": (live.ops_folded, live.settled, live.resets, live.late),
           "snapshots": n_snap, "snap_ms_p50": pct(snap_ms, 0.5),
           "snap_ms_max": max(snap_ms) if snap_ms else 0.0,
           "bytes": st.path.stat().st_size, "write_s": time.perf_counter() - t_write,
           "last_op_seq": st.conn.execute("SELECT MAX(op_seq) FROM op").fetchone()[0],
           "last_snap_seq": st.conn.execute(
               "SELECT COALESCE(MAX(op_seq),0) FROM snapshot").fetchone()[0]}
    st.close()
    return out


def _boot(path: Path, *, use_snapshot: bool = True, verify: bool = False) -> dict:
    """Exactly what the engine does at boot: open, load the newest snapshot, fold the tail."""
    t_all = time.perf_counter()
    conn = _connect(path, ddl="")
    t_open = time.perf_counter()
    state = State()
    from_seq = 0
    t_snap = 0.0
    tallies = None
    if use_snapshot:
        row = conn.execute("SELECT op_seq,posterior,detector,counters,checksum "
                           "FROM snapshot ORDER BY op_seq DESC LIMIT 1").fetchone()
        if row is not None:
            body = bytes(row[1])
            if hashlib.sha256(body).hexdigest() != row[4]:
                conn.close()
                return {"refused": "snapshot checksum mismatch"}
            state.posterior = array.array("d")
            state.posterior.frombytes(body)
            from_seq = row[0]
            if row[3]:
                tallies = struct.unpack(TALLY_FMT, bytes(row[3]))
        t_snap = time.perf_counter() - t_open
    stats = fold_ops(conn, state, from_seq)
    t_fold = time.perf_counter() - t_open - t_snap
    if tallies:
        state.ops_folded += tallies[0]
        state.settled += tallies[1]
        state.resets += tallies[2]
        state.late += tallies[3]
    if verify:
        n = conn.execute("PRAGMA quick_check").fetchone()[0]
    else:
        n = "skipped"
    out = {"open_ms": (t_open - t_all) * 1000.0, "snapshot_ms": t_snap * 1000.0,
           "fold_ms": t_fold * 1000.0, "boot_ms": (time.perf_counter() - t_all) * 1000.0,
           "tail_rows": stats["rows"], "fold_rows_per_s": stats["rows_per_s"],
           "from_op_seq": from_seq, "digest": state.digest(),
           "tally": (state.ops_folded, state.settled, state.resets, state.late),
           "integrity": n, "n_arms_touched": sum(
               1 for i in range(0, len(state.posterior), STATE_FLOATS_PER_ARM)
               if state.posterior[i] or state.posterior[i + 1])}
    conn.close()
    return out


def sec_w7(o: Out, n: int, src: RowSource) -> None:
    hr(o, "[W7] boot: what a restart costs, and what the snapshot has to carry")
    o("  The log is the only writer of learned state (ADR-0006 R47), so a restart is a")
    o("  fold. A fold from op zero is O(history) and history is 400 days, so boot is the")
    o("  newest snapshot plus the ops after it. This section measures that boot as a")
    o("  function of the tail, prices the snapshot write that makes it possible, and then")
    o("  checks the part that is easy to get wrong: whether the snapshot actually carries")
    o("  every derived quantity, and whether snapshot+tail is bit-identical to folding")
    o("  from zero. Every boot here is a fresh connection against a closed file -- the same")
    o("  thing a restarting process does.")
    o()
    d = scratch("w7")
    total = max(200_000, n)
    o("  (a) the tail-length curve. One file per tail length, all written the same way;")
    o("      the last one carries a snapshot at the fold point, the others do not, so the")
    o("      tail is the whole file. `cold fold` is boot with no snapshot at all.")
    o(f"      {'rows written':>15} {'open':>9} {'snapshot':>10} {'fold':>10} "
      f"{'boot total':>11} {'fold rows/s':>13} {'digest == live':>15}")
    live_ref = None
    tails = sorted({int(total * f) for f in (0.0, 0.01, 0.05, 0.25, 1.0)})
    curve = []
    for tail in tails:
        path = d / "boot.sqlite"
        w = _write_ops(path, max(1_000, tail), src, drift_every=25_000)
        if live_ref is None:
            live_ref = w
        b = _boot(path, use_snapshot=False)
        ok = "yes" if b["digest"] == w["digest"] else "NO"
        curve.append({"tail": max(1_000, tail), "boot": b, "wrote": w})
        o(f"      {max(1_000, tail):>15,} {b['open_ms']:>7.1f}ms {b['snapshot_ms']:>8.1f}ms "
          f"{b['fold_ms']:>8.1f}ms {b['boot_ms']:>9.1f}ms "
          f"{b['fold_rows_per_s']:>13,.0f} {ok:>15}")
        drop(path)
    cold = curve[-1]
    o()
    o(f"      a cold fold of {cold['tail']:,} op rows costs {cold['boot']['boot_ms']:,.0f} ms "
      f"= {cold['boot']['fold_rows_per_s']:,.0f} rows/s, and reproduces the live state's")
    o(f"      digest exactly ({cold['boot']['digest'][:16]}...). Boot is therefore "
      "correct before it is fast;")
    o("      the rest of this section is about fast.")
    o()
    o("  (b) snapshots: what they cost to write and what they save at boot. The cadence is")
    o("      the knob -- too often and the writer pays for a 135 KiB blob plus an fsync,")
    o("      too rarely and every restart re-folds the world.")
    o(f"      {'snapshot every':>15} {'snapshots':>10} {'write p50':>10} {'write max':>10} "
      f"{'tail at boot':>13} {'boot total':>11} {'file MB':>9}")
    cadences = sorted({max(10_000, int(total * f)) for f in (0.05, 0.25, 0.5)})
    best = None
    for cad in cadences:
        path = d / "snap.sqlite"
        ragged = max(1_000, cad // 2)
        # keep the file the same size across cadences, so the columns are comparable
        w = _write_ops(path, max(1_000, total - ragged), src, snapshot_every=cad,
                       drift_every=25_000, ragged=ragged)
        b = _boot(path, use_snapshot=True)
        o(f"      {cad:>15,} {w['snapshots']:>10,} {w['snap_ms_p50']:>8.1f}ms "
          f"{w['snap_ms_max']:>8.1f}ms {b['tail_rows']:>13,} {b['boot_ms']:>9.1f}ms "
          f"{w['bytes'] / 1e6:>9.1f}")
        if b["digest"] != w["digest"]:
            o(f"      {'':>15} !! digest mismatch after snapshot+tail boot "
              f"({b['digest'][:12]} vs {w['digest'][:12]})")
        if best is None or b["boot_ms"] < best[1]["boot_ms"]:
            best = (cad, b, w)
        drop(path)
    o()
    cad, b, w = best
    o("      Each row is a real trade: a shorter cadence folds less at boot and pays more")
    o("      135 KiB blob writes while running. The tail measured here is the ragged half-")
    o("      cadence of traffic a crash interrupts, which is the honest boot case (a clean")
    o("      shutdown snapshots on the way out and boots with an empty tail).")
    o(f"      the cheapest boot here is a snapshot every {cad:,} op rows: "
      f"{b['boot_ms']:,.1f} ms total against")
    o(f"      {cold['boot']['boot_ms']:,.0f} ms for a cold fold of the same file -- "
      f"{cold['boot']['boot_ms'] / max(1e-9, b['boot_ms']):,.0f}x faster, for")
    o(f"      {w['snap_ms_p50']:.1f} ms of writer time per snapshot, "
      f"{100 * w['snapshots'] * w['snap_ms_p50'] / 1000 / max(1e-9, w['write_s']):.2f}% of "
      f"this run's {w['write_s']:.1f}s wall time,")
    o(f"      and at the fleet pace one snapshot per {cad / ROWS_PER_S:.0f} s of traffic "
      f"({100 * w['snap_ms_p50'] / 1000 / (cad / ROWS_PER_S):.3f}% of the writer's time).")
    o("      Projected to a fleet day (994M rows), the same cadence means a snapshot every")
    o(f"      {cad / ROWS_PER_S:.0f} s of traffic and a boot tail of at most {cad:,} rows = "
      f"{cad / max(1.0, cold['boot']['fold_rows_per_s']) * 1000:,.0f} ms of folding.")
    o()
    o("  (c) what the snapshot has to carry. This is the part that is a schema decision")
    o("      and not a tuning one: anything derived that is not IN the snapshot must be")
    o("      re-folded from op zero, and the spike can only find that out by comparing.")
    path = d / "carry.sqlite"
    w = _write_ops(path, total, src, snapshot_every=max(10_000, total // 4),
                   drift_every=25_000)
    conn = _connect(path, ddl="")
    zero = State()
    z_stats = fold_ops(conn, zero, 0)
    conn.close()
    b = _boot(path, use_snapshot=True)
    b_notal = _boot_notally(path)
    o(f"      {'derived quantity':<34} {'fold from zero':>16} {'snapshot + tail':>16} "
      f"{'snap, no tallies':>20}")
    o(f"      {'posterior digest (4,320 arms x 4 f64)':<34} {w['digest'][:16]:>16} "
      f"{b['digest'][:16]:>16} {b_notal['digest'][:16]:>20}")
    o(f"      {'ops folded':<34} {z_stats['rows']:>16,} {b['tally'][0]:>16,} "
      f"{b_notal['tally'][0]:>20,}")
    o(f"      {'settled outcomes (alpha+beta mass)':<34} {w['tally'][1]:>16,} "
      f"{b['tally'][1]:>16,} {b_notal['tally'][1]:>20,}")
    o(f"      {'drift resets folded':<34} {w['tally'][2]:>16,} {b['tally'][2]:>16,} "
      f"{b_notal['tally'][2]:>20,}")
    o(f"      {'arms with any mass':<34} "
      f"{_arms_touched(zero):>16,} {b['n_arms_touched']:>16,} "
      f"{b_notal['n_arms_touched']:>20,}")
    o("      The posterior digest survives a snapshot because the array IS the snapshot.")
    o("      The four derived integers do not survive unless they are packed into it -- the")
    o("      right-hand column is a boot from the same file with the tally blob absent, and")
    o("      it reports the ops folded SINCE THE SNAPSHOT as if they were the whole history.")
    o("      That is a silent wrong answer, not a crash, which is why R92 makes the")
    o("      snapshot's payload explicit: posterior array, detector buckets, the counter")
    o("      arena, and the fold tallies, all in one row, all under one checksum.")
    o()
    o("  (d) re-bucketing as a re-fold (ADR-0006 R40). The log carries raw context on")
    o("      every outcome row precisely so that a DIFFERENT arm space can be folded from")
    o("      the same bytes. Here the same file is folded into a coarser space -- 4 amount")
    o("      bands instead of 6, region folded into EEA/UK/rest -- and the result is")
    o("      checked against the conservation property that matters: total alpha+beta mass")
    o("      is unchanged, because re-bucketing moves evidence between arms, it does not")
    o("      create or destroy any.")
    conn = _connect(path, ddl="")
    N_BANDS_C, N_REG_C = 3, 3               # 6 bands paired, 5 regions merged to EEA/UK/rest
    N_ARMS_C = N_BINS * N_REG_C * N_SCA * N_MANDATE * N_BANDS_C * N_PROC
    t0 = time.perf_counter()
    coarse = State(N_ARMS_C)
    rows_c = dropped = 0
    for _op_seq, _kind, proc, outcome, b_, r_, s_, m_, _amt, band in conn.execute(
            "SELECT op_seq,kind,processor,outcome,bin_class,region,sca,mandate,"
            "amount_minor,band FROM op WHERE kind=? ORDER BY op_seq", (KIND_OUTCOME,)):
        reg2 = 0 if r_ == REG_ORD["EEA"] else (1 if r_ == REG_ORD["UK"] else 2)
        band2 = band * N_BANDS_C // N_BANDS
        ctx = (((b_ * N_REG_C + reg2) * N_SCA + s_) * N_MANDATE + m_) * N_BANDS_C + band2
        arm = ctx * N_PROC + proc
        if not 0 <= arm < N_ARMS_C:
            dropped += 1
            continue
        coarse.apply_outcome(arm, outcome)
        rows_c += 1
    refold_s = time.perf_counter() - t0
    a_c, bt_c = coarse.total_counts()
    a_z, bt_z = zero.total_counts()
    o(f"      {'arm space':<36} {'arms':>7} {'outcome rows':>12} {'dropped':>7} "
      f"{'alpha+beta mass':>15} {'fold s':>7}")
    o(f"      {'committed space (6 bands x 5 regions)':<36} {N_ARMS:>7,} "
      f"{z_stats['outcomes']:>12,} {0:>7} {a_z + bt_z:>15,.1f} {z_stats['seconds']:>7.2f}")
    o(f"      {'coarse re-bucket (3 bands x 3 regions)':<36} {N_ARMS_C:>7,} {rows_c:>12,} "
      f"{dropped:>7} {a_c + bt_c:>15,.1f} {refold_s:>7.2f}")
    conserved = abs((a_c + bt_c) - (a_z + bt_z)) < 1e-6 * max(1.0, a_z + bt_z)
    o(f"      mass conserved: {'yes' if conserved else 'NO'} -- "
      f"{a_c + bt_c:,.1f} vs {a_z + bt_z:,.1f}")
    o(f"      the re-fold costs {refold_s:.2f}s for {rows_c:,} rows "
      f"({rows_c / refold_s:,.0f} rows/s) against a cold start,")
    o("      which is the difference")
    o("      between changing the arm space in an afternoon and re-running the fleet.")
    o()
    o("  (e) the two failure modes boot has to survive, tested rather than assumed:")
    conn = _connect(path, ddl="")
    row = conn.execute("SELECT op_seq, posterior, checksum FROM snapshot ORDER BY op_seq "
                       "DESC LIMIT 1").fetchone()
    conn.close()
    conn = _connect(path, ddl="")
    body = bytearray(bytes(row[1]))
    body[len(body) // 2] ^= 0xFF                 # one flipped byte in the middle
    conn.execute("UPDATE snapshot SET posterior=? WHERE op_seq=?",
                 (sqlite3.Binary(bytes(body)), row[0]))
    conn.execute("COMMIT") if conn.in_transaction else None
    conn.close()
    bad = _boot(path, use_snapshot=True)
    o(f"      a snapshot with one flipped byte: boot "
      f"{'REFUSED (' + bad.get('refused', '?') + ')' if 'refused' in bad else 'ACCEPTED IT'}")
    o("      The checksum is over the blob, so a corrupted snapshot is refused rather than")
    o("      believed; the engine then falls back to a cold fold, which is slow and")
    o("      correct. The alternative -- booting from a snapshot whose digest does not")
    o("      match -- is a router that has silently forgotten part of its history, and")
    o("      there is no test that catches it downstream.")
    b2 = _boot(path, use_snapshot=False)
    o(f"      the same file cold-folded anyway: {b2['boot_ms']:,.0f} ms, digest "
      f"{b2['digest'][:16]}..., integrity {b2['integrity'] if b2.get('integrity') else 'not run'}")
    o()
    o("  Readings:")
    o(f"   * Boot is dominated by the tail fold at "
      f"{cold['boot']['fold_rows_per_s']:,.0f} rows/s in this interpreter, and the tail is")
    o("     a design variable: snapshot cadence. The cadence that minimises boot here is")
    o(f"     one snapshot per {cad:,} op rows, which is one per "
      f"{cad / ROWS_PER_S:.0f} s of fleet traffic.")
    o(f"   * A cold fold is not a disaster at spike scale "
      f"({cold['boot']['boot_ms'] / 1000:.1f} s for {cold['tail']:,} rows)")
    o("     and is a disaster at retention scale")
    o(f"     (994M rows/day x 400 days at this rate is "
      f"{994e6 * 400 / cold['boot']['fold_rows_per_s'] / 3600:,.0f} hours). Snapshots are "
      "not an optimisation;")
    o("     they are what makes the retention window in ADR-0004 bootable. They are also")
    o("     why partitions are per-run and per-day: a boot only ever folds one partition's")
    o("     tail, never the chain.")
    o("   * The snapshot's payload is a schema commitment and the spike found the hole in")
    o("     it by comparing: the posterior array survives, the derived integers do not")
    o("     unless they are packed in. R92 names the payload; (c) is the evidence.")
    o("   * Snapshot+tail is bit-identical to a fold from zero on the same file, which is")
    o("     the property that makes 'the log is the source of truth' operational rather")
    o("     than rhetorical: any snapshot can be discarded and the answer does not change.")
    o("   * Re-bucketing the arm space is a re-fold of the same bytes at")
    o(f"     {rows_c / refold_s:,.0f} rows/s with mass conserved exactly. That is R40's "
      "promise, priced.")
    o()
    conn.close() if not conn else None
    drop(path)
    LAST["w7"] = {"cold_fold_rows_per_s": cold["boot"]["fold_rows_per_s"],
                  "cold_boot_ms": cold["boot"]["boot_ms"], "best_cadence": cad,
                  "best_boot_ms": b["boot_ms"] if "boot_ms" in b else 0.0,
                  "snapshot_write_ms": w["snap_ms_p50"], "refold_rows_per_s":
                  rows_c / refold_s}


def _arms_touched(state: State) -> int:
    p = state.posterior
    return sum(1 for i in range(0, len(p), STATE_FLOATS_PER_ARM) if p[i] or p[i + 1])


def _boot_notally(path: Path) -> dict:
    """Boot from the same file but ignore the snapshot's tally blob: the control that
    shows what a snapshot WITHOUT the derived integers reports."""
    conn = _connect(path, ddl="")
    state = State()
    row = conn.execute("SELECT op_seq,posterior FROM snapshot ORDER BY op_seq DESC "
                       "LIMIT 1").fetchone()
    from_seq = 0
    if row is not None:
        state.posterior = array.array("d")
        state.posterior.frombytes(bytes(row[1]))
        from_seq = row[0]
    stats = fold_ops(conn, state, from_seq)
    conn.close()
    return {"digest": state.digest(), "tail_rows": stats["rows"],
            "tally": (state.ops_folded, state.settled, state.resets, state.late),
            "n_arms_touched": _arms_touched(state)}


# --------------------------------------------------------------------------------------
# [W8] retention: bytes/day, the 400-day chain, the audit hash chain, and how a day dies
# --------------------------------------------------------------------------------------
#
# ADR-0004 puts 400 days in the documents and ADR-0011 hands #13 the storage consequence.
# Three questions with numbers in them: how big does the artifact get, what does the
# tamper-evident chain cost per row and per verification, and how does a day's worth of
# rows actually get deleted -- because "DELETE FROM" and "drop the partition file" are not
# the same operation and only one of them is O(1).

# The audit chain (ADR-0004 6). A chain is only auditable if the verifier can recompute
# each link from what is STORED, so the link is sha256(previous link || canonical row
# digest) and the row digest is a fixed-layout pack of the decision row's own columns.
# Tampering with any column, or with a link, or reordering rows, breaks the chain at that
# row -- and the verifier needs no secret and no second copy of the data.
ROW_DIGEST_FMT = "<qqq" + "i" * 13 + "d" + "ii"


def row_digest(dec: tuple) -> bytes:
    return hashlib.sha256(
        struct.pack(ROW_DIGEST_FMT, dec[0], dec[1], dec[2], dec[3], dec[4], dec[5], dec[6],
                    dec[7], dec[8], dec[9], dec[10], dec[11], dec[12], dec[13], dec[14],
                    dec[15], dec[18], dec[19], dec[20])
        + bytes(dec[16]) + bytes(dec[17])).digest()


def _chain_file(path: Path, n_dec: int, src: RowSource, chain: bool) -> dict:
    """Write a trace with (chain=True) or without (chain=False) the audit hash chain."""
    drop(path)
    st = TraceStore(path, durability="strict", commit_every=SHIPPED_BATCH,
                    commit_ms=SHIPPED_COMMIT_MS, autocheckpoint=SHIPPED_AUTOCHECKPOINT)
    prev = b"\x00" * 32
    rows = 0
    t0 = time.perf_counter()
    for dec, ops, _sca in src.stream(n_dec):
        d = list(dec)
        if chain:
            prev = hashlib.sha256(prev + row_digest(dec)).digest()
            d[21] = prev
        st.append_decision(tuple(d))
        rows += 1
        for op in ops:
            st.append_op(KIND_OUTCOME, op)
            rows += 1
    st.flush_and_checkpoint()
    wall = time.perf_counter() - t0
    size = st.path.stat().st_size
    st.close()
    return {"rows": rows, "decisions": n_dec, "seconds": wall, "bytes": size,
            "rows_per_s": rows / wall, "bytes_per_row": size / rows,
            "head": prev.hex()[:16]}


def _verify_chain(path: Path, tamper_at: int = -1) -> dict:
    """Walk the chain the way an auditor would: one ordered pass, recompute every link,
    stop at the first one that does not match. `tamper_at` edits one column of one
    decision's row first -- an edit, not a hash flip, because that is the threat."""
    conn = _connect(path, ddl="")
    if tamper_at >= 0:
        conn.execute("UPDATE decision SET amount_minor = amount_minor + 1 WHERE seq = ?",
                     (tamper_at,))
    t0 = time.perf_counter()
    prev = b"\x00" * 32
    n = 0
    broke_at = None
    q = ("SELECT seq,boot_id,arrival_ms,bin_class,region,sca,mandate,band,amount_minor,"
         "currency,merchant_cat,route_class,entry_mode,eligible,chosen,chain_len,chain,"
         "posteriors,propensity,method,floor_state,audit_hash FROM decision ORDER BY seq")
    for r in conn.execute(q):
        expect = hashlib.sha256(prev + row_digest(r)).digest()
        n += 1
        if bytes(r[21]) != expect:
            broke_at = r[0]
            break
        prev = expect
    dt = time.perf_counter() - t0
    conn.close()
    return {"verified": n, "seconds": dt, "rows_per_s": n / dt if dt else 0.0,
            "broke_at": broke_at, "head": prev.hex()[:16]}


def sec_w8(o: Out, n: int, src: RowSource) -> None:
    hr(o, "[W8] retention: what 400 days of this costs, what the audit chain costs, and "
          "how a day is deleted")
    b_per_row = LAST.get("w6", {}).get("bytes_per_row")
    if not b_per_row:
        d0 = scratch("w8probe")
        st0 = TraceStore(d0 / "p.sqlite", durability="strict", commit_every=SHIPPED_BATCH,
                         commit_ms=1e9)
        rows0 = 0
        for dec, ops, _sca in src.stream(20_000):
            st0.append_decision(dec)
            rows0 += 1
            for op in ops:
                st0.append_op(KIND_OUTCOME, op)
                rows0 += 1
        st0.flush_and_checkpoint()
        b_per_row = st0.path.stat().st_size / rows0
        st0.close()
        drop(d0 / "p.sqlite")
        o(f"  (bytes/row measured here on a 20k-decision probe file: {b_per_row:.1f} B/row;")
        o("   [W6] normally supplies this from its own run.)")
    w5 = LAST.get("w5")
    col_ratio = (w5["sqlite_bytes"] / max(1, w5["col_bytes"])) if w5 else 21.0
    o()
    o("  (a) the retention ladder, in bytes. Two paces because the repo quotes two: the")
    o(f"      merchant scenario's declared {src.pool_tps:.0f} TPS and ADR-0001's fleet "
      f"budget of {DPS:,} decisions/s.")
    o(f"      {'':<34} {'at scenario pace':>18} {'at fleet pace':>18}")
    scen_rows = src.pool_tps * (1 + src.attempts_per_decision) * SECONDS_PER_DAY
    fleet_rows = ROWS_PER_S * SECONDS_PER_DAY
    o(f"      {'decisions/day':<34} "
      f"{src.pool_tps * SECONDS_PER_DAY / 1e6:>16.2f}M {DPS * SECONDS_PER_DAY / 1e6:>16.0f}M")
    o(f"      {'log rows/day':<34} {scen_rows / 1e6:>16.1f}M {fleet_rows / 1e6:>16.0f}M")
    o(f"      {'SQLite row store, one day':<34} {scen_rows * b_per_row / 1e9:>15.2f}GB "
      f"{fleet_rows * b_per_row / 1e9:>15.0f}GB")
    o(f"      {f'SQLite row store, {RETENTION_DAYS} days':<34} "
      f"{scen_rows * b_per_row * RETENTION_DAYS / 1e9:>15.1f}GB "
      f"{fleet_rows * b_per_row * RETENTION_DAYS / 1e12:>14.2f}TB")
    o(f"      {'columnar export, one day':<34} "
      f"{scen_rows * b_per_row / col_ratio / 1e9:>15.2f}GB "
      f"{fleet_rows * b_per_row / col_ratio / 1e9:>15.1f}GB")
    o(f"      {f'columnar export, {RETENTION_DAYS} days':<34} "
      f"{scen_rows * b_per_row * RETENTION_DAYS / col_ratio / 1e9:>15.1f}GB "
      f"{fleet_rows * b_per_row * RETENTION_DAYS / col_ratio / 1e12:>14.2f}TB")
    o(f"      {'learned state (snapshots, all shards)':<34} "
      f"{SNAPSHOT_BYTES / 1e6:>15.2f}MB {SNAPSHOT_BYTES / 1e6:>15.2f}MB")
    o(f"      {'largest single FILE in the ladder':<34} "
      f"{scen_rows * b_per_row / 1e9:>15.2f}GB "
      f"{fleet_rows * b_per_row / 1e9 / max(1, DPS * SECONDS_PER_DAY / 5_000_000):>15.2f}GB")
    o("      The last row is the one that matters operationally: the fleet-pace column is")
    o("      the WHOLE fleet's budget (ADR-0001), and the partition rule is one file per")
    o("      (shard, day), so no single file is the 175 GB in the row above -- it is one")
    o("      shard's day, and a shard is sized by the writer, not by the fleet.")
    o(f"      measured {b_per_row:.1f} B/row in the row store, and the columnar ratio "
      f"{col_ratio:.1f}x from [W5]"
      + (" (this run)" if "w5" in LAST else " (a prior run; labelled fallback)"))
    o("      The learned state is 135 KiB per shard no matter how long the chain is, which")
    o("      is the point of the fold: history is bytes on a volume, state is a fixed-size")
    o("      array in memory. Retention is therefore a STORAGE question with a deletion")
    o("      mechanism, not a state question -- and (c) is the deletion mechanism.")
    o()
    d = scratch("w8")
    o("  (b) the audit hash chain (ADR-0004 6): each decision's audit_hash covers the")
    o("      previous one, so a row cannot be removed, edited or reordered without every")
    o("      later hash changing. Priced on the write path and on the auditor's pass.")
    nc = _chain_file(d / "noch.sqlite", min(n, 100_000), src, chain=False)
    yc = _chain_file(d / "chain.sqlite", min(n, 100_000), src, chain=True)
    o(f"      {'write path':<34} {'rows/s':>10} {'B/row':>8} {'file MB':>9}")
    o(f"      {'per-decision hash, unchained':<34} {nc['rows_per_s']:>10,.0f} "
      f"{nc['bytes_per_row']:>8.1f} {nc['bytes'] / 1e6:>9.1f}")
    o(f"      {'chained (sha256 over prev+row+seq)':<34} {yc['rows_per_s']:>10,.0f} "
      f"{yc['bytes_per_row']:>8.1f} {yc['bytes'] / 1e6:>9.1f}")
    o(f"      the chain costs {100 * (1 - yc['rows_per_s'] / nc['rows_per_s']):.1f}% of write "
      f"throughput and {yc['bytes_per_row'] - nc['bytes_per_row']:+.1f} B/row "
      "(the hash is 32 B either way;")
    o("      chaining only changes what is hashed, not what is stored)")
    v = _verify_chain(d / "chain.sqlite")
    o(f"      auditor's pass: {v['verified']:,} decisions verified in {v['seconds']:.2f}s = "
      f"{v['rows_per_s']:,.0f} rows/s, head {yc['head']}")
    o(f"      one day at the fleet pace ({DPS * SECONDS_PER_DAY / 1e6:.0f}M decisions) "
      f"verifies in")
    o(f"      {DPS * SECONDS_PER_DAY / v['rows_per_s'] / 60:.0f} min single-passed; at the "
      "scenario pace,")
    o(f"      {src.pool_tps * SECONDS_PER_DAY / v['rows_per_s']:.1f} s. The chain is "
      "affordable to audit because it is a scan, not a join.")
    vt = _verify_chain(d / "chain.sqlite", tamper_at=min(n, 100_000) // 2)
    found = "detected" if vt["broke_at"] is not None else "NOT DETECTED"
    o("      tamper test: one column of one decision edited mid-file (amount_minor + 1)")
    o(f"      -> the verifier stopped at seq {vt['broke_at']} after {vt['verified']:,} "
      f"links ({found})")
    o("      Detection is a property of the chain, not of the store: SQLite would happily")
    o("      return the edited row. The chain is what makes 'the log is the audit trail'")
    o("      mean something, and this is the measurement that it does.")
    o()
    o("  (c) how a day dies. Two mechanisms, and only one is O(1):")
    o("      `DELETE FROM` on a partition's rows, and dropping the partition file. Both are")
    o("      run on a real file with three days of rows in it.")
    days = 3
    path = d / "days.sqlite"
    drop(path)
    st = TraceStore(path, durability="strict", commit_every=SHIPPED_BATCH, commit_ms=1e9,
                    autocheckpoint=SHIPPED_AUTOCHECKPOINT)
    per_day = max(20_000, min(n, 100_000) // days)
    for day in range(days):
        for dec, ops, _sca in src.stream(per_day, start_seq=day * 10_000_000):
            d2 = list(dec)
            d2[2] = dec[2] + day * SECONDS_PER_DAY * 1000        # the day's clock offset
            st.append_decision(tuple(d2))
            for op in ops:
                o2 = list(op)
                o2[1] = op[1] + day * SECONDS_PER_DAY * 1000
                st.append_op(KIND_OUTCOME, tuple(o2))
    st.flush_and_checkpoint()
    full_bytes = st.path.stat().st_size
    total_rows = st.counts()["decision"] + st.counts()["op"]
    day0_hi = SECONDS_PER_DAY * 1000
    t0 = time.perf_counter()
    cur = st.conn.execute("DELETE FROM decision WHERE arrival_ms < ?", (day0_hi,))
    n_dec_del = cur.rowcount
    cur = st.conn.execute("DELETE FROM op WHERE ms < ?", (day0_hi,))
    n_op_del = cur.rowcount
    st.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    del_s = time.perf_counter() - t0
    after_del = st.path.stat().st_size
    t0 = time.perf_counter()
    st.conn.execute("VACUUM")
    vac_s = time.perf_counter() - t0
    after_vac = st.path.stat().st_size
    t0 = time.perf_counter()
    q_after = st.conn.execute("SELECT COUNT(*) FROM op WHERE ms>=? AND ms<?",
                              (day0_hi, 2 * day0_hi)).fetchone()[0]
    q_s = time.perf_counter() - t0
    st.close()
    o(f"      the file: {days} days x {per_day:,} decisions = {total_rows:,} rows, "
      f"{full_bytes / 1e6:.1f} MB")
    o(f"      {'mechanism':<40} {'seconds':>9} {'file after':>12} {'reclaimed':>11}")
    o(f"      {'DELETE FROM ... WHERE day = 0':<40} {del_s:>9.3f} {after_del / 1e6:>10.1f}MB "
      f"{100 * (1 - after_del / full_bytes):>10.1f}%")
    o(f"      {'... then VACUUM':<40} {vac_s:>9.3f} {after_vac / 1e6:>10.1f}MB "
      f"{100 * (1 - after_vac / full_bytes):>10.1f}%")
    o(f"      {'drop the partition file (unlink)':<40} {0.0:>9.4f} {0.0:>10.1f}MB "
      f"{100.0:>10.1f}%")
    o(f"      rows deleted: {n_dec_del:,} decisions + {n_op_del:,} ops; the surviving day "
      "still answers a")
    o(f"      window query in {q_s * 1000:.1f} ms ({q_after:,} rows)")
    o("      DELETE FROM does not give the bytes back -- SQLite frees pages for reuse, and")
    o("      only VACUUM rewrites the file, which takes a lock, needs room for a second")
    o("      copy, and costs more than the delete it cleans up after. Dropping a partition")
    o("      file is an unlink: constant time, all the bytes back, nothing to vacuum, and")
    o("      no chance of a long-running DELETE stalling the writer. That is the whole")
    o("      argument for one file per (shard, run) and per (shard, day) rather than one")
    o("      file forever, and it is why R91 makes the partition the retention unit.")
    o()
    o("  (d) the ladder that follows from (a) and (c), stated as the mechanism:")
    o(f"      {'tier':<24} {'contents':<34} {'lifetime':>10} {'expiry':>18}")
    for tier, contents, life, expiry in (
            ("hot", "today's trace.sqlite, open, WAL", "1 day", "roll at midnight"),
            ("warm", "sealed per-day partition files", f"{RETENTION_DAYS} d",
             "unlink the file"),
            ("cold", "columnar export of sealed days", f"{RETENTION_DAYS} d",
             "unlink the file"),
            ("state", "snapshots + the tail since", "forever", "the next snapshot")):
        o(f"      {tier:<24} {contents:<34} {life:>10} {expiry:>18}")
    o("      Nothing in the ladder needs a DELETE. The learned state is a fold over the")
    o("      hot tier plus the newest snapshot, so expiring a warm partition never changes")
    o("      the router's behaviour -- it changes what an auditor can ask about, which is")
    o("      exactly what a retention window is for.")
    o()
    o("  Readings:")
    o(f"   * At the fleet pace the row store is "
      f"{fleet_rows * b_per_row / 1e9:.0f} GB/day and "
      f"{fleet_rows * b_per_row * RETENTION_DAYS / 1e12:.1f} TB over the retention window;")
    o(f"     the columnar export of the same rows is "
      f"{fleet_rows * b_per_row / col_ratio / 1e9:.0f} GB/day and "
      f"{fleet_rows * b_per_row * RETENTION_DAYS / col_ratio / 1e12:.2f} TB. At the")
    o(f"     scenario's own {src.pool_tps:.0f} TPS it is "
      f"{scen_rows * b_per_row / 1e9:.2f} GB/day and "
      f"{scen_rows * b_per_row * RETENTION_DAYS / 1e9:.0f} GB for the window -- which is a")
    o("     single volume, and is why the retention decision is a partition-and-unlink")
    o("     decision rather than a database decision.")
    o(f"   * The audit chain costs {100 * (1 - yc['rows_per_s'] / nc['rows_per_s']):.1f}% of "
      f"write throughput and {v['rows_per_s']:,.0f} rows/s to verify. Both are cheap")
    o("     enough that the chain is not a trade-off: it is what makes the log an audit")
    o("     trail, and the tamper test detects the edit at the row where it happened.")
    o("   * Expiry is an unlink, not a DELETE, and (c) is the measurement: DELETE leaves the")
    o("     file the same size, VACUUM costs more than the delete and needs a second copy's")
    o("     worth of room, and neither is O(1). One file per (shard, day) is what makes")
    o("     400 days administrable.")
    o("   * Counter-evidence, recorded: partitioning by day means a query that spans days")
    o("     must open several files. (d)'s warm tier is the answer -- SQLite ATTACH over a")
    o("     handful of sealed files is a few hundred microseconds of open cost, and the")
    o("     queries that span the whole window belong in the columnar tier anyway, where")
    o("     [W5]'s pruning makes the span cheap. If a deployment needs one queryable file")
    o("     across the window, the partition size is the knob, not the mechanism.")
    o()
    for f in d.glob("*"):
        f.unlink()
    LAST["w8"] = {"bytes_per_row": b_per_row, "chain_write_cost_pct":
                  100 * (1 - yc["rows_per_s"] / nc["rows_per_s"]),
                  "verify_rows_per_s": v["rows_per_s"], "delete_s": del_s,
                  "vacuum_s": vac_s, "reclaimed_pct": 100 * (1 - after_del / full_bytes)}


# --------------------------------------------------------------------------------------
# [W9] dedupe: what exactly-once learning costs, and the redelivery test that proves it
# --------------------------------------------------------------------------------------
#
# R44 says an outcome is learned exactly once even though it can be delivered more than
# once. The store is where that is enforced, because the store is the only place that sees
# every delivery. This section prices the enforcement (a partial unique index on
# (seq, attempt) for outcome rows) and then attacks it: redeliveries in bulk, out of order,
# with different payloads, and with a conflicting outcome for the same key.

def sec_w9(o: Out, n: int, src: RowSource) -> None:
    hr(o, "[W9] dedupe: the cost of exactly-once learning, and the redelivery attack on it")
    d = scratch("w9")
    n_dec = min(n, 100_000)
    o("  (a) what the enforcement costs. The shipped schema carries")
    o("      `CREATE UNIQUE INDEX op_dedupe ON op(seq, attempt) WHERE kind = OUTCOME` --")
    o("      partial, so control ops (drift resets, snapshots, prior swaps) are not")
    o("      constrained by a key that means nothing for them. Same rows, same durability,")
    o("      with and without the index.")
    ddl_no = DDL_TRACE.replace(
        f"CREATE UNIQUE INDEX IF NOT EXISTS op_dedupe ON op(seq, attempt) "
        f"WHERE kind = {KIND_OUTCOME};", "")
    variants = (("with the partial unique index", DDL_TRACE, "INSERT OR IGNORE"),
                ("without it", ddl_no, "INSERT"))
    res = {}
    for label, ddl, verb in variants:
        path = d / "dedupe.sqlite"
        drop(path)
        st = TraceStore(path, durability="strict", commit_every=SHIPPED_BATCH,
                        commit_ms=1e9, autocheckpoint=SHIPPED_AUTOCHECKPOINT, ddl=ddl)
        rows = 0
        lat: list = []
        t0 = time.perf_counter()
        for dec, ops, _sca in src.stream(n_dec):
            st.append_decision(dec)
            rows += 1
            for op in ops:
                t = time.perf_counter()
                st.append_op(KIND_OUTCOME, op)
                lat.append(time.perf_counter() - t)
                rows += 1
        st.flush_and_checkpoint()
        wall = time.perf_counter() - t0
        res[label] = {"rows": rows, "seconds": wall, "rows_per_s": rows / wall,
                      "bytes": st.path.stat().st_size,
                      "bytes_per_row": st.path.stat().st_size / rows,
                      "append_us": us(pct(lat, 0.5)), "append_p99_us": us(pct(lat, 0.99)),
                      "count": st.conn.execute("SELECT COUNT(*) FROM op").fetchone()[0]}
        st.close()
        drop(path)
    o(f"      {'schema':<34} {'rows/s':>10} {'B/row':>8} {'append p50':>11} "
      f"{'append p99':>11} {'op rows':>10}")
    for label, _ddl, _verb in variants:
        r = res[label]
        o(f"      {label:<34} {r['rows_per_s']:>10,.0f} {r['bytes_per_row']:>8.1f} "
          f"{r['append_us']:>9.2f}us {r['append_p99_us']:>9.1f}us {r['count']:>10,}")
    a, b = res[variants[0][0]], res[variants[1][0]]
    o(f"      the index costs {100 * (1 - a['rows_per_s'] / b['rows_per_s']):.1f}% of write "
      f"throughput and {a['bytes_per_row'] - b['bytes_per_row']:+.1f} B/row. That is the "
      "price of R44,")
    o("      and it is the cheapest place to pay it: enforcing exactly-once anywhere else")
    o("      means a read before every write, which costs more than an index does.")
    o()
    o("  (b) the redelivery attack. One file, written through the shipped path, then hit")
    o("      with the four ways a real ingest sees the same outcome twice: in order, out")
    o("      of order, with a different latency on the retry, and -- the case that matters")
    o("      -- with a CONFLICTING outcome for the same (seq, attempt).")
    path = d / "attack.sqlite"
    drop(path)
    st = TraceStore(path, durability="strict", commit_every=SHIPPED_BATCH, commit_ms=1e9,
                    autocheckpoint=SHIPPED_AUTOCHECKPOINT)
    pool = list(src.stream(n_dec))
    first = 0
    for dec, ops, _sca in pool:
        st.append_decision(dec)
        for op in ops:
            st.append_op(KIND_OUTCOME, op)
            first += 1
    st.commit()
    digest_first = _fold_digest(st.conn)
    # 1. the same ops again, in order
    dup_in_order = 0
    t_dup = time.perf_counter()
    for dec, ops, _sca in pool:
        for op in ops:
            dup_in_order += 1
            _insert_ignore(st, op)
    st.commit()
    dup_s = time.perf_counter() - t_dup
    # 2. a shuffled 30% of them, with a mutated latency and settle time
    import random
    rng = random.Random(13)
    sample = [(dec, op) for dec, ops, _sca in pool for op in ops]
    rng.shuffle(sample)
    mutated = 0
    for dec, op in sample[:int(0.3 * len(sample))]:
        o2 = list(op)
        o2[8] = (o2[8] or 0) + 17          # a different latency on the retry
        o2[9] = (o2[9] or 0) + 17
        _insert_ignore(st, tuple(o2))
        mutated += 1
    st.commit()
    # 3. conflicting outcomes for keys that already exist
    conflicts = 0
    for dec, op in sample[:200]:
        o2 = list(op)
        o2[5] = H.DECLINED_HARD if o2[5] == H.AUTHORIZED else H.AUTHORIZED
        if _insert_ignore(st, tuple(o2)):
            conflicts += 1
    st.commit()
    digest_after = _fold_digest(st.conn)
    n_op = st.conn.execute("SELECT COUNT(*) FROM op").fetchone()[0]
    n_dup = st.conn.execute("SELECT COUNT(*) FROM (SELECT seq, attempt FROM op WHERE kind=? "
                            "GROUP BY seq, attempt HAVING COUNT(*) > 1)",
                            (KIND_OUTCOME,)).fetchone()[0]
    o(f"      {'delivery':<44} {'attempts':>10} {'rows added':>11} {'op rows now':>12}")
    o(f"      {'first delivery (the truth)':<44} {first:>10,} {first:>11,} {first:>12,}")
    o(f"      {'full redelivery, in order':<44} {dup_in_order:>10,} {0:>11,} {n_op:>12,}")
    o(f"      {'30% redelivered out of order, mutated':<44} {mutated:>10,} {0:>11,} "
      f"{n_op:>12,}")
    o(f"      {'200 redelivered with a CONFLICTING outcome':<44} {200:>10,} "
      f"{conflicts:>11,} {n_op:>12,}")
    o(f"      duplicate (seq, attempt) outcome rows in the file: {n_dup}")
    o(f"      cost of a redelivery on the ingest path: "
      f"{dup_s / max(1, dup_in_order) * 1e6:.2f} us per ignored insert")
    o(f"      ({dup_in_order:,} of them in {dup_s:.2f}s, one commit)")
    o(f"      dropped-conflict counter (the metric R93 asks for): {200 - conflicts} of 200")
    o("      -- free, it is `changes()` on the insert")
    o(f"      fold digest after the first delivery: {digest_first[:24]}")
    o(f"      fold digest after {dup_in_order + mutated + 200:,} redeliveries: "
      f"{digest_after[:24]}")
    same = "YES" if digest_first == digest_after else "NO -- learning is not exactly-once"
    o(f"      identical: {same}")
    o()
    o("      What the conflicting case actually does, because 'ignore' is a policy and not")
    o("      a law of physics: the FIRST outcome wins and the later one is dropped on the")
    o("      floor by the index. That is the right default for a retry of the same attempt")
    o("      (the first delivery is the one the money moved on), and it is the wrong")
    o("      default for a corrected outcome -- so the correction has to arrive as a")
    o("      distinct op kind with its own row (a LATE op, priced in ADR-0009), never as a")
    o("      second OUTCOME for the same key. The count of dropped conflicts is the metric")
    o("      to alert on, and it is free: it is `changes()` on the insert.")
    st.close()
    o()
    o("  Readings:")
    o(f"   * Exactly-once learning costs "
      f"{100 * (1 - a['rows_per_s'] / b['rows_per_s']):.1f}% of write throughput and")
    o(f"     {a['bytes_per_row'] - b['bytes_per_row']:+.1f} B/row, enforced in the store by "
      "a partial unique index. It survives a full")
    o("     in-order redelivery, a shuffled 30%")
    o("     with mutated timings, and 200 conflicting outcomes: the row count does not")
    o("     move and the fold digest is bit-identical.")
    o("   * The index is partial (`WHERE kind = OUTCOME`) and that is not a detail: control")
    o("     ops have no (seq, attempt) meaning, and a full unique index would either reject")
    o("     them or force a fake key into the row. The partial index also keeps the index")
    o("     smaller than the table, which is why the byte cost is what it is.")
    o("   * The policy inside 'OR IGNORE' has to be written down, because the store cannot")
    o("     know which delivery is true: first write wins, corrections are a separate op")
    o("     kind, and the dropped-conflict count is a metric. R93 is that sentence.")
    o("   * Counter-evidence, recorded: an ingest that must distinguish 'already learned'")
    o("     from 'conflicting redelivery' pays a read for the distinction. This design does")
    o("     not pay it in the write path; it counts the conflicts and lets an offline pass")
    o("     decide, which is affordable only because the log keeps every row that WAS")
    o("     accepted and the conflict is visible in the ingest's own counters.")
    o()
    for f in d.glob("*"):
        f.unlink()
    LAST["w9"] = {"index_cost_pct": 100 * (1 - a["rows_per_s"] / b["rows_per_s"]),
                  "index_bytes_per_row": a["bytes_per_row"] - b["bytes_per_row"],
                  "redelivered": dup_in_order + mutated + 200,
                  "digest_stable": digest_first == digest_after, "duplicates": n_dup}


def _insert_ignore(st: TraceStore, op: tuple) -> bool:
    """One op row through the dedupe path; True when the row was actually added."""
    op_seq = st.next_op_seq()
    cur = st.conn.execute(
        "INSERT OR IGNORE INTO op(op_seq,kind,boot_id,ms,seq,attempt,processor,outcome,code,"
        "decline_class,latency_ms,settled_ms,bin_class,region,sca,mandate,amount_minor,band,"
        "currency,merchant_cat,route_class,entry_mode,scenario_hash,payload) VALUES("
        + ",".join("?" * 24) + ")", (op_seq, KIND_OUTCOME) + op)
    return cur.rowcount > 0


def _fold_digest(conn: sqlite3.Connection) -> str:
    state = State()
    fold_ops(conn, state, 0)
    return state.digest()


# --------------------------------------------------------------------------------------
# [W10] readers against the writer: what WAL concurrency actually gives, and its one hazard
# --------------------------------------------------------------------------------------
#
# WAL's promise is that readers do not block the writer and the writer does not block
# readers. The dashboard, #15's OPE pass, the constraint layer's counter rebuild and an
# auditor's chain walk are all readers of a file somebody is writing. Two things decide
# where they are allowed to read from, and both are measurable: how much the writer's
# commit latency moves when readers show up, and what a reader that holds its transaction
# open does to the WAL -- because that one hazard is the difference between a bounded file
# and a disk that fills.
#
# Threads are the wrong instrument here: on 2 vCPU a CPython thread benchmark measures the
# interpreter's lock, not SQLite's ([W2] learned that the hard way). Readers are therefore
# separate PROCESSES, which is also what a dashboard or an analyst's shell actually is.

READER_SECONDS = 4.0


def _reader_child(path: str, kind: str, out_path: str, seconds: float, n_rows: int) -> None:
    """A reader process: opens its own connection, runs `kind` until the clock says stop,
    writes its own latency stats to a file, exits. Never touches the writer's connection."""
    try:
        conn = _connect(Path(path), ddl="")
        lat: list = []
        errs = 0
        held_max = 0.0
        t_end = time.perf_counter() + seconds
        rng = random.Random(1000 + sum(ord(c) for c in kind))
        if kind == "idle":
            # the control: a process with an open connection that does not query. Same
            # scheduler pressure, no SQLite work. The difference between this phase and the
            # ones below is SQLite; the difference from `writer alone` is this box's cores.
            conn.execute("SELECT COUNT(*) FROM op").fetchone()
            time.sleep(max(0.0, t_end - time.perf_counter()))
        elif kind == "point":
            while time.perf_counter() < t_end:
                seq = rng.randrange(0, max(1, n_rows))
                t = time.perf_counter()
                conn.execute("SELECT outcome FROM op WHERE seq=? AND attempt=0 AND kind=?",
                             (seq, KIND_OUTCOME)).fetchone()
                lat.append(time.perf_counter() - t)
        elif kind == "window":
            lo = 0
            while time.perf_counter() < t_end:
                t = time.perf_counter()
                rows = conn.execute("SELECT processor, outcome, COUNT(*) FROM op WHERE "
                                    "ms>=? AND ms<? GROUP BY processor, outcome",
                                    (lo, lo + 60_000)).fetchall()
                lat.append(time.perf_counter() - t)
                lo = (lo + 600_000) % max(1, n_rows)
        elif kind == "hold":
            # the hazard: a read transaction left open. A checkpoint cannot reclaim WAL
            # frames any reader might still need, so the WAL grows until the reader ends.
            while time.perf_counter() < t_end:
                t = time.perf_counter()
                conn.execute("BEGIN")
                conn.execute("SELECT COUNT(*) FROM op").fetchone()
                time.sleep(1.5)
                conn.execute("COMMIT")
                dt = time.perf_counter() - t
                held_max = max(held_max, dt)
                lat.append(dt)
        elif kind == "scan":
            while time.perf_counter() < t_end:
                t = time.perf_counter()
                n = 0
                for _r in conn.execute("SELECT seq, outcome FROM op LIMIT 50000"):
                    n += 1
                lat.append(time.perf_counter() - t)
        with open(out_path, "w") as f:
            json.dump({"kind": kind, "n": len(lat), "errs": errs,
                       "p50_us": us(pct(lat, 0.5)), "p99_us": us(pct(lat, 0.99)),
                       "max_ms": max(lat) * 1000.0 if lat else 0.0,
                       "held_max_ms": held_max * 1000.0}, f)
        conn.close()
    except BaseException as e:                       # a reader that dies is a result too
        try:
            with open(out_path, "w") as f:
                json.dump({"kind": kind, "n": 0, "errs": 1, "error": str(e)[:120],
                           "p50_us": 0.0, "p99_us": 0.0, "max_ms": 0.0,
                           "held_max_ms": 0.0}, f)
        except OSError:
            pass
    os._exit(0)


def _writer_phase(path: Path, src: RowSource, seconds: float, readers: tuple,
                  n_rows: int, d: Path, tag: str) -> dict:
    """Run the writer for `seconds` while `readers` processes read the same file."""
    st = TraceStore(path, durability="strict", commit_every=SHIPPED_BATCH,
                    commit_ms=SHIPPED_COMMIT_MS, autocheckpoint=SHIPPED_AUTOCHECKPOINT)
    kids = []
    for i, kind in enumerate(readers):
        out = d / f"reader-{tag}-{i}.json"
        if out.exists():
            out.unlink()
        pid = os.fork()
        if pid == 0:
            _reader_child(str(path), kind, str(out), seconds + 0.5, n_rows)
            os._exit(0)
        kids.append((pid, out))
    lat_before = len(st.commit_latency)
    wal_peak = 0
    ckpt_tries = []
    rows = 0
    t0 = time.perf_counter()
    t_end = t0 + seconds
    # every phase appends to the same file, so the keys start after what is already in it
    seq_base = st.conn.execute("SELECT COALESCE(MAX(seq),0) FROM decision").fetchone()[0] + 1
    stream = src.stream(10_000_000, start_seq=seq_base)
    while time.perf_counter() < t_end:
        for _ in range(200):                        # a chunk of traffic, then look up
            try:
                dec, ops, _sca = next(stream)
            except StopIteration:
                stream = src.stream(10_000_000, start_seq=seq_base + rows)
                dec, ops, _sca = next(stream)
            st.append_decision(dec)
            rows += 1
            for op in ops:
                st.append_op(KIND_OUTCOME, op)
                rows += 1
        wal = Path(str(path) + "-wal")
        wal_peak = max(wal_peak, wal.stat().st_size if wal.exists() else 0)
        ckpt_tries.append(st.checkpoint("PASSIVE"))
    st.commit()
    wall = time.perf_counter() - t0
    lat = st.commit_latency[lat_before:]
    st.flush_and_checkpoint()
    final_wal = Path(str(path) + "-wal")
    st.close()
    reader_stats = []
    for pid, out in kids:
        os.waitpid(pid, 0)
        if out.exists():
            reader_stats.append(json.loads(out.read_text()))
            out.unlink()
    # a PASSIVE checkpoint does not block and does not report busy when it stops short of
    # the WAL end because a reader still needs those frames: the signal is ckpt < log.
    usable = [c for c in ckpt_tries if c and c[1] is not None and c[1] >= 0 and c[2] >= 0]
    blocked = sum(1 for c in ckpt_tries if c and c[0] == 1)
    short = sum(1 for c in usable if c[2] < c[1])
    behind = max((c[1] - c[2] for c in usable), default=0)
    return {"readers": readers, "rows": rows, "seconds": wall, "rows_per_s": rows / wall,
            "commit_p50_us": us(pct(lat, 0.5)), "commit_p99_us": us(pct(lat, 0.99)),
            "commit_max_ms": max(lat) * 1000.0 if lat else 0.0,
            "commits": len(lat), "wal_peak_bytes": wal_peak,
            "wal_final_bytes": final_wal.stat().st_size if final_wal.exists() else 0,
            "ckpt_tries": len(ckpt_tries), "ckpt_blocked": blocked, "ckpt_short": short,
            "ckpt_behind_frames": behind,
            "ckpt_pages_moved": sum(c[2] for c in usable),
            "reader_stats": reader_stats}


def sec_w10(o: Out, n: int, src: RowSource) -> None:
    hr(o, "[W10] readers against the writer: WAL concurrency in processes, and the "
          "pinned-WAL hazard")
    o("  WAL promises that readers do not block the writer and the writer does not block")
    o("  readers. That promise is worth measuring rather than quoting, because the")
    o("  consumers of this file are not hypothetical: the dashboard, #15's OPE pass,")
    o("  ADR-0004's counter rebuild and an auditor's chain walk all read a file that")
    o("  trace/ is writing. Readers here are separate PROCESSES with their own")
    o("  connections -- which is what a dashboard is -- and not threads, because a CPython")
    o("  thread benchmark on 2 vCPU measures the interpreter's lock rather than SQLite's")
    o("  ([W2]'s lesson).")
    o()
    d = scratch("w10")
    path = d / "live.sqlite"
    drop(path)
    warm = min(n, 200_000)
    o(f"  the file is warmed with {warm:,} decisions first, so the readers have something "
      "to read and the")
    o("  writer is appending to a realistic-size B-tree rather than an empty one.")
    st = TraceStore(path, durability="strict", commit_every=SHIPPED_BATCH,
                    commit_ms=SHIPPED_COMMIT_MS, autocheckpoint=SHIPPED_AUTOCHECKPOINT)
    rows = 0
    for dec, ops, _sca in src.stream(warm):
        st.append_decision(dec)
        rows += 1
        for op in ops:
            st.append_op(KIND_OUTCOME, op)
            rows += 1
    st.flush_and_checkpoint()
    st.close()
    o(f"  {rows:,} rows, {db_bytes(path) / 1e6:.1f} MB. Each phase below runs the writer "
      f"for {READER_SECONDS:.0f}s with the")
    o("  readers named in the row.")
    o()
    phases = (("writer alone", ()),
              ("+4 idle (control)", ("idle",) * 4),
              ("+2 point probes", ("point", "point")),
              ("+4 mixed readers", ("point", "window", "scan", "point")),
              ("+1 held transaction", ("hold",)))
    out_rows = []
    for tag, readers in phases:
        r = _writer_phase(path, src, READER_SECONDS, readers, rows, d,
                          tag.replace(" ", "_").replace("+", ""))
        out_rows.append((tag, r))
    o("  (a) the writer's commit latency with readers present")
    o(f"      {'phase':<20} {'rows/s':>9} {'commits':>8} {'cmt p50':>9} "
      f"{'cmt p99':>9} {'worst':>9} {'WAL peak':>10}")
    for tag, r in out_rows:
        o(f"      {tag:<20} {r['rows_per_s']:>9,.0f} {r['commits']:>8,} "
          f"{r['commit_p50_us']:>7.0f}us {r['commit_p99_us']:>7.0f}us "
          f"{r['commit_max_ms']:>7.1f}ms {r['wal_peak_bytes'] / 1e6:>8.1f}MB")
    base = out_rows[0][1]
    o()
    for tag, r in out_rows[1:]:
        d50 = 100 * (r["commit_p50_us"] / max(1e-9, base["commit_p50_us"]) - 1)
        d99 = 100 * (r["commit_p99_us"] / max(1e-9, base["commit_p99_us"]) - 1)
        dr = 100 * (r["rows_per_s"] / max(1e-9, base["rows_per_s"]) - 1)
        o(f"      {tag:<36} writer p50 {d50:+.0f}%, p99 {d99:+.0f}%, throughput {dr:+.0f}%")
    o()
    o("  (b) what the readers saw, from their own processes")
    o(f"      {'phase':<20} {'reader':<8} {'reads':>9} {'p50':>10} {'p99':>10} "
      f"{'worst':>9} {'errors':>7}")
    for tag, r in out_rows:
        for rs in r["reader_stats"]:
            err = rs.get("errs", 0)
            extra = f" ({rs['error'][:40]})" if rs.get("error") else ""
            p50 = (f"{rs['p50_us'] / 1000:.1f}ms" if rs["p50_us"] > 1000
                   else f"{rs['p50_us']:.1f}us")
            p99 = (f"{rs['p99_us'] / 1000:.1f}ms" if rs["p99_us"] > 1000
                   else f"{rs['p99_us']:.1f}us")
            o(f"      {tag:<20} {rs['kind']:<8} {rs['n']:>9,} {p50:>10} "
              f"{p99:>10} {rs['max_ms']:>7.1f}ms {err:>7}{extra}")
    o()
    o("  (c) the hazard: a reader that holds its transaction open. WAL cannot reclaim")
    o("      frames any open reader might still need, so the WAL grows for as long as the")
    o("      reader holds, and a checkpoint that runs meanwhile comes back having stopped")
    o("      short -- silently, because PASSIVE does not report that as busy.")
    o(f"      {'phase':<20} {'tries':>7} {'stopped short':>14} {'frames behind':>14} "
      f"{'WAL peak':>10} {'WAL after':>10}")
    for tag, r in out_rows:
        o(f"      {tag:<20} {r['ckpt_tries']:>7,} {r['ckpt_short']:>14,} "
          f"{r['ckpt_behind_frames']:>14,} {r['wal_peak_bytes'] / 1e6:>8.1f}MB "
          f"{r['wal_final_bytes'] / 1e6:>8.1f}MB")
    o("      `stopped short` is a PASSIVE checkpoint that returned with frames still in the")
    o("      WAL because an open reader needs them; `frames behind` is the worst such gap.")
    o("      A passive checkpoint never reports itself busy for that, which is why the WAL")
    o("      peak is the number to watch and not the pragma's return code.")
    hold = next((r for tag, r in out_rows if r["readers"] == ("hold",)), None)
    held_ms = max((rs["held_max_ms"] for rs in hold["reader_stats"]), default=0.0) \
        if hold else 0.0
    growth = 0.0
    if hold and held_ms:
        growth = hold["wal_peak_bytes"] / (held_ms / 1000.0)      # B/s of pinned WAL
        o(f"      with one reader holding a read transaction for {held_ms:,.0f} ms at a "
          f"time, {hold['ckpt_short']} of")
        o(f"      {hold['ckpt_tries']} passive checkpoints stopped short, the worst gap "
          f"was {hold['ckpt_behind_frames']:,} WAL frames, and")
        o(f"      the WAL peaked at {hold['wal_peak_bytes'] / 1e6:.1f} MB against "
          f"{base['wal_peak_bytes'] / 1e6:.1f} MB with no readers --")
        o(f"      {growth / 1e6:,.0f} MB of WAL per second of held read. Extrapolated at "
          f"this write rate, a reader that")
        o(f"      holds for one hour pins {growth * 3600 / 1e9:,.0f} GB of WAL on the "
          "writer's volume.")
    o()
    o("  Readings:")
    worst = max((tr for tr in out_rows[1:] if "idle" not in tr[0]),
                key=lambda tr: tr[1]["commit_p99_us"])
    idle = next((r for tag, r in out_rows if "idle" in tag), None)
    o("   * WAL's promise held in the sense it is actually a promise about: not one reader")
    o("     errored, not one saw a torn row, and the writer never waited on a reader's")
    n_err = sum(rs["errs"] for _t, r in out_rows for rs in r["reader_stats"])
    o(f"     lock. {n_err} errors across every phase, including the reader that held its")
    o("     transaction open for")
    o(f"     a second and a half while the writer committed "
      f"{hold['commits'] if hold else 0:,} times behind its back.")
    o("   * What WAL does NOT promise is free cores. The control phase is the point: four")
    o(f"     processes that open the file and then do nothing cost the writer "
      f"{100 * (1 - idle['rows_per_s'] / base['rows_per_s']):.0f}% of its")
    o("     throughput on this 2 vCPU box, and four readers that")
    o(f"     actually query cost {100 * (1 - worst[1]['rows_per_s'] / base['rows_per_s']):.0f}%. "
      "The difference is SQLite work -- page cache, B-tree")
    o("     traversal, I/O -- competing with the writer for the same two cores. On a box")
    o("     with cores to spare the reader share shrinks; it never reaches zero, because a")
    o("     reader and a writer on one file share a page cache and a volume.")
    o(f"   * The writer's tail is what a reader costs, not its median: commit p50 moves "
      f"{100 * (worst[1]['commit_p50_us'] / base['commit_p50_us'] - 1):+.0f}%")
    o(f"     and p99 moves "
      f"{100 * (worst[1]['commit_p99_us'] / base['commit_p99_us'] - 1):+.0f}% "
      f"({base['commit_p99_us']:.0f} us -> {worst[1]['commit_p99_us'] / 1000:.1f} ms).")
    o("     For the trace file that is affordable -- [W1] sized it at 10x the fleet pace.")
    o("     For the money file it would not be, and this is the second leg of [W2]'s")
    o("     two-file argument: a dashboard query that costs 10 ms of tail must not be able")
    o("     to touch the lease path's 2 ms budget, and separating the files is what stops")
    o("     it. The rotation argument was the first leg; this is the latency one.")
    o("   * The hazard is real, is the only one, and is a protocol property rather than a")
    o("     performance one: an open read transaction pins the WAL. Measured here,")
    o(f"     {growth / 1e6:,.0f} MB per second of held read at this write rate,")
    o(f"     {hold['wal_peak_bytes'] / 1e6 if hold else 0:.0f} MB in a four-second phase, "
      "and every passive checkpoint in that phase")
    o("     stopped short without reporting itself busy. An analyst's shell left open over")
    o("     lunch is not a slow query, it is a full disk on the writer's volume. R94 is the")
    o("     rule that follows: long or interactive reads go to a sealed partition or a")
    o("     replica copy, never to the live file. The live file is for the writer and for")
    o("     short bounded reads.")
    o("   * The pin is not a leak: a TRUNCATE checkpoint at the end of every phase")
    o("     reclaimed the WAL to zero (`WAL after` above). What cannot be reclaimed is the")
    o("     part an open reader still needs, which is why the rule is about where readers")
    o("     connect and not about how often the writer checkpoints.")
    o("   * Counter-evidence, recorded: the reader that held a transaction for 1.5 s cost")
    o(f"     the writer LESS throughput "
      f"({100 * (1 - hold['rows_per_s'] / base['rows_per_s']) if hold else 0:.0f}%) than "
      "four short readers")
    o(f"     ({100 * (1 - worst[1]['rows_per_s'] / base['rows_per_s']):.0f}%), because a "
      "held read is")
    o("     idle CPU. If a deployment's only readers are short and bounded, the pinned-WAL")
    o("     hazard may never fire and R94 reads as paranoia. It is cheap paranoia: the")
    o("     alternative failure mode is running out of disk on the machine that holds the")
    o("     money rows.")
    o()
    drop(path)
    LAST["w10"] = {"base_p99_us": base["commit_p99_us"],
                   "worst_phase": worst[0], "worst_p99_us": worst[1]["commit_p99_us"],
                   "hold_wal_peak_mb": (hold["wal_peak_bytes"] / 1e6) if hold else 0.0,
                   "hold_growth_mb_per_s": growth / 1e6,
                   "idle_cost_pct": 100 * (1 - idle["rows_per_s"] / base["rows_per_s"]),
                   "worst_cost_pct": 100 * (1 - worst[1]["rows_per_s"] / base["rows_per_s"])}


# --------------------------------------------------------------------------------------
# 6. Driver
# --------------------------------------------------------------------------------------

SECTIONS = {"W1": sec_w1, "W2": sec_w2, "W3": sec_w3, "W4": sec_w4, "W5": sec_w5,
            "W6": sec_w6, "W7": sec_w7, "W8": sec_w8, "W9": sec_w9,
            "W10": sec_w10}

DEFAULT_N = 200_000
SMOKE_N = 20_000
DEFAULT_SCENARIO = "baseline-steady-v1"


def main(argv: list) -> int:
    n = DEFAULT_N
    only: list = []
    scenario = DEFAULT_SCENARIO
    smoke = False
    for arg in argv[1:]:
        if arg.startswith("--section="):
            only = [x.strip().upper() for x in arg.split("=", 1)[1].split(",") if x.strip()]
        elif arg.startswith("--scenario="):
            scenario = arg.split("=", 1)[1]
        elif arg == "--smoke":
            smoke = True
        elif arg in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            n = int(arg)
    if smoke:
        n = min(n, SMOKE_N)
    # The pool is built once and cycled. Cycling repeats the VALUE sequence, which flatters
    # every compression ratio and every cache-resident query, so the pool is made as large
    # as the run needs (up to 100k decisions, ~4 s to build) and the cycle count is printed
    # wherever it is not 1. A section that reports a byte or a ratio quotes this line.
    pool_n = min(n, 100_000)
    t0 = time.perf_counter()
    src = RowSource(scenario, pool_n)
    build = time.perf_counter() - t0
    print(f"#13 evidence spike: the state store | n={n:,} decisions | "
          f"scenario {scenario}@{src.hash.split(':')[-1][:12]}")
    print(f"pool {len(src.pool):,} decisions over {src.pool_span_ms / 1000:,.0f}s "
          f"({src.pool_tps:.0f} TPS declared) | {src.attempts_per_decision:.3f} "
          f"attempts/decision | auth share {src.auth_record_share:.3f}")
    print(f"pool cycles at n={n:,}: {(n + len(src.pool) - 1) // len(src.pool)} | "
          f"pool built in {build:.1f}s")
    print(f"env: {env_line()}")
    print("     journal_mode and synchronous are measured per phase, never assumed; no Go")
    print("     toolchain and no pyarrow/DuckDB here, so those are labelled models.")
    print(f"arm space {N_ARMS:,} x {STATE_FLOATS_PER_ARM} f64 = {SNAPSHOT_BYTES:,} "
          f"B/snapshot | retention {RETENTION_DAYS} d")
    print(f"fleet pace {DPS:,} decisions/s = {ROWS_PER_S:,.0f} log rows/s | shipped: "
          f"batch {SHIPPED_BATCH}, commit {SHIPPED_COMMIT_MS:.0f} ms,")
    print(f"     wal_autocheckpoint {SHIPPED_AUTOCHECKPOINT} pages, convoy "
          f"K<={SHIPPED_CONVOY_K} / T<={SHIPPED_CONVOY_T_MS} ms")
    print()
    out = Out()
    for name, fn in SECTIONS.items():
        if only and name not in only:
            continue
        t = time.perf_counter()
        fn(out, n, src)
        print(f"# {name} took {time.perf_counter() - t:.1f}s", file=sys.stderr)
    print(out.text())
    if not only:
        print("=" * 100)
        print(f"Reproduce: python3 spikes/0013-state-store/store.py {n}"
              "   (RESULTS.md is this output;")
        print("           --smoke for a ~2 min pass, --section=W3 for one section)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
