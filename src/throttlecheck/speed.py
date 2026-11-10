"""Measure download speed from one service: several parallel streams for a fixed time, sampled as it goes."""
from __future__ import annotations

import ssl
import statistics
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterator

UA = "Mozilla/5.0 (throttlecheck; +https://github.com/ArkaKhorchidian/throttlecheck)"
CHUNK = 64 * 1024


@dataclass
class Measurement:
    service: str
    mbps: float = 0.0
    seconds: float = 0.0
    megabytes: float = 0.0
    samples: list[float] = field(default_factory=list)  # Mbps per half second, after warm-up
    error: str = ""
    where: str = ""  # server location when the service says

    @property
    def steadiness(self) -> float:
        """Coefficient of variation of the per-half-second speed. A rate limiter makes a flat line (close to 0)."""
        s = self.samples[1:-1] if len(self.samples) > 4 else self.samples
        if len(s) < 3 or not statistics.mean(s):
            return 1.0
        return statistics.pstdev(s) / statistics.mean(s)


def open_url(url: str, timeout: float = 10):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
    return urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context())


def measure(service: str, urls: Callable[[], Iterator[str]], seconds: float = 8.0, streams: int = 4,
            warmup: float = 1.5, opener=open_url) -> Measurement:
    """Download from `urls` (an endless supply of URLs to fetch) on `streams` connections for `seconds`."""
    m = Measurement(service)
    total = [0]
    lock = threading.Lock()
    stop = threading.Event()
    errors: list[str] = []
    source = urls()
    src_lock = threading.Lock()

    def worker():
        while not stop.is_set():
            with src_lock:
                url = next(source, None)
            if url is None:
                return
            try:
                with opener(url) as r:
                    while not stop.is_set():
                        b = r.read(CHUNK)
                        if not b:
                            break
                        with lock:
                            total[0] += len(b)
            except Exception as e:  # network errors of every kind just end this stream
                errors.append(str(getattr(e, "reason", e))[:120])
                if len(errors) > 3 * streams:
                    return
                time.sleep(0.2)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(streams)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    base_t, base_b, last_t, last_b = None, 0, t0, 0
    while time.monotonic() - t0 < seconds and any(t.is_alive() for t in threads):
        time.sleep(0.5)
        now = time.monotonic()
        with lock:
            b = total[0]
        if now - t0 >= warmup:
            if base_t is None:
                base_t, base_b = now, b
            else:
                m.samples.append((b - last_b) * 8 / (now - last_t) / 1e6)
        last_t, last_b = now, b
    stop.set()
    end = time.monotonic()
    with lock:
        b = total[0]
    m.megabytes = b / 1e6
    m.seconds = end - t0
    if base_t is not None and end - base_t > 0.4:
        m.mbps = (b - base_b) * 8 / (end - base_t) / 1e6
    elif m.seconds:
        m.mbps = b * 8 / m.seconds / 1e6
    if b == 0:
        m.error = errors[0] if errors else "no data"
    return m
