"""Spinning detection from cgroup CPU usage samples."""


class CpuMeter:
    """Turns cumulative ``usage_usec`` samples into a CPU percentage.

    Time is passed in by the caller, so the meter never reads a clock.
    """

    def __init__(self, threshold: float, window: float) -> None:
        self._threshold = threshold
        self._window = window
        self._previous: tuple[float, int] | None = None
        self._above_since: float | None = None
        self._latest = 0.0

    def sample(self, now: float, usage_usec: int | None) -> float | None:
        """Record a sample; return % of one core since the previous sample.

        The first sample (and the first after a ``None`` or a counter reset)
        only sets the baseline and returns None.
        """
        previous = self._previous
        if usage_usec is None:
            self._previous = None
            self._above_since = None
            return None
        self._previous = (now, usage_usec)
        if previous is None:
            return None
        then, before = previous
        elapsed = now - then
        if elapsed <= 0 or usage_usec < before:
            self._above_since = None
            return None
        percent = (usage_usec - before) / (elapsed * 1_000_000) * 100.0
        self._latest = now
        if percent >= self._threshold:
            if self._above_since is None:
                self._above_since = then
        else:
            self._above_since = None
        return percent

    def spinning(self) -> bool:
        """True once usage has stayed at or above the threshold for the window."""
        return (
            self._above_since is not None
            and self._latest - self._above_since >= self._window
        )
