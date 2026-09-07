"""Console-script entrypoint for the ingestion API, registered as
`dart-api` in `pyproject.toml`.

The worker and scheduler each have their own dedicated entrypoint module
(`dart.worker.runner`, `dart.worker.scheduler_runner`) since they're
fully decoupled processes with no ASGI dependency. The API's entrypoint
lives here instead, so `dart.api` stays a pure ASGI-app-definition
package — importable directly via `uvicorn dart.api.app:app`, or through
this thin wrapper for a plain `dart-api` shell command.
"""

from __future__ import annotations

import uvicorn

from dart.core.config import Settings


def run_api() -> None:
    """Synchronous entrypoint registered as the `dart-api` console script."""
    settings = Settings()
    log_level = "debug" if settings.environment == "development" else "info"
    uvicorn.run("dart.api.app:app", host="0.0.0.0", port=8000, log_level=log_level)


if __name__ == "__main__":
    run_api()
