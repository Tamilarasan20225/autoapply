"""
Generic API key pool with rotation, cooldown, and permanent-failure tracking.

Used by every integrated external API (LLM providers, Adzuna, Jooble, Serper, ...)
so multiple free-tier accounts/keys can share the load instead of a single key
hitting its quota and stalling the whole pipeline.
"""

import os
import threading
import time
from typing import Optional


class KeyPool:
    """
    Rotates across a list of credential entries (dicts), skipping entries that
    are cooling down (temporary rate limit) or marked bad (permanent auth failure).

    Supports per-key minimum interval (min_interval) so different keys can run
    concurrently without a global sleep — each key enforces its own cooldown between
    successive uses, enabling true parallel LLM calls across multiple API keys.

    Thread-safe for use from concurrent scoring/discovery workers.
    """

    def __init__(self, entries: list[dict], min_interval: float = 0.0):
        """
        Args:
            entries: List of credential dicts (e.g. [{"key": "..."}])
            min_interval: Minimum seconds between successive uses of the SAME key.
                          Set to call_delay_seconds (e.g. 1.0) so Groq/Gemini rate
                          limits are respected per-key without blocking other keys.
        """
        self._entries = entries
        self._lock = threading.Lock()
        self._cooldown_until: dict[int, float] = {}
        self._bad: set[int] = set()
        self._busy: set[int] = set()
        self._last_used: dict[int, float] = {}   # per-key last-used timestamp
        self._min_interval = min_interval
        self._next_index = 0

    def __len__(self) -> int:
        return len(self._entries)

    def get(self) -> Optional[dict]:
        """
        Return the next available, non-busy entry (round-robin) and reserve it
        for the caller, or None if every entry is unavailable/already in use.
        Concurrent callers must call release() when done so other threads don't
        pile onto the same single key at once.

        Per-key min_interval is enforced here: a key is skipped if it was used
        too recently, allowing other keys to be selected instead (true parallelism).
        """
        with self._lock:
            n = len(self._entries)
            if n == 0:
                return None
            now = time.time()
            for offset in range(n):
                idx = (self._next_index + offset) % n
                if idx in self._bad or idx in self._busy:
                    continue
                if self._cooldown_until.get(idx, 0) > now:
                    continue
                # Per-key minimum interval check (replaces global time.sleep in caller)
                if self._min_interval > 0:
                    elapsed = now - self._last_used.get(idx, 0)
                    if elapsed < self._min_interval:
                        continue  # skip this key — too recent; try next
                self._next_index = (idx + 1) % n
                self._busy.add(idx)
                self._last_used[idx] = now
                return {**self._entries[idx], "_pool_index": idx}
            return None

    def get_blocking(self, timeout: float = 15.0, poll_interval: float = 0.2) -> Optional[dict]:
        """
        Like get(), but waits (bounded by timeout) for an entry to become
        available instead of failing immediately — covers busy keys, rate-limit
        cooldowns, and per-key min_interval windows.
        Only gives up early if every entry is permanently bad.
        """
        deadline = time.time() + timeout
        while True:
            entry = self.get()
            if entry is not None:
                return entry
            with self._lock:
                all_bad = len(self._entries) > 0 and all(i in self._bad for i in range(len(self._entries)))
            if all_bad or time.time() >= deadline:
                return None
            time.sleep(poll_interval)

    def time_until_any_available(self) -> float:
        """
        Seconds until the soonest key becomes available (considering cooldowns
        AND min_interval gaps). Returns 0.0 if something is available right now.
        """
        with self._lock:
            now = time.time()
            min_wait = float("inf")
            for idx in range(len(self._entries)):
                if idx in self._bad:
                    continue
                cooldown_wait = max(0.0, self._cooldown_until.get(idx, 0) - now)
                interval_wait = max(0.0, self._min_interval - (now - self._last_used.get(idx, 0)))
                key_wait = max(cooldown_wait, interval_wait)
                if idx not in self._busy:
                    min_wait = min(min_wait, key_wait)
            return min_wait if min_wait != float("inf") else 0.0

    def release(self, entry: dict) -> None:
        """Release a key reserved via get()/get_blocking() so other threads can use it."""
        idx = entry.get("_pool_index")
        if idx is None:
            return
        with self._lock:
            self._busy.discard(idx)

    def mark_cooldown(self, entry: dict, seconds: float = 60.0) -> None:
        """Temporarily skip this entry (e.g. after a 429/quota/timeout)."""
        idx = entry.get("_pool_index")
        if idx is None:
            return
        with self._lock:
            self._cooldown_until[idx] = time.time() + seconds
            self._busy.discard(idx)

    def mark_bad(self, entry: dict) -> None:
        """Permanently skip this entry for the process lifetime (e.g. 401/403)."""
        idx = entry.get("_pool_index")
        if idx is None:
            return
        with self._lock:
            self._bad.add(idx)
            self._busy.discard(idx)

    def all_cooling_down(self) -> bool:
        """True if every entry is currently bad or cooling down (nothing available)."""
        with self._lock:
            now = time.time()
            for idx in range(len(self._entries)):
                if idx in self._bad:
                    continue
                if self._cooldown_until.get(idx, 0) <= now:
                    return False
            return len(self._entries) > 0

    def time_until_next_available(self) -> float:
        """Seconds until the soonest cooling-down entry becomes available again."""
        with self._lock:
            now = time.time()
            candidates = [
                until - now
                for idx, until in self._cooldown_until.items()
                if idx not in self._bad and until > now
            ]
            return max(0.0, min(candidates)) if candidates else 0.0


def load_keys_from_env(base_name: str, max_keys: int = 10) -> list[dict]:
    """
    Read BASE_NAME, BASE_NAME_2, BASE_NAME_3, ... from the environment until a
    suffix is unset. Returns a list of {"key": "..."} entries.
    """
    entries: list[dict] = []
    value = os.environ.get(base_name, "").strip()
    if value:
        entries.append({"key": value})
    for i in range(2, max_keys + 1):
        value = os.environ.get(f"{base_name}_{i}", "").strip()
        if not value:
            break
        entries.append({"key": value})
    return entries


def load_paired_keys_from_env(pairs: list[tuple[str, str]]) -> list[dict]:
    """
    Read numbered pairs of env vars (e.g. ADZUNA_APP_ID/ADZUNA_APP_KEY,
    ADZUNA_APP_ID_2/ADZUNA_APP_KEY_2, ...) into {"app_id": ..., "app_key": ...} entries.

    `pairs` is the pre-expanded list of (id_env, key_env) names to check, in order.
    """
    entries: list[dict] = []
    for id_env, key_env in pairs:
        app_id = os.environ.get(id_env, "").strip()
        app_key = os.environ.get(key_env, "").strip()
        if app_id and app_key:
            entries.append({"app_id": app_id, "app_key": app_key})
    return entries


def paired_env_names(id_base: str, key_base: str, max_pairs: int = 10) -> list[tuple[str, str]]:
    """Build the numbered-suffix env var name list for load_paired_keys_from_env, stopping at the first fully-missing pair."""
    names: list[tuple[str, str]] = []
    for i in range(1, max_pairs + 1):
        suffix = "" if i == 1 else f"_{i}"
        id_env, key_env = f"{id_base}{suffix}", f"{key_base}{suffix}"
        if not (os.environ.get(id_env) and os.environ.get(key_env)):
            if i == 1:
                names.append((id_env, key_env))  # keep first pair even if unset, for a clear error message downstream
            break
        names.append((id_env, key_env))
    return names
