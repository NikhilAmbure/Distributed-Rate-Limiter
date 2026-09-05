import redis
import time
import os

REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
r = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)

LIMIT = 5
WINDOW_SIZE = 60

def is_allowed(user_id):
    now = time.time()
    key = f"sliding:{user_id}"

    # Remove timestamps older than the window
    # delete anything older than 60 seconds ago.
    r.zremrangebyscore(key, 0, now - WINDOW_SIZE) # Step 1: cleanup old timestamps

    # Count how many requests remain in the window
    count = r.zcard(key) # Step 2: READ current count
    # ZCARD just means "how many members are in this sorted set right now."

    print(f"Key: {key} | Current count in window: {count}")

    if count < LIMIT: # Step 3: CHECK
        r.zadd(key, {str(now): now}) # Step 4: WRITE (add new timestamp)
        r.expire(key, WINDOW_SIZE)
        return True
    else:
        return False


# Test
user = "alice"
for i in range(8):
    allowed = is_allowed(user)
    print(f"Request {i+1}: {'✅ allowed' if allowed else '❌ blocked'}")