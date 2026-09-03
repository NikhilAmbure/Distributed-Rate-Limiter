# Redis-Backed Fixed Window — Race Condition & Fix

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

## Key takeaway

The bug isn't in the rate-limiting *algorithm* — fixed window's
logic is fine. The bug is in **how the check and the update are
executed against shared state**. Any naive "read, then decide, then
write" pattern against a value multiple processes can touch
concurrently is vulnerable to this same class of race condition,
regardless of what that value represents (a request count, an
inventory count, an account balance, etc.). The fix — atomicity —
is a general pattern, not something specific to rate limiting.

## Next steps

- Migrate sliding window and token bucket to Redis using the same
  naive → race test → atomic fix pattern
- Wrap the atomic fixed-window logic as Django middleware, so it
  protects real API endpoints instead of running as standalone
  scripts