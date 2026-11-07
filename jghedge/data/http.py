"""Disk-cached, per-host rate-limited HTTP fetcher with retries."""
from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from ..config import CACHE_DIR

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Basketball-Reference bans clients that exceed 20 requests/minute.
MIN_INTERVAL = {"www.basketball-reference.com": 3.4}


class FetchError(RuntimeError):
    pass


class Fetcher:
    def __init__(self, cache_dir: Path = CACHE_DIR, offline: bool = False, verbose: bool = False):
        self.cache_dir = Path(cache_dir)
        self.offline = offline
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
        self._locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}
        self._glock = threading.Lock()

    def _path(self, url: str, kind: str) -> Path:
        host = urlparse(url).netloc
        h = hashlib.sha1(url.encode()).hexdigest()[:20]
        return self.cache_dir / "http" / host / f"{h}.{ 'json' if kind == 'json' else 'html'}"

    def _throttle(self, host: str) -> None:
        gap = MIN_INTERVAL.get(host)
        if not gap:
            return
        with self._glock:
            lock = self._locks.setdefault(host, threading.Lock())
        with lock:
            wait = self._last.get(host, 0) + gap - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.time()

    def get(self, url: str, ttl: float | None = 3600, kind: str = "json", store: bool = True):
        """Fetch `url`. ttl=None caches forever; ttl=0 always refetches (cache as fallback)."""
        path = self._path(url, kind)
        if path.exists() and (ttl is None or time.time() - path.stat().st_mtime < ttl):
            return self._load(path, kind)
        if self.offline:
            if path.exists():
                return self._load(path, kind)
            raise FetchError(f"offline and not cached: {url}")
        host = urlparse(url).netloc
        last_err: Exception | None = None
        for attempt in range(4):
            self._throttle(host)
            try:
                r = self.session.get(url, timeout=30)
                if r.status_code == 429:
                    time.sleep(30 * (attempt + 1))
                    continue
                if r.status_code >= 500:
                    raise FetchError(f"{r.status_code} {url}")
                if r.status_code != 200:
                    raise FetchError(f"HTTP {r.status_code} for {url}")
                body = r.json() if kind == "json" else r.content.decode("utf-8", errors="replace")
                if not store:
                    return body
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(body) if kind == "json" else body, encoding="utf-8")
                tmp.replace(path)
                if self.verbose:
                    print(f"  fetched {url}")
                return body
            except (requests.RequestException, ValueError, FetchError) as e:
                last_err = e
                if isinstance(e, FetchError) and "HTTP 4" in str(e):
                    break
                time.sleep(2 ** attempt + random.random())
        if path.exists():  # stale cache beats nothing
            return self._load(path, kind)
        raise FetchError(str(last_err))

    @staticmethod
    def _load(path: Path, kind: str):
        text = path.read_text(encoding="utf-8")
        return json.loads(text) if kind == "json" else text
