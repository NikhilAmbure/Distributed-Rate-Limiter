import redis
import time

r = redis.Redis(host='localhost', port=6379, decode_responses=True)

LIMIT = 5
WINDOW_SIZE = 60

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