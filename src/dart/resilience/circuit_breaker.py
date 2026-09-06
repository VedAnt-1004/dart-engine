"""Per-domain circuit breaker, backed by `dart:circuit:<domain>`.

State machine::

    CLOSED --(failure_count >= threshold)--> OPEN
    OPEN --(cooldown elapsed, on next allow_request)--> HALF_OPEN
    HALF_OPEN --(probe successes >= half_open_probe_count)--> CLOSED
    HALF_OPEN --(any probe failure)--> OPEN

Every state transition is implemented as a single Lua script (`EVAL`)
rather than a client-side read-modify-write, so concurrent workers
hammering the same domain converge to a correct state without lost
updates — Redis executes each script atomically and to completion
before processing any other command.
"""

from __future__ import annotations

import time

import redis.asyncio as redis

from dart.core.config import RedisSettings
from dart.models.circuit import CircuitState

# KEYS[1] = circuit hash key
# ARGV[1] = now (epoch seconds)
# ARGV[2] = open_cooldown_seconds
# ARGV[3] = half_open_probe_count
# Returns 1 (allow) or 0 (deny). A transition from OPEN to HALF_OPEN, or
# consumption of a HALF_OPEN probe slot, happens as a side effect.
_ALLOW_REQUEST_SCRIPT = """
local state = redis.call('HGET', KEYS[1], 'state')
if not state or state == 'CLOSED' then
    return 1
end

if state == 'OPEN' then
    local next_probe_at = tonumber(redis.call('HGET', KEYS[1], 'next_probe_at')) or 0
    if tonumber(ARGV[1]) >= next_probe_at then
        redis.call('HMSET', KEYS[1], 'state', 'HALF_OPEN', 'success_count', '0', 'probes_issued', '1')
        return 1
    end
    return 0
end

if state == 'HALF_OPEN' then
    local probes_issued = tonumber(redis.call('HGET', KEYS[1], 'probes_issued')) or 0
    local max_probes = tonumber(ARGV[3])
    if probes_issued < max_probes then
        redis.call('HINCRBY', KEYS[1], 'probes_issued', 1)
        return 1
    end
    return 0
end

return 0
"""

# KEYS[1] = circuit hash key
# ARGV[1] = half_open_probe_count
# Returns the resulting state as a string.
_RECORD_SUCCESS_SCRIPT = """
local state = redis.call('HGET', KEYS[1], 'state')

if not state or state == 'CLOSED' then
    redis.call('HMSET', KEYS[1], 'state', 'CLOSED', 'failure_count', '0')
    return 'CLOSED'
end

if state == 'OPEN' then
    -- Shouldn't normally happen (allow_request would have denied the
    -- attempt), but a stray success is unambiguous evidence of recovery.
    redis.call('HMSET', KEYS[1], 'state', 'CLOSED', 'failure_count', '0',
        'success_count', '0', 'probes_issued', '0')
    return 'CLOSED'
end

if state == 'HALF_OPEN' then
    local success_count = tonumber(redis.call('HINCRBY', KEYS[1], 'success_count', 1))
    local max_probes = tonumber(ARGV[1])
    if success_count >= max_probes then
        redis.call('HMSET', KEYS[1], 'state', 'CLOSED', 'failure_count', '0',
            'success_count', '0', 'probes_issued', '0')
        return 'CLOSED'
    end
    return 'HALF_OPEN'
end

return state
"""

# KEYS[1] = circuit hash key
# ARGV[1] = now (epoch seconds)
# ARGV[2] = failure_threshold
# ARGV[3] = open_cooldown_seconds
# Returns the resulting state as a string.
_RECORD_FAILURE_SCRIPT = """
local state = redis.call('HGET', KEYS[1], 'state')
if not state then state = 'CLOSED' end

if state == 'HALF_OPEN' then
    -- Any failure during a probe immediately re-opens the circuit.
    local next_probe_at = tonumber(ARGV[1]) + tonumber(ARGV[3])
    redis.call('HMSET', KEYS[1], 'state', 'OPEN', 'opened_at', ARGV[1],
        'next_probe_at', tostring(next_probe_at), 'last_failure_at', ARGV[1],
        'success_count', '0', 'probes_issued', '0')
    return 'OPEN'
end

if state == 'OPEN' then
    -- Already open: record the straggling failure but do NOT reset the
    -- cooldown timer, so late-arriving failures from before the circuit
    -- opened can't perpetually delay recovery.
    redis.call('HSET', KEYS[1], 'last_failure_at', ARGV[1])
    return 'OPEN'
end

-- CLOSED
local failure_count = tonumber(redis.call('HINCRBY', KEYS[1], 'failure_count', 1))
redis.call('HSET', KEYS[1], 'last_failure_at', ARGV[1])

local threshold = tonumber(ARGV[2])
if failure_count >= threshold then
    local next_probe_at = tonumber(ARGV[1]) + tonumber(ARGV[3])
    redis.call('HMSET', KEYS[1], 'state', 'OPEN', 'opened_at', ARGV[1],
        'next_probe_at', tostring(next_probe_at), 'failure_count', '0',
        'success_count', '0', 'probes_issued', '0')
    return 'OPEN'
end

return 'CLOSED'
"""


class CircuitBreaker:
    """Per-domain circuit breaker gating dispatch attempts."""

    def __init__(
        self,
        client: redis.Redis,
        settings: RedisSettings,
        failure_threshold: int = 5,
        open_cooldown_seconds: int = 30,
        half_open_probe_count: int = 3,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if open_cooldown_seconds < 1:
            raise ValueError("open_cooldown_seconds must be >= 1")
        if half_open_probe_count < 1:
            raise ValueError("half_open_probe_count must be >= 1")

        self._client = client
        self._key_prefix = settings.circuit_key_prefix
        self._failure_threshold = failure_threshold
        self._open_cooldown_seconds = open_cooldown_seconds
        self._half_open_probe_count = half_open_probe_count

        self._allow_request_script = client.register_script(_ALLOW_REQUEST_SCRIPT)
        self._record_success_script = client.register_script(_RECORD_SUCCESS_SCRIPT)
        self._record_failure_script = client.register_script(_RECORD_FAILURE_SCRIPT)

    def _key(self, domain: str) -> str:
        return f"{self._key_prefix}{domain}"

    async def allow_request(self, domain: str, now: float | None = None) -> bool:
        """Whether a dispatch attempt against `domain` should proceed.

        `now` is exposed as an optional override purely for deterministic
        testing of cooldown-boundary behavior; production callers should
        omit it and let it default to the current time.
        """
        current_time = now if now is not None else time.time()
        result = await self._allow_request_script(
            keys=[self._key(domain)],
            args=[current_time, self._open_cooldown_seconds, self._half_open_probe_count],
        )
        return bool(result)

    async def record_success(self, domain: str) -> CircuitState:
        """Record a successful dispatch, returning the resulting state."""
        result = await self._record_success_script(
            keys=[self._key(domain)],
            args=[self._half_open_probe_count],
        )
        return CircuitState(result)

    async def record_failure(self, domain: str, now: float | None = None) -> CircuitState:
        """Record a failed dispatch, returning the resulting state."""
        current_time = now if now is not None else time.time()
        result = await self._record_failure_script(
            keys=[self._key(domain)],
            args=[current_time, self._failure_threshold, self._open_cooldown_seconds],
        )
        return CircuitState(result)

    async def get_state(self, domain: str) -> CircuitState:
        """Current state for `domain`. Defaults to CLOSED for a domain
        that has never recorded a failure."""
        state = await self._client.hget(self._key(domain), "state")
        return CircuitState(state) if state else CircuitState.CLOSED
