# Distributed Rate Limiter

A distributed rate-limiting system built from scratch to understand how distributed systems enforce request limits correctly under concurrency.

The project intentionally avoids existing rate-limiting libraries such as `django-ratelimit` and DRF throttle classes. Instead, it explores how rate-limiting algorithms work internally, how naive implementations fail under concurrent load, and how atomic Redis operations can be used to enforce limits correctly across multiple application instances.

---

## 🎯 Why This Project?

Rate limiting is easy to implement incorrectly.

A simple implementation might:

1. Read the current request count.
2. Check whether the limit has been reached.
3. Increment the counter.
4. Allow or reject the request.

This appears correct until multiple requests arrive concurrently.

Two or more processes can read the same counter before any of them updates it, resulting in **race conditions and over-admission**.

This project was built to investigate that problem step by step:

```text
Plain Python
     ↓
Understand rate-limiting algorithms
     ↓
Redis-backed implementation
     ↓
Reproduce race condition
     ↓
Fix with atomic Redis Lua script
     ↓
Integrate with Django
     ↓
Run multiple Django instances
     ↓
Put Nginx in front as load balancer
     ↓
Verify global rate limit under concurrency
```

---

# 🚦 Rate-Limiting Algorithms

Three algorithms are being explored.

| Algorithm      | Plain Python | Redis | Django | Distributed |
| -------------- | -----------: | ----: | -----: | ----------: |
| Fixed Window   |            ✅ |     ✅ |      ✅ |           ✅ |
| Sliding Window |            ✅ |     ⏳ |      ⏳ |           ⏳ |
| Token Bucket   |            ✅ |     ⏳ |      ⏳ |           ⏳ |

### 1. Fixed Window

Counts requests within fixed time intervals.

Example:

```text
Limit: 5 requests / 60 seconds

00:00 ───────────────── 01:00
      maximum 5 requests
```

The implementation demonstrates the **boundary exploit** where requests can be concentrated around a window boundary:

```text
Previous window       New window

   5 requests             5 requests
        │                      │
        ▼                      ▼
──────────────┬──────────────────────
              │
          boundary
              
Potentially 10 requests in a short period
despite a configured limit of 5.
```

The algorithm was first implemented in plain Python and then migrated to Redis.

See:

`experiments/notes/fixed-window.md`

---

### 2. Sliding Window

Instead of resetting a counter at fixed boundaries, the sliding-window implementation tracks request timestamps over a rolling time period.

This avoids the fixed-window boundary exploit.

```text
Current time
     │
     ▼
─────┬────────────────────────
     │      ← 60 seconds →
     │
     └── only requests inside
         the rolling window count
```

Implemented and verified in plain Python.

Redis migration is planned.

See:

`experiments/notes/sliding_window.md`

---

### 3. Token Bucket

The token bucket algorithm maintains a bucket of tokens that continuously refills over time.

Each request consumes a token.

This allows controlled bursts while maintaining an average request rate.

```text
        Token refill
             ↓
      ┌─────────────┐
      │ ● ● ● ● ●   │
      │   Bucket    │
      └──────┬──────┘
             │
          request
             ↓
         consume 1
           token
```

Implemented and verified in plain Python.

Redis migration is planned.

See:

`experiments/notes/token-bucket.md`

---

# 🐛 The Race Condition

The most important part of the project is demonstrating why a naive Redis implementation is not enough.

### Naive implementation

The naive implementation performs:

```text
READ count
     ↓
CHECK limit
     ↓
INCREMENT count
     ↓
SET expiry
```

These operations are performed separately.

Under concurrent requests, multiple workers can observe the same count before another worker increments it.

For example:

```text
Limit = 5

Request A ──┐
Request B ──┤
Request C ──┤──→ READ count = 4
Request D ──┤
Request E ──┘

Multiple requests see the same state
before the counter is updated.
```

### Race condition reproduced

A concurrent test firing **20 requests** against a limit of **5** produced:

| Implementation | Allowed | Expected |
| -------------- | ------: | -------: |
| Naive          |   **7** |        5 |
| Atomic         |   **5** |        5 |

The bug was reproducible under concurrent load.

---

# ⚛️ Atomic Redis Fix

The race condition was fixed by moving the complete operation into a single Redis Lua script.

Instead of:

```text
READ
 ↓
CHECK
 ↓
INCREMENT
 ↓
EXPIRE
```

the entire operation becomes one atomic Redis execution:

