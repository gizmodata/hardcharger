# Backpressure: verification and proposed resumable execution

Status: design for review, 2026-10-26. The limits patch implements the upload-message
limit and retained-result budgets. It does **not** implement the scheduling changes below.
No engine-pool starvation fix is claimed by that patch.

## Verification

Against Warp SQL b74af004 (the Hardcharger pin), all three reported gaps are present:

- `Api::Run` posts a whole execution/fetch loop onto the session's engine-pool strand.
  `Statement::AddBatch` waits synchronously on `room_`. Ingest enters the same pool;
  `ByteQueue::Read` waits synchronously on `data_`. Live clients can keep these calls
  occupied indefinitely without hitting the per-I/O timeout.
- Retained quotas were per principal only, with no Hardcharger option wiring. Neither
  sessions nor concurrent statements have a finite default. Finished results can still
  hold buffers while clients drain them.
- `QueueInputStream::Read(nbytes)` allocated the requested size before receiving it.
  Arrow decompression also trusted declared expanded lengths without an application cap.

Two qualifications to the earlier review matter: the 32 MiB statement and 64 MiB encode
windows are soft thresholds (minimum batch counts and a batch of overshoot), and the old
byte counter missed nested child/dictionary buffers. The limits patch fixes that counter
with Arrow's recursive `TotalBufferSize` for both statements and encoding. There can still
be transient incoming batches, engine allocations, compression work, and socket buffers
outside those counters. These are not hard whole-process memory bounds.

## Results: keep the existing backend interface

`StatementOutcome::rows` already holds a stateful `RecordBatchReader`. DuckDB's
`Outcome`/`KeptReader` owns its Arrow C stream and SQL statement until that reader is
destroyed. The smallest interface change for result production is therefore **none**:
preserve the reader between calls to `Next()`. A call can still spend time computing;
the guarantee here is to avoid waiting for network consumption on an engine worker.

Replace the blocking producer path with a shared production state: session, statement,
reader, one pending batch and its destination partition, counters and finalization flag.
Add a nonblocking `TryAddBatch` outcome: accepted, needs room, or stopped. Preserve the
current minimum batches per partition, routing order, quota errors and terminal states.
On needs-room retain that single batch, register a versioned notification, and return.
The notification posts a continuation to the **same session strand**. Sample the version
before checking room and recheck after arming; a consumption/cancellation race must never
lose a wakeup. Coalesce posts so at most one producer continuation runs or is scheduled.
Use a small batch/time quantum and repost even when room remains, for fairness.

Releasing a strand handler must not release the session's logical engine ownership.
Introduce an asynchronous per-session operation gate: a streaming execution holds its
lease until its reader is destroyed and transaction/options refresh completes. Metadata,
prepare, SQL, option changes and transaction operations queue behind that lease without
parking a worker. Merely posting fetch continuations to the existing strand would allow
other handlers to use the same DuckDB connection while its cursor remains live. Preserve
session running-statement identity across yields; it cannot be the current stack RAII
object. Finalize once, on the strand, release the gate, then schedule the next operation.
Backend calls must support serialized migration between pool threads, not require a fixed
OS thread; verify that constraint for each backend and its instrumentation wrappers.

Cancellation and shutdown notify both capacity waiters and the operation gate. A parked
producer must resume for cleanup even with zero reader progress. Interrupt only the
owning operation; never a later statement after a stale callback. Keep the existing
repeated interrupt while DuckDB may clear an early interrupt. An explicit timeout covers
time parked as well as execution; existing socket read/write timeouts still cover I/O.
Reader abandonment retains `finish_if_abandoned` semantics for writes. Session close
cancels pending work and schedules connection destruction after cursor cleanup.

Partitioning retains the pending batch's assigned partition across resumes, without
rerouting, duplication or a new `Next()` while it is blocked. All partitions' minimum
bootstrap batches remain available; bounded prefetch must account for one pending batch.
Any partition consumption can wake the producer, which rechecks its target partition.
A client that abandons a required partition follows the existing cancellation rule.

Idle eviction must continue to see a running, parked producer waiting for an absent
reader as evictable when the existing rule allows it. An active read, including a live
slow read, still protects the session. Keep the statement/ingest registered while parked,
and retain activity/reader counters independently of whether a worker is currently running.
Eviction must wake capacity waiters and release their reader and reservation exactly once.

## Uploads: a backend extension is needed

`EngineSession::Ingest(target, reader)` executes one synchronous DuckDB INSERT/CTAS
which pulls Arrow batches through a table-function callback. That stack cannot return
"try again later" without DuckDB treating it as EOF or failure. Moving the current CV
to another method, or yielding only between result batches, does not solve uploads.
Arrow parameters to prepared execution can also block on this input path; include them
in the scheduling work. The current DuckDB prepared-Arrow path additionally materializes
its output batches; the proposed stream budgets do not bound that separate accumulation.

