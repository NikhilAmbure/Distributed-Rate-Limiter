import redis
import threading
import time
from django.conf import settings
import os

REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')

r = redis.Redis(host='localhost', port=6379, decode_responses=True)


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
    limit = settings.RATE_LIMIT_MAX_REQUESTS
    window_size = settings.RATE_LIMIT_WINDOW
    now = time.time()
    key = f"sliding:{user_id}"

    result = rate_limit_script(keys=[key], args=[now, window_size, limit])
    return result == 1