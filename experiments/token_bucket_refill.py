# CASE: refill test — wait 2s, get 1 more; wait 0.3s, blocked; wait 4s, get 2 more

import time

buckets = {}  # user_id -> {"tokens": float, "last_refill": timestamp}

CAPACITY = 5        # max tokens the bucket can hold
REFILL_RATE = 1 / 2  # tokens added per second (1 token every 2 seconds)

def is_allowed(user_id):
    now = time.time()

    if user_id not in buckets: 
        buckets[user_id] = {"tokens": CAPACITY, "last_refill": now}

    bucket = buckets[user_id]
    print("Bucket:", bucket)

    # Step 1: figure out how much time passed since we last checked
    elapsed = now - bucket["last_refill"]
    print("elapsed time: ", elapsed)

    # Step 2: calculate how many tokens should have refilled in that time
    refill_amount = elapsed * REFILL_RATE
    bucket["tokens"] = min(CAPACITY, bucket["tokens"] + refill_amount)
    bucket["last_refill"] = now

    print(f"Tokens available: {bucket['tokens']:.2f}")

    # Step 3: check if there's at least 1 token to spend
    if bucket["tokens"] >= 1:
        bucket["tokens"] -= 1
        return True
    else:
        return False


user = "alice"

print("--- Batch 1 ---")
for i in range(5):
    allowed = is_allowed(user)
    print(f"Request {i+1}: {'✅ allowed' if allowed else '❌ blocked'}")

time.sleep(2) 

print("--- Batch 2 ---")
for i in range(5):
    allowed = is_allowed(user)
    print(f"Request {i+1}: {'✅ allowed' if allowed else '❌ blocked'}")