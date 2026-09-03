# Sliding Window Rate Limiting — Notes

## The problem this solves

Fixed window rate limiting resets its counter based on rigid clock
slots (e.g., every 60 seconds, on the dot). This creates a boundary
exploit: a user can send a full quota of requests right before a
window ends, then another full quota right as the next window
starts — getting up to 2x the intended limit through in a very
short real-world timeframe. See `fixed-window.md` for the
full breakdown of that bug.

Sliding window fixes this by removing the concept of a fixed
"window box" entirely. Instead of asking "how many requests
happened in *this specific* clock slot," it continuously asks
"how many requests happened in the last N seconds, counting
backwards from right now." There's no reset point for an attacker
to time their requests around.

## How it works

Instead of one counter per user, each user gets a **list of
timestamps** — one for every request they've made recently.

```python
request_log = {}  # user_id -> [timestamp1, timestamp2, ...]
```

Every time a new request comes in:

1. Look up the user's list of past timestamps.
2. Filter out any timestamp older than `WINDOW_SIZE` seconds —
   these are no longer relevant, since they fall outside our
   rolling lookback window.
3. Count what's left. If it's under the limit, allow the request
   and add the current timestamp to the list. If not, block it.

```python
def is_allowed(user_id):
    now = time.time()

    if user_id not in request_log:
        request_log[user_id] = []

    request_log[user_id] = [
        t for t in request_log[user_id] if now - t < WINDOW_SIZE
    ]

    if len(request_log[user_id]) < LIMIT:
        request_log[user_id].append(now)
        return True
    return False
```

The key difference from fixed window: there's no "window number"
baked into a key. Every check re-evaluates "the last N seconds from
right now," so old timestamps age out one at a time, individually —
never all at once in a single reset event.

## What I verified

**Basic correctness (LIMIT = 5):** Sending 8 requests back-to-back
allowed the first 5 and blocked the remaining 3 — matching fixed
window's behavior for simple, non-boundary cases.

**Full-window gap (WINDOW_SIZE = 3, sleep = 3s):** Sent 5 requests,
waited exactly 3 seconds (the full window duration), then sent 5
more. All 10 were allowed. At first this looked like a failure —
but it's actually correct behavior. After a full window's worth of
time with no activity, *every* timestamp from batch 1 has genuinely
aged out of the "last 3 seconds." The user made 0 requests in the
most recent 3 seconds, so a fresh quota is the honest answer. Any
correctly-implemented rate limiter — fixed or sliding — should
behave this way when given a full window of idle time.

**Partial-window gap (WINDOW_SIZE = 3, sleep = 1s) — the real test:**
This is the scenario that actually distinguishes sliding window from
fixed window. Waiting only 1 second out of a 3-second window means
most of batch 1's timestamps are still well within the lookback
window when batch 2 arrives.

- With **fixed window**, a 1-second gap *could* still cross a window
  number boundary if timed right at the edge — incorrectly granting
  a fresh quota of 5 despite almost no real time passing.
- With **sliding window**, after only 1 second, batch 1's 5
  timestamps are still all within the last 3 seconds, so batch 2 is
  correctly blocked — the quota hasn't meaningfully freed up yet.

This partial-gap test is the actual proof that sliding window closes
the boundary exploit: it responds to *how much time has actually
passed*, not to which arbitrary clock slot the request landed in.

## Fixed window vs. sliding window — side by side

| Scenario | Fixed Window | Sliding Window |
|---|---|---|
| 8 requests, no delay | 5 allowed, 3 blocked | 5 allowed, 3 blocked |
| Full window of idle time between batches | Resets, allows fresh batch | Also allows fresh batch (correctly — no recent activity) |
| Partial idle time, timed at a window boundary | Can incorrectly allow a fresh batch | Correctly blocks — timestamps haven't aged out yet |

## Trade-offs

Sliding window (log-based, as implemented here) is more accurate
than fixed window, but it comes at a cost: memory usage grows with
the number of requests, since we're storing an individual timestamp
per request rather than a single counter. In a production system
with high request volume, this list could get large before old
entries are cleaned out.

(A middle-ground approach — "sliding window counter" — approximates
this behavior using two fixed windows and a weighted average,
trading a little accuracy for the memory efficiency of a simple
counter. Worth exploring as a future variant.)

## Next step

Move both fixed window and sliding window into Redis, so the state
is shared across multiple app instances instead of living in a
single process's memory. This is also where the real distributed
systems challenge begins: concurrent requests from different
instances can race on reading/updating the same Redis key, which is
the bug we'll need to reproduce and fix with atomic operations
(Lua scripting).