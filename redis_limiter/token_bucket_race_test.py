import redis
import time
import threading

r = redis.Redis(host='localhost', port=6379, decode_responses=True)

CAPACITY = 5
REFILL_RATE = 0.5

def is_allowed(user_id):
    now = time.time()
    key = f"bucket:{user_id}"

    data = r.hmget(key, 'tokens', 'last_refill')
    tokens, last_refill = data

    if tokens is None:
        tokens = CAPACITY
        last_refill = now
    else:
        tokens = float(tokens)
        last_refill = float(last_refill)

    elapsed = now - last_refill
    refill_amount = elapsed * REFILL_RATE
    tokens = min(CAPACITY, tokens + refill_amount)

    if tokens >= 1:
        tokens -= 1
        r.hset(key, mapping={'tokens': tokens, 'last_refill': now})
        return True
    else:
        r.hset(key, mapping={'tokens': tokens, 'last_refill': now})
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
    t = threading.Thread(target=make_request, args=("charlie",))
    threads.append(t)

for t in threads:
    t.start()

for t in threads:
    t.join()

print(f"\nAllowed: {allowed_count}")
print(f"Blocked: {blocked_count}")
print(f"Capacity was: {CAPACITY}")