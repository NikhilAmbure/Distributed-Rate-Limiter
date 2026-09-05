# Redis-Backed Rate Limiters — Race Conditions & Fixes

This folder documents migrating each rate-limiting algorithm from a
single-process Python dictionary (Phase A) into Redis — and the race
conditions that migration exposes, along with the atomic fixes.

---

# Fixed Window

## Why move to Redis

The Phase A implementation (`experiments/`) used a plain Python
dictionary to store counts. That only works within a single running
process — if you had multiple app server instances, each would have
its own separate dictionary, completely unaware of each other's
counts. A user could dodge the limit just by hitting a different
server instance.

Redis solves this by acting as a single, shared store that every
app instance connects to. This is what makes the rate limiter
genuinely "distributed" rather than per-process.

## The naive migration

The most direct translation of the Python dict logic into Redis
looks like this:

```python
def is_allowed(user_id):
    current_window = int(time.time() // WINDOW_SIZE)
    key = f"{user_id}:{current_window}"

    count = r.get(key)
    count = int(count) if count else 0

    if count < LIMIT:
        r.incr(key)
        r.expire(key, WINDOW_SIZE)
        return True
    else:
        return False
```

This behaves correctly under sequential requests — tested with 8
back-to-back requests (limit=5), correctly allowing 5 and blocking 3.

See [`fixed_window_naive.py`](fixed_window_naive.py).

## The race condition

This naive version performs three separate operations against
Redis: **read** the count (`GET`), **check** it against the limit,
then **write** the increment (`INCR`). Each of these is a distinct
round-trip to Redis. Nothing stops another request from reading the
count in between another request's read and write.

**Reproduced with a concurrency test:** firing 20 requests at once
using Python's `threading` module (limit=5) resulted in:

Allowed: 7
Blocked: 13
Limit was: 5



See [`fixed_window_race_test.py`](fixed_window_race_test.py).

### Why this happens

Multiple threads can read the same stale count *before* any of them
completes their increment. For example:

| Time | Thread A | Thread B | Thread C |
|---|---|---|---|
| t1 | reads count = 4 | | |
| t2 | | reads count = 4 | |
| t3 | | | reads count = 4 |
| t4 | checks 4 < 5 ✅ | | |
| t5 | | checks 4 < 5 ✅ | |
| t6 | | | checks 4 < 5 ✅ |
| t7 | writes count = 5 | | |
| t8 | | writes count = 6 | |
| t9 | | | writes count = 7 |

All three threads see the same outdated value (4) and all pass the
check, even though logically only one of them should have been
allowed to push the count past the limit. This is a **check-then-act
race condition** (also called TOCTOU — time-of-check to
time-of-use) — the same category of bug that unprotected shared
state anywhere is vulnerable to (e.g. an unguarded inventory
decrement).

## The fix: atomic Lua scripting

Redis executes Lua scripts as a single, uninterruptible unit — no
other client's commands can run in the middle of one. Moving the
entire read-check-write sequence into a Lua script closes the
interleaving window that caused the bug.

```lua
local current = redis.call('GET', KEYS[1])
if current == false then
    current = 0
else
    current = tonumber(current)
end

if current < tonumber(ARGV[1]) then
    redis.call('INCR', KEYS[1])
    redis.call('EXPIRE', KEYS[1], ARGV[2])
    return 1
else
    return 0
end
```

Called from Python via `redis-py`'s `register_script`:

```python
rate_limit_script = r.register_script(LUA_SCRIPT)

def is_allowed(user_id):
    current_window = int(time.time() // WINDOW_SIZE)
    key = f"{user_id}:{current_window}"
    result = rate_limit_script(keys=[key], args=[LIMIT, WINDOW_SIZE])
    return result == 1
```

See [`fixed_window_atomic.py`](fixed_window_atomic.py).

### Why this works

Redis processes commands one at a time. Normally, separate `GET` and
`INCR` calls from different clients can be interleaved between each
other. A Lua script is treated as a single command from Redis's
perspective — the entire script runs start-to-finish before Redis
even looks at the next client's request. So in the same scenario as
above, Thread B literally cannot begin its `GET` until Thread A's
entire read-check-increment-expire sequence has completed. Thread B
and C now see the correct, up-to-date count, not a stale one.

**Verified with the same concurrency test (20 threads, limit=5):**

Allowed: 5
Blocked: 15
Limit was: 5



Exactly 5, consistently, across repeated runs.

## Results summary

| Version | 20 concurrent requests, limit=5 | Enforces limit correctly? |
|---|---|---|
| Naive (GET → check → INCR as separate steps) | 7 allowed | ❌ No |
| Atomic (single Lua script) | 5 allowed | ✅ Yes |

---

# Sliding Window

## Why a different data structure

Fixed window only needed a single counter per key. Sliding window
needs to know *when* each request happened, not just *how many*
there were — so a simple counter isn't enough. Redis's **sorted set
(ZSET)** is a natural fit: each request is stored as a member with
its timestamp as the score, which lets us efficiently remove old
entries and count what's left.

