import redis
import time
import os
from django.conf import settings

REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


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
    limit = settings.RATE_LIMIT_MAX_REQUESTS
    window_size = settings.RATE_LIMIT_WINDOW

    current_window = int(time.time() // window_size)
    key = f"fixed:{user_id}:{current_window}"

    result = rate_limit_script(keys=[key], args=[limit, window_size])
    return result == 1