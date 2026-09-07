"""Signing secret resolution: `signing_secret_id` -> `WebhookSigner`.

Per approved architecture decision #4, `signing_secret_id` on
`WebhookTask` is a reference, never the raw secret — resolving it to an
actual `WebhookSigner` is the job of a `Callable[[str], WebhookSigner]`
supplied by the deployment environment. This module provides two such
resolvers: an environment-variable-backed one suitable for a real
single-tenant deployment, and an in-memory one for tests/local dev. A
production multi-tenant deployment would likely swap in a resolver
backed by a secrets manager (Vault, AWS Secrets Manager, etc.)
implementing the same callable signature — nothing else in the codebase
needs to change to support that.
"""

from __future__ import annotations

import os
from typing import Callable

from dart.core.exceptions import ConfigurationError
from dart.security.signer import WebhookSigner

#: The contract every resolver in this module implements, and the type
#: `AsyncDispatcher` depends on — never the concrete resolver class.
SigningSecretResolver = Callable[[str], WebhookSigner]


class EnvSigningSecretResolver:
    """Resolves `signing_secret_id` to a `WebhookSigner` via environment
    variables named `DART_SIGNING_SECRET__<signing_secret_id>`.

    Implements `SigningSecretResolver` (i.e. is directly usable anywhere
    a `Callable[[str], WebhookSigner]` is expected) via `__call__`.
    """

    _ENV_PREFIX = "DART_SIGNING_SECRET__"

    def __call__(self, signing_secret_id: str) -> WebhookSigner:
        env_var = f"{self._ENV_PREFIX}{signing_secret_id}"
        secret = os.environ.get(env_var)
        if not secret:
            raise ConfigurationError(
                f"No signing secret configured for signing_secret_id="
                f"{signing_secret_id!r} (expected environment variable {env_var})"
            )
        return WebhookSigner(secret)


class StaticSigningSecretResolver:
    """Resolves from an in-memory mapping.

    Intended for tests and local development, where secrets don't need
    to come from the environment or a real secrets manager.
    """

    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = secrets

    def __call__(self, signing_secret_id: str) -> WebhookSigner:
        secret = self._secrets.get(signing_secret_id)
        if not secret:
            raise ConfigurationError(
                f"No signing secret configured for signing_secret_id={signing_secret_id!r}"
            )
        return WebhookSigner(secret)
