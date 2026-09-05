import redis
import time
import threading

r = redis.Redis(host='localhost', port=6379, decode_responses=True)

LIMIT = 5
WINDOW_SIZE = 60

def is_allowed(user_id):
    now = time.time()
    key = f"sliding:{user_id}"

    r.zremrangebyscore(key, 0, now - WINDOW_SIZE)
    count = r.zcard(key)

    if count < LIMIT:
        r.zadd(key, {str(now): now})
        r.expire(key, WINDOW_SIZE)
        return True
    else:
        return False

allowed_count = 0
blocked_count = 0
lock = threading.Lock()

def make_request(user_id):
    global allowed_count, blocked_count
    result = is_allowed(user_id)
    with lock:
        if result:
            allowed_count += 1
        else:
            blocked_count += 1

threads = []
for i in range(20):
    t = threading.Thread(target=make_request, args=("bob",))
    threads.append(t)

for t in threads:
    t.start()

for t in threads:
    t.join()

print(f"\nAllowed: {allowed_count}")
print(f"Blocked: {blocked_count}")
print(f"Limit was: {LIMIT}")