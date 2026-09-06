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

## 🚦 Rate-Limiting Algorithms

Three algorithms are implemented and compared, each carried through every phase of the project.

| Algorithm      | Plain Python | Redis | Django | Distributed |
| -------------- | -----------: | ----: | -----: | ----------: |
| Fixed Window   |            ✅ |     ✅ |      ✅ |           ✅ |
| Sliding Window |            ✅ |     ✅ |      ✅ |           ⏳ |
| Token Bucket   |            ✅ |     ✅ |      ✅ |           ⏳ |

### 1. Fixed Window

Counts requests within fixed time intervals.

```text
Limit: 5 requests / 60 seconds

00:00 ───────────────── 01:00
      maximum 5 requests
```

Vulnerable to a **boundary exploit** — requests concentrated around a window boundary can get up to 2x the intended limit through:

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

Full write-up: [`experiments/notes/fixed-window.md`](experiments/notes/fixed-window.md)

### 2. Sliding Window

Tracks request timestamps over a rolling time period instead of resetting at fixed boundaries — closing the fixed-window boundary exploit.

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

Full write-up: [`experiments/notes/sliding_window.md`](experiments/notes/sliding_window.md)

### 3. Token Bucket

Maintains a bucket of tokens that continuously refills over time. Each request consumes a token — this allows controlled bursts while maintaining an average sustained rate, rather than a strict cap.

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

Unlike fixed/sliding window, `capacity` (burst size) and `refill_rate` (sustained rate) are independent knobs — this is what real APIs (AWS, Stripe, GitHub) tend to use in production, since it tolerates legitimate bursty traffic (e.g. a page load firing several calls at once) without punishing normal usage.

Full write-up: [`experiments/notes/token-bucket.md`](experiments/notes/token-bucket.md)

---

## ⚖️ When to Use Which Algorithm

Building all three revealed that they aren't interchangeable — each has a different failure mode and a different real-world fit.

| | Fixed Window | Sliding Window | Token Bucket |
|---|---|---|---|
| **Memory cost** | Lowest — one counter per user | Higher — one entry per request until it ages out | Low — two fields per user |
| **Precision** | Weak — vulnerable to a boundary exploit | Strong — no exploitable boundary | Strong for average rate, allows deliberate bursts |
| **Compute cost per request** | Cheapest (`INCR`) | More expensive (`ZREMRANGEBYSCORE` + `ZCARD` + `ZADD`) | Cheap (fractional math + `HSET`) |
| **Predictability for callers** | Simple to explain ("resets every N seconds") | Less intuitive ("rolling window") | Two numbers to reason about (burst + refill rate) |
| **Best suited for** | High-volume, low-stakes limits where simplicity matters more than precision | Security-sensitive limits (login attempts, password resets) where timing exploits are a real concern | General-purpose public APIs that need to tolerate legitimate bursty traffic |

### The boundary exploit is an algorithm limitation, not a concurrency bug

An important distinction this project surfaced: the atomic Lua fix eliminates the **race condition** (concurrent requests over-admitting *within* a single key), but it does **not** eliminate fixed window's **boundary exploit** (requests timed around a window's edge getting up to 2x the limit through). These are two separate problems:

- The **race condition** is a bug in *how* the check-then-act sequence was implemented — fixed by atomicity, regardless of algorithm.
- The **boundary exploit** is a property of fixed window's *design* — it uses a different Redis key for each window number, so atomicity correctly protects each key in isolation, but has no way to relate two different keys to each other. No amount of atomicity fixes this; only switching to an algorithm that doesn't rely on window-number-based keys (i.e. sliding window) does.

This is why, even with a fully atomic, race-condition-free fixed window implementation, sending 5 requests at `:59` and 5 more at `:01` of the next window still lets all 10 through — confirmed by testing both the plain-Python version ([`experiments/notes/fixed-window.md`](experiments/notes/fixed-window.md)) and the Redis-backed atomic version.

### Why production APIs often use token bucket

Real traffic is rarely steady — a single page load might legitimately fire 10 API calls at once, then go quiet for 30 seconds. Fixed and sliding window would both reject that burst outright. Token bucket tolerates it: as long as tokens have accumulated during idle time, a burst is allowed, while the *sustained* rate is still capped by the refill rate. This is the same reasoning behind how AWS, Stripe, and GitHub document their own API rate limits — in terms of burst capacity and steady-state rate, not a rigid per-second cutoff.

---

## 🐛 The Race Condition

The most important part of the project: demonstrating why a naive Redis implementation isn't enough, for *any* of the three algorithms.

### The naive pattern

```text
READ current state
     ↓
CHECK against limit
     ↓
WRITE updated state
```

These are separate Redis calls. Under concurrent requests, multiple workers can read the same stale state before any of them writes back.

### Reproduced with a concurrency test (20 requests, limit/capacity = 5)

| Algorithm      | Naive (allowed) | Atomic (allowed) | Bug type |
| -------------- | ---------------: | ----------------: | -------- |
| Fixed Window   |                7 |                  5 | Stale read feeding an incorrect check |
| Sliding Window |                6 |                  5 | Same as above, narrower window |
| Token Bucket   |               17 |                  5 | **Lost update** — concurrent writes overwrite instead of accumulating |

