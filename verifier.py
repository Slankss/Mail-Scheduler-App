"""Email verification via the Zeruh API.

The API key is read from the environment (loaded from a local .env file).
Each request verifies a single email address; we verify the contacts in
parallel while respecting the API rate limit (max 30 req/s, we cap at 25),
and classify each one as existing ("var") or non-existing ("yok").
"""
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from dotenv import load_dotenv

load_dotenv()

ZERUH_API_URL = "https://api.zeruh.com/v1/verify"
ZERUH_API_KEY = os.environ.get("ZERUH_API_KEY")

# The API allows up to 30 requests/second; stay safely under it.
RATE_LIMIT_PER_SECOND = 25

# A mailbox is treated as non-existing only when the API is confident it is
# undeliverable. "risky"/"unknown" results are kept to avoid deleting valid
# addresses behind catch-all servers.
INVALID_STATUSES = {"undeliverable"}


def verify_email(email, timeout=20):
    """Verify a single email address.

    Returns a dict: {email, status, exists, error}.
    `exists` is True when the mailbox is deliverable, False when undeliverable,
    and None when the result is inconclusive or the request failed.
    """
    if not ZERUH_API_KEY:
        return {"email": email, "status": "no_api_key", "exists": None,
                "error": "ZERUH_API_KEY tanimli degil (.env dosyasini kontrol edin)."}

    try:
        resp = requests.get(
            ZERUH_API_URL,
            params={"api_key": ZERUH_API_KEY, "email_address": email},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        return {"email": email, "status": "error", "exists": None, "error": str(exc)}
    except ValueError as exc:  # invalid JSON
        return {"email": email, "status": "error", "exists": None, "error": str(exc)}

    if not data.get("success"):
        return {"email": email, "status": "error", "exists": None,
                "error": data.get("message", "Dogrulama basarisiz.")}

    result = data.get("result", {})
    status = result.get("status", "unknown")

    if status in INVALID_STATUSES:
        exists = False
    elif status == "deliverable":
        exists = True
    else:
        exists = None  # risky / unknown -> belirsiz, silinmez

    return {"email": email, "status": status, "exists": exists, "error": None}


class RateLimiter:
    """Allow at most `max_calls` acquisitions within any rolling `period`.

    Thread-safe; used to keep the parallel verification under the API's
    requests-per-second limit.
    """

    def __init__(self, max_calls, period=1.0):
        self.max_calls = max_calls
        self.period = period
        self._calls = deque()
        self._lock = threading.Lock()

    def acquire(self):
        while True:
            with self._lock:
                now = time.monotonic()
                # Drop timestamps that fell outside the rolling window.
                while self._calls and self._calls[0] <= now - self.period:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                wait = self.period - (now - self._calls[0])
            time.sleep(max(wait, 0.0))


def verify_emails(items, rate=RATE_LIMIT_PER_SECOND):
    """Verify many emails in parallel, capped at `rate` requests per second.

    `items` is a list of (id, email) tuples. Returns a list of result dicts,
    each like `verify_email` plus the originating `id`.
    """
    items = list(items)
    if not items:
        return []

    limiter = RateLimiter(rate, 1.0)
    # Enough workers to keep `rate` requests in flight even with API latency,
    # while the limiter guarantees we never exceed `rate` per second.
    max_workers = min(len(items), max(rate, 1))

    def worker(item):
        contact_id, email = item
        limiter.acquire()
        result = verify_email(email)
        result["id"] = contact_id
        return result

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(worker, item) for item in items]
        for future in as_completed(futures):
            results.append(future.result())
    return results
