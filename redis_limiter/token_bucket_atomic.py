import redis
import threading
import time

r = redis.Redis(host='localhost', port=6379, decode_responses=True)

CAPACITY = 5
REFILL_RATE = 0.5

LUA_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local refill_rate = tonumber(ARGV[3])

local data = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(data[1])
local last_refill = tonumber(data[2])

if tokens == nil then
    tokens = capacity
    last_refill = now
end

local elapsed = now - last_refill
local refill_amount = elapsed * refill_rate
tokens = math.min(capacity, tokens + refill_amount)

if tokens >= 1 then
    tokens = tokens - 1
    redis.call('HSET', key, 'tokens', tokens, 'last_refill', now)
    return 1
else
    redis.call('HSET', key, 'tokens', tokens, 'last_refill', now)
    return 0
end
"""

rate_limit_script = r.register_script(LUA_SCRIPT)

def is_allowed(user_id):
    now = time.time()
    key = f"bucket:{user_id}"
    result = rate_limit_script(keys=[key], args=[now, CAPACITY, REFILL_RATE])
    return result == 1


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