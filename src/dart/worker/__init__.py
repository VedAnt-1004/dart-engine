"""Standalone process entrypoints: the dispatch worker (`dart-worker`,
Phase 4) and the retry scheduler (`dart-scheduler`, Phase 3).

Deliberately decoupled per the approved architecture: each runs as its
own CLI entrypoint/process, importing only `core`/`queue`/`resilience`
— never `api` — so the worker and scheduler tiers can be deployed,
scaled, and restarted independently of the ingestion API.
"""
