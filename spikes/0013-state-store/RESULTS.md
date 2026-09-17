#13 evidence spike: the state store | n=200,000 decisions | scenario baseline-steady-v1@b49193b7e715
pool 100,000 decisions over 3,325s (30 TPS declared) | 1.140 attempts/decision | auth share 0.289
pool cycles at n=200,000: 2 | pool built in 3.9s
env: python 3.11.2 | sqlite 3.40.1 | 2 vCPU | 3.8 GiB | scratch /tmp/switchback-0013 (21 GB free)
     journal_mode and synchronous are measured per phase, never assumed; no Go
     toolchain and no pyarrow/DuckDB here, so those are labelled models.
arm space 4,320 x 4 f64 = 138,240 B/snapshot | retention 400 d
fleet pace 5,000 decisions/s = 11,500 log rows/s | shipped: batch 64, commit 5 ms,
     wal_autocheckpoint 1000 pages, convoy K<=32 / T<=0.5 ms

====================================================================================================
[W1] the durability ladder: what a durable append costs, and which config clears the fleet pace
====================================================================================================
  The write profile is not a guess: ADR-0001 budgets 5,000 decisions/s, ADR-0006
  measures 1.3 attempts/decision, so the log takes 11,500 rows/s
  (5,000 decision rows + 6,500 outcome ops). Every config below writes the REAL rows --
  DECISION_LOG v1 plus the outcome op with its raw context -- not a synthetic
  one-column insert. `us/row` is wall time per row including the row build;
  `commit p50/p99` is per COMMIT, which is what an ack waits for (R83).
  fsync column: for FULL it is one per commit; for NORMAL one per checkpoint.

  journal durability batch     rows    rows/s  us/row   cmt p50   cmt p99  B/row  vs fleet
  WAL     strict         1   20,001     7,015  142.56     126us     541us    178      0.6x
  WAL     strict         8   20,001    34,071   29.35     185us     654us    178      3.0x
  WAL     strict        32  120,001    80,953   12.35     320us    1648us    177      7.0x
  WAL     strict        64  120,001   108,006    9.26     467us    2339us    177      9.4x
  WAL     strict       256  120,001   140,174    7.13    1408us    5077us    177     12.2x
  WAL     strict      1024  120,001   167,092    5.98    4385us   11088us    177     14.5x
  WAL     batched        1   20,001    39,407   25.38      14us      56us    178      3.4x
  WAL     batched        8   20,001    87,367   11.45      53us     288us    178      7.6x
  WAL     batched       32  120,001   142,325    7.03     146us    3324us    177     12.4x
  WAL     batched       64  120,001   162,938    6.14     265us    4868us    177     14.2x
  WAL     batched      256  120,001   196,676    5.08     949us    6848us    177     17.1x
  WAL     batched     1024  120,001   199,757    5.01    3672us   12465us    177     17.4x
  WAL     ephemeral      1   20,001    55,927   17.88      14us      44us    178      4.9x
  WAL     ephemeral      8   20,001   136,880    7.31      46us      97us    178     11.9x
  WAL     ephemeral     32  120,001   187,385    5.34     136us     599us    177     16.3x
  WAL     ephemeral     64  120,001   201,817    4.95     253us     957us    177     17.5x
  WAL     ephemeral    256  120,001   210,363    4.75     970us    2633us    177     18.3x
  WAL     ephemeral   1024  120,001   216,519    4.62    3684us    6582us    177     18.8x
  DELETE  strict         1   20,001     1,068  936.33     889us    1512us    176      0.1x
  DELETE  strict         8   20,001     6,906  144.79    1089us    1876us    176      0.6x
  DELETE  strict        32  120,001    20,405   49.01    1476us    2798us    177      1.8x
  DELETE  strict        64  120,001    35,358   28.28    1669us    2626us    177      3.1x
  DELETE  strict       256  120,001    93,737   10.67    2472us    3323us    177      8.2x
  DELETE  strict      1024  120,001   162,413    6.16    5369us    6750us    177     14.1x
  DELETE  batched        1   20,001     1,175  850.90     807us    1374us    176      0.1x
  DELETE  batched        8   20,001     8,086  123.67     934us    1562us    176      0.7x
  DELETE  batched       32  120,001    22,766   43.93    1314us    2226us    177      2.0x
  DELETE  batched       64  120,001    38,773   25.79    1525us    2490us    177      3.4x
  DELETE  batched      256  120,001   106,993    9.35    2159us    2834us    177      9.3x
  DELETE  batched     1024  120,001   171,577    5.83    5016us    7727us    177     14.9x
  MEMORY  ephemeral      1   20,001    58,651   17.05      15us      35us    176      5.1x
  MEMORY  ephemeral      8   20,001   159,288    6.28      41us      73us    176     13.9x
  MEMORY  ephemeral     32  120,001   202,853    4.93     128us     219us    177     17.6x
  MEMORY  ephemeral     64  120,001   219,489    4.56     240us     305us    177     19.1x
  MEMORY  ephemeral    256  120,001   234,218    4.27     899us    1068us    177     20.4x
  MEMORY  ephemeral   1024  120,001   247,927    4.03    3292us    3673us    177     21.6x

  Readings:
   * A durable append is an fsync, and an fsync is a batch decision, not a row
     decision: WAL+FULL is 7,015 rows/s one-row-per-commit and
     108,006 rows/s at batch 64 -- 15x for the same
     guarantee. The rollback journal (DELETE+FULL, one row per commit) is
     1,068 rows/s: it fsyncs twice per commit and is
     6.6x worse than WAL at the SAME durability. That is the
     whole ticket question 'is SQLite fast enough' answered: the journal mode and
     the batch size decide it, not SQLite.
   * At batch 64 the ladder is 108,006 (FULL) / 162,938 (NORMAL) /
     201,817 (OFF) rows/s. NORMAL buys 1.5x over FULL
     and OFF buys 1.9x -- i.e. once the batch exists, the fsync
     is no longer the binding cost, so the guarantee is nearly free. That is why
     the log can run at FULL and still keep R83's ack a real durability claim.
   * MEMORY (58,651 rows/s at batch 1) is the ticket's 'in-memory + periodic
     checkpoint' option measured: it is the fastest writer here and it is not a
     store -- [W3] shows what a crash takes from it.
   * Fleet headroom: the config the ADR ships (WAL + FULL + batch 64)
     clears 11,500 rows/s by 9x on this box, in CPython, with
     no Go driver in the loop. The binding constraint is not throughput.

  (b) the fsync itself: commit latency vs the bytes in the commit (WAL, FULL).
      A convoy is only worth assembling if one fsync carries many rows cheaply.
       batch     rows  B/row  B/commit   cmt p50   cmt p99  us/row in cmt
           1   50,000    177       177     135us     402us        134.665
           8   50,000    177     1,413     182us     633us         22.716
          32   50,000    177     5,650     354us    1597us         11.051
          64   50,000    177    11,293     460us    2331us          7.193
         256   50,000    177    45,056    1161us    5721us          4.552
        1024   50,000    177   180,224    4181us    9789us          4.097
        4096   50,000    177   679,306   14477us   22822us          3.764
      The p50 grows sub-linearly in the batch: an fsync is a fixed device cost plus
      a per-page cost, so the amortised per-row price of durability falls by two
      orders of magnitude between batch 1 and batch 1024. This is the measurement
      the group-commit knob (ADR-0006 7's 'fsync interval is yours') rests on.

  (c) the checkpoint is a writer stall: commit tail vs wal_autocheckpoint
      (WAL, FULL, batch 64 -- the shipped config; the stall is the p99.9).
       autocheckpoint    rows/s      p50       p99     p99.9       max   WAL peak
                  200   111,905    427us    1630us    2239us    2892us      0.9MB
                 1000   112,711    440us    2271us    3305us    3969us      4.2MB
                 8000   100,751    442us    1129us    8804us    9623us     33.0MB
                    0    64,628    902us    1314us    2064us    4317us     87.7MB
      Never checkpointing is not a free buffer. It buys no throughput --
      64,628 rows/s against 112,711 at wal_autocheckpoint=1000,
      43% slower -- and it pays for that with an unbounded WAL
      (88 MB after only 100,000 rows, and it grows with the run), because SQLite has to walk a
      longer frame chain on every commit and every byte of it is a byte the next
      boot has to replay ([W7]). The shipped setting is
      1000 pages: 3,305 us of p99.9 stall -- a tail on one commit in a
      thousand, not a wall -- and a WAL that stays under 4.2 MB instead of growing with the run.
      [W2](a2) measures the same effect from the money path's latency side, and
      [W10] prices the reader that can pin the WAL open no matter what this pragma
      says.

====================================================================================================
[W2] the money row: where the LEASE append lives, and what a crash-safe dispatch costs
====================================================================================================
  ADR-0009 R71 is the one durability claim that moves money: the lease row --
  including the CSPRNG idempotency key -- is durable BEFORE the dispatch it
  authorizes, or a restart re-opens the double-charge window on its own. That puts
  a synchronous fsynced write on the route path, against ADR-0001's 2 ms p99
  in-engine budget and a dispatch pace of 6,500/s (one lease per
  attempt). Three questions, measured where CPython can measure them cleanly and
  modelled -- with the constants inline -- where it cannot: what does one durable
  lease cost, how many can one fsync carry, and what does sharing a file with the
  log writer do to it.
  The lease rows carry the catalog's own contract facts, not constants invented
  here: #13 is the first consumer of the three fields ADR-0011 added, so m_ms is
  acquirer-catalog-2026.09.2's per-processor max_response_ms (3,400 ms for
  delta to 10,500 ms for foxtrot), window_ms is
  ADR-0009 R67's oneoff_cnp resolution window, and import asserts R66's
  key_lifetime_ms >= max_response_ms for all six processors.

  (a) one durable lease, and the convoy curve: K leases per fsync. Single
      thread, own file, synchronous=FULL, wal_autocheckpoint=0 and a FRESH file
      per K, so every row of the curve writes the same number of leases through
      the same WAL growth -- the device physics with nothing else in the loop.
      `WAL B/lease` is the write-ahead-log amplification: a commit writes a frame
      for every page it dirtied, so many small transactions write the SAME page
      many times. Batching buys bytes as well as fsyncs.
         K   cmt p50  cmt mean  us/lease   ceiling  vs fleet  WAL B/lease
         1     467us     475us     475.3     2,104      0.3x        9,045
         2     469us     477us     238.3     4,196      0.6x        4,901
         4     489us     498us     124.5     8,033      1.2x        2,752
         8     410us     419us      52.4    19,078      2.9x        1,617
        16     467us     486us      30.4    32,894      5.1x        1,022
        32     547us     553us      17.3    57,905      8.9x          664
        64     778us     763us      11.9    83,890     12.9x          459
      The commit cost moves 1.8x while K moves 64x (475us at K=1,
      763us at K=64, and the run-to-run spread inside one K is up to
      1.1x on this box): an fsync is a device round trip, not a byte count, so K
      leases ride it for free and the per-lease price falls
      40x across the curve while the WAL amplification falls
      20x. One durable lease costs 467us p50 /
      475us mean / 703us p99 here -- 23% of ADR-0001's 2 ms budget at p50 and
      35% at p99 -- so R71 FITS the route path per lease
      on this device. Throughput is the tighter half: K=1 sustains 2,104
      dispatches/s against a fleet pace of 6,500 (0.3x). That ratio is a
      device-shaped number, not a design-shaped one: this box fsyncs in
      ~475us, and on a network-attached volume in the 1-5 ms class the same
      arithmetic gives (labelled model -- the fsync is the only constant, and it is
      the one number this sandbox cannot speak for):
           K  ceiling here   @1ms fsync   @5ms fsync  fleet pace
           1         2,104        1,000          200       6,500
           8        19,078        8,000        1,600       6,500
          32        57,905       32,000        6,400       6,500
          64        83,890       64,000       12,800       6,500
      At 5 ms per fsync, K=1 serves 200 dispatches/s -- 3% of the pace --
      and K=32 serves 6,400. The convoy therefore ships as the mechanism, not as an
      optimisation, with K=1 as its degenerate low-volume case: at the 20 TPS
      deployment ADR-0006 sizes the WAL against, the assembly wait T fires before
      K fills and the convoy IS the solo commit. K and T are config; the design must
      not depend on being handed a fast fsync.

  (a2) the checkpoint tax on the money path: the same K=1 convoy, 4x the leases,
      with the shipped wal_autocheckpoint and without.
       wal_autocheckpoint   cmt p50  cmt mean   ceiling   WAL MB  WAL B/lease
                     1000     131us     170us     5,868      4.1        1,007
                        0     450us     472us     2,117     38.3        9,349
      An un-checkpointed WAL is not a free buffer: the SAME commit is 2.8x more
      expensive and writes 9x the WAL bytes per lease. It is the same effect [W1](c) shows on
      the log writer's throughput, seen from the latency side. Checkpoint cadence
      is part of the write path's cost model, not housekeeping -- and [W10] shows
      the reader that can pin the WAL open no matter what the pragma says.

  (b) does the money row need its own FILE? Two measurements and one model,
      because the honest answer is not the intuitive one.
      (b1) device interference, single-threaded and alternating: one log batch
           commit, then one lease commit, repeatedly -- the same sequence the
           engine runs, without the interpreter's thread scheduler in the numbers.
      placement                   lease p50  lease p99  log batch p50  log batch p99   pairs/s
      two files (journal+trace)       193us      721us           39us          328us     1,391
      one file (shared)               219us      659us           39us          330us     1,503
      Lease p99 721us with its own file, 659us sharing the log's; log batch p99
      328us vs 330us. Whichever way the sign falls on a given run,
      the difference is inside this box's run-to-run spread ([W1] moved 20% between
      identical configs). The split is NOT bought by device interference, and this
      ADR does not claim that it is.

      (b2) the write lock, modelled from measured parts: SQLite gives one file one
      writer, so in a concurrent engine a lease that shares the log's file waits
      for the log's in-flight write transaction. From [W1](a) at the shipped
      config a batch-64 commit holds the lock ~467us (measured in this run), and the fleet pace
      needs 180 such commits/s, so the lock is held 8.4% of the time:
      a shared-file lease waits an extra 467us on ~8% of dispatches, i.e. its
      p99 moves 721us -> 1188us. Still inside the 2 ms budget; a checkpoint
      stall ([W1](c): p99.9 3.3 ms at wal_autocheckpoint=1000, 8.8 ms at 8000) is
      not, and a checkpoint is exactly what a shared file would put in the money
      path's tail. THAT is the latency argument for the split, and it is a tail
      argument, not a median one.

      (b3) the two arguments that do not need a benchmark at all:
        * rotation. The trace file is a partition: it is written for a day (or a
          run), sealed, exported and eventually dropped ([W8]). A lease written at
          23:59:58 resolves after midnight, and the ledger keeps its key for
          key_lifetime_ms and its reconciliation entry for longer than that. Money
          rows in a rotating file have to be migrated forward at every rotation;
          money rows in their own file do not.
        * writer cadence. The log's writer is the ingest fold's writer -- batched,
          K <= 64, throughput-shaped (R48). The money writer is the route path's
          convoy -- latency-shaped, one row per dispatch. One connection cannot be
          both without one of them inheriting the other's cadence, which is the
          `shared_writer` breach in (c).

  (c) `shared_writer` -- the lease riding the log writer's own batch and
      connection -- is the placement ADR-0006 R48's wording invites if it is read
      as a file rule instead of an array rule. It is not measurable with CPython
      threads at the fleet pace without measuring the interpreter, so it is a
      MODEL with the constants taken from [W1] and (a):
        lease durable latency = the group-commit interval it waits for
          (up to 5 ms at the shipped cadence) + the batch's own
          fsync (467us at batch 64, [W1](a))
          = up to 5,467us, i.e. 2.7x the 2 ms budget
        before any device tail, and it inherits the checkpoint stall on top.
      Rejected. R48's one-writer-per-shard rule is about who mutates the posterior
      arrays; it says nothing about how many files a shard owns, and reading it as
      a file rule costs the money path its latency budget.

  (d) the convoy as a service: 4 dispatcher threads, own file, trace log at the
      fleet pace in parallel. This is the mechanism the ADR ships, end to end;
      the absolute rate is CPython's (see the caveat below), the amortisation is
      the device's.
         K       T  leases/s   fsyncs  leases/fsync      p50      p99  log rows/s
         8  0.25ms     4,812      922           2.2    654us   2041us      20,348
        32   0.5ms     4,823    1,000           2.0    670us   1822us      20,186
        32   2.0ms     1,509      500           4.0   2589us   3334us      13,835
        64   4.0ms       848      500           4.0   4650us   5543us      12,584

  Readings:
   * R71 fits the route path per lease (23% of the 2 ms budget at p50,
     35% at p99 on this device) and fits the fleet pace only with a convoy: K=1
     leaves 0.3x headroom here and negative headroom on a network-attached
     volume, so the group-commit service ships as the mechanism, not as an
     optimisation.
   * The money file is separate, and the reason is rotation and tail, not
     throughput: interference between the two writers measured inside this box's
     noise, the write-lock model costs ~0.5 ms of p99, and a checkpoint stall in
     a shared file costs 3-10 ms of p99.9. Money rows also must not live in a file
     that gets sealed and dropped on a schedule.
   * The convoy's K and T are config: K binds at the fleet pace, T binds at the
     20 TPS deployment, and one service does both. `leases/fsync` is the number to
     watch -- if it sits at 1.0 at peak, the money path is paying full price for
     durability and reopen trigger 1 is live.
   * CPython caveat, stated because it bounds the claim: the GIL serialises the
     Python side of (b) and (d), so those absolute rates carry interpreter overhead
     a Go engine would not pay -- a Go convoy parks goroutines in fsync without
     holding a global lock, and (a)'s curve, which is single-threaded, is the part
     that transfers unchanged in shape. What the GIL does not touch is the thing
     being measured: SQLite's one-write-lock-per-file and the device's fsync both
     run with the GIL released. #17 re-measures the absolutes in Go on the
     production device class.

