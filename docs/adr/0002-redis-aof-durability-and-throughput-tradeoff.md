# ADR 0002: Redis AOF Durability and the Throughput Tradeoff

**Status:** Accepted
**Deciders:** DART core engineering
**Context date:** Phase 3 productization (infrastructure hardening), following the Phase 1 architecture lock and the Phase 2 atomic-ingestion/SSRF work

## Context

DART's correctness guarantees — atomic ingestion, at-least-once delivery, crash recovery via `XAUTOCLAIM` — are all built on the assumption that Redis itself does not lose data. Every one of those guarantees is void if Redis's own persistence layer loses writes on an unclean shutdown, independent of how correct the application code above it is. A crash-recovery story that assumes durable storage underneath it, without actually configuring durable storage, is incomplete.

Redis ships with two persistence mechanisms, and neither is durable by default in the way this system requires:

- **RDB snapshotting** (Redis's default): periodic point-in-time snapshots. Data written since the last snapshot is lost entirely on an unclean shutdown — for a system under continuous write load, this window is unbounded in the worst case (bounded only by the snapshot interval, which trades off against snapshot I/O cost).
- **AOF (Append Only File)**: every write command is appended to a log, which Redis replays on restart. Durability here is governed by `appendfsync`, which controls how often the log is actually flushed to disk (as opposed to sitting in the OS page cache, where it is vulnerable to loss on a hard crash even though it has been "written").

## Decision

**Enable AOF with `appendfsync everysec`, backed by a named Docker volume**, per `docker-compose.yml`:

```
command: ["redis-server", "--appendonly", "yes", "--appendfsync", "everysec"]
volumes:
  - redis-data:/data
```

This is a deliberately bounded durability guarantee, not an absolute one, and it is important to state that precisely rather than round it up:

- `appendfsync everysec` fsyncs the AOF **at most once per second**. On a clean shutdown, no data is lost. On a **hard crash** (power loss, OOM-kill, `SIGKILL` of the Redis process itself), **up to ~1 second of the most recent writes can be lost.**
- `appendfsync always` is the actual zero-data-loss option: it fsyncs on every single write. It was **not** chosen, because it imposes an fsync round-trip on every `HSET`/`XADD`/`ZADD` DART issues — for a system explicitly positioned as high-throughput, this is a real and direct cost, not a hypothetical one.
- `appendfsync everysec` is Redis's own documented recommended default specifically because it is the pragmatic middle point on this curve: a bounded, small, and — for a webhook delivery system — acceptable loss window in exchange for near-`no`-level write performance.

The named volume (`redis-data`) matters independently of the fsync policy: without it, the AOF file lives in the container's writable layer and is destroyed on `docker compose down -v` or container recreation — meaning persistence would be configured but not actually anchored to storage that outlives the container. This was a real gap in the original `docker-compose.yml` (AOF was added before the volume was), closed in the same change.

### On "prioritizing consistency over throughput"

The atomic Lua ingestion script (`EventIngestor`, see the Phase 2 Target A postmortem) and this AOF configuration are frequently discussed together, and it's worth being precise about what each one actually buys, since they address different failure classes:

- The **Lua script** eliminates a *correctness* bug (a multi-round-trip idempotency race) that existed regardless of Redis's persistence configuration — it would have been just as broken against a perfectly durable Redis.
- **AOF** protects against a *different* failure class entirely: Redis's own process dying. It does not make the Lua script "more atomic" — Redis command/script execution was already atomic. It makes the *result* of that atomic execution survive a Redis restart.

Conflating the two would overstate what either one does. The honest framing is: Target A closed an application-logic race; this ADR closes an infrastructure-durability gap. Both were necessary; neither substitutes for the other.

### Acknowledged bottleneck: fsync latency on virtualized storage

AOF's fsync cost is sensitive to the latency characteristics of the underlying storage, and that latency varies substantially between bare-metal NVMe and virtualized/network-attached storage (e.g. cloud block storage at lower IOPS/throughput tiers, or a container overlay filesystem backed by a network volume). Under sustained high write load, fsync latency can become a meaningful contributor to overall write throughput on such storage — this is a known, general property of AOF-backed Redis, not specific to DART.

**This has not been isolated or measured in this system.** The benchmark run referenced elsewhere (`scripts/benchmark_load.py`, ~178 req/s at connection-pool saturation) was run with AOF already enabled; no comparative run with AOF disabled was performed to measure AOF's specific contribution to that ceiling. Attributing a specific portion of that number to fsync overhead versus the connection-pool limit (`RedisSettings.max_connections`, default 50), Python/asyncio scheduling overhead, or Docker's virtual networking would be speculation, not a measured finding. **Recommended follow-up:** an A/B benchmark run (AOF `everysec` vs. AOF disabled, same hardware, same load profile) to isolate this variable before making any quantitative throughput claim that depends on it.

## Consequences

**Positive:**
- Bounded (~1s), well-understood data-loss window on hard crash, down from RDB's unbounded-by-default window.
- Verified structurally: the black-box crash-recovery test (`scripts/blackbox_crash_recovery_test.py`) exercises worker-process crash recovery against this exact Redis configuration and passes.

**Negative / open gap:**
- **AOF durability itself has not been independently verified.** The existing black-box test kills the *worker* container, not Redis — it validates `XAUTOCLAIM` recovery, not "data survives a Redis crash." No test in this project currently kills the Redis container mid-write and confirms the AOF replays correctly on restart. **Recommended follow-up:** a dedicated test extending the black-box harness to `SIGKILL` the `redis` container mid-load and confirm post-restart state matches expectations.
- The fsync-overhead-vs-throughput question above remains an open, unmeasured question rather than a documented finding.

## Alternatives Considered

| Alternative | Why rejected |
|---|---|
| RDB only (Redis default) | Unbounded data-loss window under continuous write load; incompatible with an explicit fault-tolerance claim. |
| `appendfsync always` | Zero data loss, but fsyncs on every write — directly conflicts with the throughput goals this system is benchmarked against. |
| AOF without a named volume | Persistence configured but not anchored to storage outliving the container — solves nothing on `docker compose down -v` or container recreation. |
