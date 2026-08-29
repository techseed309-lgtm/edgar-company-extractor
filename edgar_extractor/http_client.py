"""Rate-limited HTTP client with retry and an on-disk response cache.

The SEC's access policy asks that automated clients declare a User-Agent
containing contact information and stay under 10 requests/second. Both are
enforced here rather than left to the caller to remember.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

# Transient statuses worth retrying; anything else is a real error.
RETRY_STATUSES = {429, 500, 502, 503, 504}


class RateLimiter:
    """Enforces a minimum interval between requests."""

    def __init__(self, requests_per_second: float) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._min_interval = 1.0 / requests_per_second
        self._last_call = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()


class HttpClient:
    """Session wrapper adding throttling, backoff retry and caching."""

    def __init__(
        self,
        user_agent: str,
        cache_dir: str | Path = ".cache",
        requests_per_second: float = 6.0,
        max_retries: int = 4,
        timeout: int = 30,
        use_cache: bool = True,
    ) -> None:
        if "@" not in user_agent:
            raise ValueError(
                "SEC requires a User-Agent with contact details, e.g. "
                '"CompanyExtractor/1.0 (you@example.com)". Set USER_AGENT or pass --email.'
            )
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        )
        self.cache_dir = Path(cache_dir)
        self.use_cache = use_cache
        if self.use_cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.limiter = RateLimiter(requests_per_second)
        self.max_retries = max_retries
        self.timeout = timeout
        self.stats = {"cache_hits": 0, "downloads": 0, "retries": 0}

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        return self.cache_dir / f"{digest}.cache"

    def get_text(self, url: str) -> str:
        """Fetch a URL as text, serving from cache when available.

        Raises requests.HTTPError if the server keeps failing after retries.
        """
        cache_file = self._cache_path(url)
        if self.use_cache and cache_file.exists():
            self.stats["cache_hits"] += 1
            log.debug("cache hit %s", url)
            return cache_file.read_text(encoding="utf-8")

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self.limiter.wait()
            try:
                response = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:  # network-level failure
                last_error = exc
            else:
                if response.status_code == 200:
                    self.stats["downloads"] += 1
                    if self.use_cache:
                        cache_file.write_text(response.text, encoding="utf-8")
                    return response.text
                if response.status_code not in RETRY_STATUSES:
                    response.raise_for_status()
                last_error = requests.HTTPError(
                    f"HTTP {response.status_code} for {url}", response=response
                )

            if attempt < self.max_retries:
                delay = 2**attempt
                self.stats["retries"] += 1
                log.warning(
                    "%s (attempt %d/%d) - retrying in %ds",
                    last_error,
                    attempt + 1,
                    self.max_retries,
                    delay,
                )
                time.sleep(delay)

        raise requests.HTTPError(f"Gave up on {url} after {self.max_retries} retries") from last_error

    def get_json(self, url: str):
        import json

        return json.loads(self.get_text(url))
