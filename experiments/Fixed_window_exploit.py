# Fixed window - Bug
import time

request_counts = {}

LIMIT = 5
WINDOW_SIZE = 3  # shrunk to 3 seconds just so we don't have to wait a full minute

def is_allowed(user_id):
    current_window = int(time.time() // WINDOW_SIZE)
    print("Current Window:", current_window)

    key = f"{user_id}:{current_window}"
    print("Key:", key)

    if key not in request_counts:
        request_counts[key] = 0

    request_counts[key] += 1
    print(request_counts)

    if request_counts[key] > LIMIT:
        return False
    return True


user = "alice"

print("--- Batch 1 ---")
for i in range(5):
    allowed = is_allowed(user)
    print(f"Request {i+1}: {'✅ allowed' if allowed else '❌ blocked'}")

print("\nWaiting 3 seconds for window to reset...\n")
time.sleep(3)

print("--- Batch 2 ---")
for i in range(5):
    allowed = is_allowed(user)
    print(f"Request {i+1}: {'✅ allowed' if allowed else '❌ blocked'}")


# Problem : 
# the window number changing between Batch 1 and Batch 2, and all 10 requests ending up allowed — 
# even though your limit is only 5. That's the exploit happening live on your own machine.


# Bug : (Wind)
"""
instead of letting the counter climb past 5 in one window, 
we waited for the window to change before sending the next batch. 
So when requests "6, 7, 8, 9, 10" arrived (in real-world order), 
the system didn't see them as "6, 7, 8, 9, 10" at all — 
it saw them as a fresh "1, 2, 3, 4, 5" in a brand new window, 
because the counter had reset to zero.

So the API isn't broken in the sense of "forgetting to block" — 
it's doing exactly what it's designed to do (count within a window, reset on a new window). 
The flaw is that its definition of "per minute" is too rigid: it only checks 
"did you exceed 5 in this specific clock slot," never "did you exceed 5 in any rolling 60 seconds."
An attacker exploits that rigidity by timing requests to land in two different windows instead of 
one.
"""