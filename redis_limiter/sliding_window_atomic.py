import redis
import threading
import time

r = redis.Redis(host='localhost', port=6379, decode_responses=True)

LIMIT = 5
WINDOW_SIZE = 60

LUA_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)

local count = redis.call('ZCARD', key)

if count < limit then
    redis.call('ZADD', key, now, tostring(now))
    redis.call('EXPIRE', key, window)
    return 1
else
    return 0
end
"""

rate_limit_script = r.register_script(LUA_SCRIPT)

def is_allowed(user_id):
    now = time.time()
    key = f"sliding:{user_id}"

    result = rate_limit_script(keys=[key], args=[now, WINDOW_SIZE, LIMIT])
    return result == 1

allowed_count = 0
blocked_count = 0
lock = threading.Lock()  

def make_request(user_id, request_num):
    global allowed_count, blocked_count
    result = is_allowed(user_id)
    with lock:
        if result:
            allowed_count += 1
        else:
            blocked_count += 1

threads = []
for i in range(20):
    t = threading.Thread(target=make_request, args=("bob", i))
    threads.append(t)

for t in threads:
    t.start()   

for t in threads:
    t.join()    

print(f"\nAllowed: {allowed_count}")
print(f"Blocked: {blocked_count}")
print(f"Limit was: {LIMIT}")