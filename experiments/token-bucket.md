# Token Bucket Rate Limiting — Notes

## The core idea

Fixed window and sliding window both answer "has this user exceeded
N requests in a given time period?" Token bucket asks a different
question: "does this user currently have enough saved-up allowance
to make a request?"

Picture an actual bucket that holds tokens:

- It has a **maximum capacity** (e.g., 5 tokens)
- It **refills continuously over time** at a fixed rate (e.g., 1
  token every 2 seconds → 0.5 tokens/second)
- Every request **costs 1 token**
- If enough tokens are available, the request is allowed and a
  token is spent
- If the bucket is empty, the request is blocked
- Tokens accumulate while unused, up to the max capacity — so an
  idle user can "save up" and burst through several requests at
  once when they return

This is philosophically different from sliding window's strictness.
Sliding window says "never more than N in any rolling period, full
stop." Token bucket says "you get a steady trickle of allowance,
but you're free to save it and spend it in a burst."

## How it works — no background timer needed

A naive approach might try to run a background loop that adds a
token every 2 seconds. This is unnecessary and hard to keep
consistent across processes. Instead, refill is calculated lazily,
right at the moment a request arrives, based purely on elapsed time:

```python
def is_allowed(user_id):
    now = time.time()

    if user_id not in buckets:
        buckets[user_id] = {"tokens": CAPACITY, "last_refill": now}

    bucket = buckets[user_id]

    elapsed = now - bucket["last_refill"]
    refill_amount = elapsed * REFILL_RATE
    bucket["tokens"] = min(CAPACITY, bucket["tokens"] + refill_amount)
    bucket["last_refill"] = now

    if bucket["tokens"] >= 1:
        bucket["tokens"] -= 1
        return True
    return False
```

Each check computes "how many tokens should have accumulated since
we last looked," adds that fractional amount, caps it at capacity,
and then checks if there's at least 1 whole token to spend.

## What I verified

**Baseline burst test (CAPACITY = 5):** New user starts with a full
bucket. Firing 8 requests instantly allowed the first 5 (burst
capacity) and blocked the remaining 3 (bucket empty, no time passed
to refill anything).

**Refill timeline test (REFILL_RATE = 0.5 tokens/sec, i.e. 1 token
per 2 seconds):**

| Time | Elapsed since last check | Tokens refilled | Tokens available | Result |
|---|---|---|---|---|
| t=0.0s | — | — | 5 → 0 (after 5 requests) | 5 allowed, 6th blocked |
| t=2.0s | 2.0s | 2.0 × 0.5 = 1.0 | 1.0 | ✅ allowed, spend → 0 |
| t=2.3s | 0.3s | 0.3 × 0.5 = 0.15 | 0.15 | ❌ blocked (< 1 token) |
| t=6.0s | 4.0s (since t=2.0s) | 4.0 × 0.5 = 2.0 | 2.0 | ✅ allowed, spend → 1.0 |

This confirms the defining behavior: tokens refill **fractionally
and continuously**, not in discrete jumps on a timer. A short gap
(0.3s) earns a small fraction that isn't enough to spend; a longer
gap (4s) earns enough for two requests. This is fundamentally
different from fixed window's abrupt full-reset, and different from
sliding window's "exact count in a rolling period" — here, unused
allowance smoothly accumulates over time, capped at the bucket's
maximum capacity.

## Why the capacity cap matters

Without `min(CAPACITY, ...)`, a user who's idle for a very long time
(hours, days) would accumulate an unbounded number of tokens and
could then fire an enormous burst all at once. Capping at capacity
ensures the maximum possible burst is always bounded and predictable
— idle time lets you "catch up" to a full bucket, but never beyond
it.

## Fixed window vs. sliding window vs. token bucket

| | Fixed Window | Sliding Window | Token Bucket |
|---|---|---|---|
| Tracks | One counter per window | List of timestamps | Fractional token count + last refill time |
| Resets | Abruptly, at window boundary | Continuously, per-timestamp | Continuously, fractional refill |
| Allows bursts | Only within one window | No — strictly enforces rolling limit | Yes, by design — up to capacity |
| Main weakness | Boundary exploit (2x limit possible) | Higher memory use (stores every timestamp) | More complex refill math; still needs atomicity fix under concurrency |
| Best suited for | Simple, low-stakes limits | Strict, security-sensitive limits | APIs where occasional bursts are legitimate (e.g. page load firing several calls at once) |

## Next step

All three algorithms now work correctly as single-process, in-memory
implementations. The next phase is moving each into Redis, so state
is shared across multiple app instances — and reproducing the race
condition where concurrent requests can read stale token/count
values before either writes back, over-admitting requests beyond
the intended limit. That bug gets fixed with atomic Lua scripting.