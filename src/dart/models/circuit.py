"""Circuit breaker state model.

Backs the `dart:circuit:<domain>` Redis Hash described in the DART
architecture blueprint (§2.3). The state machine is:

    CLOSED --(failure_count >= threshold)--> OPEN
    OPEN --(cooldown elapsed)--> HALF_OPEN
    HALF_OPEN --(probe successes)--> CLOSED
    HALF_OPEN --(probe failure)--> OPEN
"""

from __future__ import annotations

from enum import StrEnum


class CircuitState(StrEnum):
    """Per-domain circuit breaker state."""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"
