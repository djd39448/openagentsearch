"""
Tests for the HostRateLimiter implementation.
"""

import time
from unittest.mock import Mock, patch
from openagentsearch.fetch.ratelimit import HostRateLimiter


def test_first_call_never_sleeps():
    """Test that the first call for a host never sleeps."""
    # Use a mock clock that always returns 0
    mock_clock = Mock(return_value=0.0)
    
    # Use a mock sleep that records calls
    sleep_calls = []
    mock_sleep = Mock(side_effect=lambda x: sleep_calls.append(x))
    
    limiter = HostRateLimiter(min_interval_s=1.0, clock=mock_clock, sleep=mock_sleep)
    
    # First call should not sleep
    limiter.wait("example.com")
    
    assert len(sleep_calls) == 0
    mock_clock.assert_called_once_with()
    mock_sleep.assert_not_called()


def test_second_call_sleeps():
    """Test that an immediate second call sleeps for the remaining interval."""
    # Create a mock clock that returns increasing values
    clock_values = [0.0, 1.5]  # First call: t=0, Second call: t=1.5
    mock_clock = Mock(side_effect=clock_values)
    
    # Use a mock sleep that records calls
    sleep_calls = []
    mock_sleep = Mock(side_effect=lambda x: sleep_calls.append(x))
    
    limiter = HostRateLimiter(min_interval_s=2.0, clock=mock_clock, sleep=mock_sleep)
    
    # First call (t=0)
    limiter.wait("example.com")
    
    # Second call (t=1.5) - should sleep 0.5 seconds to reach the 2s interval
    limiter.wait("example.com")
    
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == 0.5  # Should sleep 0.5s
    assert mock_clock.call_count == 2  # Called twice
    mock_sleep.assert_called_once_with(0.5)


def test_different_hosts_independent():
    """Test that different hosts don't delay each other."""
    # Create a mock clock that returns increasing values for calls
    clock_values = [0.0, 0.2, 0.5]  # host1, host2, host1 again
    mock_clock = Mock(side_effect=clock_values)

    # Use a mock sleep that records calls
    sleep_calls = []
    mock_sleep = Mock(side_effect=lambda x: sleep_calls.append(x))

    limiter = HostRateLimiter(min_interval_s=1.0, clock=mock_clock, sleep=mock_sleep)

    # First call to host1 (t=0) - no sleep needed
    limiter.wait("host1.com")

    # First call to host2 (t=0.2) - no sleep: host1's history does not apply
    limiter.wait("host2.com")

    # Second call to host1 (t=0.5) - 0.5s elapsed for host1, sleeps the remaining 0.5s
    limiter.wait("host1.com")

    assert sleep_calls == [0.5]
    assert mock_sleep.call_count == 1


def test_rate_limiter_respects_interval():
    """Test that the rate limiter properly respects the minimum interval."""
    clock_values = [0.0, 1.0]
    mock_clock = Mock(side_effect=clock_values)
    
    sleep_calls = []
    mock_sleep = Mock(side_effect=lambda x: sleep_calls.append(x))
    
    limiter = HostRateLimiter(min_interval_s=1.5, clock=mock_clock, sleep=mock_sleep)
    
    # First call (t=0) - no sleep
    limiter.wait("example.com")
    
    # Second call at t=1.0 - only 1s since last, so should sleep 0.5s to make it 1.5s
    limiter.wait("example.com")
    
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == 0.5  # Should sleep 0.5s