
# the Lua-scripted fix

import redis
import time
import threading

r = redis.Redis(host='localhost', port=6379, decode_responses=True)

LIMIT = 5
WINDOW_SIZE = 60

# This Lua script runs as ONE atomic operation inside Redis itself.
# KEYS[1] = the rate limit key
# ARGV[1] = limit
# ARGV[2] = window size (seconds)
LUA_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if current == false then
    current = 0
else
    current = tonumber(current)
end

if current < tonumber(ARGV[1]) then
    redis.call('INCR', KEYS[1])
    redis.call('EXPIRE', KEYS[1], ARGV[2])
    return 1
else
    return 0
end
"""

rate_limit_script = r.register_script(LUA_SCRIPT)

def is_allowed(user_id):
    current_window = int(time.time() // WINDOW_SIZE)
    key = f"{user_id}:{current_window}"

    result = rate_limit_script(keys=[key], args=[LIMIT, WINDOW_SIZE])
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

#  Fire 20 requests, all at essentially the same moment
threads = []
for i in range(20):
    t = threading.Thread(target=make_request, args=("bob", i))
    threads.append(t)

for t in threads:
    t.start()   # start all threads as close together as possible

for t in threads:
    t.join()    # wait for all to finish

print(f"\nAllowed: {allowed_count}")
print(f"Blocked: {blocked_count}")
print(f"Limit was: {LIMIT}")