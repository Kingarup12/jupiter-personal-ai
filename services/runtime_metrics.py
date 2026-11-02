"""One lightweight host sampler; request threads never block to sample CPU."""
from __future__ import annotations

import math
import threading
import time


class RuntimeMetrics:
    def __init__(self, provider=None, interval=2.0):
        self.provider = provider
        self.interval = interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._value = {}

    @staticmethod
    def _percent(value):
        value = float(value)
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError("Invalid measurement")
        return round(value, 1)

    def sample(self):
        try:
            value = {"cpu_percent": self._percent(self.provider.cpu_percent(interval=None)),
                     "memory_percent": self._percent(self.provider.virtual_memory().percent),
                     "measured_at": round(time.time(), 3)}
        except Exception:
            value = {}
        with self._lock:
            self._value = value

    def _run(self):
        try:
            if self.provider is None:
                import psutil
                self.provider = psutil
            # psutil's first nonblocking CPU sample is not a valid measurement.
            self.provider.cpu_percent(interval=None)
        except Exception:
            return
        while not self._stop.wait(self.interval):
            self.sample()

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="jupiter-host-metrics", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()

    def snapshot(self):
        with self._lock:
            value = dict(self._value)
        if not value or time.time() - value["measured_at"] > self.interval * 3:
            return {"cpu_percent": None, "memory_percent": None, "measured_at": None, "scope": "backend_host"}
        return {**value, "scope": "backend_host"}


runtime_metrics = RuntimeMetrics()
