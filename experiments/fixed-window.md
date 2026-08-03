# Fixed Window Rate Limiting — Notes

## How it works

Each user gets a counter tied to a specific time slot ("window").
The window number is calculated as:

    current_window = int(time.time() // WINDOW_SIZE)

Every request increments the counter for `{user_id}:{current_window}`.
If the counter exceeds the limit, the request is blocked. Once the
clock moves into a new window, a brand new counter starts at 0.

## What I verified

With `LIMIT = 5`, sending 8 requests back-to-back in the same window
correctly allowed the first 5 and blocked the remaining 3. This
confirms the basic counting and blocking logic works as intended
within a single window.

## The bug I found

The window number is baked directly into the dictionary key
(`f"{user_id}:{current_window}"`). This means the *same user*
effectively gets a *different identity* every time the window
changes — `alice:1000` and `alice:1001` are treated as two
completely unrelated entries, even though they're the same person.

I proved this with a timing experiment: I shrank the window to 3
seconds, sent 5 requests (all allowed, hitting the limit exactly),
waited 3 seconds so the window rolled over, then sent 5 more
requests — which were *also* all allowed, because they landed under
a brand new key.

Result: 10 requests got through in ~3-4 seconds of real time, even
though the limit was supposed to be 5.

## Why this happens

Fixed window only checks "did this user exceed the limit *within
this specific clock slot*" — it never checks "did this user exceed
the limit within any recent stretch of real time." Resetting the
counter on a new window is necessary for the algorithm to work at
all, but it also means anyone who times their requests around the
window boundary can effectively get two full quotas back-to-back.

## Reproduction

See `experiments/fixed_window_2.py` — sends two batches
of 5 requests with a sleep in between, using a short window size to
make the boundary-crossing exploit reproducible in seconds instead
of waiting a full minute.

## Next step

Sliding window fixes this by removing the concept of separate
window "boxes" entirely — instead of a key that changes over time,
it tracks a rolling history of timestamps per user and checks "how
many requests happened in the last N seconds counting back from
right now," with no fixed reset point to exploit.