```text
┌─────────────────────────────┐
│       Redis Lua Script      │
│                             │
│  Read → Check → Increment   │
│         → Expire            │
│                             │
└──────────────┬──────────────┘
               ↓
        Atomic execution
```

Redis executes the Lua script atomically, preventing other commands from interleaving with the rate-limit operation.

The same concurrent test now consistently produces:

```text
20 concurrent requests
        ↓
Limit = 5
        ↓
Allowed: 5
Blocked: 15
```

Implementation:

`redis_limiter/fixed_window_atomic.py`

---

# 🌐 Django Integration

The atomic fixed-window limiter is integrated into Django as middleware.

### Request flow

```text
HTTP Request
     ↓
RateLimitMiddleware
     ↓
Identify client
     ↓
Redis Lua script
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
  View
```

The middleware:

* Runs early in the Django middleware stack.
* Identifies clients using their IP address.
* Checks `X-Forwarded-For` first.
* Falls back to `REMOTE_ADDR`.
* Uses the same atomic `is_allowed()` implementation.
* Returns `429 Too Many Requests` when the limit is exceeded.
* Prevents the Django view from executing when the request is blocked.

Current configuration:

```text
Limit: 5 requests
Window: 60 seconds
Scope: Global
Identifier: Client IP
```

Test endpoint:

```text
GET /api/hello/
```

Middleware:

`distributed_rate_limiter/main/middleware/middleware.py`

---

# 🌍 Distributed Deployment

A single Django instance sharing Redis demonstrates that the atomic algorithm works.

However, that isn't enough to prove the system is actually distributed.

The real test is:

> Does the same global limit hold when requests are handled by multiple independent application servers?

The project uses Docker Compose to run:

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

If each Django instance maintained its own in-memory counter:

```text
Client
  ↓
Nginx
  ├── Django 1 → counter = 5
  ├── Django 2 → counter = 5
  └── Django 3 → counter = 5
```

A client could potentially make significantly more requests than the configured limit because each instance would maintain independent state.

With shared Redis:

```text
Django 1 ──┐
Django 2 ──┼──→ Redis
Django 3 ──┘
```

All instances operate against the same atomic counter.

The limit is therefore enforced **globally across the application cluster**.

---

# 🧪 Distributed Concurrent Test

The distributed test sends **20 concurrent requests** through Nginx:

```text
20 concurrent requests
          ↓
        Nginx
          ↓
 Django 1 / Django 2 / Django 3
          ↓
     Shared Redis
```

### Run 1 — Fresh Window

```text
Allowed: 5
Blocked: 15
```

The configured limit is correctly enforced across all three Django instances combined.

### Run 2 — Same Window

Running the test again immediately:

```text
Allowed: 0
Blocked: 20
```

This confirms that the limit is stored globally in Redis rather than independently inside each Django process.

Docker logs also confirm that requests were distributed across:

```text
django1
django2
django3
```

while the combined number of allowed requests never exceeded the configured limit.

---

# 📁 Project Structure

```text
Distributed-Rate-Limiter/
│
├── experiments/                         # Phase A: Algorithms in plain Python
│   ├── notes/                           # Algorithm explanations
│   ├── Fixed_window_baseline.py
│   ├── Fixed_window_exploit.py
│   ├── sliding_window_baseline.py
│   ├── sliding_window_full_gap.py
│   ├── sliding_window_partial_gap.py
│   ├── token_bucket_baseline.py
│   └── token_bucket_refill.py
│
├── redis_limiter/                       # Phase B: Redis implementation
│   ├── fixed_window_naive.py            # Non-atomic implementation
│   ├── fixed_window_race_test.py        # Reproduces race condition
│   └── fixed_window_atomic.py           # Atomic Lua implementation
│
├── load_tests/                          # Phase D: Distributed testing
│   └── distributed_race_test.py
│
├── distributed_rate_limiter/            # Phase C/D: Django application
│   │
│   ├── distributed_rate_limiter/        # Django project configuration
│   ├── main/                            # Application logic & middleware
│   ├── nginx/                           # Nginx configuration
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── requirements.txt
│   └── manage.py
│
└── README.md
```

---

# 🛠️ Tech Stack

