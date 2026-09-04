# Distributed Rate Limiter

A rate-limiting system built from scratch to understand how
distributed systems enforce request limits correctly under
concurrency — implemented without using existing rate-limiting
libraries (e.g. `django-ratelimit`, DRF throttle classes).

## Why this project

Rate limiting libraries already exist and are what you'd actually
use in production. The point of this project isn't to replace
them — it's to understand what's happening underneath one: how
naive implementations break under concurrent load, how to fix that
correctly using atomic operations, and whether that fix actually
holds up once there's more than one app server involved.

## What's implemented so far

Three rate-limiting algorithms, each first built and understood in
plain Python, then migrated to Redis so state is shared across
multiple processes/instances:

- **Fixed Window** — simple counter per time slot. Vulnerable to a
  boundary exploit: requests timed around the window reset can get
  up to 2x the intended limit through. Fully migrated to Redis,
  including the atomic fix (below), wired into Django as middleware,
  and verified across multiple Django instances behind a load
  balancer. See
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

The atomic fixed-window limiter is wrapped as Django middleware in
[`distributed_rate_limiter/`](distributed_rate_limiter/), so it
protects a real HTTP endpoint instead of running as a standalone
script.

- `RateLimitMiddleware` (in
  [`distributed_rate_limiter/main/middleware/middleware.py`](distributed_rate_limiter/main/middleware/middleware.py))
  runs early in the middleware stack, identifies the caller by
  client IP (checking `X-Forwarded-For` first, falling back to
  `REMOTE_ADDR`), and calls the same `is_allowed()` Lua-scripted
  check used in `redis_limiter/`.
- If the limit is exceeded, the middleware short-circuits the
  request and returns a `429` with a JSON error body — the view
  never runs.
- Currently applied globally to every route (limit=5 per 60s,
  hardcoded), demonstrated against a test endpoint: `GET /api/hello/`.

## Distributed deployment — proving it holds across multiple instances

A single Django server sharing a single Redis instance proves the
algorithm works, but not that it's genuinely *distributed*. The real
test is whether the limit holds globally across multiple independent
app servers, the way it would behind a real load balancer.

**Setup:** Docker Compose (in
[`distributed_rate_limiter/docker-compose.yml`](distributed_rate_limiter/docker-compose.yml))
runs 3 Django containers (`django1`, `django2`, `django3`), 1 shared
Redis container, and 1 Nginx container
([`distributed_rate_limiter/nginx/`](distributed_rate_limiter/nginx/))
configured to round-robin incoming requests across the 3 Django
instances. Every instance runs the exact same `RateLimitMiddleware`
and connects to the same Redis instance for shared state.

**Why this matters:** if each Django instance kept its own in-memory
count (like the original Phase A version), a client could get up to
3x the intended limit through, simply by having their requests
spread across different instances by the load balancer. Because all
three instances check the same Redis-backed atomic script, the limit
is enforced globally, regardless of which physical instance actually
handles a given request.

**Verified with an automated concurrent load test**
(`load_tests/distributed_race_test.py`, 20 threads firing
simultaneously at `http://localhost/api/hello/` through Nginx):

Run 1 (fresh window): Allowed: 5 Blocked: 15
Run 2 (same window, run immediately after): Allowed: 0 Blocked: 20


Run 1 shows the limit holding at exactly 5 across all three
instances combined, even though requests were round-robined between
them. Run 2, run again inside the same 60-second window, correctly
shows 0 allowed — proving the limit is enforced per time window
globally in Redis, not just "per script execution."

Docker Compose logs confirm requests were actually distributed
across all three containers — `django1`, `django2`, and `django3`
each independently logged both allowed and blocked requests, but the
combined total across all of them never exceeded the configured
limit.

## Project structure
├── experiments/ # Phase A: algorithms in plain Python
│ ├── notes/ # write-ups for each algorithm
│ ├── Fixed_window_baseline.py
│ ├── Fixed_window_exploit.py
│ ├── sliding_window_baseline.py
│ ├── sliding_window_full_gap.py
│ ├── sliding_window_partial_gap.py
│ ├── token_bucket_baseline.py
│ └── token_bucket_refill.py
├── redis_limiter/ # Phase B: Redis-backed fixed window,
│ │ # race condition proof, atomic fix
│ ├── fixed_window_naive.py
│ ├── fixed_window_race_test.py
│ └── fixed_window_atomic.py
├── load_tests/ # Phase D: concurrent test proving the
│ └── distributed_race_test.py # limit holds across all instances
├── distributed_rate_limiter/ # Phase C/D: Django project + Docker setup
│ ├── distributed_rate_limiter/ # Django settings package
│ ├── main/ # Django app (views, middleware, urls)
│ ├── nginx/ # Nginx config for load balancing
│ ├── Dockerfile
│ ├── docker-compose.yml
│ ├── requirements.txt
│ └── manage.py
└── readme.md


## Tech stack

- Python
- Redis (via Docker)
- Lua (Redis scripting, for atomicity)
- Django — atomic fixed-window limiter live as global middleware
- Docker Compose — 3x Django instances + shared Redis + Nginx
- Nginx — load balancing (round robin) across Django instances
- Gunicorn — WSGI server for the containerized Django app

## Running it

### Standalone algorithm scripts (no Django, no Docker)

```bash
pip install -r requirements.txt

# Phase A — plain Python
python experiments/Fixed_window_baseline.py
python experiments/Fixed_window_exploit.py

# Phase B — Redis-backed, single instance
docker run -d --name redis-ratelimiter -p 6379:6379 redis
python redis_limiter/fixed_window_naive.py
python redis_limiter/fixed_window_race_test.py
python redis_limiter/fixed_window_atomic.py
```

### Full distributed setup (Django + Docker Compose)

```bash
cd distributed_rate_limiter
docker compose up --build

# in another terminal — fire 6 requests through Nginx
for i in {1..6}; do curl -i http://localhost/api/hello/; done
# the 6th should return 429, regardless of which Django instance served each request

# or run the automated concurrent load test (from repo root)
cd ..
pip install requests
python load_tests/distributed_race_test.py
```

## Status

🚧 In progress.

- ✅ Fixed window: plain Python → Redis (naive) → race condition
  proven → atomic Lua fix → wired into Django as middleware →
  verified across 3 Django instances behind Nginx, sharing one Redis
  backend, via an automated concurrent load test.
- ⏳ Sliding window and token bucket: implemented and verified in
  plain Python only. Redis migration (naive → race test → atomic
  fix, same pattern as fixed window) is next.
- ⏳ Django integration is currently a single global limit on every
  route. Planned next: per-route/per-user configurable limits, and
  exposing algorithm choice (fixed window / sliding window / token
  bucket) instead of hardcoding fixed window.