====================================================================================================
[W3] crash semantics: what each configuration actually loses when the process is killed
====================================================================================================
  Every durability claim in this ADR is a statement about a crash, so the crash is
  measured rather than argued: a forked child opens the store, appends, and dies by
  SIGKILL -- no close, no atexit, no clean shutdown. The parent then reopens the
  file the way a restarting engine would and counts what is there.
    issued    rows the child handed to SQLite
    durable   rows in transactions that COMMITTED before the kill (what a correct
              store owes back -- an uncommitted transaction is not owed)
    recovered rows the reopened file returned;  integrity = PRAGMA quick_check

  configuration                              issued  durable  recovered  lost  integrity    reopen
  WAL + FULL (strict)                         2,000    2,000      2,000     0         ok     0.2ms
  WAL + NORMAL (batched)                      2,000    2,000      2,000     0         ok     0.2ms
  WAL + OFF (ephemeral)                       2,000    2,000      2,000     0         ok     0.2ms
  DELETE + FULL                               2,000    2,000      2,000     0         ok     0.3ms
  DELETE + OFF                                2,000    2,000      2,000     0         ok     0.2ms
  MEMORY journal + OFF                        2,000    2,000      2,000     0         ok     0.2ms
  WAL + FULL, killed mid-transaction          2,001    1,992      1,992     0         ok     0.2ms
  DELETE + OFF, killed mid-transaction        2,001    1,992      1,992     0         ok     0.2ms
  MEMORY + OFF, killed mid-transaction        2,001    1,992      1,992     0         ok     0.2ms
  WAL + FULL, killed mid-checkpoint           2,000    2,000      2,000     0         ok     0.2ms
  in-memory + checkpoint every 750, no log    2,000    1,500      1,500   500     no log     0.0ms

  Readings of (a):
   * Not one committed row is lost by any configuration on a process crash,
     including synchronous=OFF, because a process crash leaves the WAL (or the
     rollback journal) in the operating system's page cache and the operating
     system is still alive. That is why FULL-vs-NORMAL cannot be settled by
     killing processes: it is a POWER-loss question, which (b) addresses.
   * Killed mid-transaction, WAL+FULL returns exactly the committed prefix and
     rolls the open transaction back: durable 1,992 of 2,000 issued, recovered
     1,992, lost 0. That is atomicity measured, not assumed.
   * The mid-transaction kill is where the journal mode earns its name: with the
     rollback journal in MEMORY (or with synchronous=OFF under a rollback journal)
     the journal that would undo the partial transaction never reached a file. On
     this box the reopen still reports ok and the count is still the committed
     prefix -- the write was small enough to be inside one page -- but the
     guarantee is now the filesystem's, not SQLite's, which is exactly what
     SQLite's own documentation declines to promise for those settings. The
     engine refuses them for the log; [W1] shows what they would have bought
     (1.5x throughput at batch 64) and that is not a trade for an audit trail.
   * Killed mid-checkpoint, the file opens clean and the committed prefix is
     intact: a checkpoint is a copy plus a WAL reset, and both are recoverable.
     This matters because the checkpoint is the one place where the log and the
     database file are both being written.
   * `in-memory + checkpoint` -- the ticket's first model-state option -- loses
     every op since the last checkpoint (500 of 2,000 here) and has NO log to
     reconstruct them from. The loss is not only learning: those decisions have no
     audit row, so #15 cannot recompute their propensities and ADR-0004 6's
     one-record-per-decision claim has a hole in it. That is why this ADR keeps the
     log canonical and puts the in-memory arrays downstream of it (R47) instead of
     adopting the option as the ticket wrote it.

  (b) the power-loss emulation. A sandbox cannot pull the plug, so the plug is
      pulled by hand at the layer that a plug-pull actually exposes: the WAL file's
      tail. The child writes with wal_autocheckpoint=0 (nothing reaches the
      database file), the parent truncates the WAL to a fraction of its length, and
      the store is reopened. What this measures is (i) how much of the log lives
      only in the WAL, and (ii) whether a torn WAL is recoverable at all. A real
      power loss can also tear pages in the database file and reorder filesystem
      metadata; neither is emulated, and the claim below does not reach them.

       WAL kept   issued  recovered    lost  lost %  integrity    reopen
             0%    2,000         -1    2001  100.0% unqueryabl     0.1ms
               (the 0% row lost the WAL HEADER itself, so there is no frame
               chain to validate: unqueryable: no such table: op)
            10%    2,000        200    1800   90.0%         ok     0.1ms
            25%    2,000        544    1456   72.8%         ok     0.1ms
            50%    2,000      1,048     952   47.6%         ok     0.1ms
            75%    2,000      1,528     472   23.6%         ok     0.1ms
            90%    2,000      1,808     192    9.6%         ok     0.1ms
            99%    2,000      1,976      24    1.2%         ok     0.1ms
           100%    2,000      2,000       0    0.0%         ok     0.1ms
      A torn WAL tail is DISCARDED, not believed: every truncation point reopens
      `ok` and returns a prefix of the log, never a corrupt middle. The loss is
      linear in the bytes the device did not get, which is the whole content of the
      durability question -- so the design variable is not 'can SQLite recover'
      (it can, and this is the measurement) but 'how many bytes are un-fsynced at
      any instant', which is the pragma and the group commit.

  (c) the un-fsynced window, in rows and in seconds, at the shipped config. This
      is the number a durability guarantee is made of: synchronous=FULL fsyncs
      every commit, so the window is one group commit; synchronous=NORMAL fsyncs at
      the checkpoint, so the window is one checkpoint interval.
      pragma                   un-fsynced window      rows  s at fleet pace   s at 20 TPS
      WAL + FULL (strict)      one group commit         64          0.006s         2.5s
      WAL + NORMAL (batched)   14 checkpoints seen    4,863          0.423s       187.0s

      The shipped answer is the first row: at synchronous=FULL with a group commit
      of 64 rows the un-fsynced window is one batch -- 5.6 ms of traffic at the fleet pace,
      2.5 s at 20 TPS -- so an ack given after that commit is a durability claim
      that survives a power loss, which is what ADR-0011 R83 asks of the ingest path.
      NORMAL's window is
      the checkpoint interval, 4,863 rows here = 76x wider, and that cadence is
      a housekeeping parameter
      rather than a promise: it is what harness mode uses, where the artifact is
      reproducible from its scenario hash and losing it costs a re-run rather than
      an audit hole. Both files ship FULL; the batched class exists for the
      harness and for nothing that carries money.

