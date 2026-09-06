# Django Integration — Configurable Rate Limiting Middleware

This is the Django project that wraps the atomic, Redis-backed rate limiters (from [`../redis_limiter/`](../redis_limiter/)) as reusable middleware, and runs them across a distributed, multi-instance deployment.

---

## Architecture

### Request flow

```text
HTTP Request
     ↓
RateLimitMiddleware
     ↓
Identify client (X-Forwarded-For → REMOTE_ADDR)
     ↓
Dispatch to configured algorithm
     ↓
Redis Lua script (atomic)
     ↓
┌───────────────┐
│ Is limit hit? │
└───────┬───────┘
        │
   ┌────┴────┐
   │         │
   ▼         ▼
 Allowed   Blocked
   │         │
   ▼         ▼
 Django    HTTP 429
  View     (JSON body)
```

### Folder structure

```text
distributed_rate_limiter/
├── distributed_rate_limiter/      # Django settings package
├── main/
│   ├── views.py                    # test endpoint: GET /api/hello/
│   └── middleware/
        ├── __init__.py   
│       ├── middleware.py           # RateLimitMiddleware
│       └── ratelimiter/
│           ├── __init__.py         # dispatcher — picks algorithm from settings
│           ├── fixed_window_atomic.py
│           ├── sliding_window_atomic.py
│           └── token_bucket_atomic.py
├── nginx/                          # load balancer config
├── Dockerfile
├── docker-compose.yml
└── manage.py
```

---

## The middleware

`main/middleware/middleware.py`:

- Runs early in the Django middleware stack (placed right after `SecurityMiddleware`), so abusive traffic is rejected before session/auth/CSRF processing runs.
- Identifies the client by IP — checks `X-Forwarded-For` first (correct behind Nginx/a reverse proxy), falls back to `REMOTE_ADDR`.
- Calls a single dispatcher function, `is_allowed(user_id)`, without needing to know which algorithm or data structure is behind it.
- Returns a `429` with a JSON error body if blocked, short-circuiting before the view runs.

## Choosing an algorithm

The middleware doesn't hardcode a single algorithm. `main/middleware/ratelimiter/__init__.py` reads `settings.RATE_LIMIT_ALGORITHM` and routes to the matching implementation:

```python
ALGORITHMS = {
    'fixed_window': fixed_window_atomic.is_allowed,
    'sliding_window': sliding_window_atomic.is_allowed,
    'token_bucket': token_bucket_atomic.is_allowed,
}
```

Each algorithm module uses its own Redis key prefix (`fixed:`, `sliding:`, `bucket:`), so switching algorithms doesn't collide with leftover state from a previously selected one.

### Configuration (`settings.py`)

```python
RATE_LIMIT_ALGORITHM = 'fixed_window'   # 'fixed_window' | 'sliding_window' | 'token_bucket'

# Used by fixed_window and sliding_window
RATE_LIMIT_MAX_REQUESTS = 5             # max requests allowed per window
RATE_LIMIT_WINDOW = 60                  # window size, in seconds

# Used by token_bucket only
RATE_LIMIT_BUCKET_CAPACITY = 5          # max burst size (tokens the bucket can hold)
RATE_LIMIT_REFILL_RATE = 0.5            # tokens refilled per second
```

Each setting is named for what it actually controls, since `RATE_LIMIT_MAX_REQUESTS` and `RATE_LIMIT_BUCKET_CAPACITY` mean genuinely different things — a burst capacity of 5 with a 0.5/sec refill rate does **not** behave like "5 requests per minute." With these values, token bucket allows an initial burst of 5, then settles into roughly 1 request every 2 seconds indefinitely (~30/min sustained) — a deliberate design difference from fixed/sliding window's strict per-window cap, not a bug. See the root README's "When to Use Which Algorithm" section for why this asymmetry is actually useful in real production APIs.

All three were manually verified through the full Docker Compose stack: switching `RATE_LIMIT_ALGORITHM` and rebuilding correctly changes enforcement behavior, with each algorithm blocking/allowing exactly as its own logic dictates.

**Note:** since the Docker image bakes in code at build time (`COPY . .`), any change to `settings.py` or other source files requires `docker compose up --build` (not just `docker compose up`) to take effect in the running containers.

---

## Distributed deployment

`docker-compose.yml` runs:

- **3 Django containers** (`django1`, `django2`, `django3`) — identical copies of the same app, each running Gunicorn and the same `RateLimitMiddleware`
- **1 shared Redis container** — all three Django instances connect to it via the `REDIS_HOST` environment variable (defaults to `localhost` for non-Docker use)
- **1 Nginx container** — listens on port 80, round-robins incoming requests across the 3 Django instances via an `upstream` block

```text
                  ┌──────────────┐
                  │    Nginx     │
                  │ Load Balancer│
                  └──────┬───────┘
                         │
                Round-robin requests
                         │
          ┌──────────────┼──────────────┐
          ↓              ↓              ↓
     ┌─────────┐    ┌─────────┐    ┌─────────┐
     │ Django 1│    │ Django 2│    │ Django 3│
     │ Gunicorn│    │ Gunicorn│    │ Gunicorn│
     └────┬────┘    └────┬────┘    └────┬────┘
          │              │              │
          └──────────────┼──────────────┘
                         ↓
                  ┌─────────────┐
                  │    Redis    │
                  │ Shared State│
                  └─────────────┘
```

### Why shared Redis matters

If each Django instance kept its own in-memory count instead of checking Redis, a client's requests could get spread across all 3 instances by Nginx's round-robin, each instance independently thinking it was starting fresh — allowing up to 3x the intended limit through. Because all three instances check the same Redis-backed atomic script, the limit holds globally, regardless of which physical instance actually serves a given request.

### Verified with an automated concurrent load test

`load_tests/distributed_race_test.py` fires 20 threads simultaneously at `http://localhost/api/hello/` through Nginx (fixed window, limit=5):

```text
Run 1 (fresh window):                        Allowed: 5   Blocked: 15
Run 2 (same window, run immediately after):  Allowed: 0   Blocked: 20
```

Docker Compose logs confirm requests were distributed across all three containers — `django1`, `django2`, and `django3` each independently logged both allowed and blocked requests — but the combined total across all of them never exceeded the configured limit.

---

## Running it

### Local, no Docker

```bash
docker run -d --name redis-ratelimiter -p 6379:6379 redis
pip install -r requirements.txt
python manage.py runserver
# then hit http://127.0.0.1:8000/api/hello/ repeatedly to see the 429
```

### Full distributed setup (Docker Compose)

```bash
docker compose up --build

# fire 6 requests through Nginx — the 6th should 429
for i in {1..6}; do curl -i http://localhost/api/hello/; done

# or run the automated concurrent load test (from repo root)
cd ..
pip install requests
python load_tests/distributed_race_test.py
```

### Switching algorithms

1. Edit `RATE_LIMIT_ALGORITHM` in `settings.py`
2. `docker compose up --build` (rebuild required — see note above)
3. Re-run the curl loop or load test

---

## Known limitations / next steps

- One global limit applied to every route — no per-endpoint or per-user configuration yet
- No rate-limit response headers (`X-RateLimit-Remaining`, `Retry-After`) yet
- No defined fail-open/fail-closed behavior if Redis becomes unreachable — currently would raise an exception
- Automated distributed load test currently only exercises fixed window explicitly; sliding window and token bucket have been manually verified through the same stack but don't yet have their own automated distributed test run