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
  up to 2x the intended limit through. See
  [`experiments/fixed-window.md`](experiments/fixed-window.md).
- **Sliding Window** — tracks a rolling log of request timestamps
  instead of a fixed counter, closing the boundary exploit. See
  [`experiments/sliding-window.md`](experiments/sliding-window.md).
- **Token Bucket** — allows controlled bursts via a continuously
  refilling token pool. See
  [`experiments/token-bucket.md`](experiments/token-bucket.md).

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

## Project structure
├── experiments/ # Phase A: algorithms in plain Python, with notes
├── redis_limiter/ # Phase B: Redis-backed implementations,
│   # race condition proof, and atomic fix
└── (Django integration — in progress)


## Tech stack

- Python
- Redis (via Docker)
- Lua (Redis scripting, for atomicity)
- Django (in progress — wrapping this as reusable middleware)

## Running it

```bash
# Start Redis
docker run -d --name redis-ratelimiter -p 6379:6379 redis

# Install dependencies
pip install redis

# Run the naive version and the race condition test
python redis_limiter/fixed_window_naive.py
python redis_limiter/fixed_window_race_test.py

# Run the atomic (fixed) version
python redis_limiter/fixed_window_atomic.py
```

## Status

🚧 In progress. Sliding window and token bucket are implemented in
plain Python; Redis migration for those, and Django integration, are
next.