Proposed optional backend API (names illustrative):

```cpp
class IngestSink {
 public:
  virtual void Push(std::shared_ptr<arrow::RecordBatch>, int64_t batch_index) = 0;
  virtual int64_t Finish() = 0;
  virtual void Abort() = 0;
  virtual ~IngestSink() = default;
};
// BeginIngest(target, schema) returns an owned sink. Push never pulls network input.
```

Keep the synchronous API for compatible backends, advertise resumable support explicitly,
and never silently use that fallback on the shared pool under the new guarantee. A sink
must preserve create/replace/append, all-or-nothing upload semantics, caller transactions,
temporary tables, RBAC/default catalog, conversion rules, partition batch indices and
row counts. DuckDB likely needs transaction-scoped staging or a resumable pending query
API below the current v2 C interface; a simple per-batch INSERT changes atomicity and is
insufficient. Define Abort failure handling and connection disposal, especially inside a
caller's transaction, before choosing an implementation.

Frame bounded IPC messages asynchronously outside engine workers, using a push decoder
or a separate bounded decoder executor given complete messages. Do not put the existing
blocking MessageReader on a shared decode pool with incomplete network input. Maintain
dictionary state and partition ordering, bound wire and decoded queues, pause socket
reads when full, and post only ready Push operations onto the owning session strand.
Finish only after all parts and the explicit commit have arrived; abort cancels input
and releases staged data. For Arrow prepared parameters, stage bounded ready batches or
add incremental prepared execution rather than allowing ExecutePrepared to pull input.

This is too invasive to implement safely as part of the limits patch. The result
scheduler and transactional ingest extension should be separate reviewed changes.

## Stopgap and server-wide streaming budget

A bounded transfer executor can isolate existing blocking execution from engine workers,
provided the entire owning operation (including input schema reads and prepared uploads)
is placed there from the start and respects the per-session gate. Admission is
nonblocking and finite, including its waiting queue; refuse excess work with
`rate_limited` and preserve normal client retry behavior. It protects fast control-plane
work but costs one OS thread per admitted slow transfer and can still exhaust its own
slots; new transfers cannot be guaranteed fast when that executor is full. Moving an
already blocked stack between pools is not feasible. Lowering Cloud's statement limit
below engine threads alone is incomplete: uploads, privileged bypasses and drained-but-
buffered results need independent accounting. No stopgap is enabled in this patch.

The retained budget now reserves cached Arrow buffer bytes atomically against both
principal and server ceilings (1 GiB each by default). Failures use
`result_quota_exceeded`. Reservations survive expiry while an active retained read still
needs the cache, and are returned on cache disposal. A released cache may still have
temporary encoder references; this budget is deliberately not a process RSS cap.

For unretained streams, propose a separate byte budget with ownership leases transferred
from production to statement buffers to encoding and socket completion. Count nested
buffers, slices conservatively, raw/encoded overlap and each partition's bootstrap.
Acquire before producing/encoding when the maximum size is known; otherwise account for
one bounded pending batch, with an explicit oversized-batch policy. Exhaustion must park
asynchronously with cancellation/fair wakeups, never add another engine-thread CV wait.
If a single batch exceeds the configured ceiling, fail cleanly with
`result_quota_exceeded`; do not wait forever for an impossible reservation. Coordinate
this with resumable production rather than claiming that admission limits alone bound
all finished streams. Application budgets remain separate from DuckDB's memory limit.

## Acceptance tests for the scheduler change

Use a test backend with exactly two engine workers and synchronization hooks, then the
DuckDB backend. Start at least five independent sessions whose clients read just enough
to remain live but leave each producer parked. Confirm every producer reached capacity
before starting a fresh session; open, metadata and `SELECT 42` must finish within a
bounded deadline (for example 2 seconds locally, scaled for sanitizers). No sleeps as
proof of being backpressured. Release clients and compare every returned row.

Repeat with incomplete trickle uploads, mixed uploads/readers, Arrow prepared inputs,
compressed streams and skewed partitions. Cancel each parked operation, hit statement
and queue timeouts, abandon a reader, evict an idle absent-reader session, close a session,
and stop the server. Verify single finalization, correct timeout/cancel labels, atomic
ingest rollback, no deadlocks/lost wakeups, no post-close engine calls, returned leases,
and an immediately usable fast session after each case. Add a same-session test proving
metadata/SQL waits for the parked cursor without occupying a pool thread.
