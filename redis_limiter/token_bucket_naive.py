import redis
import time

r = redis.Redis(host='localhost', port=6379, decode_responses=True)

CAPACITY = 5
REFILL_RATE = 0.5  # tokens per second (1 token every 2 seconds)

def is_allowed(user_id):
    now = time.time()
    key = f"bucket:{user_id}"

    data = r.hmget(key, 'tokens', 'last_refill')
    # HMGET : buckets[user_id] = {"tokens": ..., "last_refill": ...}
    
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

    print(f"Key: {key} | Tokens available: {tokens:.2f}")

    if tokens >= 1:
        tokens -= 1
        r.hset(key, mapping={'tokens': tokens, 'last_refill': now})
        return True
    else:
        r.hset(key, mapping={'tokens': tokens, 'last_refill': now})
        return False


# Test
user = "alice"
for i in range(8):
    allowed = is_allowed(user)
    print(f"Request {i+1}: {'✅ allowed' if allowed else '❌ blocked'}")