Token bucket's naive bug is meaningfully worse than the other two: each thread independently computes a brand-new token value and overwrites the previous thread's write, rather than each decrement correctly stacking. Full explanation: [`redis_limiter/README.md`](redis_limiter/README.md)

---

## ⚛️ The Fix: Atomic Redis Lua Scripting

Each algorithm's full read-check-write sequence is moved into a single Redis Lua script, executed atomically — no other client's commands can interleave mid-script.

```text
┌─────────────────────────────┐
│       Redis Lua Script      │
│                              │
│  Read → Check → Update      │
│                              │
└──────────────┬───────────────┘
               ↓
        Atomic execution
```

All three algorithms now consistently enforce their configured limit under concurrent load. Full implementations and write-ups: [`redis_limiter/`](redis_limiter/)

---

## 🌐 Django Integration

All three atomic algorithms are wired into Django as a single, configurable middleware — switch algorithms via one setting, no code changes required.

```text
HTTP Request
     ↓
RateLimitMiddleware
     ↓
Identify client (IP)
     ↓
Dispatch to configured algorithm
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

Full details on the middleware architecture, configuration, and Docker/distributed setup: **[`distributed_rate_limiter/README.md`](distributed_rate_limiter/README.md)**

---

## 🌍 Distributed Deployment

Docker Compose runs 3 Django instances behind Nginx (round-robin), all sharing one Redis backend — proving the rate limit holds **globally across the cluster**, not just within a single process.

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
     └────┬────┘    └────┬────┘    └────┬────┘
          │              │              │
          └──────────────┼──────────────┘
                         ↓
                  ┌─────────────┐
                  │    Redis    │
                  │ Shared State│
                  └─────────────┘
```

**Verified with an automated concurrent load test** (20 requests through Nginx, fixed window, limit=5):

```text
Allowed: 5
Blocked: 15
```

Full setup, config, and running instructions: **[`distributed_rate_limiter/README.md`](distributed_rate_limiter/README.md)**

---

## 📁 Project Structure

```text
Distributed-Rate-Limiter/
│
├── experiments/                    # A: algorithms in plain Python
│   └── notes/                       # write-ups per algorithm
│
├── redis_limiter/                   # B: Redis-backed algorithms
│   ├── README.md                    # naive → race condition → atomic fix, all 3 algorithms
│   ├── fixed_window_*.py
│   ├── sliding_window_*.py
│   └── token_bucket_*.py
│
├── load_tests/                      # D: distributed concurrent test
│   └── distributed_race_test.py
│
├── distributed_rate_limiter/        # C/D: Django project + Docker setup
│   ├── README.md                    # middleware, config, Docker, distributed details
│   ├── main/                        # views, middleware, ratelimiter dispatcher
│   ├── nginx/                       # load balancer config
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── manage.py
│
└── README.md
```

---

## 🛠️ Tech Stack

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

---

## 🚀 Quick Start

```bash
git clone https://github.com/NikhilAmbure/Distributed-Rate-Limiter.git
cd Distributed-Rate-Limiter

# Phase A/B — algorithms + Redis, standalone
pip install -r requirements.txt
python experiments/Fixed_window_baseline.py
docker run -d --name redis-ratelimiter -p 6379:6379 redis
python redis_limiter/fixed_window_atomic.py

# Phase C/D — full Django + Docker distributed setup
cd distributed_rate_limiter
docker compose up --build
curl -i http://localhost/api/hello/
```

See [`redis_limiter/README.md`](redis_limiter/README.md) and [`distributed_rate_limiter/README.md`](distributed_rate_limiter/README.md) for detailed, phase-by-phase instructions.

---

## 📊 Current Status

🚧 **In Progress**

* ✅ All three algorithms: plain Python → Redis (naive → race condition → atomic fix)
* ✅ Django middleware supporting all three algorithms via a single config setting
* ✅ Dockerized, 3-instance distributed deployment behind Nginx, shared Redis
* ✅ Automated distributed concurrent load test (fixed window)
* ⏳ Distributed load test coverage for sliding window and token bucket
* ⏳ Per-route / per-user configurable limits (currently one global limit)
* ⏳ Rate-limit response headers (`X-RateLimit-Remaining`, etc.)
* ⏳ Fail-open/fail-closed behavior if Redis becomes unavailable

---

## 📚 What This Project Demonstrates

This project is primarily a learning exercise in **distributed systems and concurrency**, rather than an attempt to create a production-ready replacement for established rate-limiting solutions.

The main concepts explored:

* Rate-limiting algorithms and their trade-offs
* Concurrency and race conditions
* Atomic operations and Redis scripting
* Distributed shared state
* Django middleware
* Reverse proxies and load balancing
* Docker networking
* Multi-instance application architecture
* Concurrent load testing

The goal is to understand **why distributed systems fail under concurrency, and how to design them so correctness holds across multiple application instances.**