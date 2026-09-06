# A dispatcher that picks the right algo
from django.conf import settings
from . import fixed_window_atomic
from . import sliding_window_atomic
from . import token_bucket_atomic

ALGORITHMS = {
    'fixed_window': fixed_window_atomic.is_allowed,
    'sliding_window': sliding_window_atomic.is_allowed,
    'token_bucket': token_bucket_atomic.is_allowed,
}

def is_allowed(user_id):
    algorithm = getattr(settings, 'RATE_LIMIT_ALGORITHM', 'fixed_window')
    check_function = ALGORITHMS.get(algorithm)

    if check_function is None:
        raise ValueError(f"Unknown RATE_LIMIT_ALGORITHM: '{algorithm}'")

    return check_function(user_id)