## The naive migration

```python
def is_allowed(user_id):
    now = time.time()
    key = f"sliding:{user_id}"

    # Remove timestamps older than the window
    r.zremrangebyscore(key, 0, now - WINDOW_SIZE)

    # Count how many requests remain in the window
    count = r.zcard(key)

    if count < LIMIT:
        r.zadd(key, {str(now): now})
        r.expire(key, WINDOW_SIZE)
        return True
    else:
        return False
```

- `ZREMRANGEBYSCORE` deletes any timestamp older than `WINDOW_SIZE`
  seconds ago — the Redis equivalent of the Python list
  comprehension that filtered out old timestamps in Phase A.
- `ZCARD` counts how many timestamps remain — this is "how many
  requests happened in the last N seconds."
- `ZADD` records the new request's timestamp if the count is under
  the limit.

Tested with 8 sequential requests (limit=5): correctly allowed the
first 5, blocked the remaining 3.

See [`sliding_window_naive.py`](sliding_window_naive.py).

## The race condition

Same underlying problem as fixed window: `ZCARD` (read) and `ZADD`
(write) are two separate Redis calls. Nothing stops another thread
from reading the count in between another thread's read and write.

**Reproduced with a concurrency test:** 20 concurrent requests
against limit=5 resulted in:

Allowed: 6
Blocked: 14
Limit was: 5



See [`sliding_window_race_test.py`](sliding_window_race_test.py).

### Why this happens (and why the over-admission is smaller here)

Multiple threads can read the same stale `ZCARD` count before any of
them adds their own entry via `ZADD`:

| Time | Thread A | Thread B | Thread C |
|---|---|---|---|
| t1 | `ZCARD` → reads 4 | | |
| t2 | | `ZCARD` → reads 4 | |
| t3 | | | `ZCARD` → reads 4 |
| t4 | checks 4 < 5 ✅ | | |
| t5 | | checks 4 < 5 ✅ | |
| t6 | | | checks 4 < 5 ✅ |
| t7 | `ZADD` → set now has 5 | | |
| t8 | | `ZADD` → set now has 6 | |
| t9 | | | `ZADD` → set now has 7 |

This is the same class of check-then-act race condition as fixed
window. The over-admission observed here (6, one extra) tends to be
smaller than fixed window's (7, two extra) because `ZADD` is itself
a single atomic operation — the exploitable gap is narrower, limited
to the time between one thread's own `ZCARD` and its own `ZADD`,
during which other threads can slip in their reads. The exact number
varies by timing, but the underlying bug — allowing more requests
than the configured limit — is equally real in both cases.

## The fix: atomic Lua scripting

```lua
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)

local count = redis.call('ZCARD', key)

if count < limit then
    redis.call('ZADD', key, now, tostring(now))
    redis.call('EXPIRE', key, window)
    return 1
else
    return 0
end
```

```python
rate_limit_script = r.register_script(LUA_SCRIPT)

def is_allowed(user_id):
    now = time.time()
    key = f"sliding:{user_id}"
    result = rate_limit_script(keys=[key], args=[now, WINDOW_SIZE, LIMIT])
    return result == 1
```

See [`sliding_window_atomic.py`](sliding_window_atomic.py).

**One detail worth noting:** `now` is computed once in Python and
passed into the script as an argument, rather than letting the
script call `time.time()`-equivalent logic itself at multiple
points. This ensures the cleanup step (`ZREMRANGEBYSCORE`) and the
insert step (`ZADD`) both agree on the exact same "current time,"
avoiding subtle inconsistencies that could arise if they were
computed at slightly different instants.

**Verified with the same concurrency test (20 threads, limit=5):**

Allowed: 5
Blocked: 15
Limit was: 5



Exactly 5, consistently, across repeated runs.

## Results summary

| Version | 20 concurrent requests, limit=5 | Enforces limit correctly? |
|---|---|---|
| Naive (ZCARD → check → ZADD as separate steps) | 6 allowed | ❌ No |
| Atomic (single Lua script) | 5 allowed | ✅ Yes |

---

## Key takeaway

The bug isn't in either rate-limiting *algorithm* — both fixed
window's and sliding window's logic are correct on their own. The
bug is in **how the check and the update are executed against
shared state**. Any naive "read, then decide, then write" pattern
against a value multiple processes can touch concurrently is
vulnerable to this same class of race condition, regardless of the
underlying data structure (a simple counter or a sorted set) or what
the value represents (a request count, an inventory count, an
account balance, etc.). The fix — atomicity via Lua scripting — is a
general pattern, not something specific to one algorithm.

## Next steps

- Migrate token bucket to Redis using the same naive → race test →
  atomic fix pattern (via a Redis hash storing `tokens` and
  `last_refill`)
- Extend the Django middleware to support choosing between fixed
  window / sliding window / token bucket, instead of hardcoding
  fixed window