| Technology         | Purpose                                      |
| ------------------ | -------------------------------------------- |
| **Python**         | Algorithm implementations and testing        |
| **Django**         | HTTP application and middleware integration  |
| **Redis**          | Shared distributed rate-limit state          |
| **Lua**            | Atomic Redis rate-limit operations           |
| **Docker**         | Containerized application environment        |
| **Docker Compose** | Multi-container distributed setup            |
| **Nginx**          | Reverse proxy and round-robin load balancing |
| **Gunicorn**       | Production WSGI server                       |
| **Requests**       | Concurrent HTTP load testing                 |

---

# 🚀 Running the Project

## Prerequisites

* Python 3.x
* Docker
* Docker Compose
* Git

---

## Phase A — Plain Python Algorithms

Clone the repository:

```bash
git clone https://github.com/NikhilAmbure/Distributed-Rate-Limiter.git
cd Distributed-Rate-Limiter
```

Install dependencies:

```bash
pip install -r distributed_rate_limiter/requirements.txt
```

Run the fixed-window experiments:

```bash
python experiments/Fixed_window_baseline.py
python experiments/Fixed_window_exploit.py
```

Run the sliding-window experiments:

```bash
python experiments/sliding_window_baseline.py
python experiments/sliding_window_full_gap.py
python experiments/sliding_window_partial_gap.py
```

Run the token-bucket experiments:

```bash
python experiments/token_bucket_baseline.py
python experiments/token_bucket_refill.py
```

---

## Phase B — Redis Implementation

Start Redis:

```bash
docker run -d \
  --name redis-ratelimiter \
  -p 6379:6379 \
  redis
```

Run the naive implementation:

```bash
python redis_limiter/fixed_window_naive.py
```

Reproduce the race condition:

```bash
python redis_limiter/fixed_window_race_test.py
```

Run the atomic implementation:

```bash
python redis_limiter/fixed_window_atomic.py
```

---

## Phase C/D — Full Distributed Setup

Start the complete environment:

```bash
cd distributed_rate_limiter

docker compose up --build
```

The environment starts:

```text
Nginx
Django × 3
Redis
```

Test the endpoint:

```bash
curl -i http://localhost/api/hello/
```

Send six sequential requests:

```bash
for i in {1..6}; do
    curl -i http://localhost/api/hello/
done
```

The first five requests should be allowed and the sixth should return:

```text
HTTP 429 Too Many Requests
```

---

## Run the Distributed Concurrent Test

From the repository root:

```bash
cd ..
pip install requests
python load_tests/distributed_race_test.py
```

Expected result on a fresh window:

```text
Allowed: 5
Blocked: 15
```

Running it again within the same 60-second window should produce:

```text
Allowed: 0
Blocked: 20
```

---

# 📊 Current Status

🚧 **In Progress**

### Fixed Window

* ✅ Plain Python implementation
* ✅ Boundary exploit demonstrated
* ✅ Redis-backed implementation
* ✅ Race condition reproduced
* ✅ Concurrent test created
* ✅ Atomic Redis Lua implementation
* ✅ Django middleware integration
* ✅ Dockerized deployment
* ✅ 3 Django instances
* ✅ Nginx round-robin load balancing
* ✅ Shared Redis state
* ✅ Distributed concurrent load test

### Sliding Window

* ✅ Plain Python implementation
* ⏳ Redis implementation
* ⏳ Race-condition testing
* ⏳ Atomic implementation
* ⏳ Django integration
* ⏳ Distributed verification

### Token Bucket

* ✅ Plain Python implementation
* ⏳ Redis implementation
* ⏳ Race-condition testing
* ⏳ Atomic implementation
* ⏳ Django integration
* ⏳ Distributed verification

---

# 🔮 Planned Improvements

* Redis implementation of sliding window
* Redis implementation of token bucket
* Atomic concurrency handling for all algorithms
* Per-user rate limits
* Per-IP rate limits
* Per-route configuration
* Configurable limits instead of hardcoded values
* Algorithm selection
* Rate-limit response headers
* Improved observability and metrics
* Automated CI/CD pipeline
* Production deployment

---

# 📚 What This Project Demonstrates

This project is primarily a learning exercise in **distributed systems and concurrency**, rather than an attempt to create a production-ready replacement for established rate-limiting solutions.

The main concepts explored are:

* Rate-limiting algorithms
* Concurrency
* Race conditions
* Atomic operations
* Redis scripting
* Distributed shared state
* Django middleware
* HTTP `429` responses
* Reverse proxies
* Load balancing
* Docker networking
* Multi-instance application architecture
* Concurrent load testing

The goal is to understand **why distributed systems fail under concurrency and how to design them so that correctness is maintained across multiple application instances.**
