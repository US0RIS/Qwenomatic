"""Clocks. Simulated time makes generations replayable; wall time runs the real farm."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


class SimulatedClock:
    """Time is a pure function of the scheduler tick."""

    mode = "simulated"

    def __init__(self, start: str, tick_seconds: float) -> None:
        self.start = parse_ts(start)
        self.tick_seconds = tick_seconds
        self.tick = 0

    def set_tick(self, tick: int) -> None:
        self.tick = max(tick, 0)

    def now_dt(self) -> datetime:
        return self.start + timedelta(seconds=self.tick * self.tick_seconds)

    def now(self) -> str:
        return self.now_dt().isoformat()

    def sleep_until_next_tick(self) -> None:
        return None


class WallClock:
    mode = "wall"

    def __init__(self, tick_seconds: float) -> None:
        self.tick_seconds = tick_seconds
        self.tick = 0
        self.start = datetime.now(timezone.utc)

    def set_tick(self, tick: int) -> None:
        self.tick = tick

    def now_dt(self) -> datetime:
        return datetime.now(timezone.utc)

    def now(self) -> str:
        return self.now_dt().isoformat()

    def sleep_until_next_tick(self) -> None:  # pragma: no cover - real time
        import time

        time.sleep(self.tick_seconds)
