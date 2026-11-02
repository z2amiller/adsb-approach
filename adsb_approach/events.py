"""Tiny synchronous pub/sub. Subscribers receive (kind, t, approach_id, flight_id, payload)."""
from __future__ import annotations

from collections.abc import Callable

Subscriber = Callable[[str, float, int | None, int | None, dict], None]


class EventBus:
    def __init__(self) -> None:
        self._subs: list[Subscriber] = []

    def subscribe(self, cb: Subscriber) -> None:
        self._subs.append(cb)

    def publish(self, kind: str, t: float, approach_id: int | None, flight_id: int | None, payload: dict) -> None:
        for cb in self._subs:
            cb(kind, t, approach_id, flight_id, payload)
