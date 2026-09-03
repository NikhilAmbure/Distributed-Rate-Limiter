# Distributed Rate Limiter

A rate-limiting system built from scratch to understand how
distributed systems enforce request limits correctly under
concurrency — implemented without using existing rate-limiting
libraries (e.g. `django-ratelimit`, DRF throttle classes).

## Why this project

Rate limiting libraries already exist and are what you'd actually
use in production. The point of this project isn't to replace
them — it's to understand what's happening underneath one: how
naive implementations break under concurrent load, and how to fix
that correctly using atomic operations.

## What's implemented so far

Three rate-limiting algorithms, each first built and understood in
plain Python, then migrated to Redis so state is shared across
multiple processes/instances:

- **Fixed Window** — simple counter per time slot. Vulnerable to a
  boundary exploit: requests timed around the window reset can get
  up to 2x the intended limit through. Fully migrated to Redis,
  including the atomic fix (below), and now enforced live via
  Django middleware. See
  [`experiments/notes/fixed-window.md`](experiments/notes/fixed-window.md).
- **Sliding Window** — tracks a rolling log of request timestamps
  instead of a fixed counter, closing the boundary exploit.
  Implemented and verified in plain Python; Redis migration not
  started yet. See
  [`experiments/notes/sliding_window.md`](experiments/notes/sliding_window.md).
- **Token Bucket** — allows controlled bursts via a continuously
  refilling token pool. Implemented and verified in plain Python;
  Redis migration not started yet. See
  [`experiments/notes/token-bucket.md`](experiments/notes/token-bucket.md).

## The race condition (the core of this project)

Moving fixed window into Redis exposed a real concurrency bug: the
naive implementation reads the current count, checks it against the
limit, then writes back an increment — as three separate steps.
Under concurrent load, multiple requests can read the same stale
count before any of them writes back, letting more requests through
than the limit allows.

**Proven with a live test:** firing 20 concurrent requests against a
limit of 5 resulted in **7 requests being allowed** instead of 5.

**Fixed** by moving the read-check-increment-expire logic into a
single Lua script, executed atomically by Redis — eliminating the
interleaving window that caused the over-admission. The same
20-request concurrent test now correctly allows exactly 5 every run.

| | Naive (read-check-write as separate steps) | Atomic (Lua script) |
|---|---|---|
| Concurrent test result (limit=5) | 7 allowed | 5 allowed |

See [`redis_limiter/`](redis_limiter/) for the naive implementation,
the concurrency test that reproduces the bug, and the atomic fix.

## Django integration

The atomic fixed-window limiter is now wrapped as Django middleware
in [`distributed_rate_limiter/`](distributed_rate_limiter/), so it
protects a real HTTP endpoint instead of running as a standalone
script.

- `RateLimitMiddleware` (in
  [`main/middleware/middleware.py`](distributed_rate_limiter/main/middleware/middleware.py))
  runs early in the middleware stack, identifies the caller by
  client IP (checking `X-Forwarded-For` first, falling back to
  `REMOTE_ADDR`), and calls the same `is_allowed()` Lua-scripted
  check used in `redis_limiter/`.
- If the limit is exceeded, the middleware short-circuits the
  request and returns a `429` with a JSON error body — the view
  never runs.
- Currently applied globally to every route (limit=5 per 60s,
  hardcoded), demonstrated against a single test endpoint:
  `GET /api/hello/`.

```bash
cd distributed_rate_limiter
python manage.py runserver

# in another terminal — fire 6 requests, the 6th should 429
for i in {1..6}; do curl -i http://127.0.0.1:8000/api/hello/; done
```

This is intentionally minimal for now — one algorithm, one hardcoded
limit, applied to everything. Making it configurable per-route or
per-user is the next step (see Status below).

## Project structure

```
├── experiments/               # Phase A: algorithms in plain Python, with notes
├── redis_limiter/              # Phase B: Redis-backed fixed window,
│                                # race condition proof, and atomic fix
└── distributed_rate_limiter/   # Phase C: Django project wrapping the
                                 # atomic fixed-window limiter as middleware
```

## Tech stack

- Python
- Redis (via Docker)
- Lua (Redis scripting, for atomicity)
- Django — atomic fixed-window limiter is live as global middleware

## Running it

```bash
# Start Redis
docker run -d --name redis-ratelimiter -p 6379:6379 redis

# Install dependencies
pip install redis django

# --- Standalone scripts (redis_limiter/) ---
# Run the naive version and the race condition test
python redis_limiter/fixed_window_naive.py
python redis_limiter/fixed_window_race_test.py

# Run the atomic (fixed) version
python redis_limiter/fixed_window_atomic.py

# --- Django integration (distributed_rate_limiter/) ---
cd distributed_rate_limiter
python manage.py runserver
# then hit http://127.0.0.1:8000/api/hello/ repeatedly to see the 429
```

## Status

🚧 In progress.

- ✅ Fixed window: plain Python → Redis (naive) → race condition
  proven → atomic Lua fix → wired into Django as middleware
  protecting a live endpoint.
- ⏳ Sliding window and token bucket: implemented and verified in
  plain Python only. Redis migration (naive → race test → atomic
  fix, same pattern as fixed window) is next.
- ⏳ Django integration is currently a single global limit on every
  route. Planned next: per-route/per-user configurable limits, and
  exposing algorithm choice (fixed window / sliding window / token
  bucket) instead of hardcoding fixed window.