====================================================================================================
[W4] the row layout: what a column costs, what a blob costs, and who can answer the query
====================================================================================================
  The ticket asks for a schema. A schema is a pricing exercise: every field is
  either a COLUMN (SQLite can filter and group on it, and pays B-tree width for it
  on every row) or inside a BLOB (free to SQLite, decoded by committed code,
  invisible to WHERE). Four layouts are priced on the same rows from the same
  scenario pool -- 200,000 decisions and their outcome ops -- at the shipped
  durability (WAL + synchronous=FULL, group commit 64 rows).
  Bytes are the file after a TRUNCATE checkpoint and ANALYZE, so they are the
  artifact's size, not the WAL's. Queries are the five the downstream tickets run,
  named by the ticket that needs them, and each is reported with SQLite's own plan.

  (a) write cost and artifact size
      layout                 rows   rows/s   cmt p50   cmt p99 bytes/row B/decision  file MB
      A columns + blobs   427,998  112,369     451us    2254us     177.6      380.1     76.0
      B blob only         427,998  133,777     315us    2002us     165.0      353.1     70.6
      C json text         427,998   56,848     335us    4115us     486.9     1041.9    208.4
      D no raw context    427,998  117,401     408us    2266us     155.7      333.2     66.6

      Against ADR-0008 [C4]'s 104 B in-engine decision record and its ~104 B/decision
      budget at k_max=5: layout A lands at 380 B/decision, which is the record
      plus the outcome ops it owns (1.14 ops/decision here) plus SQLite's
      B-tree overhead. The blob-only layout is
      1.08x narrower per row and the JSON layout is 2.74x wider;
      neither of those numbers is the decision, because the decision is what a
      query costs, and that is (b).

  (b) the five query shapes, priced per layout. 'impossible' means the layout
      cannot answer the query from this file at all -- a schema decision, not a slow
      query. 'engine touched' is the rows SQLite had to hand over; when it is the
      whole file, the layout has no way to reduce the work before the boundary.
      the file's clock spans 6,651s at the scenario's declared 30 TPS;
      Q2/Q3 use the first 831s of it, Q4/Q5 the whole file

      Q1
        #15/ingest idempotency probe, 1,000 (seq,attempt) lookups
        layout                 us each  answer groups  rows matched  engine touched
        A columns + blobs          5.4          1,000         1,000   index-bounded
        B blob only                5.4          1,000         1,000   index-bounded
        C json text                9.8          1,000         1,000   index-bounded
        D no raw context           5.1          1,000         1,000   index-bounded

      Q2
        ADR-0004 counter rebuild, 831s window
        layout                      ms  answer groups  rows matched  engine touched
        A columns + blobs         27.4             15        28,116   index-bounded
        B blob only              137.7             15        28,116         227,998
        C json text              365.4             15        28,116         227,998
        D no raw context          24.2             15        28,116   index-bounded

      Q3
        analyst slice: region+bin_class+band in the same window
        layout                      ms  answer groups  rows matched  engine touched
        A columns + blobs         10.8              3           629   index-bounded
        B blob only              138.9              3           629         227,998
        C json text              271.8              3           629         227,998
        D no raw context     IMPOSSIBLE              -             -               -

      Q4
        ADR-0006 R40 re-bucket: whole file, 7-key group by
        layout                      ms  answer groups  rows matched  engine touched
        A columns + blobs        384.4          2,032       227,998   index-bounded
        B blob only              214.8          2,032       227,998         227,998
        C json text              947.4          2,032       227,998         227,998
        D no raw context     IMPOSSIBLE              -             -               -

      Q5
        #15 OPE decode of the posterior array, 20,000 decisions
        layout                 us each  answer groups  rows matched  engine touched
        A columns + blobs          1.8         20,000        20,000   index-bounded
        B blob only                0.9         20,000        20,000   index-bounded
        C json text                7.2         20,000        20,000   index-bounded
        D no raw context           1.1         20,000        20,000   index-bounded

      IMPOSSIBLE = Q3: region/bin_class/band are not in this file
      IMPOSSIBLE = Q4: the arm key is not reconstructible from this file
      The plan behind 'engine touched', for Q2 -- reported because the difference
      is a plan, not a mood:
        A columns + blobs    SEARCH op USING INDEX op_ms (ms>? AND ms<?); USE TEMP B-TREE FOR G
        B blob only          SCAN op
        C json text          SCAN op
        D no raw context     SEARCH op USING INDEX op_ms (ms>? AND ms<?); USE TEMP B-TREE FOR G

  (c) what those per-row costs become at the fleet's own pace. Every number here is
      arithmetic on the measured per-row costs above and the write profile this ADR
      is sized against (5,000 decisions/s, 1.3 attempts each, 11,500 log rows/s).
      a one-hour window at the fleet pace holds 41.4M rows; a day holds 994M
      layout                 Q2 one-hour window         Q3 slice   Q4 whole-day re-bucket
      A columns + blobs                     40s              16s                226.4 min
      B blob only                          600s             605s                 15.6 min
      C json text                        1,592s           1,185s                 68.8 min
      D no raw context                      36s       impossible               impossible
      (Each cell is measured-seconds / rows-the-engine-touched x rows-it-would-touch
       at the fleet pace. A full-file scan's denominator is the file, so it scales
       with the DAY; an index-bounded probe touches only the window, so it scales
       with the WINDOW -- which is the whole difference between a store you can ask
       questions of and a tape. Q3's selectivity does not appear: the engine pays for
       the rows it examines, not the rows it returns.)

  Readings:
   * Q1 is the cheapest place to see the layout's tax and it is small: a point probe
     costs 5.4 us on the column layout, 5.4 us with a blob decode on top and
     9.8 us with a document parse. At the ingest path's pace (one probe per redelivered
     outcome, R44) that difference is inside the noise of the durable write it
     precedes. Q1 alone would not decide anything.
   * Q2 and Q3 decide it. The blob layout has no ms column, so a time window becomes
     a full-file walk with a decode per row: 138 ms and 227,998 rows handed over, against
     27 ms for the column layout's index-bounded probe. The JSON layout asks SQLite
     to parse every document in the file to evaluate its own WHERE clause:
     365 ms, 13x the column layout.
     At the fleet pace those become the minutes in (c). The bytes the column layout
     spends -- 13 B/row more than the blob layout -- are bought back on the first
     windowed query.
   * Q3 is why raw context rides every outcome row. Layout D is the shipped layout
     minus six context columns: it is the narrowest file here AND IT CANNOT ANSWER
     THE QUERY, because the arm a row belongs to is a function of fields the row no
     longer carries. ADR-0006 R40's promise -- re-bucketing is a re-fold, not a cold
     start -- is a schema commitment, and six int32 columns per row are its price.
   * Q4 is the counter-evidence, and it is recorded rather than argued away: over the
     WHOLE file, a 7-key group-by is SLOWER in SQLite than decoding the same rows in
     CPython (384 ms vs 215 ms), because SQLite builds a temp B-tree per group while the
     Python loop builds a dict. The column layout's advantage on that shape is
     expressibility, not speed, and the honest response is not to add an index for a
     7-column key -- it is to run whole-file high-cardinality aggregation in the
     columnar tier, which is [W5]'s subject and the reason the export exists.
   * Q5 is the other half of R89 and it points the opposite way: the posterior array
     decodes at 1.80 us/row as a fixed f32 blob and 7.18 us/row as 20 JSON numbers --
     4.0x. An ARRAY is not a set of columns; making it one costs a parse per row
     read and buys nothing, because no query filters on 'the third eligible arm's
     alpha'. Scalars a query names are columns, arrays are blobs, and both halves of
     that rule are measurements now.
   * Layout C is rejected on all three counts at once: 2.7x the bytes, 2.3x the
     write cost, and the worst time on every query it can answer at all. JSON is the
     right shape for an interface and the wrong shape for a store; where this design
     needs self-description it puts a fixed-layout blob next to a version byte and a
     committed decoder in analysis/ (R60's rule, ADR-0008).
   * Layout B is not rejected on throughput -- it writes slightly faster than A and is
     the narrowest file that can still answer Q1. It is rejected on Q2/Q3: a store
     whose rows SQLite cannot filter is a tape, and the tape's advantage evaporates
     the first time someone asks a question nobody predicted. That is the actual
     content of R89.

====================================================================================================
[W5] the analysis tier: what a columnar export buys over the SQLite row store
====================================================================================================
  ADR-0011's DAG gives trace/ the only write path into learned state and puts no
  Parquet writer inside the engine. What it leaves open is whether the analysis
  tier exports at all, and what an export earns. Parquet itself cannot be measured
  here (no pyarrow, no DuckDB, no Arrow C++ in this sandbox), so the MECHANISM is
  measured instead: row groups, typed column chunks, per-chunk zlib, and a footer
  carrying per-chunk min/max so a predicate can skip chunks it does not need to
  read. That mechanism -- not the file format's brand -- is where Parquet's
  numbers come from, and it is ~150 lines of stdlib below. The bytes, the
  compression ratios, the skip ratios and the scan times are measured; anything
  said about pyarrow or DuckDB is a labelled model on cited numbers.

  (a) export cost, measured on the rows the engine actually wrote
      427,998 log rows written to SQLite in 4.3s (99,761 rows/s), file 76.0 MB (178 B/row)
      227,998 op rows exported in 2.3s (100,469 rows/s), columnar file 3.5 MB (15 B/row)
      uncompressed column bytes 24.6 MB -> compressed 3.5 MB = 7.11x with zlib level 6
      the columnar file is 21.68x smaller than the SQLite row store holding the same op rows
      (14 row groups of <= 16,384 rows; footer 31,024 B)

      per-column cost -- this is where the ratio comes from, and it is the reason a
      columnar artifact is a compression win and not a re-encoding win:
      column           kind  raw B/row  comp B/row   ratio
      amount_minor      i64       8.00        2.44   3.28x
      ms                i64       8.00        2.05   3.91x
      settled_ms        i64       8.00        2.04   3.93x
      latency_ms        i32       4.00        1.71   2.34x
      op_seq            i64       8.00        1.52   5.28x
      seq               i64       8.00        1.42   5.63x
      ...                                                 
      kind              i32       4.00        0.01 743.27x
      boot_id           i32       4.00        0.01 743.27x
      mandate           i32       4.00        0.06  63.86x
      Read the two ends of that table, not the middle. The best-compressing columns
      are the near-constant ones (kind, boot_id) and the two-valued ones (mandate,
      sca): zlib on a run of identical int32s is nearly free, which is the same
      property a real Parquet writer exploits with RLE and dictionary encoding
      BEFORE it compresses -- so this ratio is a floor on what Parquet + zstd would
      do, not an estimate of it. The worst are the high-entropy numerics
      (amount_minor, ms, settled_ms) at 3-4x, and they are the columns that carry the
      information; a delta or dictionary encoding on the two timestamp columns would
      take them well past this, and that is a real format's job, not this one's.
      Disclosure: the row pool (100,000 decisions) is cycled 2x to
      reach this n, so the VALUE sequence repeats and every compression ratio
      here is flattered by it. At --smoke (pool == n, one cycle) the same export
      measures ~7.1x zlib and ~21x against the row store instead of the numbers
      above; the honest figure is the uncycled one and the ADR quotes that.

  (b) the query that decides the tier: a windowed aggregate over the ops. This is
      ADR-0004's counter rebuild and the dashboard's shape, and it is the query where
      a row store's index and a columnar file's min/max footer are doing the same
      job by different means.
      the window is the first 831s of the file's 6,651s clock span (28,116 of 227,998 op rows)
      path                                  rows matched  groups opened   seconds      rows/s
      SQLite: op_ms index + group by              28,116              -    0.0201           -
      columnar: every row group opened            28,116             14    0.0252   1,116,841
      columnar: min/max pruning                   28,116              2    0.0042   6,709,329
      pruning opened 2 of 14 row groups and skipped 12 (86%);
      all three agree on the answer (True), which is the correctness check on both readers
      projected to a fleet day at 65,536-row groups: 15,161 groups in the file,
      632 in a one-hour window, so 95.8% of the file is never opened.
      That projection is arithmetic on the measured skip, and it is the reason the
      footer exists: the skip ratio is a property of the DATA's time ordering, not
      of the format, and it only works because trace/ appends in arrival order.

  (c) the whole-file aggregate: the OPE-style pass that touches every row and a
      handful of columns. This is #15's shape and the one a row store is worst at,
      because it must walk B-tree pages holding columns it does not want.
      path                                groups   seconds      rows/s  B/row read
      SQLite full scan + group by              -     0.133   3,208,018         178
      columnar read (3 of 22 columns)         14     0.011  21,653,714        1.17
      columnar read + Python aggregate        14     0.056   4,041,538            
      The two columnar rows separate what the FORMAT buys (reading 3 typed columns
      instead of 22 interleaved ones: the compressed bytes it must touch are a
      fraction of the row store's) from what the LANGUAGE costs (aggregating in
      CPython is slower than aggregating in SQLite's C loop, and that gap is
      exactly what a vectorized engine closes). DuckDB or pyarrow reading the same
      layout would keep the format's win and drop the interpreter's loss; this
      spike cannot measure that, and says so.

  Readings:
   * The export costs 100,469 rows/s in this interpreter, i.e. 5.3 us/row,
     against a write path that sustains
     99,761 rows/s. Export is therefore an OFFLINE step (a sealed
     partition file is exported once, after the run or after the day closes), never
     an in-path one -- which is the same conclusion ADR-0011's DAG reached without
     these numbers, and now has them.
   * The columnar artifact is 21.68x smaller than the row store for the same op rows. Over the
     retention window that is the difference between an artifact a team can keep on
     one volume and one it has to shard; [W8] turns this into bytes/day.
   * Pruning is the mechanism that matters most and it is free once the footer
     exists: a one-hour window in a multi-day file opens only the row groups whose
     min/max overlap it. The SQLite path answers the same query from an index and
     is competitive at this size; the columnar path wins when the query touches
     few columns over many rows, and when the file is sealed and cold.
   * So the format decision is not 'SQLite or Parquet'. It is: SQLite is the log
     and the queryable hot store (W1-W4, W6-W10), and the columnar export is the
     cold analytics artifact, written once per sealed partition. The engine writes
     no Parquet (ADR-0011); the export is a batch step owned by analysis/, and its
     column list is the EXPORT_COLUMNS table above -- the same names, the same
     ordinals, the same scenario_hash, so a row means the same thing in both tiers.
   * Counter-evidence, recorded: if the only consumer were #15's OPE pass over a
     single run, the SQLite file with a covering index would be enough and the
     export would be pure cost. The export is justified by the retention window
     (400 days of sealed partitions is where columnar compression pays for itself)
     and by consumers this repo does not own yet. If neither materialises, drop
     the export and keep the schema.

====================================================================================================
[W6] scale: a 1M-transaction run with the store attached, and what the store actually costs
====================================================================================================
  One file, one writer, the shipped configuration (WAL, synchronous=FULL, group
  commit 64 rows / 5 ms, autocheckpoint 1000 pages), 200,000 decisions from the
  committed scenario and their 1.140 attempts each. The run is segmented so that the rate at
  the END of the file is reported next to the rate at the beginning: that is the
  difference between 'SQLite can do this' and 'SQLite can do this for a day'.

  (a) the run, by segment. `through` is the decision count reached; the rate is
      that segment's, not the cumulative one.
       through   rows/seg   seg s    rows/s      dec/s   cmt p50   cmt p99     worst      MB  B/row
        10,000     21,197    0.21    99,670     46,575     438us    2696us    3.58ms     7.4  351.3
        20,000     21,357    0.17   125,368     58,583     417us    2010us    2.54ms    11.4  184.1
        50,000     64,245    0.52   122,637     57,307     422us    2107us    3.00ms    23.1  182.5
       100,000    107,200    0.92   116,335     54,362     439us    2149us    2.79ms    41.7  173.9
       200,000    213,999    1.89   113,324     52,955     457us    2204us    3.10ms    79.7  177.2

      whole run: 427,998 log rows (200,000 decisions + 227,998 ops) in 3.7s
      = 115,118 rows/s, 53,794 decisions/s
      file 76.1 MB = 177.7 B/row = 380.3 B/decision; 6,689 commits, 3.3s of them (89% of wall)
      first segment 99,670 rows/s vs last 113,324 rows/s
      = 1.14x -- the answer to 'does it degrade as the file grows'

  (b) where the wall time goes. Three buckets, all measured on this run:
      bucket                                         seconds   share
      SQLite commit (BEGIN..COMMIT, incl. fsync)                    3.30     89%
      append minus commit (tuple build, executemany, B-tree)        0.17      5%
      the loop feeding it (row tuples from the pool)                0.25      7%
      The timer around the store's own calls encloses the commit, so the commit is
      subtracted from it rather than counted twice; the remainder is the generator
      loop, which here reads pre-built rows out of a pool (the world model's own
      cost is priced separately in (c) -- it is not free, and it is not the store's).
      The commit bucket is the one a faster device shrinks, and [W2]'s slow-device
      model is how far it can move: on a device with a 1-5 ms fsync the commit
      bucket grows by roughly that factor over the 443 us measured here, which at the shipped
      batch of 64 rows is still 12,800 rows/s of ceiling on one shard.

  (c) what the store costs the harness. ADR-0005's harness runs the world; #13's
      store hangs off it. The two are timed separately -- the world by building a
      fresh pool of decisions through the acquirer models, the store by (a) -- and
      then added, because in this spike the world's rows are pre-built and a single
      loop cannot be split mid-flight. In the Go engine they are one loop; the
      per-decision costs are what transfer, not the interpreter's.
      bucket                                        per decision   decisions/s
      world model alone (fresh pool, 50,000 dec)          38.9us        25,727
      store alone (115,118 rows/s, 2.14 rows/dec)         18.6us        53,794
      world + store, summed                               57.5us        17,404
      the store adds 48% to the harness's per-decision cost in this interpreter, and
      a run of 200,000 decisions costs 11s with the store against 8s without it
      Both sides are CPython here, so the ratio is the transferable part: a Go world
      model is faster per decision, which makes the store's share LARGER, and the
      store's own ceiling is set by fsync ([W1]/[W2]) rather than by the language.
      The reading is that the store is not free to a simulation run and never hides
      behind the world model -- which is why [W1]'s batch size and [W8]'s partition
      size are harness-facing knobs and not just production ones.

  (d) the shard arithmetic. One writer per shard (ADR-0006 R48), so the fleet's
      pace is met by adding shards, not by making one faster.
      quantity                                                      value
      fleet pace to sustain (ADR-0001)                      11,500 rows/s
      one shard, measured here                             115,118 rows/s
      headroom per shard                                            10.0x
      shards needed at the fleet pace                                   1
      one day of fleet traffic                                 0.99G rows
      one day on one shard, at this rate                            2.4 h
      one day's file, at this B/row                                177 GB
      1M transactions, rows                                         2.14M
      1M transactions, wall time on one shard                        19 s
      1M transactions, artifact                                    380 MB
      fleet pace incl. AUTH_RECORD rows (R72, share 0.29)   13,376 rows/s
      headroom per shard at that pace                                8.6x
      one day of fleet traffic at that pace                    1.16G rows
      one day's file at that pace                                  205 GB
      The profile above counts decision + outcome rows, which is what this spike
      writes. AUTH_RECORD v1 (ADR-0010 R72) is one more narrow row per attempt whose
      flow touched an authentication step, so the last four lines price it at the
      world's own measured share instead of leaving it out of the sizing. The shipped
      schema gives that record a table of its own (ADR-0012 3.4) rather than a payload
      blob, on [W4]'s columns-vs-blob evidence; the mechanism it exercises -- one more
      row in the same file, the same transaction, the same durability class -- is what
      [W1] and [W4] priced, and #17 re-measures the shipped schema in Go.

  Readings:
   * Yes, on throughput: one shard sustains 115,118 log rows/s against the 11,500 rows/s
     the fleet is budgeted for -- 10.0x headroom -- and a 1M-transaction run is a
     19-second job on one shard, not an overnight one. That is the
     ticket's fourth consideration, answered for the harness case, which is the case
     the ticket asked about.
   * The rate does not fall off a cliff as the file grows (1.14x first segment to last), which is
     the part of the claim that a 10k-row benchmark cannot make: at these sizes the
     B-tree is 3-4 levels deep either way and the write path is append-mostly. It is
     not a claim about a 400-day file; [W8] prices that one and it is why partitions
     are per-run and per-day rather than one file forever.
   * The commit bucket is 89% of wall time at 443 us per commit, so the store is
     I/O-bound on fsync and not CPU-bound on SQLite -- which is exactly the shape
     that makes the batch size the lever ([W1], [W2]) and the device the risk
     ([W2]'s slow-device model). A Go driver's per-call overhead lands in the append
     bucket, not this one, which is why #17 can choose it later.
   * Against the harness, in this interpreter, the store costs about as much per
     decision as the world model does (19 us vs 39 us), so a run with the
     store attached is roughly twice the wall time of the same run without it. That
     is the honest cost of an audit trail in a simulation, and it is affordable
     because the absolute numbers are small: 200,000 decisions in 11s. In Go the world
     side gets faster and the store side stays fsync-bound, so expect the store's
     share to grow, not shrink; the headroom in (d) is a floor, not a forecast.

====================================================================================================
[W7] boot: what a restart costs, and what the snapshot has to carry
====================================================================================================
  The log is the only writer of learned state (ADR-0006 R47), so a restart is a
  fold. A fold from op zero is O(history) and history is 400 days, so boot is the
  newest snapshot plus the ops after it. This section measures that boot as a
  function of the tail, prices the snapshot write that makes it possible, and then
  checks the part that is easy to get wrong: whether the snapshot actually carries
  every derived quantity, and whether snapshot+tail is bit-identical to folding
  from zero. Every boot here is a fresh connection against a closed file -- the same
  thing a restarting process does.

  (a) the tail-length curve. One file per tail length, all written the same way;
      the last one carries a snapshot at the fold point, the others do not, so the
      tail is the whole file. `cold fold` is boot with no snapshot at all.
         rows written      open   snapshot       fold  boot total   fold rows/s  digest == live
                1,000     0.3ms      0.0ms      1.0ms       1.3ms       528,491             yes
                2,000     0.2ms      0.0ms      2.0ms       2.2ms       535,458             yes
               10,000     0.3ms      0.0ms      9.5ms       9.9ms       554,855             yes
               50,000     0.3ms      0.0ms     48.5ms      48.8ms       547,323             yes
              200,000     0.3ms      0.0ms    211.9ms     212.3ms       502,928             yes

      a cold fold of 200,000 op rows costs 212 ms = 502,928 rows/s, and reproduces the live state's
      digest exactly (afd706883b5e6260...). Boot is therefore correct before it is fast;
      the rest of this section is about fast.

  (b) snapshots: what they cost to write and what they save at boot. The cadence is
      the knob -- too often and the writer pays for a 135 KiB blob plus an fsync,
      too rarely and every restart re-folds the world.
       snapshot every  snapshots  write p50  write max  tail at boot  boot total   file MB
               10,000         19      0.8ms      1.2ms         8,260      15.5ms      39.1
               50,000          3      1.0ms      1.0ms        41,647      76.2ms      40.9
              100,000          1      1.0ms      1.0ms        83,495     157.8ms      45.8

      Each row is a real trade: a shorter cadence folds less at boot and pays more
      135 KiB blob writes while running. The tail measured here is the ragged half-
      cadence of traffic a crash interrupts, which is the honest boot case (a clean
      shutdown snapshots on the way out and boots with an empty tail).
      the cheapest boot here is a snapshot every 10,000 op rows: 15.5 ms total against
      212 ms for a cold fold of the same file -- 14x faster, for
      0.8 ms of writer time per snapshot, 0.76% of this run's 2.0s wall time,
      and at the fleet pace one snapshot per 1 s of traffic (0.091% of the writer's time).
      Projected to a fleet day (994M rows), the same cadence means a snapshot every
      1 s of traffic and a boot tail of at most 10,000 rows = 20 ms of folding.

  (c) what the snapshot has to carry. This is the part that is a schema decision
      and not a tuning one: anything derived that is not IN the snapshot must be
      re-folded from op zero, and the spike can only find that out by comparing.
      derived quantity                     fold from zero  snapshot + tail     snap, no tallies
      posterior digest (4,320 arms x 4 f64) afd706883b5e6260 afd706883b5e6260     afd706883b5e6260
      ops folded                                  106,564          106,564                    0
      settled outcomes (alpha+beta mass)           97,438           97,438                    0
      drift resets folded                               3                3                    0
      arms with any mass                              734              734                  734
      The posterior digest survives a snapshot because the array IS the snapshot.
      The four derived integers do not survive unless they are packed into it -- the
      right-hand column is a boot from the same file with the tally blob absent, and
      it reports the ops folded SINCE THE SNAPSHOT as if they were the whole history.
      That is a silent wrong answer, not a crash, which is why R92 makes the
      snapshot's payload explicit: posterior array, detector buckets, the counter
      arena, and the fold tallies, all in one row, all under one checksum.

  (d) re-bucketing as a re-fold (ADR-0006 R40). The log carries raw context on
      every outcome row precisely so that a DIFFERENT arm space can be folded from
      the same bytes. Here the same file is folded into a coarser space -- 4 amount
      bands instead of 6, region folded into EEA/UK/rest -- and the result is
      checked against the conservation property that matters: total alpha+beta mass
      is unchanged, because re-bucketing moves evidence between arms, it does not
      create or destroy any.
      arm space                               arms outcome rows dropped alpha+beta mass  fold s
      committed space (6 bands x 5 regions)   4,320      106,561       0        97,438.0    0.20
      coarse re-bucket (3 bands x 3 regions)   1,296      106,561       0        97,438.0    0.19
      mass conserved: yes -- 97,438.0 vs 97,438.0
      the re-fold costs 0.19s for 106,561 rows (559,164 rows/s) against a cold start,
      which is the difference
      between changing the arm space in an afternoon and re-running the fleet.

  (e) the two failure modes boot has to survive, tested rather than assumed:
      a snapshot with one flipped byte: boot REFUSED (snapshot checksum mismatch)
      The checksum is over the blob, so a corrupted snapshot is refused rather than
      believed; the engine then falls back to a cold fold, which is slow and
      correct. The alternative -- booting from a snapshot whose digest does not
      match -- is a router that has silently forgotten part of its history, and
      there is no test that catches it downstream.
      the same file cold-folded anyway: 199 ms, digest afd706883b5e6260..., integrity skipped

  Readings:
   * Boot is dominated by the tail fold at 502,928 rows/s in this interpreter, and the tail is
     a design variable: snapshot cadence. The cadence that minimises boot here is
     one snapshot per 10,000 op rows, which is one per 1 s of fleet traffic.
   * A cold fold is not a disaster at spike scale (0.2 s for 200,000 rows)
     and is a disaster at retention scale
     (994M rows/day x 400 days at this rate is 220 hours). Snapshots are not an optimisation;
     they are what makes the retention window in ADR-0004 bootable. They are also
     why partitions are per-run and per-day: a boot only ever folds one partition's
     tail, never the chain.
   * The snapshot's payload is a schema commitment and the spike found the hole in
     it by comparing: the posterior array survives, the derived integers do not
     unless they are packed in. R92 names the payload; (c) is the evidence.
   * Snapshot+tail is bit-identical to a fold from zero on the same file, which is
     the property that makes 'the log is the source of truth' operational rather
     than rhetorical: any snapshot can be discarded and the answer does not change.
   * Re-bucketing the arm space is a re-fold of the same bytes at
     559,164 rows/s with mass conserved exactly. That is R40's promise, priced.

====================================================================================================
[W8] retention: what 400 days of this costs, what the audit chain costs, and how a day is deleted
====================================================================================================

  (a) the retention ladder, in bytes. Two paces because the repo quotes two: the
      merchant scenario's declared 30 TPS and ADR-0001's fleet budget of 5,000 decisions/s.
                                           at scenario pace      at fleet pace
      decisions/day                                  2.60M              432M
      log rows/day                                    5.6M              994M
      SQLite row store, one day                     0.99GB             177GB
      SQLite row store, 400 days                   395.2GB          70.62TB
      columnar export, one day                      0.05GB             8.1GB
      columnar export, 400 days                     18.2GB           3.26TB
      learned state (snapshots, all shards)            0.14MB            0.14MB
      largest single FILE in the ladder             0.99GB            2.04GB
      The last row is the one that matters operationally: the fleet-pace column is
      the WHOLE fleet's budget (ADR-0001), and the partition rule is one file per
      (shard, day), so no single file is the 175 GB in the row above -- it is one
      shard's day, and a shard is sized by the writer, not by the fleet.
      measured 177.7 B/row in the row store, and the columnar ratio 21.7x from [W5] (this run)
      The learned state is 135 KiB per shard no matter how long the chain is, which
      is the point of the fold: history is bytes on a volume, state is a fixed-size
      array in memory. Retention is therefore a STORAGE question with a deletion
      mechanism, not a state question -- and (c) is the deletion mechanism.

  (b) the audit hash chain (ADR-0004 6): each decision's audit_hash covers the
      previous one, so a row cannot be removed, edited or reordered without every
      later hash changing. Priced on the write path and on the auditor's pass.
      write path                             rows/s    B/row   file MB
      per-decision hash, unchained          114,207    177.3      37.9
      chained (sha256 over prev+row+seq)     91,269    177.3      37.9
      the chain costs 20.1% of write throughput and +0.0 B/row (the hash is 32 B either way;
      chaining only changes what is hashed, not what is stored)
      auditor's pass: 100,000 decisions verified in 0.51s = 194,561 rows/s, head c4537ae58909f93b
      one day at the fleet pace (432M decisions) verifies in
      37 min single-passed; at the scenario pace,
      13.4 s. The chain is affordable to audit because it is a scan, not a join.
      tamper test: one column of one decision edited mid-file (amount_minor + 1)
      -> the verifier stopped at seq 50000 after 50,001 links (detected)
      Detection is a property of the chain, not of the store: SQLite would happily
      return the edited row. The chain is what makes 'the log is the audit trail'
      mean something, and this is the measurement that it does.

  (c) how a day dies. Two mechanisms, and only one is O(1):
      `DELETE FROM` on a partition's rows, and dropping the partition file. Both are
      run on a real file with three days of rows in it.
      the file: 3 days x 33,333 decisions = 213,321 rows, 38.4 MB
      mechanism                                  seconds   file after   reclaimed
      DELETE FROM ... WHERE day = 0                0.091       38.4MB        0.0%
      ... then VACUUM                              0.112       25.5MB       33.6%
      drop the partition file (unlink)            0.0000        0.0MB      100.0%
      rows deleted: 33,333 decisions + 37,774 ops; the surviving day still answers a
      window query in 1.3 ms (37,774 rows)
      DELETE FROM does not give the bytes back -- SQLite frees pages for reuse, and
      only VACUUM rewrites the file, which takes a lock, needs room for a second
      copy, and costs more than the delete it cleans up after. Dropping a partition
      file is an unlink: constant time, all the bytes back, nothing to vacuum, and
      no chance of a long-running DELETE stalling the writer. That is the whole
      argument for one file per (shard, run) and per (shard, day) rather than one
      file forever, and it is why R91 makes the partition the retention unit.

  (d) the ladder that follows from (a) and (c), stated as the mechanism:
      tier                     contents                             lifetime             expiry
      hot                      today's trace.sqlite, open, WAL         1 day   roll at midnight
      warm                     sealed per-day partition files          400 d    unlink the file
      cold                     columnar export of sealed days          400 d    unlink the file
      state                    snapshots + the tail since            forever  the next snapshot
      Nothing in the ladder needs a DELETE. The learned state is a fold over the
      hot tier plus the newest snapshot, so expiring a warm partition never changes
      the router's behaviour -- it changes what an auditor can ask about, which is
      exactly what a retention window is for.

  Readings:
   * At the fleet pace the row store is 177 GB/day and 70.6 TB over the retention window;
     the columnar export of the same rows is 8 GB/day and 3.26 TB. At the
     scenario's own 30 TPS it is 0.99 GB/day and 395 GB for the window -- which is a
     single volume, and is why the retention decision is a partition-and-unlink
     decision rather than a database decision.
   * The audit chain costs 20.1% of write throughput and 194,561 rows/s to verify. Both are cheap
     enough that the chain is not a trade-off: it is what makes the log an audit
     trail, and the tamper test detects the edit at the row where it happened.
   * Expiry is an unlink, not a DELETE, and (c) is the measurement: DELETE leaves the
     file the same size, VACUUM costs more than the delete and needs a second copy's
     worth of room, and neither is O(1). One file per (shard, day) is what makes
     400 days administrable.
   * Counter-evidence, recorded: partitioning by day means a query that spans days
     must open several files. (d)'s warm tier is the answer -- SQLite ATTACH over a
     handful of sealed files is a few hundred microseconds of open cost, and the
     queries that span the whole window belong in the columnar tier anyway, where
     [W5]'s pruning makes the span cheap. If a deployment needs one queryable file
     across the window, the partition size is the knob, not the mechanism.

====================================================================================================
[W9] dedupe: the cost of exactly-once learning, and the redelivery attack on it
====================================================================================================
  (a) what the enforcement costs. The shipped schema carries
      `CREATE UNIQUE INDEX op_dedupe ON op(seq, attempt) WHERE kind = OUTCOME` --
      partial, so control ops (drift resets, snapshots, prior swaps) are not
      constrained by a key that means nothing for them. Same rows, same durability,
      with and without the index.
      schema                                 rows/s    B/row  append p50  append p99    op rows
      with the partial unique index         101,020    177.3      0.42us     475.7us    113,999
      without it                            116,982    169.7      0.40us     410.9us    113,999
      the index costs 13.6% of write throughput and +7.6 B/row. That is the price of R44,
      and it is the cheapest place to pay it: enforcing exactly-once anywhere else
      means a read before every write, which costs more than an index does.

  (b) the redelivery attack. One file, written through the shipped path, then hit
      with the four ways a real ingest sees the same outcome twice: in order, out
      of order, with a different latency on the retry, and -- the case that matters
      -- with a CONFLICTING outcome for the same (seq, attempt).
      delivery                                       attempts  rows added  op rows now
      first delivery (the truth)                      113,999     113,999      113,999
      full redelivery, in order                       113,999           0      113,999
      30% redelivered out of order, mutated            34,199           0      113,999
      200 redelivered with a CONFLICTING outcome          200           0      113,999
      duplicate (seq, attempt) outcome rows in the file: 0
      cost of a redelivery on the ingest path: 5.84 us per ignored insert
      (113,999 of them in 0.67s, one commit)
      dropped-conflict counter (the metric R93 asks for): 200 of 200
      -- free, it is `changes()` on the insert
      fold digest after the first delivery: 6f7533725b1f68edf318e2f1
      fold digest after 148,398 redeliveries: 6f7533725b1f68edf318e2f1
      identical: YES

      What the conflicting case actually does, because 'ignore' is a policy and not
      a law of physics: the FIRST outcome wins and the later one is dropped on the
      floor by the index. That is the right default for a retry of the same attempt
      (the first delivery is the one the money moved on), and it is the wrong
      default for a corrected outcome -- so the correction has to arrive as a
      distinct op kind with its own row (a LATE op, priced in ADR-0009), never as a
      second OUTCOME for the same key. The count of dropped conflicts is the metric
      to alert on, and it is free: it is `changes()` on the insert.

  Readings:
   * Exactly-once learning costs 13.6% of write throughput and
     +7.6 B/row, enforced in the store by a partial unique index. It survives a full
     in-order redelivery, a shuffled 30%
     with mutated timings, and 200 conflicting outcomes: the row count does not
     move and the fold digest is bit-identical.
   * The index is partial (`WHERE kind = OUTCOME`) and that is not a detail: control
     ops have no (seq, attempt) meaning, and a full unique index would either reject
     them or force a fake key into the row. The partial index also keeps the index
     smaller than the table, which is why the byte cost is what it is.
   * The policy inside 'OR IGNORE' has to be written down, because the store cannot
     know which delivery is true: first write wins, corrections are a separate op
     kind, and the dropped-conflict count is a metric. R93 is that sentence.
   * Counter-evidence, recorded: an ingest that must distinguish 'already learned'
     from 'conflicting redelivery' pays a read for the distinction. This design does
     not pay it in the write path; it counts the conflicts and lets an offline pass
     decide, which is affordable only because the log keeps every row that WAS
     accepted and the conflict is visible in the ingest's own counters.

====================================================================================================
[W10] readers against the writer: WAL concurrency in processes, and the pinned-WAL hazard
====================================================================================================
  WAL promises that readers do not block the writer and the writer does not block
  readers. That promise is worth measuring rather than quoting, because the
  consumers of this file are not hypothetical: the dashboard, #15's OPE pass,
  ADR-0004's counter rebuild and an auditor's chain walk all read a file that
  trace/ is writing. Readers here are separate PROCESSES with their own
  connections -- which is what a dashboard is -- and not threads, because a CPython
  thread benchmark on 2 vCPU measures the interpreter's lock rather than SQLite's
  ([W2]'s lesson).

  the file is warmed with 200,000 decisions first, so the readers have something to read and the
  writer is appending to a realistic-size B-tree rather than an empty one.
  427,998 rows, 76.0 MB. Each phase below runs the writer for 4s with the
  readers named in the row.

  (a) the writer's commit latency with readers present
      phase                   rows/s  commits   cmt p50   cmt p99     worst   WAL peak
      writer alone            83,997    5,254     519us    1014us     8.1ms      0.5MB
      +4 idle (control)       76,436    4,787     569us    1087us     3.0ms      0.9MB
      +2 point probes         49,635    3,107     571us    8391us    12.1ms      5.7MB
      +4 mixed readers        28,358    1,777     968us   11117us    19.2ms    117.4MB
      +1 held transaction     43,308    2,711    1291us    2181us    17.8ms    180.2MB

      +4 idle (control)                    writer p50 +10%, p99 +7%, throughput -9%
      +2 point probes                      writer p50 +10%, p99 +727%, throughput -41%
      +4 mixed readers                     writer p50 +87%, p99 +996%, throughput -66%
      +1 held transaction                  writer p50 +149%, p99 +115%, throughput -48%

  (b) what the readers saw, from their own processes
      phase                reader       reads        p50        p99     worst  errors
      +4 idle (control)    idle             0      0.0us      0.0us     0.0ms       0
      +4 idle (control)    idle             0      0.0us      0.0us     0.0ms       0
      +4 idle (control)    idle             0      0.0us      0.0us     0.0ms       0
      +4 idle (control)    idle             0      0.0us      0.0us     0.0ms       0
      +2 point probes      point      217,332     12.5us    162.4us     2.5ms       0
      +2 point probes      point      234,059     12.4us    103.2us    13.9ms       0
      +4 mixed readers     point      116,979     12.5us    118.7us    25.2ms       0
      +4 mixed readers     window         268     15.1ms     42.0ms    45.1ms       0
      +4 mixed readers     scan            69     64.2ms    104.3ms   104.3ms       0
      +4 mixed readers     point      128,279     12.4us    115.5us    20.7ms       0
      +1 held transaction  hold             3   1507.8ms   1510.9ms  1510.9ms       0

  (c) the hazard: a reader that holds its transaction open. WAL cannot reclaim
      frames any open reader might still need, so the WAL grows for as long as the
      reader holds, and a checkpoint that runs meanwhile comes back having stopped
      short -- silently, because PASSIVE does not report that as busy.
      phase                  tries  stopped short  frames behind   WAL peak  WAL after
      writer alone             786              0              0      0.5MB      0.0MB
      +4 idle (control)        716              1            100      0.9MB      0.0MB
      +2 point probes          464            240             27      5.7MB      0.0MB
      +4 mixed readers         266            266          1,220    117.4MB      0.0MB
      +1 held transaction      405            405         18,064    180.2MB      0.0MB
      `stopped short` is a PASSIVE checkpoint that returned with frames still in the
      WAL because an open reader needs them; `frames behind` is the worst such gap.
      A passive checkpoint never reports itself busy for that, which is why the WAL
      peak is the number to watch and not the pragma's return code.
      with one reader holding a read transaction for 1,511 ms at a time, 405 of
      405 passive checkpoints stopped short, the worst gap was 18,064 WAL frames, and
      the WAL peaked at 180.2 MB against 0.5 MB with no readers --
      119 MB of WAL per second of held read. Extrapolated at this write rate, a reader that
      holds for one hour pins 429 GB of WAL on the writer's volume.

  Readings:
   * WAL's promise held in the sense it is actually a promise about: not one reader
     errored, not one saw a torn row, and the writer never waited on a reader's
     lock. 0 errors across every phase, including the reader that held its
     transaction open for
     a second and a half while the writer committed 2,711 times behind its back.
   * What WAL does NOT promise is free cores. The control phase is the point: four
     processes that open the file and then do nothing cost the writer 9% of its
     throughput on this 2 vCPU box, and four readers that
     actually query cost 66%. The difference is SQLite work -- page cache, B-tree
     traversal, I/O -- competing with the writer for the same two cores. On a box
     with cores to spare the reader share shrinks; it never reaches zero, because a
     reader and a writer on one file share a page cache and a volume.
   * The writer's tail is what a reader costs, not its median: commit p50 moves +87%
     and p99 moves +996% (1014 us -> 11.1 ms).
     For the trace file that is affordable -- [W1] sized it at 10x the fleet pace.
     For the money file it would not be, and this is the second leg of [W2]'s
     two-file argument: a dashboard query that costs 10 ms of tail must not be able
     to touch the lease path's 2 ms budget, and separating the files is what stops
     it. The rotation argument was the first leg; this is the latency one.
   * The hazard is real, is the only one, and is a protocol property rather than a
     performance one: an open read transaction pins the WAL. Measured here,
     119 MB per second of held read at this write rate,
     180 MB in a four-second phase, and every passive checkpoint in that phase
     stopped short without reporting itself busy. An analyst's shell left open over
     lunch is not a slow query, it is a full disk on the writer's volume. R94 is the
     rule that follows: long or interactive reads go to a sealed partition or a
     replica copy, never to the live file. The live file is for the writer and for
     short bounded reads.
   * The pin is not a leak: a TRUNCATE checkpoint at the end of every phase
     reclaimed the WAL to zero (`WAL after` above). What cannot be reclaimed is the
     part an open reader still needs, which is why the rule is about where readers
     connect and not about how often the writer checkpoints.
   * Counter-evidence, recorded: the reader that held a transaction for 1.5 s cost
     the writer LESS throughput (48%) than four short readers
     (66%), because a held read is
     idle CPU. If a deployment's only readers are short and bounded, the pinned-WAL
     hazard may never fire and R94 reads as paranoia. It is cheap paranoia: the
     alternative failure mode is running out of disk on the machine that holds the
     money rows.


====================================================================================================
Reproduce: python3 spikes/0013-state-store/store.py 200000   (RESULTS.md is this output;
           --smoke for a ~2 min pass, --section=W3 for one section)
