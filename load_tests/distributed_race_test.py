import requests
import threading

URL = "http://localhost/api/hello/"
allowed_count = 0
blocked_count = 0
lock = threading.Lock()

def make_request():
    global allowed_count, blocked_count
    response = requests.get(URL)
    with lock:
        if response.status_code == 200:
            allowed_count += 1
        elif response.status_code == 429:
            blocked_count += 1

threads = []
for i in range(20):
    t = threading.Thread(target=make_request)
    threads.append(t)

for t in threads:
    t.start()

for t in threads:
    t.join()

print(f"Allowed: {allowed_count}")
print(f"Blocked: {blocked_count}")