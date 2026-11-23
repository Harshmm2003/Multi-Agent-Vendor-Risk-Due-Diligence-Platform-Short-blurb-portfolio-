"""
Stay under a provider's tokens-per-minute limit instead of hitting it.

Groq's free tier allows 8,000 tokens a minute. Without pacing, the agent
sends a call, gets HTTP 429, and the client retries with backoff, wasting
time and requests. The pacer keeps a one-minute window of tokens actually
used and, before each call, waits just long enough for the next call to fit.
"""

from collections import deque
from collections.abc import Awaitable, Callable

WINDOW_SECONDS = 60.0


class TokenPacer:
    def __init__(
        self,
        tokens_per_minute: int,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        if tokens_per_minute <= 0:
            raise ValueError("tokens_per_minute must be positive")
        self.limit = tokens_per_minute
        self._clock = clock
        self._sleep = sleep
        self._window: deque[tuple[float, int]] = deque()
        self.waited_seconds = 0.0

    def _used(self, now: float) -> int:
        while self._window and now - self._window[0][0] >= WINDOW_SECONDS:
            self._window.popleft()
        return sum(tokens for _, tokens in self._window)

    async def wait_for(self, expected_tokens: int) -> float:
        """Sleep until expected_tokens fits in the window. Returns seconds waited."""
        waited = 0.0
        # A call bigger than the whole limit can never fit; send it anyway
        need = min(expected_tokens, self.limit)
        while self._window and self._used(self._clock()) + need > self.limit:
            delay = WINDOW_SECONDS - (self._clock() - self._window[0][0]) + 0.05
            await self._sleep(delay)
            waited += delay
        self.waited_seconds += waited
        return waited

    def record(self, tokens: int) -> None:
        self._window.append((self._clock(), tokens))
