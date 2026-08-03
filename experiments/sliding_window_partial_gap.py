# the 1-sec sleep test — the real proof
import time

# Each user gets a list of timestamps instead of a single counter
request_log = {}

LIMIT = 5
WINDOW_SIZE = 3 # for test - 2 

def is_allowed(user_id):
    now = time.time()

    if user_id not in request_log:
        request_log[user_id] = []

    # Step 1: drop timestamps older than our window
    request_log[user_id] = [
        timestamp for timestamp in request_log[user_id]
        if now - timestamp < WINDOW_SIZE
    ]

    print(f"Timestamps within last {WINDOW_SIZE}s: {request_log[user_id]}")

    # Step 2: check count
    if len(request_log[user_id]) < LIMIT:
        request_log[user_id].append(now)
        return True
    else:
        return False

user = "alice"

# Test - 2  (Fixed-window-2 resolved)
print("--- Batch 1 ---")
for i in range(5):
    allowed = is_allowed(user)
    print(f"Request {i+1}: {'✅ allowed' if allowed else '❌ blocked'}")

time.sleep(1) 

print("--- Batch 2 ---")
for i in range(5):
    allowed = is_allowed(user)
    print(f"Request {i+1}: {'✅ allowed' if allowed else '❌ blocked'}")