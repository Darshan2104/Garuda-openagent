"""The retry contract stays stable while the agent adds visible status."""

import pytest

from client import RetryClient


@pytest.mark.parametrize("answers,attempts,expected,delays", [
    ([True], 3, True, []),
    ([False, True], 3, True, [0.5]),
    ([False, False, False], 3, False, [0.5, 0.5]),
    ([False], 1, False, []),
])
def test_retry_contract(answers, attempts, expected, delays):
    calls, waits = [], []
    results = iter(answers)

    def connect():
        calls.append("connect")
        return next(results)

    client = RetryClient(connect, waits.append, attempts=attempts)
    assert client.reconnect() is expected
    assert client.connected is expected
    assert len(calls) == len(answers)
    assert waits == delays


def test_reconnect_clears_an_earlier_connection():
    results = iter([True, False])
    client = RetryClient(lambda: next(results), lambda _: None, attempts=1)
    assert client.reconnect() is True
    assert client.reconnect() is False
    assert client.connected is False


def test_no_attempts_is_invalid():
    with pytest.raises(ValueError):
        RetryClient(lambda: True, lambda _: None, attempts=0)
