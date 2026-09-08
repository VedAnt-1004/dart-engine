# Shared image for all four DART processes (dart-api, dart-worker,
# dart-scheduler, and the mock chaos receiver). Which one actually runs
# is decided per-service by the `command:` override in docker-compose.yml
# — this keeps a single image/build instead of four near-duplicate ones.

FROM python:3.12-slim AS base

WORKDIR /app

# Runtime-only install (no [dev] extras: pytest/fakeredis/respx aren't
# needed in the container, only for running the test suite on the host).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# No default CMD: every service in docker-compose.yml sets its own
# command explicitly, so a missing override fails loudly instead of
# silently running the wrong process.
