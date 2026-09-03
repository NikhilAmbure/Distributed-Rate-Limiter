
# concurrent test proving the race condition

import redis
import time
import threading

r = redis.Redis(host='localhost', port=6379, decode_responses=True)

LIMIT = 5
WINDOW_SIZE = 60

def is_allowed(user_id):
    current_window = int(time.time() // WINDOW_SIZE)
    key = f"{user_id}:{current_window}"

    count = r.get(key) # READ
    count = int(count) if count else 0

    if count < LIMIT: # CHECK
        r.incr(key) # WRITE
        r.expire(key, WINDOW_SIZE)
        return True
    else:
        return False

allowed_count = 0
blocked_count = 0
lock = threading.Lock()  # just for safely updating our result counters, not related to the bug

def make_request(user_id, request_num):
    global allowed_count, blocked_count
    result = is_allowed(user_id)
    with lock:
        if result:
            allowed_count += 1
        else:
            blocked_count += 1

# Fire 20 requests, all at essentially the same moment
threads = []
for i in range(20):
    t = threading.Thread(target=make_request, args=("tom", i))
    threads.append(t)

for t in threads:
    t.start()   # start all threads as close together as possible

for t in threads:
    t.join()    # wait for all to finish

print(f"\nAllowed: {allowed_count}")
print(f"Blocked: {blocked_count}")
print(f"Limit was: {LIMIT}")