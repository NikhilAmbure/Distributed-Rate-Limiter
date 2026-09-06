import redis
import time
import os
from django.conf import settings

REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
r = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)


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
    capacity = settings.RATE_LIMIT_BUCKET_CAPACITY
    refill_rate = settings.RATE_LIMIT_REFILL_RATE

    now = time.time()
    key = f"bucket:{user_id}"

    result = rate_limit_script(keys=[key], args=[now, capacity, refill_rate])
    return result == 1