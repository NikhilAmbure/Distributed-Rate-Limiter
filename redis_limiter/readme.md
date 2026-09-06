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


---

# Token Bucket

## Why a hash

Token bucket needs to track two related pieces of state per user:
how many tokens are currently available, and when the bucket was
last refilled. Redis's **hash** data structure is a natural fit — a
single key holding multiple named fields, similar to a small
dictionary living inside Redis.

## The naive migration

```python
def is_allowed(user_id):
    now = time.time()
    key = f"bucket:{user_id}"

    data = r.hmget(key, 'tokens', 'last_refill')
    tokens, last_refill = data

    if tokens is None:
        tokens = CAPACITY
        last_refill = now
    else:
        tokens = float(tokens)
        last_refill = float(last_refill)

    elapsed = now - last_refill
    refill_amount = elapsed * REFILL_RATE
    tokens = min(CAPACITY, tokens + refill_amount)

    if tokens >= 1:
        tokens -= 1
        r.hset(key, mapping={'tokens': tokens, 'last_refill': now})
        return True
    else:
        r.hset(key, mapping={'tokens': tokens, 'last_refill': now})
        return False
```

- `HMGET` reads both `tokens` and `last_refill` in one call.
- The refill math is identical to the Phase A Python version — the
  only change is reading/writing Redis instead of a dict.
- Notably, the updated token count is written back **even when the
  request is blocked** — a blocked request still represents time
  passing, so the partially-refilled (but insufficient) amount must
  be persisted, or the next check would miscalculate elapsed time.

Tested with 8 sequential requests: correctly allowed the first 5
(bucket starts full), blocked the remaining 3. A second run shortly
after correctly picked up refill from where the bucket left off,
rather than resetting — confirming persistence works as expected.

See [`token_bucket_naive.py`](token_bucket_naive.py).

## The race condition — a lost update, not just a stale read

Firing 20 concurrent requests against a **fresh** bucket (capacity=5)
produced:

Allowed: 17
Blocked: 3
Capacity was: 5


This is a dramatically worse over-admission than fixed window's 7 or
sliding window's 6 — because the underlying bug here is a different
and more severe category: a **lost update**, not just a stale read
feeding a check.

### Why this happens

| Time | Thread A               | Thread B               | Thread C               |
|------|------------------------|------------------------|------------------------|
| t1   | HMGET → tokens = 5     |                        |                        |
| t2   |                        | HMGET → tokens = 5     |                        |
| t3   |                        |                        | HMGET → tokens = 5     |
| t4   | computes 5 - 1 = 4     |                        |                        |
| t5   |                        | computes 5 - 1 = 4     |                        |
| t6   |                        |                        | computes 5 - 1 = 4     |
| t7   | HSET tokens = 4        |                        |                        |
| t8   |                        | HSET tokens = 4        |                        |
| t9   |                        |                        | HSET tokens = 4        |

All three threads read the same starting value (5), independently
compute "5 - 1 = 4," and each overwrite the hash with 4 — completely
unaware of each other's decrements. After all three "requests," the
stored value is still 4, as if only one request had ever happened,
even though three were allowed and each thread believed it had
correctly spent a token.

This differs from fixed/sliding window's race condition: there, the
*check* used stale data, but each write still correctly accumulated
(`INCR` and `ZADD` both add relative to existing state). Here, the
write is **destructive** — each thread computes and writes an
absolute new value from scratch, so concurrent writes erase each
other's work instead of stacking. This is why the number of
over-admitted requests is so much higher.

See [`token_bucket_race_test.py`](token_bucket_race_test.py).

## The fix: atomic Lua scripting

```lua
local key = KEYS[1]
local now = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local refill_rate = tonumber(ARGV[3])

local data = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(data[1])
local last_refill = tonumber(data[2])

if tokens == nil then
    tokens = capacity
    last_refill = now
end

local elapsed = now - last_refill
local refill_amount = elapsed * refill_rate
tokens = math.min(capacity, tokens + refill_amount)

if tokens >= 1 then
    tokens = tokens - 1
    redis.call('HSET', key, 'tokens', tokens, 'last_refill', now)
    return 1
else
    redis.call('HSET', key, 'tokens', tokens, 'last_refill', now)
    return 0
end
```

```python
rate_limit_script = r.register_script(LUA_SCRIPT)

def is_allowed(user_id):
    now = time.time()
    key = f"bucket:{user_id}"
    result = rate_limit_script(keys=[key], args=[now, CAPACITY, REFILL_RATE])
    return result == 1
```

Because the entire read-refill-check-decrement-write sequence now
executes as one uninterruptible Redis operation, no two threads can
ever read the same starting token count and independently overwrite
each other's decrements — each thread's script fully completes,
write included, before the next thread's script can even begin
reading.

See [`token_bucket_atomic.py`](token_bucket_atomic.py).

**Verified with the same concurrency test (20 threads, fresh bucket,
capacity=5):**

Allowed: 5
Blocked: 15
Capacity was: 5


A follow-up run in the same window correctly allowed only 1 request
— consistent with token bucket's gradual refill behavior, since a
small amount of real time had passed and refilled just over one
token's worth, unlike fixed/sliding window's all-or-nothing reset.

## Results summary

| Version | 20 concurrent requests, fresh bucket (capacity=5) | Enforces limit correctly? |
|---|---|---|
| Naive (HMGET → compute → HSET as separate steps) | 17 allowed | ❌ No |
| Atomic (single Lua script) | 5 allowed | ✅ Yes |

---

## Key takeaway

None of the three rate-limiting *algorithms* are wrong on their own
— fixed window, sliding window, and token bucket all correctly
implement their intended logic in isolation. The bug in every case
is in **how the check and the update are executed against shared
state**. Any naive "read, then decide, then write" pattern against a
value multiple processes can touch concurrently is vulnerable to a
race condition — though the exact failure mode can differ. Fixed and
sliding window suffer from stale reads feeding an incorrect check,
while still writing correctly (7 and 6 over-admitted, respectively).
Token bucket suffers from something worse — a lost update, where
concurrent writes overwrite rather than accumulate, leading to a
much larger over-admission (17). In all three cases, the fix is the
same general pattern: atomicity via Lua scripting, ensuring the
entire read-check-write sequence executes as one uninterruptible
unit, regardless of the underlying data structure or what the value
represents.

## Next steps

- Extend the Django middleware to support choosing between fixed
  window / sliding window / token bucket, instead of hardcoding
  fixed window
- Make limits/windows configurable per route or per user, instead of
  one global hardcoded limit