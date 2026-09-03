
# sequential testing

# Redis's own INCR command — which does the same "increment a counter" job,
#  but the counter now lives in Redis instead of your script's memory,
#  meaning any process that connects to this Redis server sees the same counter.

import redis
import time

r = redis.Redis(host='localhost', port=6379, decode_responses=True)

LIMIT = 5 
WINDOW_SIZE = 60

def is_allowed(user_id):
    current_window = int(time.time() // WINDOW_SIZE)
    key = f"{user_id}:{current_window}"

    count = r.get(key)
    count = int(count) if count else 0

    print(f"Key: {key} | Current count: {count}")

    if count < LIMIT:
        r.incr(key)
        r.expire(key, WINDOW_SIZE) # auto-delete the key once the window passes
        return True
    else:
        return False

user = "alice"
for i in range(8):
    allowed = is_allowed(user)
    print(f"Request {i+1}: {'✅ allowed' if allowed else '❌ blocked'}")

# same as before, first 5 allowed, 
# last 3 blocked — just now backed by Redis instead of a dictionary.

# --------------------------------------
# If you run this script multiple times, you'll see the counter persists across runs,
# because Redis is a separate process that keeps its own state.
# -------------------------------------