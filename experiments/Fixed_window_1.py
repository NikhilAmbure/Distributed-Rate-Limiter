# Fixed Window - Baseline (correct behavior within a single window)
# Verifies: requests 1-5 are allowed, requests 6-8 are correctly
# blocked, since all 8 land within the same window and the counter
# correctly exceeds the limit of 5.

import time

request_counts = {}

LIMIT = 5
WINDOW_SIZE = 60

def is_allowed(user_id):
    current_window = int(time.time() // WINDOW_SIZE)
    print("Current Window:", current_window)

    key = f"{user_id}:{current_window}"
    print("Key:", key)

    if key not in request_counts:
        request_counts[key] = 0

    request_counts[key] += 1
    print(request_counts)

    if request_counts[key] > LIMIT:
        return False  
    return True 


# Test
user = "alice"
    
for i in range(8):
    allowed = is_allowed(user)
    print(f"Request {i+1}: {'✅ allowed' if allowed else '❌ blocked'}")