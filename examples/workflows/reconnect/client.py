"""A synchronous retry client; inject transport and delay for deterministic tests."""

from collections.abc import Callable


class RetryClient:
    def __init__(self, connect: Callable[[], bool], delay: Callable[[float], None],
                 *, attempts: int = 3, retry_delay: float = 0.5):
        if attempts < 1:
            raise ValueError("attempts must be positive")
        self.connect = connect
        self.delay = delay
        self.attempts = attempts
        self.retry_delay = retry_delay
        self.connected = False

    def reconnect(self) -> bool:
        self.connected = False
        for attempt in range(self.attempts):
            if self.connect():
                self.connected = True
                return True
            if attempt + 1 < self.attempts:
                self.delay(self.retry_delay)
        return False
