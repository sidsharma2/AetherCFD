"""Tiny thread-safe publish/subscribe bus for the headless core.

Core code publishes from any thread; the studio's Qt bridge subscribes and re-emits on the GUI
thread with queued signals. Subscribers must not block.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)
Handler = Callable[[str, dict[str, Any]], None]


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, topic: str, fn: Handler) -> Callable[[], None]:
        """Subscribe to a topic ("*" = everything). Returns an unsubscribe function."""
        with self._lock:
            self._subs[topic].append(fn)

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._subs.get(topic, []):
                    self._subs[topic].remove(fn)

        return unsubscribe

    def publish(self, topic: str, payload: dict[str, Any] | None = None) -> None:
        payload = payload or {}
        with self._lock:
            handlers = list(self._subs.get(topic, [])) + list(self._subs.get("*", []))
        for fn in handlers:
            try:
                fn(topic, payload)
            except Exception:  # one bad subscriber must never break publishers
                log.exception("event handler failed for %s", topic)


BUS = EventBus()  # process-wide default bus
