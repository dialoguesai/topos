from __future__ import annotations

from websockets.exceptions import InvalidURI

from topos.core.connection_resilience import (
    ExponentialBackoff,
    ResilienceConfig,
    classify_connection_error,
    is_fatal_connection_category,
)


def test_exponential_backoff_increases_and_resets():
    backoff = ExponentialBackoff(
        ResilienceConfig(initial_backoff_s=0.1, max_backoff_s=1.0, jitter_ratio=0.0)
    )
    first = backoff.next_delay()
    second = backoff.next_delay()
    third = backoff.next_delay()

    assert first == 0.1
    assert second == 0.2
    assert third == 0.4

    backoff.reset()
    assert backoff.next_delay() == 0.1


def test_classify_connection_error_common_cases():
    timeout_category, _timeout_reason = classify_connection_error(TimeoutError("boom"))
    assert timeout_category == "timeout"

    network_category, _network_reason = classify_connection_error(OSError("net down"))
    assert network_category == "network"

    protocol_category, _protocol_reason = classify_connection_error(InvalidURI("bad://url", "bad"))
    assert protocol_category == "protocol"


def test_is_fatal_connection_category():
    assert is_fatal_connection_category("auth") is True
    assert is_fatal_connection_category("protocol") is True
    assert is_fatal_connection_category("network") is False
