"""
Login rate limiting for the ADAM GUI backend (Pass 1 web hardening).

Throttles POST /api/auth/login by BOTH the source IP and the attempted
username, to blunt password-guessing without locking a real user out
permanently.

Implementation limitations (deliberate, for the pilot -- DOCUMENTED):
  - In-memory only: counters live in this process's RAM. They are NOT
    shared across workers and they RESET ON RESTART. This is fine for the
    single-process uvicorn the pilot runs; a multi-worker / multi-host
    deployment would need a shared store (Redis) instead.
  - Sliding window: each failed attempt records a timestamp; a key is
    "over limit" when the count of failures within the last
    `window_seconds` reaches the threshold.

Asymmetry note (intentional, do NOT "normalize"): governance fails
CLOSED, this rate limiter fails OPEN. A bug in the limiter must never
lock every user out of logging in -- so the caller wraps every limiter
call in try/except and, on error, allows the login to proceed. The
limiter is a speed bump, not an authorization gate.
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

# Defaults. Username threshold sits in the prompt's suggested 5-10 band;
# the per-IP ceiling is higher so a few legitimate users behind one NAT
# don't throttle each other, while a single host hammering many usernames
# still trips it.
DEFAULT_WINDOW_SECONDS = 15 * 60   # 15 minutes
DEFAULT_MAX_PER_USERNAME = 7       # failed attempts / username / window
DEFAULT_MAX_PER_IP = 30            # failed attempts / IP / window


class LoginRateLimiter:
    """
    Sliding-window failure counter keyed independently by username and by
    IP. Thread-safe (uvicorn runs sync endpoints in a threadpool).
    """

    def __init__(
        self,
        *,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        max_per_username: int = DEFAULT_MAX_PER_USERNAME,
        max_per_ip: int = DEFAULT_MAX_PER_IP,
    ) -> None:
        self.window_seconds = window_seconds
        self.max_per_username = max_per_username
        self.max_per_ip = max_per_ip
        self._username_fails: Dict[str, List[float]] = {}
        self._ip_fails: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    # -- internal helpers -------------------------------------------------

    @staticmethod
    def _prune(stamps: List[float], cutoff: float) -> List[float]:
        """Drop timestamps older than the window cutoff."""
        return [t for t in stamps if t >= cutoff]

    def _retry_after(self, stamps: List[float], now: float) -> int:
        """Seconds until the oldest in-window failure ages out (>= 1)."""
        if not stamps:
            return self.window_seconds
        oldest = min(stamps)
        remaining = int(self.window_seconds - (now - oldest))
        return max(1, remaining)

    # -- public API -------------------------------------------------------

    def check(self, username: Optional[str], ip: Optional[str]) -> Optional[int]:
        """
        Return the Retry-After (seconds) if EITHER the username or the IP
        is currently over its limit, else None. Called BEFORE attempting
        authentication, so a throttled valid user and a throttled invalid
        user get the identical 429 (no username enumeration).
        """
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            retry_candidates: List[int] = []

            if username:
                u = self._username_fails.get(username)
                if u is not None:
                    u = self._prune(u, cutoff)
                    self._username_fails[username] = u
                    if len(u) >= self.max_per_username:
                        retry_candidates.append(self._retry_after(u, now))

            if ip:
                p = self._ip_fails.get(ip)
                if p is not None:
                    p = self._prune(p, cutoff)
                    self._ip_fails[ip] = p
                    if len(p) >= self.max_per_ip:
                        retry_candidates.append(self._retry_after(p, now))

            if retry_candidates:
                return max(retry_candidates)
            return None

    def record_failure(self, username: Optional[str], ip: Optional[str]) -> None:
        """Record one failed login attempt against both keys."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            if username:
                stamps = self._prune(self._username_fails.get(username, []), cutoff)
                stamps.append(now)
                self._username_fails[username] = stamps
            if ip:
                stamps = self._prune(self._ip_fails.get(ip, []), cutoff)
                stamps.append(now)
                self._ip_fails[ip] = stamps

    def reset_username(self, username: Optional[str]) -> None:
        """
        Clear a username's failure counter. Called on a SUCCESSFUL login
        so a user who eventually types the right password isn't penalized
        for earlier typos. The IP counter is intentionally left intact --
        a successful login from one account shouldn't reset brute-force
        pressure attributed to the source host.
        """
        if not username:
            return
        with self._lock:
            self._username_fails.pop(username, None)
