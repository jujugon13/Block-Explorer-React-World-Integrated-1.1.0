# Redis-compatible cache adapter

This adapter implements the existing `src.shared.CacheStore` port for a
Redis-compatible key-value store.  It is intentionally synchronous because
the application ports are synchronous.

## Applicable specification

| Specification | Requirement |
|---|---|
| `specs/03-schemas-and-defaults.md` §7.1 | Key-value-store timeout is fixed at 1 second. |
| `specs/14-search-and-rag.md` S10 | Cache read/write failures are treated as cache misses and processing continues. |
| `specs/14-search-and-rag.md` FR-SEARCH-140~142 | Search owns requester-separated keys and permission revalidation. |

## Configuration and contract

`REDIS_URL` is required. The adapter creates the official `redis` client only
when `build_redis_cache()` is called, uses `decode_responses=True`, and sets
both connection and operation timeouts to one second. It does not probe Redis
at construction time: cache outage is an allowed fail-open runtime condition.

Operational failures are exposed as built-in `ConnectionError`, so both the
auth and search callers retain their specified fail-open behavior. URLs and
credentials are redacted from the configuration representation and errors.

The adapter does not own cache keys, TTL selection, cache invalidation policy,
or any search behavior.
